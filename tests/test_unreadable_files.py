"""Empty and damaged files stop at ingestion. No model sees them."""

import json

import pytest

from graph import run_invoice
from tests.fake_llm_eval import AS_OF
from tests.fakes import FakeClock, FakeLLM, ZeroRandom
from parsers import unreadable_reason


def _never(messages):
    raise AssertionError("the model was called for a file code already rejected")


GOOD_JSON = {
    "invoice_number": "INV-7001",
    "vendor": "Widgets Inc.",
    "date": "2026-09-01",
    "due_date": "2026-10-01",
    "line_items": [{"item": "WidgetA", "quantity": 2, "unit_price": 250, "amount": 500}],
    "subtotal": 500,
    "total": 500,
    "currency": "USD",
}


@pytest.mark.parametrize(
    "name, body, reason",
    [
        ("empty.txt", "", "The file is empty."),
        ("blank.txt", "  \n\t\n", "The file is empty."),
        ("cut.json", json.dumps(GOOD_JSON, indent=2)[:120], "The JSON is cut off or invalid at line"),
        ("list.json", "[1, 2]", "The JSON is not an invoice object."),
        ("cut.xml", "<invoice><vendor>Widgets Inc.</vendor><total>500", "The XML is cut off or invalid at line"),
        ("header.csv", "Item,Qty,Unit Price,Line Total\n", "The CSV has a header but no invoice rows."),
    ],
)
def test_a_damaged_file_is_unreadable_without_a_model_call(name, body, reason, db_path, tmp_path):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    llm = FakeLLM(_never, fraud=_never)
    result = run_invoice(
        path, db_path, llm, clock=FakeClock(), rng=ZeroRandom(), max_attempts=1, as_of=AS_OF,
        ledger_path=tmp_path / "payments.db",
    )
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "UNREADABLE"
    assert reason in result["findings"][0].message
    assert llm.calls == [] and llm.fraud_calls == []


def test_a_whole_json_invoice_is_not_flagged(tmp_path):
    path = tmp_path / "whole.json"
    body = json.dumps(GOOD_JSON)
    assert unreadable_reason(path, body) is None
