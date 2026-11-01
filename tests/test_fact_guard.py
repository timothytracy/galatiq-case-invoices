"""Critique and audit cannot contradict a fact the code has already checked."""

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


def test_stock_concern_dropped_when_quantity_is_covered():
    invoice = _invoice([("WidgetB", 8, "500")], total="4000")
    false_claim = FactConcern(
        category="stock",
        item="WidgetB",
        text="WidgetB requested 8 vs available 10",
    )
    unnamed = FactConcern(category="STOCK", text="stock does not cover the order")
    kept, dropped = partition_concerns([false_claim, unnamed], invoice, STOCK, AS_OF)
    assert kept == []
    assert dropped[0]["item"] == "WidgetB"
    assert dropped[0]["reason"] == "WidgetB requested 8 and available stock is 10."
    assert "within available stock" in dropped[1]["reason"]


def test_stock_concern_kept_when_quantity_exceeds_stock():
    invoice = _invoice([("GadgetX", 8, "750")], total="6000")
    claim = FactConcern(category="stock", item="GadgetX", text="GadgetX requested 8 vs available 5")
    kept, dropped = partition_concerns([claim], invoice, STOCK, AS_OF)
    assert kept == [claim]
    assert dropped == []


def test_totals_concern_follows_reconciliation():
    sound = _invoice([("WidgetA", 2, "250")], total="500")
    false_claim = FactConcern(category="totals", field="total", text="totals do not reconcile")
    kept, dropped = partition_concerns([false_claim], sound, STOCK, AS_OF)
    assert kept == []
    assert dropped[0]["reason"] == "Line totals and the grand total reconcile."

    broken = _invoice([("WidgetA", 2, "250")], total="999")
    real = FactConcern(category="totals", field="total", text="printed total does not match")
    kept, dropped = partition_concerns([real], broken, STOCK, AS_OF)
    assert kept == [real]
    assert dropped == []


def test_date_concern_dropped_only_when_not_after_today():
    invoice = _invoice(
        [("WidgetA", 2, "250")],
        total="500",
        invoice_date="2026-04-16",
        due_date="2026-05-16",
    )
    claims = [
        FactConcern(category="date", field="invoice_date", text="2026-04-16 is in the future"),
        FactConcern(category="date", field="due_date", text="2026-05-16 is in the future"),
        FactConcern(category="date", field="2026-10-31", text="today is in the future"),
    ]
    kept, dropped = partition_concerns(claims, invoice, STOCK, AS_OF)
    assert kept == []
    assert "invoice_date 2026-04-16 is not after 2026-10-31." in [row["reason"] for row in dropped]

    later = date(2026, 4, 1)
    future = FactConcern(category="date", field="due_date", text="due date is in the future")
    kept, dropped = partition_concerns([future], invoice, STOCK, later)
    assert kept == [future]
    assert dropped == []

    unparsed = _invoice([("WidgetA", 1, "250")], total="250", due_date="yesterday")
    vague = FactConcern(category="date", field="due_date", text="yesterday is in the future")
    kept, dropped = partition_concerns([vague], unparsed, STOCK, AS_OF)
    assert kept == [vague]
    assert dropped == []


def test_other_and_plain_text_concerns_are_kept():
    invoice = _invoice([("WidgetB", 8, "500")], total="4000")
    prose = FactConcern.model_validate("WidgetB requested 8 vs available 10")
    other = FactConcern(category="other", text="Possible duplicate of an invoice already paid")
    kept, dropped = partition_concerns([prose, other], invoice, STOCK, AS_OF)
    assert kept == [prose, other]
    assert dropped == []
    assert prose.category == "other"


def test_audit_send_back_dropped_only_when_every_concern_contradicts():
    dropped = [{"category": "date", "text": "2026-05-16 is in the future"}]
    verdict, feedback = apply_concern_guard("SEND_BACK", ["due date"], [], [], dropped)
    assert verdict == "PASS"
    assert feedback == []

    kept = [FactConcern(category="other", text="possible duplicate")]
    verdict, feedback = apply_concern_guard("SEND_BACK", ["due date"], [], kept, dropped)
    assert verdict == "SEND_BACK"
    assert feedback == ["due date"]

    verdict, feedback = apply_concern_guard("ESCALATE", ["note"], ["FRAUD_CLUSTER"], [], dropped)
    assert verdict == "ESCALATE"
    assert feedback == ["note"]

    verdict, feedback = apply_concern_guard("SEND_BACK", ["due_date is wrong"], [], [], [])
    assert verdict == "SEND_BACK"
    assert feedback == ["due_date is wrong"]


def test_fabricated_stock_concern_is_dropped_and_invoice_approves(tmp_path, db_path):
    path, body = _big_invoice(tmp_path)
    concern = {
        "category": "stock",
        "item": "WidgetB",
        "field": "",
        "text": "WidgetB requested 8 vs available 10",
    }
    fake = FakeLLM(_scripted(body, critique={"concerns": [concern], "stricter": True}))
    result = _run(path, db_path, fake)
    assert result["outcome"] == "approve"
    assert result["reason_code"] is None
    assert result["payment"].status == "paid"
    assert "CRITIQUE_HOLD" not in _codes(result)
    logged = _stage(result, "critique")
    assert logged["dropped_concerns"][0]["reason"] == "WidgetB requested 8 and available stock is 10."
    assert logged["concerns"] == []
    assert logged["stricter"] is False
    system, facts = _task_payload(fake, "critique")
    assert "verified by code" in system
    assert "Today's date is 2026-10-31." in system
    widget_b = next(line for line in facts["lines"] if line["item"] == "WidgetB")
    assert widget_b["requested_quantity"] == "8"
    assert widget_b["available_stock"] == 10
    assert widget_b["stock_sufficient"] is True
    assert widget_b["reference_price"] == "500.00"
    assert facts["totals_reconcile"] is True
    assert facts["today"] == "2026-10-31"
    assert facts["reference_prices"]["WidgetA"] == "250.00"


def test_fabricated_totals_concern_is_dropped(tmp_path, db_path):
    path, body = _big_invoice(tmp_path)
    concern = {
        "category": "totals",
        "field": "total",
        "text": "the line totals do not reconcile to the grand total",
    }
    result = _run(path, db_path, _scripted(body, critique={"concerns": [concern], "stricter": True}))
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    logged = _stage(result, "critique")
    assert logged["dropped_concerns"][0]["category"] == "totals"
    assert logged["dropped_concerns"][0]["reason"] == "Line totals and the grand total reconcile."
    assert "CRITIQUE_HOLD" not in _codes(result)


def test_fabricated_future_date_send_back_is_dropped(tmp_path, db_path):
    path = _write(tmp_path, "dates.txt", "Vendor: Widgets Inc.\nTotal: 500.00\n")
    body = _extraction(
        invoice_date="2026-04-16",
        due_date="2026-05-16",
    )
    concerns = [
        {"category": "date", "field": "invoice_date", "text": "2026-04-16 is in the future"},
        {"category": "date", "field": "due_date", "text": "2026-05-16 is in the future"},
    ]
    audit = {
        "verdict": "SEND_BACK",
        "concerns": concerns,
        "feedback": ["invoice_date is in the future", "due_date is in the future"],
        "notes": "dates are in the future",
        "cited_codes": [],
    }
    fake = FakeLLM(_scripted(body, audit=audit))
    result = _run(path, db_path, fake)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert result["send_backs"] == 0
    logged = _stage(result, "audit")
    assert logged["model_verdict"] == "SEND_BACK"
    assert logged["verdict"] == "PASS"
    assert {row["field"] for row in logged["dropped_concerns"]} == {"invoice_date", "due_date"}
    assert logged["concerns"] == []
    system, facts = _task_payload(fake, "audit")
    assert "verified by code" in system
    assert "Today's date is 2026-10-31." in system
    assert facts["invoice_date"] == "2026-04-16"
    assert facts["due_date"] == "2026-05-16"
    assert facts["invoice_date_after_today"] is False
    assert facts["due_date_after_today"] is False
    assert facts["today"] == "2026-10-31"


def test_real_shortage_is_still_rejected(tmp_path, db_path):
    path = _write(tmp_path, "short.txt", "Vendor: Widgets Inc.\nTotal: 6000.00\n")
    body = _extraction(
        line_items=[_line("GadgetX", 8, "750", "6000")],
        subtotal="6000",
        total="6000",
    )
    # The model would hold on stock. Validation rejects before critique runs.
    critique_body = {
        "concerns": [
            {"category": "stock", "item": "GadgetX", "text": "GadgetX requested 8 vs available 5"}
        ],
        "stricter": True,
    }
    result = _run(path, db_path, _scripted(body, critique=critique_body))
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "STOCK_SHORTFALL"
    assert result["payment"].status == "not_paid"
    assert "critique" not in [row["stage"] for row in result["logs"]]


def test_real_stock_concern_is_not_dropped(db_path):
    invoice = _invoice([("GadgetX", 8, "750")], total="6000")
    state = PipelineState(
        invoice_path="short.txt",
        filename="short.txt",
        raw_text="Total: 6000.00\n",
        invoice=invoice,
        decision=_approval(),
    )

    def handler(_messages):
        return json.dumps(
            {
                "concerns": [
                    {
                        "category": "stock",
                        "item": "GadgetX",
                        "text": "GadgetX requested 8 vs available 5",
                    }
                ],
                "stricter": True,
            }
        )

    out = critique(state, FakeLLM(handler), db_path, FakeClock(), ZeroRandom(), 3, as_of=AS_OF)
    assert out["critique_stricter"] is True
    assert out["critique_concerns"] == ["GadgetX requested 8 vs available 5"]
    assert out["logs"][-1]["result"]["dropped_concerns"] == []
    assert out["findings"][-1].code == "CRITIQUE_HOLD"


def test_new_concern_still_holds(tmp_path, db_path):
    path, body = _big_invoice(tmp_path)
    text = "Possible duplicate of an invoice already paid"
    concern = {"category": "other", "text": text}
    result = _run(path, db_path, _scripted(body, critique={"concerns": [concern], "stricter": True}))
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "CRITIQUE_HOLD"
    assert result["payment"].status == "not_paid"
    logged = _stage(result, "critique")
    assert logged["dropped_concerns"] == []
    assert logged["concerns"] == [text]
    assert text in result["decision"].reasoning.risks_considered[-1]


def test_real_future_date_still_sends_back(tmp_path, db_path):
    path = _write(tmp_path, "future.txt", "Vendor: Widgets Inc.\nTotal: 500.00\n")
    body = _extraction(invoice_date="2026-03-01", due_date="2026-05-16")
    send_back = {
        "verdict": "SEND_BACK",
        "concerns": [
            {"category": "date", "field": "due_date", "text": "due date is in the future"}
        ],
        "feedback": ["due_date is in the future"],
        "notes": "future",
        "cited_codes": [],
    }
    passed = {"verdict": "PASS", "concerns": [], "feedback": [], "notes": "PASS", "cited_codes": []}
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
    assert result["outcome"] == "approve"
    logged = _stage(result, "audit")
    assert logged["verdict"] == "SEND_BACK"
    assert logged["dropped_concerns"] == []
    assert logged["concerns"] == ["due date is in the future"]


def _invoice(lines, total, invoice_date="2026-04-16", due_date="2026-05-16"):
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
        invoice_date=invoice_date,
        due_date=due_date,
        currency="USD",
        line_items=items,
        subtotal=subtotal,
        tax_amount=Decimal("0"),
        shipping=Decimal("0"),
        total=Decimal(total),
        source=SourceInfo(path="x.txt", filename="x.txt", format="txt", raw_text="Total"),
    )


def _approval() -> ApprovalDecision:
    return ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(
            rules_applied=["stock"],
            evidence=["total 6000.00 USD"],
            risks_considered=["none"],
            conclusion="approve",
        ),
        amount=Decimal("6000"),
        currency="USD",
    )


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
        "vendor_address": None,
        "invoice_number": "INV-9",
        "invoice_date": "2026-04-16",
        "due_date": "2026-05-16",
        "currency": "USD",
        "line_items": [_line("WidgetA", 2, "250", "500")],
        "subtotal": "500",
        "tax_rate": 0,
        "tax_amount": 0,
        "shipping": 0,
        "total": "500",
        "payment_terms": "Net 30",
        "notes": None,
        "revision": None,
        "embedded_instructions": False,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _big_invoice(directory: Path):
    text = "\n".join(
        [
            "Vendor: Widgets Inc.",
            "Date: 2026-04-16",
            "Due Date: 2026-05-16",
            "WidgetA 15",
            "WidgetB 8",
            "GadgetX 5",
            "Total: 11500.00",
        ]
    )
    path = _write(directory, "big.txt", text + "\n")
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


def _task_payload(fake: FakeLLM, task: str) -> tuple[str, dict]:
    call = next(messages for messages in fake.calls if task_of(messages) == task)
    system = call[0]["content"]
    payload = json.loads(call[1]["content"])
    return system, payload["verified_by_code"]
