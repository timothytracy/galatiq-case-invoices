"""The guard drops three claims. Everything else stays."""

import json
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from db import create_database
from graph import run_invoice
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    ExtractedInvoice,
    FactConcern,
    LineItem,
    PipelineState,
    SourceInfo,
)
from pipeline.audit import audit
from pipeline.critique import critique
from pipeline.fact_guard import apply_concern_guard, partition_concerns
from tests.fakes import FakeClock, FakeLLM, ZeroRandom, task_of

AS_OF = date(2026, 10, 31)
STOCK = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5}


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


# Covered invoice: WidgetB 8 of 10, totals reconcile, dates on or before AS_OF.
DROP = [
    ("stock", "WidgetB", "", "WidgetB requested 8 vs available 10", "critique", "stock_sufficient"),
    ("totals_mismatch", "", "total", "totals do not reconcile", "critique", "totals_reconcile"),
    ("future_date", "", "due_date", "2026-05-16 is in the future", "audit", "not_after_today"),
    ("future_date", "", "invoice_date", "2026-04-16 is in the future", "critique", "not_after_today"),
]

# Shapes that must survive. "short" is GadgetX 8 of 5. "broken" totals do not reconcile.
KEEP = [
    ("short", "stock", "GadgetX", "", "GadgetX requested 8 vs available 5", "critique"),
    ("covered", "totals", "", "tax_rate", "tax rate is not the usual rate", "critique"),
    ("broken", "totals_mismatch", "", "total", "printed total does not match", "critique"),
    ("covered", "date", "", "due_date", "date is in the past", "audit"),
    ("covered", "date", "", "", "due date is before the invoice date", "audit"),
    ("covered", "date", "", "invoice_date", "invoice date is over 5 years old", "audit"),
    ("covered", "future_date", "", "ship_date", "ship date is in the future", "audit"),
    ("covered", "stock", "WidgetB", "", "raw text shows 18 but extraction says 8", "audit"),
    ("covered", "totals_mismatch", "", "total", "raw total does not match the extraction", "audit"),
    ("covered", "other", "", "", "Possible duplicate of an invoice already paid", "critique"),
]


@pytest.mark.parametrize("category,item,field,text,scope,rule", DROP)
def test_guard_drops(category, item, field, text, scope, rule):
    claim = FactConcern(category=category, item=item, field=field, text=text)
    kept, dropped = partition_concerns([claim], _covered(), STOCK, AS_OF, scope)
    assert kept == []
    assert dropped[0]["rule"] == rule
    assert "contradict" not in dropped[0]["detail"].lower()


@pytest.mark.parametrize("which,category,item,field,text,scope", KEEP)
def test_guard_keeps(which, category, item, field, text, scope):
    invoice = {"short": _short(), "broken": _broken(), "covered": _covered()}[which]
    claim = FactConcern(category=category, item=item, field=field, text=text)
    kept, dropped = partition_concerns([claim], invoice, STOCK, AS_OF, scope)
    assert kept == [claim]
    assert dropped == []


def test_plain_text_is_not_a_stock_claim():
    prose = FactConcern.model_validate("WidgetB requested 8 vs available 10")
    kept, dropped = partition_concerns([prose], _covered(), STOCK, AS_OF, "critique")
    assert prose.category == "other"
    assert kept == [prose]
    assert dropped == []


def test_future_date_kept_when_the_date_is_after_today():
    claim = FactConcern(category="future_date", field="due_date", text="due date is in the future")
    kept, dropped = partition_concerns([claim], _covered(), STOCK, date(2026, 4, 1), "audit")
    assert kept == [claim]
    assert dropped == []


def test_pass_only_when_no_feedback_remains():
    dropped = [{"rule": "not_after_today"}]
    note = FactConcern(text="invoice_number reads I-1 in raw text")
    verdict, notes = apply_concern_guard("SEND_BACK", [], dropped, [])
    assert verdict == "PASS" and notes == []
    verdict, notes = apply_concern_guard("SEND_BACK", [note], dropped, [])
    assert verdict == "SEND_BACK"
    assert notes == ["invoice_number reads I-1 in raw text"]
    verdict, notes = apply_concern_guard("ESCALATE", [], dropped, ["FRAUD_CLUSTER"])
    assert verdict == "ESCALATE"
    plain = FactConcern.model_validate("due_date is wrong")
    verdict, notes = apply_concern_guard("SEND_BACK", [plain], [], [])
    assert verdict == "SEND_BACK" and notes == ["due_date is wrong"]


def test_fabricated_stock_concern_is_dropped_and_invoice_approves(tmp_path, db_path):
    path, body = _big_invoice(tmp_path)
    concern = {"category": "stock", "item": "WidgetB", "text": "WidgetB requested 8 vs available 10"}
    fake = FakeLLM(_scripted(body, critique={"concerns": [concern], "stricter": True}))
    result = _run(path, db_path, fake)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert "CRITIQUE_HOLD" not in _codes(result)
    logged = _stage(result, "critique")
    assert logged["dropped_concerns"][0]["rule"] == "stock_sufficient"
    assert logged["dropped_concerns"][0]["detail"] == "WidgetB requested 8 and available stock is 10."
    assert logged["stricter"] is False
    system, facts = _task_payload(fake, "critique")
    assert "verified by code" in system
    assert "Today's date is 2026-10-31." in system
    widget_b = next(row for row in facts["stock"] if row["item"] == "WidgetB")
    assert widget_b == {"item": "WidgetB", "requested": "8", "available": 10}
    assert facts["totals_reconcile"] is True
    assert facts["today"] == "2026-10-31"
    assert facts["reference_prices"]["WidgetB"] == "500.00"
    assert facts["due_date_after_today"] is False


def test_fabricated_future_date_send_back_is_dropped(tmp_path, db_path):
    path = _write(tmp_path, "dates.txt", "Vendor: Widgets Inc.\nTotal: 500.00\n")
    body = _extraction(invoice_date="2026-04-16", due_date="2026-05-16")
    audit_body = {
        "verdict": "SEND_BACK",
        "feedback": [
            {"category": "future_date", "field": "invoice_date", "text": "2026-04-16 is in the future"},
            {"category": "future_date", "field": "due_date", "text": "2026-05-16 is in the future"},
        ],
        "notes": "dates are in the future",
        "cited_codes": [],
    }
    fake = FakeLLM(_scripted(body, audit=audit_body))
    result = _run(path, db_path, fake)
    assert result["outcome"] == "approve"
    assert result["send_backs"] == 0
    logged = _stage(result, "audit")
    assert logged["model_verdict"] == "SEND_BACK"
    assert logged["verdict"] == "PASS"
    assert {row["rule"] for row in logged["dropped_concerns"]} == {"not_after_today"}
    system, facts = _task_payload(fake, "audit")
    assert "verified by code" in system
    assert facts["invoice_date_after_today"] is False
    assert facts["due_date_after_today"] is False
    assert "concerns (list of objects)" not in system


def test_audit_keeps_a_raw_text_stock_note(db_path):
    text = "raw text shows WidgetB 18 but extraction says 8"
    out = _audit_once(db_path, [{"category": "stock", "item": "WidgetB", "text": text}])
    assert out["audit_verdict"] == "SEND_BACK"
    assert out["feedback"] == [text]
    assert out["logs"][-1]["result"]["dropped_concerns"] == []


def test_audit_keeps_other_feedback_when_a_future_claim_is_dropped(db_path):
    note = "invoice_number reads I-1 in raw text"
    out = _audit_once(
        db_path,
        [
            {"category": "future_date", "field": "due_date", "text": "2026-05-16 is in the future"},
            {"category": "date", "field": "due_date", "text": "date is in the past"},
            note,
        ],
    )
    assert out["audit_verdict"] == "SEND_BACK"
    assert out["feedback"] == ["date is in the past", note]
    assert out["logs"][-1]["result"]["dropped_concerns"][0]["rule"] == "not_after_today"


def test_real_shortage_is_still_rejected(tmp_path, db_path):
    path = _write(tmp_path, "short.txt", "Vendor: Widgets Inc.\nTotal: 6000.00\n")
    body = _extraction(line_items=[_line("GadgetX", 8, "750", "6000")], subtotal="6000", total="6000")
    critique_body = {
        "concerns": [{"category": "stock", "item": "GadgetX", "text": "GadgetX requested 8 vs available 5"}],
        "stricter": True,
    }
    result = _run(path, db_path, _scripted(body, critique=critique_body))
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "STOCK_SHORTFALL"
    assert "critique" not in [row["stage"] for row in result["logs"]]


def test_real_stock_concern_is_not_dropped(db_path):
    state = _approved(_short())

    def handler(_messages):
        return json.dumps(
            {
                "concerns": [
                    {"category": "stock", "item": "GadgetX", "text": "GadgetX requested 8 vs available 5"}
                ],
                "stricter": True,
            }
        )

    out = critique(state, FakeLLM(handler), db_path, FakeClock(), ZeroRandom(), 3, as_of=AS_OF)
    assert out["critique_stricter"] is True
    assert out["logs"][-1]["result"]["dropped_concerns"] == []
    assert out["findings"][-1].code == "CRITIQUE_HOLD"


def test_new_concern_still_holds(tmp_path, db_path):
    path, body = _big_invoice(tmp_path)
    text = "Possible duplicate of an invoice already paid"
    result = _run(
        path,
        db_path,
        _scripted(body, critique={"concerns": [{"category": "other", "text": text}], "stricter": True}),
    )
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "CRITIQUE_HOLD"
    assert _stage(result, "critique")["dropped_concerns"] == []


def test_tax_rate_concern_holds_and_a_mismatch_claim_is_logged(db_path):
    state = _approved(_covered())

    def handler(_messages):
        return json.dumps(
            {
                "concerns": [
                    {"category": "totals_mismatch", "field": "total", "text": "totals do not reconcile"},
                    {"category": "totals", "field": "tax_rate", "text": "tax rate is not the usual rate"},
                ],
                "stricter": True,
            }
        )

    out = critique(state, FakeLLM(handler), db_path, FakeClock(), ZeroRandom(), 3, as_of=AS_OF)
    assert out["critique_stricter"] is True
    assert out["critique_concerns"] == ["tax rate is not the usual rate"]
    assert out["logs"][-1]["result"]["dropped_concerns"][0]["rule"] == "totals_reconcile"


def test_real_future_date_still_sends_back(tmp_path, db_path):
    path = _write(tmp_path, "future.txt", "Vendor: Widgets Inc.\nTotal: 500.00\n")
    body = _extraction(invoice_date="2026-03-01", due_date="2026-05-16")
    send_back = {
        "verdict": "SEND_BACK",
        "feedback": [{"category": "future_date", "field": "due_date", "text": "due date is in the future"}],
        "notes": "",
        "cited_codes": [],
    }
    passed = {"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []}
    replies = [send_back, passed]

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            return body
        if name == "reason":
            return json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
        if name == "audit":
            return json.dumps(replies.pop(0))
        raise AssertionError(name)

    result = _run(path, db_path, handler, as_of=date(2026, 4, 1))
    assert result["send_backs"] == 1
    logged = _stage(result, "audit")
    assert logged["verdict"] == "SEND_BACK"
    assert logged["dropped_concerns"] == []
    assert logged["feedback"] == ["due date is in the future"]


def _covered():
    return _invoice([("WidgetB", "8", "500")], "4000")


def _short():
    return _invoice([("GadgetX", "8", "750")], "6000")


def _broken():
    return _invoice([("WidgetA", "2", "250")], "999")


def _invoice(lines, total):
    items = []
    for item, qty, price in lines:
        amount = Decimal(qty) * Decimal(price)
        items.append(
            LineItem(
                item=item,
                description=item,
                quantity=Decimal(qty),
                unit_price=Decimal(price),
                amount=amount,
            )
        )
    subtotal = sum((line.amount for line in items), Decimal("0"))
    return ExtractedInvoice(
        vendor="Widgets Inc.",
        invoice_date="2026-04-16",
        due_date="2026-05-16",
        currency="USD",
        line_items=items,
        subtotal=subtotal,
        tax_amount=Decimal("0"),
        shipping=Decimal("0"),
        total=Decimal(total),
        source=SourceInfo(path="x.txt", filename="x.txt", format="txt", raw_text="Total"),
    )


def _approved(invoice) -> PipelineState:
    return PipelineState(
        invoice_path="x.txt",
        filename="x.txt",
        raw_text=f"Total: {invoice.total}\n",
        invoice=invoice,
        decision=ApprovalDecision(
            decision="approve",
            reasoning=ApprovalReasoning(
                rules_applied=["stock"],
                evidence=["total"],
                risks_considered=["none"],
                conclusion="approve",
            ),
            amount=invoice.total,
            currency="USD",
        ),
    )


def _audit_once(db_path, feedback):
    def handler(_messages):
        return json.dumps(
            {"verdict": "SEND_BACK", "feedback": feedback, "notes": "", "cited_codes": []}
        )

    return audit(_approved(_covered()), FakeLLM(handler), db_path, FakeClock(), ZeroRandom(), 3, as_of=AS_OF)


def _line(item, qty, price, amount):
    return {
        "item": item,
        "description": item,
        "quantity": qty,
        "unit_price": price,
        "amount": amount,
        "note": None,
    }


def _extraction(**overrides) -> str:
    payload = {
        "vendor": "Widgets Inc.",
        "invoice_number": "INV-9",
        "invoice_date": "2026-04-16",
        "due_date": "2026-05-16",
        "currency": "USD",
        "line_items": [_line("WidgetA", 2, "250", "500")],
        "subtotal": "500",
        "tax_amount": 0,
        "shipping": 0,
        "total": "500",
        "payment_terms": "Net 30",
        "notes": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _big_invoice(directory: Path):
    path = _write(
        directory,
        "big.txt",
        "Vendor: Widgets Inc.\nDate: 2026-04-16\nDue Date: 2026-05-16\nTotal: 11500.00\n",
    )
    body = _extraction(
        line_items=[
            _line("WidgetA", 15, "250", "3750"),
            _line("WidgetB", 8, "500", "4000"),
            _line("GadgetX", 5, "750", "3750"),
        ],
        subtotal="11500",
        total="11500",
    )
    return path, body


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _scripted(extract_text, *, critique=None, audit=None):
    critique_body = critique or {"concerns": [], "stricter": False}
    audit_body = audit or {"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []}

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            return extract_text
        if name == "reason":
            return json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
        if name == "critique":
            return json.dumps(critique_body)
        if name == "audit":
            return json.dumps(audit_body)
        raise AssertionError(name)

    return handler


def _run(path, db_path, handler, as_of=AS_OF):
    llm = handler if isinstance(handler, FakeLLM) else FakeLLM(handler)
    return run_invoice(
        path,
        db_path,
        llm,
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=3,
        as_of=as_of,
    )


def _codes(result) -> list[str]:
    return [finding.code for finding in result["findings"]]


def _stage(result, name) -> dict:
    return next(row["result"] for row in result["logs"] if row["stage"] == name)


def _task_payload(fake: FakeLLM, task: str):
    call = next(messages for messages in fake.calls if task_of(messages) == task)
    return call[0]["content"], json.loads(call[1]["content"])["verified_by_code"]
