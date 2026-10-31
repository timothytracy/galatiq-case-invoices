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
    InvoiceLabel,
    LineItem,
    PipelineState,
    SourceInfo,
    ValidationReport,
)
from parsers import parse_file
from pipeline import audit
from rules import (
    APPROVAL_THRESHOLD_USD,
    CRITIQUE_CAP,
    MAX_SEND_BACKS,
    assess,
    resolve_currency,
)

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"
LABELS = ROOT / "data" / "labels.json"

README_OUTCOMES = {
    "invoice_1001.txt": "approve",
    "invoice_1004.json": "approve",
    "invoice_1006.csv": "approve",
    "invoice_1002.txt": "reject",
    "invoice_1003.txt": "reject",
    "invoice_1008.txt": "reject",
    "invoice_1016.json": "reject",
    "invoice_1009.json": "reject",
    "invoice_1012.pdf": "manual_review",
    "invoice_1012.txt": "manual_review",
    "invoice_1014.xml": "manual_review",
}


class ZeroRandom:
    def uniform(self, _start, _end):
        return 0.0


class FakeClock:
    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


class FakeLLM:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, messages, timeout=60):
        self.calls.append(messages)
        result = self.handler(messages)
        if isinstance(result, Exception):
            raise result
        return result


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


def task_of(messages) -> str:
    for message in messages:
        first = (message.get("content") or "").splitlines()[0]
        if first.startswith("TASK "):
            return first.split()[1]
    return ""


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


def scripted(extract_text, *, audits=None, critique=None, before_extract=None):
    """before_extract(call_number) may raise LLMError or return a stand-in string."""

    audit_replies = list(audits or ["PASS"])
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


def test_manual_review_does_not_pay(tmp_path, db_path):
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
    assert result["outcome"] == "manual_review"
    assert status(result) == "not_paid"
    assert "payment" not in stages(result)
    assert counts["audit"] == 0
    assert "formerly" in result["decision"].reasoning.conclusion


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
    assert result["outcome"] == "manual_review"
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
    assert out["outcome"] == "manual_review"
    assert "non-USD currency (EUR), manual review required" in out["decision"].reasoning.conclusion


def test_non_usd_invoice_cannot_reach_payment(tmp_path, db_path):
    path = write_invoice(tmp_path, "eur.txt", "Amount due 100.00 euros\n")
    body = extraction(currency="USD", total=100, subtotal=100, line_items=[
        {"item": "WidgetA", "description": "WidgetA", "quantity": 1, "unit_price": 100, "amount": 100, "note": None}
    ])
    handler, counts = scripted(body)
    result = run(path, db_path, handler)
    assert result["outcome"] == "manual_review"
    assert status(result) == "not_paid"
    assert "payment" not in stages(result)
    assert counts["reason"] == 0
    assert result["decision"].reasoning.conclusion == "non-USD currency (EUR), manual review required"


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


def test_exhausted_retries_are_manual_review_and_unpaid(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Total: 500.00\n")

    def handler(_messages):
        raise LLMError("http_429", True, "busy")

    clock = FakeClock()
    result = run(path, db_path, handler, clock=clock, max_attempts=3)
    assert clock.slept == [1.0, 2.0]
    assert result["outcome"] == "manual_review"
    assert status(result) == "not_paid"
    assert result["agent_error"] == "agent step failed after 3 attempts: http_429"
    assert "payment" not in stages(result)
    assert [row["error_type"] for row in result["attempts"]] == ["http_429", "http_429", "http_429"]


def test_non_retryable_error_skips_the_backoff(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Total: 500.00\n")
    fake = FakeLLM(lambda _messages: (_ for _ in ()).throw(LLMError("http_401", False, "no")))
    clock = FakeClock()
    result = run_invoice(path, db_path, fake, clock=clock, rng=ZeroRandom(), max_attempts=5)
    assert clock.slept == []
    assert len(fake.calls) == 1
    assert result["outcome"] == "manual_review"
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


def test_labels_cover_all_20_and_match_the_readme_table():
    payload = json.loads(LABELS.read_text(encoding="utf-8"))
    rows = [InvoiceLabel.model_validate(row) for row in payload["invoices"]]
    sample = sorted(
        path.name
        for path in INVOICES.iterdir()
        if path.is_file() and path.suffix.lower() in {".txt", ".pdf", ".json", ".csv", ".xml"}
    )
    assert len(sample) == 20
    assert [row.file for row in rows] == sample
    by_file = {row.file: row for row in rows}
    for filename, outcome in README_OUTCOMES.items():
        assert by_file[filename].outcome == outcome
    assert "FakeItem" in by_file["invoice_1003.txt"].reason
    assert "exceeds stock" in by_file["invoice_1002.txt"].reason
    assert "SuperGizmo" in by_file["invoice_1008.txt"].reason
    assert "MegaSprocket" in by_file["invoice_1008.txt"].reason
    assert "WidgetC" in by_file["invoice_1016.json"].reason
    assert "negative" in by_file["invoice_1009.json"].reason.lower()
    assert by_file["invoice_1014.xml"].reason == "non-USD currency (EUR), manual review required"
    assert "formerly" in by_file["invoice_1012.pdf"].reason
    assert by_file["invoice_1004_revised.json"].outcome == "approve"
    assert "stock 5" in by_file["invoice_1004_revised.json"].reason
    for left, right in (
        ("invoice_1004.json", "invoice_1004_revised.json"),
        ("invoice_1011.pdf", "invoice_1011.txt"),
        ("invoice_1012.pdf", "invoice_1012.txt"),
        ("invoice_1013.json", "invoice_1013.pdf"),
    ):
        assert by_file[left].outcome == by_file[right].outcome
    counts = {"approve": 0, "reject": 0, "manual_review": 0}
    from db import SEED

    stock = dict(SEED)
    for row in rows:
        counts[row.outcome] += 1
        invoice = parse_file(INVOICES / row.file)
        fresh = assess(invoice, invoice.source.raw_text or "", stock)
        assert fresh.outcome == row.outcome
        assert fresh.reason == row.reason
        assert "duplicate" not in row.reason.lower()
    assert counts == {"approve": 8, "reject": 9, "manual_review": 3}


def test_committed_diagram_matches_the_graph():
    diagram = mermaid_text()
    saved = (ROOT / "docs" / "graph.md").read_text(encoding="utf-8")
    assert diagram in saved
    for name in ("ingestion", "validation", "approval", "critique", "audit", "payment", "reject_log"):
        assert name in saved
