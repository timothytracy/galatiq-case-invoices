"""Reviewer release uses the pipeline record, not the caller's invoice."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from models import Finding
from pipeline.approve_hold import ROOT, approve_and_pay, remember
from tests.fake_llm_eval import AS_OF, handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom

WHEN = __import__("datetime").datetime(2026, 11, 2, 4, 20, tzinfo=__import__("datetime").timezone.utc)


def _run(path: Path):
    from graph import run_invoice
    return run_invoice(
        path, ROOT / "inventory.db", FakeLLM(handler_for(path)),
        clock=FakeClock(), rng=ZeroRandom(), max_attempts=5, as_of=AS_OF,
    )


def _pay(result, tmp_path, **overrides):
    invoice = result["invoice"] if isinstance(result, dict) else result.invoice
    total = invoice["total"] if isinstance(invoice, dict) else invoice.total
    return approve_and_pay(
        result,
        reviewer=overrides.pop("reviewer", "Avery Chen"),
        note=overrides.pop("note", "Confirmed with the vendor."),
        amount=overrides.pop("amount", total),
        ledger_path=overrides.pop("ledger_path", tmp_path / "payments.db"),
        log_path=overrides.pop("log_path", tmp_path / "approve.jsonl"),
        now=lambda: WHEN,
        **overrides,
    )


def _clone(result, **invoice_edits):
    invoice = result["invoice"].model_copy(update=invoice_edits)
    cloned = dict(result)
    cloned["invoice"] = invoice
    return cloned


@pytest.mark.parametrize(
    "folder, name, code",
    [
        ("data/invoices", "invoice_1012.txt", "VENDOR_RENAMED"),
        ("data/invoices", "invoice_1004_revised.json", "REVISED_INVOICE"),
        ("data/extra_invoices", "invoice_2112.txt", "FRAUD_CLUSTER"),
        ("data/extra_invoices", "invoice_2114.txt", "NEW_REMITTANCE"),
        ("data/extra_invoices", "invoice_2001.pdf", "PRICE_SPIKE"),
    ],
)
def test_genuine_review_holds_can_be_paid(folder, name, code, tmp_path, capsys):
    result = _run(ROOT / folder / name)
    assert code in [item.code for item in result["findings"]]
    paid = _pay(result, tmp_path)
    assert paid["result"] == "paid"
    assert paid["pipeline_outcome"] == "hold"
    assert code in paid["pipeline_codes"]
    assert result["invoice"].vendor in capsys.readouterr().out


def test_critique_hold_can_be_paid(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    result = dict(result)
    result["findings"] = [Finding(code="CRITIQUE_HOLD", message="Critique raised a concern.", severity="warning")]
    result["outcome"] = "hold"
    result["reason_code"] = "CRITIQUE_HOLD"
    result["critique_concerns"] = ["tax rate is not the usual rate"]
    remember(result)
    paid = _pay(result, tmp_path)
    assert paid["result"] == "paid"
    assert paid["critique_concerns"] == ["tax rate is not the usual rate"]


def test_changed_vendor_is_not_paid(tmp_path, capsys):
    result = _run(ROOT / "data/extra_invoices/invoice_2112.txt")
    cloned = _clone(result, vendor="Evil Payee LLC")
    refused = _pay(cloned, tmp_path)
    assert refused["result"] == "held"
    assert refused["reason"] == "The request does not match the pipeline record."
    assert "Evil Payee LLC" not in capsys.readouterr().out


def test_edited_reject_lines_are_not_paid(tmp_path, capsys):
    result = _run(ROOT / "data/invoices/invoice_1002.txt")
    invoice = result["invoice"]
    line = invoice.line_items[0].model_copy(update={"quantity": Decimal("5"), "amount": Decimal("3750")})
    cloned = _clone(result, line_items=[line], total=Decimal("3750"), subtotal=Decimal("3750"))
    cloned["outcome"] = "hold"
    cloned["findings"] = [Finding(code="VENDOR_RENAMED", message="formerly", severity="warning")]
    refused = _pay(cloned, tmp_path, amount="3750")
    assert refused["result"] == "held"
    assert "Paid 3750.00" not in capsys.readouterr().out


def test_missing_caller_path_does_not_skip_the_file(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1002.txt")
    invoice = result["invoice"]
    line = invoice.line_items[0].model_copy(update={"quantity": Decimal("5"), "amount": Decimal("3750")})
    source = invoice.source.model_copy(update={"path": "missing/not-a-file.txt"})
    cloned = _clone(result, line_items=[line], total=Decimal("3750"), subtotal=Decimal("3750"), source=source)
    cloned["outcome"] = "hold"
    cloned["findings"] = [Finding(code="VENDOR_RENAMED", message="formerly", severity="warning")]
    refused = _pay(cloned, tmp_path, amount="3750")
    assert refused["result"] == "held"


def test_relative_source_is_found_from_another_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    result = _run(Path("data/invoices/invoice_1012.txt"))
    assert not Path(result["invoice"].source.path).is_absolute()
    monkeypatch.chdir(tmp_path)
    paid = _pay(result, tmp_path)
    assert paid["result"] == "paid"
    cloned = _clone(result, vendor="Evil Payee LLC")
    refused = _pay(cloned, tmp_path)
    assert refused["result"] == "held"


def test_missing_recorded_file_is_refused(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    source = result["invoice"].source.model_copy(update={"path": "data/invoices/missing-invoice.txt"})
    saved = _clone(result, source=source)
    remember(saved)
    refused = _pay(saved, tmp_path)
    assert refused["result"] == "held"
    assert refused["reason"] == "The source file is missing."


def test_send_back_cap_is_not_bypassed_by_wording(tmp_path):
    from decision.outcome import SEND_BACK_CAP_MESSAGE
    result = dict(_run(ROOT / "data/invoices/invoice_1012.txt"))
    result["outcome"] = "hold"
    result["reason_code"] = "AUDIT_ESCALATED"
    result["findings"] = [Finding(
        code="AUDIT_ESCALATED", message=SEND_BACK_CAP_MESSAGE, severity="warning", send_back_cap=True,
    )]
    remember(result)
    spaced = dict(result)
    spaced["findings"] = [Finding(
        code="AUDIT_ESCALATED", message=SEND_BACK_CAP_MESSAGE + " ", severity="warning",
    )]
    assert _pay(spaced, tmp_path)["result"] == "held"
    swapped = dict(result)
    swapped["findings"] = [Finding(code="VENDOR_RENAMED", message="formerly", severity="warning")]
    refused = _pay(swapped, tmp_path)
    assert refused["result"] == "held"
    assert refused["reason_code"] == "NOT_PAYABLE"


def test_amount_is_not_rounded(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    refused = _pay(result, tmp_path, amount=result["invoice"].total + Decimal("0.001"))
    assert refused["result"] == "held"
    assert refused["reason_code"] == "AMOUNT_MISMATCH"


@pytest.mark.parametrize("reviewer", ["A", "A\u3164", "\u3164\u3164"])
def test_a_name_needs_two_letters(reviewer, tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    refused = _pay(result, tmp_path, reviewer=reviewer)
    assert refused["reason_code"] == "MISSING_REVIEWER"


def test_demo_pays_one_hold_and_refuses_one_reject(capsys):
    from pipeline.approve_hold import main
    assert main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert "Paid " in out
    records = [json.loads(line) for line in out.splitlines() if '"reviewer"' in line]
    assert [row["decision"] for row in records] == ["paid", "held"]
    assert records[0]["pipeline_outcome"] == "hold"
    assert records[1]["pipeline_outcome"] == "reject"
    assert records[1]["reason_code"] == "NOT_PAYABLE"
