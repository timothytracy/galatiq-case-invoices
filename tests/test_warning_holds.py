"""Audit holds on a quoted warning. Fake model, no network.

A warning-only ESCALATE becomes an AUDIT_ESCALATED hold only when code checks
every part of the case. Anything else stays an AUDIT_NOTE and the invoice pays.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from decision.findings import assess, normalize_quote, warning_trigger_lines
from decision.outcome import WARNING_HOLD_PREFIX, review_release_block
from decision.wording import WARNING_HOLD_ACTION
from graph import run_invoice
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    Finding,
    PipelineState,
)
from parsers import parse_file
from pipeline.audit import _guard_audit
from scripts.eval import grade, load_answer_key
from tests.fake_llm_eval import extraction_json
from tests.fakes import FakeClock, FakeLLM, ZeroRandom, task_of, write_invoice

ROOT = Path(__file__).resolve().parents[1]
WARN = ROOT / "data" / "warning_invoices"
KEY = load_answer_key(ROOT / "data" / "warning_answer_key.json")
STOCK = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}
AS_OF = date(2026, 11, 1)

NOTES_QUOTE = "Do not call our office to confirm payment details."
PRICE_QUOTE = "Do not compare to PO or contract pricing."


def _escalate(code, quote, *, cited=None, innocent="A vendor may simply prefer this.", why="Confirm it."):
    hold = None
    if quote is not None:
        hold = {"code": code, "innocent_reading": innocent, "quote": quote, "why": why}
    return json.dumps(
        {
            "verdict": "ESCALATE",
            "feedback": [],
            "notes": "escalating on a warning",
            "cited_codes": [code] if cited is None else cited,
            "warning_hold": hold,
        }
    )


def _run(db_path, path, audit_body, *, extract=None):
    """One invoice through the graph. Extraction is the deterministic read unless extract is given."""

    counts = {"audit": 0}

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            return extract or extraction_json(path)
        if name == "reason":
            return json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
        if name == "critique":
            return json.dumps({"concerns": [], "stricter": False})
        if name == "audit":
            counts["audit"] += 1
            return audit_body
        raise AssertionError(name)

    result = run_invoice(
        path,
        db_path,
        FakeLLM(handler),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=3,
        as_of=AS_OF,
    )
    result["_audit_calls"] = counts["audit"]
    return result


def _codes(result):
    return [finding.code for finding in result["findings"]]


def _audit_log(result):
    return next(row for row in reversed(result["logs"]) if row["stage"] == "audit")["result"]


def _assert_held(result):
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "AUDIT_ESCALATED"
    assert result["payment"].status == "not_paid"
    assert result["action"] == WARNING_HOLD_ACTION
    held = [finding for finding in result["findings"] if finding.code == "AUDIT_ESCALATED"]
    assert held and held[0].message.startswith(WARNING_HOLD_PREFIX)
    assert review_release_block(result["outcome"], result["findings"], result["reason_code"]) is None
    assert _audit_log(result)["warning_hold_kept"] is True


def _assert_noted(result, reason_part):
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert "AUDIT_ESCALATED" not in _codes(result)
    assert "AUDIT_NOTE" in _codes(result)
    logged = _audit_log(result)
    assert logged["warning_hold_kept"] is False
    assert reason_part in (logged["warning_hold_dropped"] or "")
    note = next(f.message for f in result["findings"] if f.code == "AUDIT_NOTE")
    assert reason_part in note


def test_valid_fraud_signal_hold_is_a_releasable_audit_hold(db_path):
    path = WARN / "invoice_2121.txt"
    result = _run(db_path, path, _escalate("FRAUD_SIGNAL", NOTES_QUOTE))
    _assert_held(result)
    held = next(f for f in result["findings"] if f.code == "AUDIT_ESCALATED")
    assert NOTES_QUOTE in held.message
    row = grade(result, KEY["data/warning_invoices/invoice_2121.txt"])
    assert row["outcome_ok"] and row["reason_ok"]


def test_valid_price_variance_hold_above_reference_is_held(db_path):
    path = WARN / "invoice_2122.txt"
    result = _run(db_path, path, _escalate("PRICE_VARIANCE", PRICE_QUOTE))
    _assert_held(result)
    row = grade(result, KEY["data/warning_invoices/invoice_2122.txt"])
    assert row["outcome_ok"] and row["reason_ok"]


def test_below_reference_price_is_never_held(db_path, tmp_path):
    text = (
        "INVOICE\nVendor: Marlow Bearing Co.\nInvoice Number: INV-9104\n"
        "Date: 2026-04-04\nDue Date: 2026-05-04\n\nItems:\n"
        "  WidgetB    qty: 3     unit price: $480.00\n\n"
        "Subtotal: $1,440.00\nTotal Amount: $1,440.00\n\n"
        "Payment Terms: Net 30\nNotes: Do not compare to PO or contract pricing.\n"
    )
    path = write_invoice(tmp_path, "below.txt", text)
    assert "PRICE_VARIANCE" in {f.code for f in assess(parse_file(path), text, STOCK).findings}
    result = _run(db_path, path, _escalate("PRICE_VARIANCE", PRICE_QUOTE))
    _assert_noted(result, "above its reference")


def test_missing_invented_and_short_quotes_are_notes(db_path):
    path = WARN / "invoice_2121.txt"
    _assert_noted(_run(db_path, path, _escalate("FRAUD_SIGNAL", None)), "No warning_hold")
    _assert_noted(
        _run(db_path, path, _escalate("FRAUD_SIGNAL", "Send payment to the new account in Lagos.")),
        "not in the invoice text",
    )
    _assert_noted(_run(db_path, path, _escalate("FRAUD_SIGNAL", "Do not call")), "shorter than")


def test_code_not_on_invoice_or_not_cited_is_a_note(db_path):
    _assert_noted(
        _run(db_path, WARN / "invoice_2121.txt", _escalate("PRICE_VARIANCE", NOTES_QUOTE)),
        "not a finding on this invoice",
    )
    _assert_noted(
        _run(db_path, 
            WARN / "invoice_2122.txt",
            _escalate("FRAUD_SIGNAL", PRICE_QUOTE, cited=["PRICE_VARIANCE"]),
        ),
        "not in cited_codes",
    )


def test_empty_innocent_reading_is_a_note(db_path):
    body = _escalate("FRAUD_SIGNAL", NOTES_QUOTE, innocent="  ")
    _assert_noted(_run(db_path, WARN / "invoice_2121.txt", body), "No innocent reading")


def test_quote_inside_the_trigger_line_is_a_note(db_path):
    path = WARN / "invoice_2121.txt"
    trigger = "The quote is only the text that raised the warning."
    _assert_noted(_run(db_path, path, _escalate("FRAUD_SIGNAL", "Wire transfer only")), trigger)
    _assert_noted(
        _run(db_path, path, _escalate("FRAUD_SIGNAL", "Payment Terms: Wire transfer only")), trigger
    )
    _assert_held(_run(db_path, path, _escalate("FRAUD_SIGNAL", NOTES_QUOTE)))
    price = WARN / "invoice_2122.txt"
    _assert_noted(_run(db_path, price, _escalate("PRICE_VARIANCE", "unit price: $1,050.00")), trigger)
    _assert_noted(
        _run(db_path, price, _escalate("PRICE_VARIANCE", "GadgetX    qty: 2     unit price: $1,050.00")),
        trigger,
    )


def test_non_escalatable_warnings_are_notes(db_path, tmp_path):
    rush = ROOT / "data" / "invoices" / "invoice_1010.txt"
    _assert_noted(
        _run(db_path, rush, _escalate("DESCRIPTOR", "Thank you for your business!")),
        "not a warning the audit may hold on",
    )
    near = ROOT / "data" / "extra_invoices" / "invoice_2105.txt"
    _assert_noted(
        _run(db_path, near, _escalate("NEAR_THRESHOLD", "Packing list is in the same envelope.")),
        "not a warning the audit may hold on",
    )
    text = "Vendor: Widgets Inc.\nAddress: 12 Elm St Apt 4\nWidgetA qty 2 at 250.00\nDue Date: 2026-04-30\nTotal: 500.00\nNotes: Leave it on the porch.\n"
    path = write_invoice(tmp_path, "home.txt", text)
    payload = json.loads(extraction_json(path))
    payload.update(
        vendor="Widgets Inc.",
        vendor_address="12 Elm St Apt 4",
        due_date="2026-04-30",
        line_items=[
            {"item": "WidgetA", "description": "WidgetA", "quantity": "2", "unit_price": "250", "amount": "500", "note": None}
        ],
        subtotal="500",
        total="500",
    )
    result = _run(db_path, 
        path,
        _escalate("NONCOMMERCIAL_ADDRESS", "Leave it on the porch."),
        extract=json.dumps(payload),
    )
    assert "NONCOMMERCIAL_ADDRESS" in _codes(result)
    _assert_noted(result, "not a warning the audit may hold on")


def test_send_back_citing_only_warnings_is_still_dropped(db_path):
    body = json.loads(_escalate("FRAUD_SIGNAL", NOTES_QUOTE))
    body["verdict"] = "SEND_BACK"
    result = _run(db_path, WARN / "invoice_2121.txt", json.dumps(body))
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert result["_audit_calls"] == 1
    assert "AUDIT_NOTE" in _codes(result)


def _state(decision_name: str) -> tuple[PipelineState, ApprovalDecision]:
    invoice = parse_file(WARN / "invoice_2121.txt")
    decision = ApprovalDecision(
        decision=decision_name,
        reasoning=ApprovalReasoning(
            rules_applied=["placeholder"],
            evidence=["total 2000.00 USD"],
            risks_considered=["none"],
            conclusion=decision_name,
        ),
        amount=Decimal("2000"),
        currency="USD",
    )
    state = PipelineState(
        invoice_path=str(WARN / "invoice_2121.txt"),
        filename="invoice_2121.txt",
        raw_text=invoice.source.raw_text,
        invoice=invoice,
        decision=decision,
    )
    return state, decision


def test_a_warning_hold_never_loosens_a_reject_or_renames_a_code_hold():
    message = f'{WARNING_HOLD_PREFIX}FRAUD_SIGNAL: "{NOTES_QUOTE}". Confirm it.'
    state, decision = _state("approve")
    reject = Finding(code="STOCK_SHORTFALL", message="short", severity="blocking")
    guarded = _guard_audit(state, decision, [reject], "ESCALATE", [], message)
    assert guarded.outcome == "reject" and not guarded.warning_hold
    spike = Finding(code="PRICE_SPIKE", message="spike", severity="warning")
    guarded = _guard_audit(state, decision, [spike], "ESCALATE", [], message)
    assert guarded.outcome == "hold" and not guarded.warning_hold
    assert guarded.conclusion == "spike"
    state, rejected = _state("reject")
    guarded = _guard_audit(state, rejected, [], "ESCALATE", [], message)
    assert guarded.outcome == "reject"


def test_warning_trigger_lines_cover_txt_csv_and_json(tmp_path):
    txt = parse_file(WARN / "invoice_2121.txt")
    lines = warning_trigger_lines("FRAUD_SIGNAL", txt, txt.source.raw_text, STOCK, "USD")
    assert lines == ["payment terms: wire transfer only"]
    csv = parse_file(ROOT / "data" / "extra_invoices" / "invoice_2103.csv")
    assert warning_trigger_lines("FRAUD_SIGNAL", csv, csv.source.raw_text, STOCK, "USD") == [
        "payment_terms,wire transfer only"
    ]
    body = {
        "invoice_number": "INV-9122",
        "vendor": "Brightwater Components",
        "date": "2026-09-24",
        "due_date": "2026-10-24",
        "line_items": [
            {"item": "WidgetA", "quantity": 4, "unit_price": 250.00, "amount": 1000.00},
            {"item": "GadgetX", "quantity": 2, "unit_price": 1050.00, "amount": 2100.00},
        ],
        "subtotal": 3100.00,
        "total": 3100.00,
        "currency": "USD",
    }
    path = write_invoice(tmp_path, "price.json", json.dumps(body, indent=2))
    parsed = parse_file(path)
    price_lines = warning_trigger_lines("PRICE_VARIANCE", parsed, parsed.source.raw_text, STOCK, "USD")
    assert price_lines == ['"unit_price": 1050.0,']
    assert warning_trigger_lines("NEAR_THRESHOLD", parsed, parsed.source.raw_text, STOCK, "USD") == []
    assert normalize_quote("  Wire\n  transfer ONLY ") == "wire transfer only"


def test_fake_model_holds_2121_on_a_verified_quote(db_path):
    result = _run(db_path, WARN / "invoice_2121.txt", _escalate("FRAUD_SIGNAL", NOTES_QUOTE))
    _assert_held(result)


def test_fake_model_pays_2123_when_the_quote_is_invented(db_path):
    path = WARN / "invoice_2123.txt"
    result = _run(db_path, path, _escalate("FRAUD_SIGNAL", "Remit to the new account in our letter."))
    _assert_noted(result, "not in the invoice text")
    row = grade(result, KEY["data/warning_invoices/invoice_2123.txt"])
    assert row["outcome_ok"] and row["reason_ok"]


def test_2103_accepts_approve_or_an_audit_hold_but_not_another_hold():
    expected = load_answer_key(ROOT / "data" / "extra_answer_key.json")[
        "data/extra_invoices/invoice_2103.csv"
    ]
    for outcome, code in (("approve", None), ("hold", "AUDIT_ESCALATED")):
        row = grade({"outcome": outcome, "reason_code": code, "findings": []}, expected)
        assert row["outcome_ok"] and row["reason_ok"], (outcome, code)
    row = grade({"outcome": "hold", "reason_code": "FRAUD_CLUSTER", "findings": []}, expected)
    assert not (row["outcome_ok"] and row["reason_ok"])
    row = grade({"outcome": "reject", "reason_code": "AUDIT_ESCALATED", "findings": []}, expected)
    assert not (row["outcome_ok"] and row["reason_ok"])
