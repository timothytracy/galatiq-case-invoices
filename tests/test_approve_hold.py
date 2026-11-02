"""Reviewer release of a held invoice. Offline. No model call."""

import importlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from decision.outcome import PAYABLE_HOLD_CODES, REJECT_CODES, SEND_BACK_CAP_MESSAGE
from ledger import paid_rows, record_payment
from models import ExtractedInvoice, Finding, LineItem, PipelineState, SourceInfo
from pipeline.approve_hold import approve_and_pay, main

WHEN = datetime(2026, 11, 2, 4, 20, tzinfo=timezone.utc)
CLOSED_HOLDS = (
    "SYSTEM_ERROR",
    "BAD_REASONING",
    "ITEM_NOT_IN_SOURCE",
    "NON_USD",
    "CREDIT_NOTE",
    "DUPLICATE_INVOICE",
    "SPLIT_INVOICE",
)


def _invoice(**overrides) -> ExtractedInvoice:
    total = Decimal("500.00")
    fields = {
        "vendor": "Northwind Supply",
        "invoice_number": "INV-1",
        "invoice_date": "2026-04-15",
        "due_date": "2026-05-15",
        "currency": "USD",
        "total": total,
        "subtotal": total,
        "line_items": [
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("2"),
                unit_price=Decimal("250"),
                amount=total,
            )
        ],
        "source": SourceInfo(path="hold.txt", filename="hold.txt", format="txt", raw_text="WidgetA"),
    }
    fields.update(overrides)
    return ExtractedInvoice(**fields)


def _held(findings, *, outcome="hold", invoice=None, reason_code=None, raw="Invoice INV-1\nWidgetA\n"):
    invoice = invoice or _invoice()
    if reason_code is None and findings:
        reason_code = findings[0].code
    return PipelineState(
        invoice_path="hold.txt",
        filename="hold.txt",
        raw_text=raw,
        invoice=invoice,
        findings=list(findings),
        outcome=outcome,
        reason_code=reason_code,
    )


def _finding(code: str, severity: str | None = None) -> Finding:
    if severity is None:
        severity = "blocking" if code in REJECT_CODES else "warning"
    return Finding(code=code, message=f"{code} on this invoice.", severity=severity)


def _amount_of(state):
    invoice = state.invoice if isinstance(state, PipelineState) else state.get("invoice")
    if invoice is None:
        return None
    if isinstance(invoice, dict):
        return invoice.get("total")
    return invoice.total


def _pay(state, tmp_path, **overrides):
    amount = overrides.pop("amount", None)
    if amount is None:
        amount = _amount_of(state)
    return approve_and_pay(
        state,
        reviewer=overrides.pop("reviewer", "Avery Chen"),
        note=overrides.pop("note", "Confirmed and ready to pay."),
        amount=amount,
        ledger_path=overrides.pop("ledger_path", tmp_path / "payments.db"),
        log_path=overrides.pop("log_path", tmp_path / "approve.jsonl"),
        now=overrides.pop("now", lambda: WHEN),
        **overrides,
    )


def _codes_paid(tmp_path) -> list[str]:
    return [row.source_file for row in paid_rows(tmp_path / "payments.db")]


@pytest.mark.parametrize("code", PAYABLE_HOLD_CODES)
def test_each_review_hold_can_be_paid(code, tmp_path, monkeypatch):
    calls = []
    payment_module = importlib.import_module("pipeline.payment")
    monkeypatch.setattr(
        payment_module,
        "mock_payment",
        lambda vendor, amount: calls.append((vendor, f"{amount:.2f}")) or {"status": "success"},
    )
    state = _held([_finding(code)])
    result = _pay(state, tmp_path)
    assert result["result"] == "paid"
    assert result["payment_status"] == "paid"
    assert result["pipeline_outcome"] == "hold"
    assert state.outcome == "hold"
    assert calls == [("Northwind Supply", "500.00")]
    assert _codes_paid(tmp_path) == ["hold.txt"]


def test_payment_uses_the_invoice_authorization(tmp_path, monkeypatch):
    seen = {}
    real = importlib.import_module("pipeline.approve_hold").payment

    def spy(state, ledger_path=None):
        seen["vendor"] = state.authorization.vendor
        seen["amount"] = state.authorization.amount
        seen["currency"] = state.authorization.currency
        seen["invoice_vendor"] = state.invoice.vendor
        seen["invoice_amount"] = state.invoice.total
        seen["invoice_currency"] = state.invoice.currency
        return real(state, ledger_path)

    monkeypatch.setattr("pipeline.approve_hold.payment", spy)
    result = _pay(_held([_finding("VENDOR_RENAMED")]), tmp_path)
    assert result["result"] == "paid"
    assert seen["vendor"] == seen["invoice_vendor"] == "Northwind Supply"
    assert seen["amount"] == seen["invoice_amount"] == Decimal("500.00")
    assert seen["currency"] == seen["invoice_currency"] == "USD"


@pytest.mark.parametrize("code", REJECT_CODES)
def test_a_reject_is_never_paid(code, tmp_path):
    result = _pay(_held([_finding(code)], outcome="reject"), tmp_path)
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert result["reason"] == "A reject is never paid."
    assert result["pipeline_outcome"] == "reject"
    assert _codes_paid(tmp_path) == []


@pytest.mark.parametrize("code", list(REJECT_CODES) + ["ITEM_NOT_IN_SOURCE"])
def test_a_hold_with_a_hard_failure_is_not_paid(code, tmp_path):
    findings = [_finding("VENDOR_RENAMED"), _finding(code)]
    result = _pay(_held(findings, reason_code="VENDOR_RENAMED"), tmp_path)
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert code in result["reason"]
    assert _codes_paid(tmp_path) == []


@pytest.mark.parametrize("code", CLOSED_HOLDS)
def test_a_closed_hold_is_not_paid(code, tmp_path):
    result = _pay(_held([_finding(code)]), tmp_path)
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert code in result["reason"]
    assert _codes_paid(tmp_path) == []


def test_two_fraud_signals_can_be_paid_and_one_cannot(tmp_path):
    one = _pay(_held([_finding("FRAUD_SIGNAL")], reason_code=None), tmp_path)
    assert one["result"] == "held"
    assert one["reason_code"] == "NOT_PAYABLE"
    two = _pay(
        _held([_finding("FRAUD_SIGNAL"), _finding("FRAUD_SIGNAL")], reason_code="FRAUD_CLUSTER"),
        tmp_path,
        ledger_path=tmp_path / "fraud.db",
        log_path=tmp_path / "fraud.jsonl",
    )
    assert two["result"] == "paid"
    assert two["pipeline_outcome"] == "hold"


def test_a_warning_beside_a_review_hold_can_be_paid(tmp_path):
    findings = [_finding("VENDOR_RENAMED"), _finding("PRICE_VARIANCE")]
    result = _pay(_held(findings, reason_code="VENDOR_RENAMED"), tmp_path)
    assert result["result"] == "paid"
    assert [item["code"] for item in result["pipeline_findings"]] == [
        "VENDOR_RENAMED",
        "PRICE_VARIANCE",
    ]


def test_non_usd_stays_held(tmp_path):
    invoice = _invoice(currency="EUR")
    state = _held([_finding("VENDOR_RENAMED")], invoice=invoice, reason_code="VENDOR_RENAMED")
    result = _pay(state, tmp_path)
    assert result["result"] == "held"
    assert result["reason_code"] == "NON_USD"
    assert result["reason"] == "Non-USD currency (EUR)."
    assert _codes_paid(tmp_path) == []


def test_amount_mismatch_stays_held(tmp_path):
    result = _pay(_held([_finding("PRICE_SPIKE")]), tmp_path, amount="100.00")
    assert result["result"] == "held"
    assert result["reason_code"] == "AMOUNT_MISMATCH"
    assert result["reason"] == "Amount 100.00 does not equal the extracted total 500.00."
    assert _codes_paid(tmp_path) == []


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"reviewer": ""}, "MISSING_REVIEWER"),
        ({"reviewer": "   "}, "MISSING_REVIEWER"),
        ({"note": ""}, "MISSING_NOTE"),
        ({"note": "   "}, "MISSING_NOTE"),
    ],
)
def test_missing_name_or_note_stays_held(kwargs, code, tmp_path):
    result = _pay(_held([_finding("REVISED_INVOICE")]), tmp_path, **kwargs)
    assert result["result"] == "held"
    assert result["reason_code"] == code
    assert _codes_paid(tmp_path) == []


def test_a_prior_payment_stays_held(tmp_path):
    ledger = tmp_path / "payments.db"
    assert record_payment(
        ledger,
        vendor="Northwind Supply",
        invoice_number="INV-1",
        revision=None,
        total=Decimal("500.00"),
        currency="USD",
        invoice_date="2026-04-15",
        source_file="earlier.txt",
        raw_text="already paid",
    ) is True
    result = _pay(_held([_finding("REVISED_INVOICE")]), tmp_path)
    assert result["result"] == "held"
    assert result["reason_code"] == "DUPLICATE_INVOICE"
    assert "already paid" in result["reason"]
    assert [row.source_file for row in paid_rows(ledger)] == ["earlier.txt"]


def test_a_split_match_stays_held(tmp_path):
    ledger = tmp_path / "payments.db"
    assert record_payment(
        ledger,
        vendor="Northwind Supply",
        invoice_number="INV-40",
        revision=None,
        total=Decimal("6000.00"),
        currency="USD",
        invoice_date="2026-04-01",
        source_file="earlier.txt",
        raw_text="first half",
    ) is True
    invoice = _invoice(
        invoice_number="INV-41",
        total=Decimal("5000.00"),
        subtotal=Decimal("5000.00"),
        line_items=[
            LineItem(item="WidgetA", description="WidgetA", quantity=Decimal("10"), unit_price=Decimal("250"), amount=Decimal("2500")),
            LineItem(item="WidgetB", description="WidgetB", quantity=Decimal("5"), unit_price=Decimal("500"), amount=Decimal("2500")),
        ],
    )
    state = _held(
        [_finding("VENDOR_RENAMED")],
        invoice=invoice,
        reason_code="VENDOR_RENAMED",
        raw="WidgetA\nWidgetB\n",
    )
    result = _pay(state, tmp_path, amount="5000.00")
    assert result["result"] == "held"
    assert result["reason_code"] == "SPLIT_INVOICE"
    assert "over the" in result["reason"]
    assert [row.source_file for row in paid_rows(ledger)] == ["earlier.txt"]


def test_a_second_approval_does_not_pay(tmp_path, monkeypatch):
    calls = []
    payment_module = importlib.import_module("pipeline.payment")
    monkeypatch.setattr(
        payment_module,
        "mock_payment",
        lambda vendor, amount: calls.append(vendor) or {"status": "success"},
    )
    state = _held([_finding("NEW_REMITTANCE")])
    first = _pay(state, tmp_path)
    second = _pay(state, tmp_path, note="Approving it again.")
    assert first["result"] == "paid"
    assert second["result"] == "held"
    assert second["reason_code"] == "DUPLICATE_INVOICE"
    assert calls == ["Northwind Supply"]
    assert len(paid_rows(tmp_path / "payments.db")) == 1


def test_a_locked_ledger_is_a_system_error(tmp_path, monkeypatch):
    ledger = tmp_path / "payments.db"
    holder = sqlite3.connect(ledger, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    calls = []
    payment_module = importlib.import_module("pipeline.payment")
    monkeypatch.setattr(payment_module, "mock_payment", lambda vendor, amount: calls.append(vendor))

    def fast(path):
        return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=0.2)

    monkeypatch.setattr("ledger.payments._connect_readonly", fast)
    try:
        result = _pay(_held([_finding("PRICE_SPIKE")]), tmp_path, ledger_path=ledger)
    finally:
        holder.close()
    assert calls == []
    assert result["result"] == "held"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert result["reason"].startswith("payments ledger unreadable:")
    assert "database is locked" in result["reason"]


def test_a_corrupt_ledger_is_a_system_error(tmp_path, monkeypatch):
    ledger = tmp_path / "payments.db"
    ledger.write_bytes(b"this is not a sqlite database")
    calls = []
    payment_module = importlib.import_module("pipeline.payment")
    monkeypatch.setattr(payment_module, "mock_payment", lambda vendor, amount: calls.append(vendor))
    result = _pay(_held([_finding("AUDIT_ESCALATED")]), tmp_path)
    assert calls == []
    assert result["result"] == "held"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert result["reason"].startswith("payments ledger unreadable:")


def test_the_log_is_append_only_and_keeps_the_pipeline_outcome(tmp_path):
    log = tmp_path / "approve.jsonl"
    log.write_text('{"kept": true}\n', encoding="utf-8")
    state = _held([_finding("VENDOR_RENAMED")])
    payload = state.model_dump(mode="json")
    seen = {}
    real = importlib.import_module("pipeline.approve_hold").payment

    def spy(state, ledger_path=None):
        attempt = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        seen["attempt"] = attempt["decision"]
        return real(state, ledger_path)

    patch = pytest.MonkeyPatch()
    patch.setattr("pipeline.approve_hold.payment", spy)
    try:
        result = _pay(payload, tmp_path, log_path=log)
    finally:
        patch.undo()
    assert payload["outcome"] == "hold"
    assert seen["attempt"] == "attempt"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"kept": True}
    record = json.loads(lines[2])
    assert record == result["record"]
    assert record["reviewer"] == "Avery Chen"
    assert record["note"] == "Confirmed and ready to pay."
    assert record["time"] == "2026-11-02T04:20:00Z"
    assert record["invoice"] == "hold.txt"
    assert record["invoice_number"] == "INV-1"
    assert record["vendor"] == "Northwind Supply"
    assert record["amount"] == "500.00"
    assert record["currency"] == "USD"
    assert record["pipeline_outcome"] == "hold"
    assert record["pipeline_codes"] == ["VENDOR_RENAMED"]
    assert record["decision"] == "paid"
    assert record["reason_code"] is None
    assert record["reason"] == "Reviewer approved this hold for payment."
    again = _pay(payload, tmp_path, log_path=log, note="Second look.")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert json.loads(lines[0]) == {"kept": True}
    assert json.loads(lines[1])["decision"] == "attempt"
    assert json.loads(lines[2])["decision"] == "paid"
    assert again["result"] == "held"
    assert json.loads(lines[3])["reason_code"] == "DUPLICATE_INVOICE"
    assert json.loads(lines[3])["pipeline_outcome"] == "hold"
    assert json.loads(lines[3])["decision"] == "held"


def test_demo_pays_one_hold_and_refuses_one_reject(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--demo"]) == 0
    assert not (tmp_path / "payments.db").exists()
    assert list(tmp_path.glob("payments-*.db")) == []
    assert not (tmp_path / "output" / "reviews" / "approve_pay.jsonl").exists()
    out = capsys.readouterr().out
    assert out.count("Paid 500.00 to Northwind Supply") == 1
    records = []
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        item = json.loads(line)
        if "reviewer" in item:
            records.append(item)
    assert [row["decision"] for row in records] == ["paid", "held"]
    assert records[0]["invoice"] == "demo_hold.txt"
    assert records[0]["pipeline_outcome"] == "hold"
    assert records[1]["invoice"] == "demo_reject.txt"
    assert records[1]["pipeline_outcome"] == "reject"
    assert records[1]["reason_code"] == "NOT_PAYABLE"


def test_cli_refuses_a_result_file(tmp_path, capsys):
    state = _held([_finding("UNKNOWN_ITEM")], outcome="reject")
    path = tmp_path / "result.json"
    path.write_text(json.dumps(state.model_dump(mode="json")), encoding="utf-8")
    code = main(
        [
            "--result",
            str(path),
            "--reviewer",
            "Avery Chen",
            "--note",
            "Trying to pay a reject.",
            "--amount",
            "500.00",
            "--ledger",
            str(tmp_path / "payments.db"),
            "--log",
            str(tmp_path / "approve.jsonl"),
        ]
    )
    assert code == 1
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["decision"] == "held"
    assert record["reason_code"] == "NOT_PAYABLE"
    assert record["pipeline_outcome"] == "reject"


ROOT = Path(__file__).resolve().parents[1]


def _tampered(invoice, raw, filename):
    """A bad invoice whose stored outcome and findings say it is a review hold."""

    return PipelineState(
        invoice_path=filename,
        filename=filename,
        raw_text=raw,
        invoice=invoice,
        findings=[_finding("VENDOR_RENAMED")],
        outcome="hold",
        reason_code="VENDOR_RENAMED",
    )


def test_a_tampered_stock_shortfall_is_not_paid(tmp_path):
    path = ROOT / "data" / "invoices" / "invoice_1002.txt"
    raw = path.read_text(encoding="utf-8")
    invoice = _invoice(
        vendor="Gadgets Co.",
        invoice_number="1002",
        invoice_date="2026-01-30",
        due_date="2026-01-30",
        total=Decimal("15000.00"),
        subtotal=Decimal("15000.00"),
        line_items=[
            LineItem(
                item="GadgetX",
                description="GadgetX",
                quantity=Decimal("20"),
                unit_price=Decimal("750"),
                amount=Decimal("15000"),
            )
        ],
        source=SourceInfo(path=str(path), filename=path.name, format="txt", raw_text=raw),
    )
    result = _pay(_tampered(invoice, raw, path.name), tmp_path, amount="15000.00")
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert "STOCK_SHORTFALL" in result["reason"]
    assert _codes_paid(tmp_path) == []


def test_a_tampered_embedded_instruction_is_not_paid(tmp_path):
    from parsers.pdf_invoice import pdf_text

    path = ROOT / "data" / "extra_invoices" / "invoice_2107.pdf"
    raw = pdf_text(path)
    invoice = _invoice(
        vendor="Glassford Paper Co.",
        invoice_number="INV-2107",
        invoice_date="2026-04-03",
        due_date="2026-05-03",
        total=Decimal("1250.00"),
        subtotal=Decimal("1250.00"),
        embedded_instructions=True,
        line_items=[
            LineItem(item="WidgetA", description="WidgetA", quantity=Decimal("3"), unit_price=Decimal("250"), amount=Decimal("750")),
            LineItem(item="WidgetB", description="WidgetB", quantity=Decimal("1"), unit_price=Decimal("500"), amount=Decimal("500")),
        ],
        source=SourceInfo(path=str(path), filename=path.name, format="pdf", raw_text=raw),
    )
    result = _pay(_tampered(invoice, raw, path.name), tmp_path, amount="1250.00")
    assert result["result"] == "held"
    assert "EMBEDDED_INSTRUCTIONS" in result["reason"]
    assert _codes_paid(tmp_path) == []


def test_a_tampered_negative_total_is_not_paid(tmp_path):
    path = ROOT / "data" / "extra_invoices" / "invoice_2109.json"
    raw = path.read_text(encoding="utf-8")
    invoice = _invoice(
        vendor="Dockside Alloy",
        invoice_number="INV-2109",
        invoice_date="2026-04-07",
        due_date="2026-05-07",
        total=Decimal("-750.00"),
        subtotal=Decimal("-750.00"),
        line_items=[
            LineItem(item="WidgetA", description="WidgetA", quantity=Decimal("3"), unit_price=Decimal("-250"), amount=Decimal("-750"))
        ],
        source=SourceInfo(path=str(path), filename=path.name, format="json", raw_text=raw),
    )
    result = _pay(_tampered(invoice, raw, path.name), tmp_path, amount="-750.00")
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert _codes_paid(tmp_path) == []


@pytest.mark.parametrize("total", ["9999999.00", "-3000.00", "0.00"])
def test_a_tampered_amount_is_not_paid(total, tmp_path):
    amount = Decimal(total)
    invoice = _invoice(
        vendor="New Vendor Co",
        invoice_number="INV-HUGE",
        total=amount,
        subtotal=amount,
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("40000"),
                unit_price=Decimal("250"),
                amount=Decimal("10000000"),
            )
        ],
    )
    result = _pay(
        _tampered(invoice, "WidgetA\n", "huge.txt"),
        tmp_path,
        amount=total,
    )
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert _codes_paid(tmp_path) == []


def test_source_text_must_match_the_file(tmp_path):
    path = tmp_path / "bill.txt"
    path.write_text("WidgetA\nTotal 500.00\n", encoding="utf-8")
    invoice = _invoice(source=SourceInfo(path=str(path), filename="bill.txt", format="txt", raw_text="nope"))
    state = _held([_finding("VENDOR_RENAMED")], invoice=invoice, raw="tampered text")
    result = _pay(state, tmp_path)
    assert result["result"] == "held"
    assert result["reason"] == "The invoice text does not match the source file."
    assert _codes_paid(tmp_path) == []


def test_an_unknown_outcome_is_refused_and_logged(tmp_path):
    payload = _held([_finding("VENDOR_RENAMED")]).model_dump(mode="json")
    payload["outcome"] = "HOLD"
    result = _pay(payload, tmp_path)
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert result["reason"] == "Only a held invoice can be approved for payment."
    assert json.loads((tmp_path / "approve.jsonl").read_text().splitlines()[-1])["decision"] == "held"


@pytest.mark.parametrize("reviewer", ["A", "A\u200b", "\u200b\u200b"])
def test_a_name_needs_two_letters(reviewer, tmp_path):
    result = _pay(_held([_finding("VENDOR_RENAMED")]), tmp_path, reviewer=reviewer)
    assert result["result"] == "held"
    assert result["reason_code"] == "MISSING_REVIEWER"
    assert _codes_paid(tmp_path) == []


def test_amount_must_match_to_the_cent(tmp_path):
    result = _pay(_held([_finding("PRICE_SPIKE")]), tmp_path, amount="500.01")
    assert result["result"] == "held"
    assert result["reason_code"] == "AMOUNT_MISMATCH"
    assert "500.01" in result["reason"]
    assert _codes_paid(tmp_path) == []


def test_critique_concerns_are_in_the_result_and_the_log(tmp_path):
    state = _held([_finding("CRITIQUE_HOLD")]).model_copy(
        update={"critique_concerns": ["tax rate is not the usual rate"]}
    )
    result = _pay(state, tmp_path)
    assert result["result"] == "paid"
    assert result["critique_concerns"] == ["tax rate is not the usual rate"]
    assert result["record"]["critique_concerns"] == ["tax rate is not the usual rate"]


def test_a_send_back_cap_is_not_payable(tmp_path):
    finding = Finding(code="AUDIT_ESCALATED", message=SEND_BACK_CAP_MESSAGE, severity="warning")
    result = _pay(_held([finding], reason_code="AUDIT_ESCALATED"), tmp_path)
    assert result["result"] == "held"
    assert result["reason_code"] == "NOT_PAYABLE"
    assert "AUDIT_ESCALATED" in result["reason"]
    assert _codes_paid(tmp_path) == []
