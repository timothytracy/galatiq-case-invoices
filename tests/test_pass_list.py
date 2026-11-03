"""The pass list: every payment meets six named checks."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from decision.pass_list import CHECK_NAMES, failed_checks, pass_checks
from graph import run_invoice
from models import ExtractedInvoice, Finding, SourceInfo
from tests.fake_llm_eval import AS_OF, code_only, handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom

ROOT = Path(__file__).resolve().parents[1]
SETS = (
    ("data/invoices", "data/answer_key.json"),
    ("data/extra_invoices", "data/extra_answer_key.json"),
    ("data/loop_invoices", "data/loop_answer_key.json"),
    ("data/fraud_invoices", "data/fraud_answer_key.json"),
    ("data/passlist_invoices", "data/passlist_answer_key.json"),
)


def _approves():
    found = []
    for _folder, key in SETS:
        path = ROOT / key
        if not path.is_file():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))["invoices"]
        for name, row in rows.items():
            if code_only(row)["outcome"] == "APPROVE":
                found.append(name)
    return found


def _invoice(**changes) -> ExtractedInvoice:
    base = {
        "vendor": "Widgets Inc.",
        "invoice_number": "INV-1",
        "currency": "USD",
        "total": Decimal("500.00"),
        "source": SourceInfo(path="x.txt", filename="x.txt", format="txt"),
    }
    base.update(changes)
    return ExtractedInvoice(**base)


def test_every_check_passes_for_a_clean_audited_invoice(real_vendor_master):
    rows = pass_checks(_invoice(), [], "PASS")
    assert [row["check"] for row in rows] == list(CHECK_NAMES)
    assert all(row["ok"] for row in rows), rows


@pytest.mark.parametrize(
    "invoice, findings, verdict, failed",
    [
        ({"vendor": "Northwind Supply"}, [], "PASS", "Known vendor"),
        ({}, [Finding(code="NEW_REMITTANCE", message="x")], "PASS", "Bank details unchanged"),
        ({}, [Finding(code="PRINTED_TOTAL_MISMATCH", message="x")], "PASS", "Printed total matches"),
        ({}, [Finding(code="INJECTION_SUSPECTED", message="x")], "PASS", "No embedded instructions"),
        ({"total": Decimal("0")}, [], "PASS", "Total above zero, in USD"),
        ({"currency": "EUR"}, [], "PASS", "Total above zero, in USD"),
        ({}, [], "ESCALATE", "Audit passed"),
    ],
)
def test_each_check_fails_on_its_own(invoice, findings, verdict, failed, real_vendor_master):
    assert failed_checks(_invoice(**invoice), findings, verdict) == [failed]


@pytest.mark.parametrize("name", _approves())
def test_every_labeled_approve_passes_the_pass_list(name, db_path, tmp_path, real_vendor_master):
    path = ROOT / name
    result = run_invoice(
        path, db_path, FakeLLM(handler_for(path)),
        clock=FakeClock(), rng=ZeroRandom(), max_attempts=5, as_of=AS_OF,
        ledger_path=tmp_path / "payments.db",
    )
    assert result["outcome"] == "approve", (name, result.get("reason"))
    payment_log = next(row for row in result["logs"] if row["stage"] == "payment")
    checks = payment_log["result"]["pass_list"]
    assert all(row["ok"] for row in checks), checks
    assert result["payment"].status == "paid"


def test_payment_refuses_an_approve_that_fails_the_pass_list(db_path, tmp_path):
    from pipeline.payment import _payment_blocked
    from models import ApprovalDecision, ApprovalReasoning, PipelineState

    decision = ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(rules_applied=["r"], evidence=["e"], risks_considered=["none"], conclusion="ok"),
    )
    state = PipelineState(
        invoice_path="x.txt",
        filename="x.txt",
        invoice=_invoice(vendor="Unlisted Vendor Co."),
        outcome="approve",
        decision=decision,
        audit_verdict="PASS",
    )
    assert _payment_blocked(state) == "pass list failed: Known vendor"
    listed = state.model_copy(update={"invoice": _invoice()})
    assert _payment_blocked(listed) == ""


def test_holds_and_rejects_record_the_pass_list(db_path, tmp_path):
    path = ROOT / "data/invoices/invoice_1002.txt"
    result = run_invoice(
        path, db_path, FakeLLM(handler_for(path)),
        clock=FakeClock(), rng=ZeroRandom(), max_attempts=5, as_of=AS_OF,
        ledger_path=tmp_path / "payments.db",
    )
    final = next(row for row in reversed(result["logs"]) if row["stage"] in {"reject_log", "hold"})
    assert [row["check"] for row in final["result"]["pass_list"]] == list(CHECK_NAMES)
    assert not all(row["ok"] for row in final["result"]["pass_list"])
