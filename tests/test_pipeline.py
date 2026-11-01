"""Graph routing, audit, retries, and labels. The model is a fake passed into the graph."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from db import connect_readonly, create_database
from graph import mermaid_text, run_invoice
from llm_client import (
    BACKOFF_SECONDS,
    BASE_URL,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MODEL,
    GrokClient,
    LLMError,
    MissingAPIKey,
    SCHEMA_REPAIR_LIMIT,
)
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    ExtractedInvoice,
    Finding,
    LineItem,
    PaymentAuthorization,
    PipelineState,
    ReasoningDraft,
    SourceInfo,
    ValidationReport,
)
from parsers import parse_file
from pipeline import audit
from pipeline.approval import _reason_messages, reasoning_output_failed
from pipeline.payment import payment
from scripts.eval import print_bad_reasoning
from decision.invoice_fields import resolve_currency
from decision.outcome import HOLD_CODES, REJECT_CODES, WARNING_CODES, outcome_from_findings
from decision.policy import APPROVAL_THRESHOLD_USD, CRITIQUE_CAP, MAX_SEND_BACKS
from decision.findings import assess
from tests.fakes import FakeClock, FakeLLM, ZeroRandom, task_of

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"

README_OUTCOMES = {
    "invoice_1001.txt": "approve",
    "invoice_1004.json": "approve",
    "invoice_1006.csv": "approve",
    "invoice_1002.txt": "reject",
    "invoice_1003.txt": "reject",
    "invoice_1008.txt": "reject",
    "invoice_1016.json": "reject",
    "invoice_1009.json": "reject",
    "invoice_1012.pdf": "hold",
    "invoice_1012.txt": "hold",
    "invoice_1014.xml": "hold",
}


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is disabled in tests")

    monkeypatch.setattr(socket, "create_connection", explode)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "inventory.db"
    create_database(path)
    return path


def extraction(**overrides) -> str:
    payload = {
        "vendor": "Widgets Inc.",
        "vendor_address": None,
        "invoice_number": "INV-9",
        "invoice_date": "2026-01-01",
        "due_date": "2026-02-01",
        "currency": "USD",
        "line_items": [
            {
                "item": "WidgetA",
                "description": "WidgetA",
                "quantity": 2,
                "unit_price": 250,
                "amount": 500,
                "note": None,
            }
        ],
        "subtotal": 500,
        "tax_rate": 0,
        "tax_amount": 0,
        "shipping": 0,
        "total": 500,
        "payment_terms": "Net 30",
        "notes": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def write_invoice(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def stages(result) -> list[str]:
    return [row["stage"] for row in result["logs"]]


def status(result) -> str:
    payment = result["payment"]
    if isinstance(payment, dict):
        return payment["status"]
    return payment.status


def scripted(extract_text, *, audits=None, critique=None, before_extract=None, reasons=None):
    """before_extract(call_number) may raise LLMError or return a stand-in string.

    reasons, when set, is the sequence of TASK reason replies. The last one repeats.
    """

    audit_replies = list(audits or ["PASS"])
    reason_replies = None if reasons is None else list(reasons)
    counts = {"extract": 0, "audit": 0, "critique": 0, "reason": 0}

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            counts["extract"] += 1
            if before_extract is not None:
                replacement = before_extract(counts["extract"], messages)
                if replacement is not None:
                    return replacement
            return extract_text
        if name == "reason":
            counts["reason"] += 1
            if reason_replies is not None:
                index = min(counts["reason"] - 1, len(reason_replies) - 1)
                return reason_replies[index]
            return json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
        if name == "critique":
            counts["critique"] += 1
            body = critique or {"concerns": ["amount is high"], "stricter": False}
            return json.dumps(body)
        if name == "audit":
            counts["audit"] += 1
            index = min(counts["audit"] - 1, len(audit_replies) - 1)
            verdict = audit_replies[index]
            feedback = ["due_date is wrong"] if verdict == "SEND_BACK" else []
            return json.dumps({"verdict": verdict, "feedback": feedback, "notes": verdict})
        raise AssertionError(f"unexpected task {name}")

    return handler, counts


def run(path, db_path, handler, clock=None, max_attempts=5):
    return run_invoice(
        path,
        db_path,
        FakeLLM(handler),
        clock=clock or FakeClock(),
        rng=ZeroRandom(),
        max_attempts=max_attempts,
    )


def test_hard_failure_goes_to_reject_and_does_not_pay(tmp_path, db_path):
    path = write_invoice(tmp_path, "hard.txt", "GadgetX 20\nTotal: 15000.00\n")
    body = extraction(
        line_items=[
            {
                "item": "GadgetX",
                "description": "GadgetX",
                "quantity": 20,
                "unit_price": 750,
                "amount": 15000,
                "note": None,
            }
        ],
        subtotal=15000,
        total=15000,
    )
    handler, counts = scripted(body)
    result = run(path, db_path, handler)
    assert result["outcome"] == "reject"
    assert status(result) == "not_paid"
    assert stages(result) == ["ingestion", "validation", "reject_log"]
    assert counts["critique"] == 0
    assert counts["audit"] == 0
    assert "GadgetX quantity 20 exceeds stock of 5." in result["decision"].reasoning.evidence
    assert "exceeds stock" in result["decision"].reasoning.conclusion
    assert [finding.code for finding in result["findings"]] == ["STOCK_SHORTFALL"]
    validation = next(row for row in result["logs"] if row["stage"] == "validation")
    assert validation["result"]["codes"] == ["STOCK_SHORTFALL"]


def test_clean_invoice_is_paid_and_reasoning_cites_real_figures(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA 2\nTotal: 500.00\n")
    handler, counts = scripted(extraction())
    result = run(path, db_path, handler)
    assert result["outcome"] == "approve"
    assert status(result) == "paid"
    assert stages(result) == ["ingestion", "validation", "approval", "audit", "payment"]
    assert counts["critique"] == 0
    reasoning = result["decision"].reasoning
    assert reasoning.rules_applied
    assert reasoning.risks_considered == ["no extra risk"]
    assert reasoning.conclusion == "The checks passed."
    blob = " ".join(reasoning.evidence)
    assert "WidgetA" in blob
    assert "500.00" in blob
    assert "stock 15" in blob
    auth = result["authorization"]
    assert auth.vendor == result["invoice"].vendor
    assert auth.amount == result["invoice"].total
    assert auth.currency == result["invoice"].currency
    assert auth.key.startswith(str(path) + ":")


def test_over_10k_goes_through_critique_and_hits_the_cap(tmp_path, db_path):
    path = write_invoice(tmp_path, "big.txt", "WidgetA 1\nTotal: 12000.00\n")
    body = extraction(
        line_items=[
            {
                "item": "WidgetA",
                "description": "WidgetA",
                "quantity": 1,
                "unit_price": 12000,
                "amount": 12000,
                "note": None,
            }
        ],
        subtotal=12000,
        total=12000,
    )
    handler, counts = scripted(body)
    result = run(path, db_path, handler)
    assert result["outcome"] == "approve"
    assert status(result) == "paid"
    assert counts["critique"] == CRITIQUE_CAP
    assert result["critique_rounds"] == CRITIQUE_CAP
    assert counts["reason"] == CRITIQUE_CAP + 1
    assert "amount is high" in result["decision"].reasoning.risks_considered[-1]
    assert len(result["decision"].revisions) == 1
    assert result["decision"].revisions[0].conclusion == "The checks passed."
    assert APPROVAL_THRESHOLD_USD == Decimal("10000")


def test_renamed_vendor_is_a_hold_and_does_not_pay(tmp_path, db_path):
    path = write_invoice(
        tmp_path,
        "rename.txt",
        "Vendor: QuickShip (formerly FastShip Ltd.)\nWidgetA 1\nTotal: 250.00\n",
    )
    body = extraction(
        vendor="QuickShip",
        line_items=[
            {
                "item": "WidgetA",
                "description": "WidgetA",
                "quantity": 1,
                "unit_price": 250,
                "amount": 250,
                "note": None,
            }
        ],
        subtotal=250,
        total=250,
    )
    handler, counts = scripted(body)
    result = run(path, db_path, handler)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "VENDOR_RENAMED"
    assert status(result) == "not_paid"
    assert "payment" not in stages(result)
    assert counts["audit"] == 0
    assert "formerly" in result["decision"].reasoning.conclusion
    assert result["findings"][0].code == "VENDOR_RENAMED"
    assert result["findings"][0].severity == "warning"


def test_audit_pass_pays_and_send_back_uses_feedback(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nTotal: 500.00\n")
    handler, counts = scripted(extraction(), audits=["SEND_BACK", "PASS"])
    result = run(path, db_path, handler)
    assert result["outcome"] == "approve"
    assert status(result) == "paid"
    assert counts["audit"] == 2
    assert result["send_backs"] == 1
    ingestions = [row for row in result["logs"] if row["stage"] == "ingestion"]
    assert ingestions[0]["result"]["feedback_used"] == []
    assert ingestions[1]["result"]["feedback_used"] == ["due_date is wrong"]


def test_three_send_backs_then_escalate(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nTotal: 500.00\n")
    handler, counts = scripted(extraction(), audits=["SEND_BACK"])
    result = run(path, db_path, handler)
    assert MAX_SEND_BACKS == 3
    assert result["send_backs"] == 3
    assert counts["audit"] == 4
    assert sum(1 for row in result["logs"] if row["stage"] == "ingestion") == 4
    assert result["audit_verdict"] == "ESCALATE"
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "AUDIT_ESCALATED"
    assert result["findings"][-1].code == "AUDIT_ESCALATED"
    assert status(result) == "not_paid"
    assert "payment" not in stages(result)


def test_audit_cannot_override_a_hard_failure(db_path):
    invoice = ExtractedInvoice(
        vendor="Gadgets Co.",
        due_date="2026-02-01",
        currency="USD",
        total=Decimal("15000"),
        subtotal=Decimal("15000"),
        tax_amount=Decimal("0"),
        line_items=[
            LineItem(
                item="GadgetX",
                description="GadgetX",
                quantity=Decimal("20"),
                unit_price=Decimal("750"),
                amount=Decimal("15000"),
            )
        ],
        source=SourceInfo(path="x", filename="x.txt", format="txt", raw_text="Total: 15000.00"),
    )
    decision = ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(
            rules_applied=["placeholder"],
            evidence=["total 15000.00 USD"],
            risks_considered=["none"],
            conclusion="pay it",
        ),
        amount=Decimal("15000"),
        currency="USD",
    )
    state = PipelineState(
        invoice_path="x",
        filename="x.txt",
        raw_text="GadgetX 20\nTotal: 15000.00\n",
        invoice=invoice,
        validation=ValidationReport(passed=True, findings=[]),
        decision=decision,
    )

    def handler(_messages):
        return json.dumps({"verdict": "PASS", "feedback": [], "notes": "override"})

    out = audit(state, FakeLLM(handler), db_path, FakeClock(), ZeroRandom(), 5)
    assert out["outcome"] == "reject"
    assert out["decision"].decision == "reject"
    assert "exceeds stock" in out["decision"].reasoning.conclusion
    assert "payment" not in out


def test_audit_cannot_pay_non_usd(db_path):
    invoice = ExtractedInvoice(
        vendor="TechParts International",
        due_date="2026-02-26",
        currency="EUR",
        total=Decimal("100"),
        subtotal=Decimal("100"),
        tax_amount=Decimal("0"),
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                amount=Decimal("100"),
            )
        ],
        source=SourceInfo(path="x", filename="x.xml", format="xml", raw_text="EUR\nTotal: 100.00"),
    )
    decision = ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(
            rules_applied=["placeholder"],
            evidence=["total 100.00 EUR"],
            risks_considered=["none"],
            conclusion="pay it",
        ),
        amount=Decimal("100"),
        currency="EUR",
    )
    state = PipelineState(
        invoice_path="x",
        filename="x.xml",
        raw_text="<currency>EUR</currency>\nTotal: 100.00\n",
        invoice=invoice,
        validation=ValidationReport(passed=True, findings=[]),
        decision=decision,
    )

    def handler(_messages):
        return json.dumps({"verdict": "PASS", "feedback": [], "notes": "pay"})

    out = audit(state, FakeLLM(handler), db_path, FakeClock(), ZeroRandom(), 5)
    assert out["outcome"] == "hold"
    assert "Non-USD currency (EUR)." in out["decision"].reasoning.conclusion


def test_non_usd_invoice_cannot_reach_payment(tmp_path, db_path):
    path = write_invoice(tmp_path, "eur.txt", "Amount due 100.00 euros\n")
    body = extraction(currency="USD", total=100, subtotal=100, line_items=[
        {"item": "WidgetA", "description": "WidgetA", "quantity": 1, "unit_price": 100, "amount": 100, "note": None}
    ])
    handler, counts = scripted(body)
    result = run(path, db_path, handler)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "NON_USD"
    assert status(result) == "not_paid"
    assert "payment" not in stages(result)
    assert counts["reason"] == 0
    assert result["decision"].reasoning.conclusion == (
        "Non-USD currency (EUR). Finance confirms the currency and amount. "
        "Nothing is converted and nothing is paid."
    )
    assert result["findings"][0].code == "NON_USD"
    assert result["findings"][0].item == "currency"


def test_currency_detection_without_a_conversion():
    assert resolve_currency(None, "Total $50.00") == "USD"
    assert resolve_currency("USD", "Total $50.00") == "USD"
    assert resolve_currency(None, "Amount: 10 euros") == "EUR"
    assert resolve_currency(None, "Pay €10") == "EUR"
    assert resolve_currency("EUR", "Total 10") == "EUR"
    assert resolve_currency("GBP", "Total 10") == "GBP"


def test_retry_then_success_uses_the_fake_clock(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Total: 500.00\n")

    def before(call_number, _messages):
        if call_number == 1:
            raise LLMError("http_429", True, "busy")
        return None

    handler, _counts = scripted(extraction(), before_extract=before)
    clock = FakeClock()
    result = run(path, db_path, handler, clock=clock)
    assert clock.slept == [1.0]
    assert result["outcome"] == "approve"
    assert status(result) == "paid"
    assert result["attempts"][0]["error_type"] == "http_429"
    assert result["attempts"][0]["backoff_seconds"] == 1.0
    assert BACKOFF_SECONDS == (1, 2, 4, 8, 16)


_BAD_REASON = json.dumps(
    {
        "risks_considered": [
            "The provided input string is not valid JSON and cannot be parsed.",
            "The task requires returning only a JSON object matching the specified schema.",
            "No vendor, total, currency, evidence, or findings data can be reliably extracted.",
        ],
        "conclusion": "Invalid input - unable to process or approve.",
    }
)
_GOOD_REASON = json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})


def test_bad_reasoning_once_then_good_retries_and_pays(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA 2\nTotal: 500.00\n")
    handler, counts = scripted(extraction(), reasons=[_BAD_REASON, _GOOD_REASON])
    clock = FakeClock()
    result = run(path, db_path, handler, clock=clock)
    assert clock.slept == [1.0]
    assert counts["reason"] == 2
    assert result["outcome"] == "approve"
    assert status(result) == "paid"
    assert result["agent_error"] is None
    assert result["attempts"] == [
        {
            "attempt": 1,
            "error_type": "bad_reasoning",
            "backoff_seconds": 1.0,
            "retryable": True,
            "matched": "invalid input",
            "conclusion": "Invalid input - unable to process or approve.",
            "risks_considered": [
                "The provided input string is not valid JSON and cannot be parsed.",
                "The task requires returning only a JSON object matching the specified schema.",
                "No vendor, total, currency, evidence, or findings data can be reliably extracted.",
            ],
        }
    ]
    assert result["decision"].reasoning.conclusion == "The checks passed."
    assert "payment" in stages(result)


def test_bad_reasoning_every_time_is_an_unpaid_hold(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA 2\nTotal: 500.00\n")
    handler, counts = scripted(extraction(), reasons=[_BAD_REASON])
    clock = FakeClock()
    max_attempts = 3
    result = run(path, db_path, handler, clock=clock, max_attempts=max_attempts)
    assert clock.slept == [1.0, 2.0]
    assert counts["reason"] == max_attempts
    assert len(result["attempts"]) == max_attempts
    assert [row["error_type"] for row in result["attempts"]] == ["bad_reasoning"] * max_attempts
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "BAD_REASONING"
    assert status(result) == "not_paid"
    assert result["agent_error"] == f"agent step failed after {max_attempts} attempts: bad_reasoning"
    assert result["findings"][-1].code == "BAD_REASONING"
    assert "payment" not in stages(result)
    assert stages(result)[-1] == "hold"


def test_bad_reasoning_attempt_log_contains_the_rejected_text(tmp_path, db_path, capsys):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA 2\nTotal: 500.00\n")
    long_conclusion = "A" * 600
    long_risk = "B" * 600
    phrase_reply = json.dumps(
        {
            "risks_considered": ["The input is unparseable.", long_risk],
            "conclusion": long_conclusion,
        }
    )
    handler, counts = scripted(extraction(), reasons=[phrase_reply, _GOOD_REASON])
    result = run(path, db_path, handler, clock=FakeClock())
    assert counts["reason"] == 2
    assert result["outcome"] == "approve"
    assert status(result) == "paid"
    assert result["decision"].reasoning.conclusion == "The checks passed."

    entry = result["attempts"][0]
    assert entry["attempt"] == 1
    assert entry["error_type"] == "bad_reasoning"
    assert entry["matched"] == "unparseable"
    assert entry["conclusion"] == "A" * 500
    assert entry["risks_considered"] == ["The input is unparseable.", "B" * 500]
    assert set(entry) == {
        "attempt",
        "error_type",
        "backoff_seconds",
        "retryable",
        "matched",
        "conclusion",
        "risks_considered",
    }
    approval = next(row for row in result["logs"] if row["stage"] == "approval")
    assert approval["invoice_file"] == "clean.txt"
    assert approval["result"]["attempts"][0] == entry

    contradict = json.dumps(
        {
            "risks_considered": ["stock is fine"],
            "conclusion": "Unable to approve this invoice.",
        }
    )
    handler, _counts = scripted(extraction(), reasons=[contradict])
    stopped = run(path, db_path, handler, clock=FakeClock(), max_attempts=1)
    assert stopped["outcome"] == "hold"
    assert status(stopped) == "not_paid"
    rejected = stopped["attempts"][0]
    assert rejected["matched"] == "approve-contradiction"
    assert rejected["conclusion"] == "Unable to approve this invoice."
    assert rejected["risks_considered"] == ["stock is fine"]
    stopped_log = next(row for row in stopped["logs"] if row["stage"] == "approval")
    assert stopped_log["result"]["attempts"][0] == rejected

    print_bad_reasoning(path.name, [rejected])
    printed = capsys.readouterr().out
    assert '"matched": "unparseable"' in printed
    assert "A" * 500 in printed
    line = (
        f"{path.name}\tbad_reasoning\tattempt=1\tmatched=approve-contradiction\t"
        f"conclusion=Unable to approve this invoice.\t"
        f"risks={json.dumps(['stock is fine'])}"
    )
    assert line in printed


def test_reasoning_output_failed_covers_refusal_and_normal_wording():
    flake = ReasoningDraft(
        risks_considered=[
            "The provided input string is not valid JSON and cannot be parsed.",
            "The task requires returning only a JSON object matching the specified schema.",
            "No vendor, total, currency, evidence, or findings data can be reliably extracted.",
        ],
        conclusion="Invalid input - unable to process or approve.",
    )
    assert reasoning_output_failed(flake, "approve") is True
    assert reasoning_output_failed(flake, "hold") is True

    unparseable = ReasoningDraft(
        risks_considered=["The input is unparseable."],
        conclusion="See the risks.",
    )
    assert reasoning_output_failed(unparseable, "approve") is True

    contradicts = ReasoningDraft(
        risks_considered=["no extra risk"],
        conclusion="Unable to approve this invoice.",
    )
    assert reasoning_output_failed(contradicts, "approve") is True
    not_to = ReasoningDraft(
        risks_considered=["no extra risk"],
        conclusion="The rules say not to approve.",
    )
    assert reasoning_output_failed(not_to, "approve") is True

    passed = ReasoningDraft(risks_considered=["no extra risk"], conclusion="The checks passed.")
    assert reasoning_output_failed(passed, "approve") is False
    approve_line = ReasoningDraft(risks_considered=["stock is valid"], conclusion="Approve.")
    assert reasoning_output_failed(approve_line, "approve") is False
    schema_mention = ReasoningDraft(
        risks_considered=["Return only a JSON object matching the specified schema."],
        conclusion="The checks passed.",
    )
    assert reasoning_output_failed(schema_mention, "approve") is False
    hold = ReasoningDraft(
        risks_considered=["amount is high"],
        conclusion="Do not approve. Manual review, no payment.",
    )
    assert reasoning_output_failed(hold, "hold") is False


def test_exhausted_retries_are_a_hold_and_unpaid(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Total: 500.00\n")

    def handler(_messages):
        raise LLMError("http_429", True, "busy")

    clock = FakeClock()
    result = run(path, db_path, handler, clock=clock, max_attempts=3)
    assert clock.slept == [1.0, 2.0]
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert status(result) == "not_paid"
    assert result["agent_error"] == "agent step failed after 3 attempts: http_429"
    assert result["findings"][-1].code == "SYSTEM_ERROR"
    assert "Rerun the invoice" in (result["action"] or "")
    assert "payment" not in stages(result)
    assert [row["error_type"] for row in result["attempts"]] == ["http_429", "http_429", "http_429"]


def test_non_retryable_error_skips_the_backoff(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Total: 500.00\n")
    fake = FakeLLM(lambda _messages: (_ for _ in ()).throw(LLMError("http_401", False, "no")))
    clock = FakeClock()
    result = run_invoice(path, db_path, fake, clock=clock, rng=ZeroRandom(), max_attempts=5)
    assert clock.slept == []
    assert len(fake.calls) == 1
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert status(result) == "not_paid"
    assert result["agent_error"] == "agent step failed after 1 attempts: http_401"
    assert "payment" not in stages(result)


def test_schema_repair_then_success_does_not_sleep(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Total: 500.00\n")
    calls = {"n": 0}

    def before(call_number, _messages):
        calls["n"] = call_number
        if call_number == 1:
            return "this is not json"
        return None

    handler, _counts = scripted(extraction(), before_extract=before)
    clock = FakeClock()
    result = run(path, db_path, handler, clock=clock)
    assert SCHEMA_REPAIR_LIMIT == 2
    assert calls["n"] == 2
    assert clock.slept == []
    assert result["outcome"] == "approve"
    assert status(result) == "paid"


def test_missing_key_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(MissingAPIKey, match="XAI_API_KEY"):
        GrokClient()
    env = os.environ.copy()
    env.pop("XAI_API_KEY", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), f"--invoice_path={INVOICES / 'invoice_1001.txt'}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "XAI_API_KEY" in proc.stderr
    folder = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), f"--invoice_path={INVOICES}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert folder.returncode == 2
    assert DEFAULT_MODEL == "grok-4.20-0309-non-reasoning"
    assert BASE_URL == "https://api.x.ai/v1"
    assert DEFAULT_MAX_ATTEMPTS == 5


def test_full_run_leaves_the_database_hash_unchanged(tmp_path, db_path, monkeypatch):
    before = db_path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    seen = []
    real_connect = sqlite3.connect

    def wrapped(*args, **kwargs):
        seen.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", wrapped)
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nTotal: 500.00\n")
    handler, _counts = scripted(extraction())
    result = run(path, db_path, handler)
    assert result["outcome"] == "approve"
    assert db_path.read_bytes() == before
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == digest
    assert seen
    for args, kwargs in seen:
        assert kwargs.get("uri") is True
        assert "mode=ro" in args[0]
    readonly = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("UPDATE inventory SET stock = 1")
    finally:
        readonly.close()


def test_answer_key_covers_the_sample_files():
    from db import SEED
    from decision.wording import KEY_REASON_TO_CODE
    from scripts.eval import load_answer_key

    key = load_answer_key()
    sample = sorted(
        path.name
        for path in INVOICES.iterdir()
        if path.is_file() and path.suffix.lower() in {".txt", ".pdf", ".json", ".csv", ".xml"}
    )
    assert len(sample) == 20
    assert sorted(Path(name).name for name in key) == sample
    stock = dict(SEED)
    counts = {"approve": 0, "reject": 0, "hold": 0}
    for filename in sample:
        expected = key[f"data/invoices/{filename}"]
        if filename in README_OUTCOMES:
            assert expected["outcome"].lower() == README_OUTCOMES[filename]
        invoice = parse_file(INVOICES / filename)
        fresh = assess(invoice, invoice.source.raw_text or "", stock)
        assert fresh.outcome == expected["outcome"].lower()
        assert fresh.reason_code == KEY_REASON_TO_CODE[expected["primary_reason"]]
        assert fresh.outcome == outcome_from_findings(fresh.findings)
        counts[fresh.outcome] += 1
        for finding in fresh.findings:
            assert finding.code in REJECT_CODES or finding.code in HOLD_CODES or finding.code in WARNING_CODES
    assert counts == {"approve": 7, "reject": 9, "hold": 4}
    assert "FakeItem" in assess(
        parse_file(INVOICES / "invoice_1003.txt"),
        (INVOICES / "invoice_1003.txt").read_text(encoding="utf-8"),
        stock,
    ).reason


def test_committed_diagram_matches_the_graph():
    diagram = mermaid_text()
    saved = (ROOT / "docs" / "graph.md").read_text(encoding="utf-8")
    assert diagram in saved
    for name in ("ingestion", "validation", "approval", "critique", "audit", "payment", "reject_log"):
        assert name in saved


def test_outcome_from_findings_rejects_holds_or_approves():
    stock = Finding(code="STOCK_SHORTFALL", message="short", severity="blocking", item="GadgetX")
    hold = Finding(code="NON_USD", message="non-USD currency (EUR), manual review required", severity="warning", item="currency")
    note = Finding(code="NOTE", message="informational", severity="info")
    soft = Finding(code="STOCK_SHORTFALL", message="short", severity="warning", item="GadgetX")

    assert outcome_from_findings([]) == "approve"
    assert outcome_from_findings([note]) == "approve"
    assert outcome_from_findings([soft]) == "approve"
    assert outcome_from_findings([stock]) == "reject"
    assert outcome_from_findings([hold]) == "hold"
    assert outcome_from_findings([hold, stock]) == "reject"


def test_approval_prompt_names_the_invoice_facts():
    invoice = ExtractedInvoice(
        vendor="Widgets Inc.",
        due_date="2026-02-01",
        currency="USD",
        total=Decimal("500"),
        line_items=[
            LineItem(item="WidgetA", description="WidgetA", quantity=Decimal("2"), unit_price=Decimal("250"))
        ],
        source=SourceInfo(path="x.txt", filename="x.txt", format="txt"),
    )
    report = ValidationReport(passed=True, findings=[])
    state = PipelineState(invoice_path="x.txt", filename="x.txt", invoice=invoice, validation=report)
    messages = _reason_messages(state, invoice, report, ["total 500.00 USD"])
    assert "valid JSON object of invoice facts" in messages[0]["content"]
    user = messages[1]["content"]
    assert user.startswith("Invoice facts (a valid JSON object):\n{")
    payload = json.loads(user.split("\n", 1)[1])
    assert payload["vendor"] == "Widgets Inc."
    assert payload["currency"] == "USD"
    assert payload["evidence"] == ["total 500.00 USD"]


def _signed(**overrides) -> PaymentAuthorization:
    fields = {
        "vendor": "Widgets Inc.",
        "amount": Decimal("500.00"),
        "currency": "USD",
        "key": "pay-once",
    }
    fields.update(overrides)
    return PaymentAuthorization(**fields)


def _payable(authorization: PaymentAuthorization | None) -> PipelineState:
    invoice = ExtractedInvoice(
        vendor="Widgets Inc.",
        due_date="2026-02-01",
        currency="USD",
        total=Decimal("500.00"),
        subtotal=Decimal("500.00"),
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("2"),
                unit_price=Decimal("250"),
                amount=Decimal("500"),
            )
        ],
        source=SourceInfo(path="memo.txt", filename="memo.txt", format="txt", raw_text="Total: 500.00"),
    )
    return PipelineState(
        invoice_path="memo.txt",
        filename="memo.txt",
        raw_text="Total: 500.00\n",
        invoice=invoice,
        validation=ValidationReport(passed=True, findings=[]),
        decision=ApprovalDecision(
            decision="approve",
            reasoning=ApprovalReasoning(
                rules_applied=["stock"],
                evidence=["total 500.00 USD"],
                risks_considered=["none"],
                conclusion="pay it",
            ),
            amount=invoice.total,
            currency=invoice.currency,
        ),
        outcome="approve",
        audit_verdict="PASS",
        authorization=authorization,
    )


def test_payment_refuses_without_authorization():
    result = payment(_payable(None))
    assert result["payment"].status == "not_paid"
    assert result["payment"].detail == "payment refused: no audit authorization"
    assert result["findings"][-1].code == "PAYMENT_REFUSED"


def test_payment_refuses_a_mismatched_amount():
    result = payment(_payable(_signed(amount=Decimal("1.00"), key="amount-mismatch")))
    assert result["payment"].status == "not_paid"
    assert "amount does not match" in result["payment"].detail


def test_payment_refuses_a_mismatched_vendor_or_currency():
    vendor = payment(_payable(_signed(vendor="Other Co.", key="vendor-mismatch")))
    currency = payment(_payable(_signed(currency="EUR", key="currency-mismatch")))
    assert vendor["payment"].status == "not_paid"
    assert "vendor does not match" in vendor["payment"].detail
    assert currency["payment"].status == "not_paid"
    assert "currency does not match" in currency["payment"].detail


def test_payment_pays_once_and_refuses_a_reused_key(capsys):
    state = _payable(_signed(key="once-only"))
    first = payment(state)
    second = payment(state)
    assert first["payment"].status == "paid"
    assert first["payment"].mock_response == {"status": "success"}
    assert second["payment"].status == "not_paid"
    assert "already used" in second["payment"].detail
    assert capsys.readouterr().out.count("Paid 500.00 to Widgets Inc.") == 1
