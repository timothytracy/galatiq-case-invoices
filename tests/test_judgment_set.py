"""The judgment set: code approves every page, and only a quoted critique or audit can hold.

Fake model, no network.
"""

import json
from pathlib import Path

import pytest

from db import SEED
from decision.findings import assess
from graph import run_invoice
from models import AuditDraft, CritiqueDraft
from parsers import SUFFIXES, parse_file
from scripts.eval import grade, load_answer_key
from tests.fake_llm_eval import AS_OF, extraction_json
from tests.fakes import FRAUD_PASS, FakeClock, FakeLLM, ZeroRandom, task_of

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
JUDGMENT = ROOT / "data" / "judgment_invoices"
KEY = load_answer_key(ROOT / "data" / "judgment_answer_key.json")
STOCK = dict(SEED)
INNOCENT = "An ordinary restock billed as delivered."
PASS = {"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []}


def _files():
    return sorted(path for path in JUDGMENT.iterdir() if path.suffix.lower() in SUFFIXES)


def _handler(path: Path, *, critique=None, audit=None):
    critique_body = critique or {"concerns": [], "stricter": False, "innocent_reading": INNOCENT}
    audit_body = audit or PASS

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            return extraction_json(path)
        if name == "reason":
            return json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
        if name == "critique":
            return json.dumps(critique_body)
        if name == "audit":
            return json.dumps(audit_body)
        if name == "fraud":
            return FRAUD_PASS
        raise AssertionError(f"unexpected task {name}")

    return handler


def _run(db_path, path, handler):
    return run_invoice(
        path, db_path, FakeLLM(handler), clock=FakeClock(), rng=ZeroRandom(), max_attempts=3, as_of=AS_OF
    )


def _stage(result, name) -> dict:
    return next(row["result"] for row in result["logs"] if row["stage"] == name)


def _codes(result) -> list[str]:
    return [finding.code for finding in result["findings"]]


def _critique(text: str, quote: str, innocent: str = INNOCENT) -> dict:
    return {
        "concerns": [{"category": "other", "text": text, "quote": quote}],
        "stricter": True,
        "innocent_reading": innocent,
    }


def test_the_set_matches_the_key():
    names = [path.name for path in _files()]
    assert names == [f"invoice_{n}.txt" for n in range(2140, 2147)]
    assert sorted(KEY) == [f"data/judgment_invoices/{name}" for name in names]
    assert {row["code_only_outcome"] for row in KEY.values()} == {"APPROVE"}


@pytest.mark.parametrize("path", _files(), ids=lambda path: path.name)
def test_code_alone_approves_every_page(path):
    invoice = parse_file(path)
    assessed = assess(invoice, invoice.source.raw_text, STOCK)
    assert assessed.outcome == "approve", path.name
    assert assessed.findings == [], path.name


@pytest.mark.parametrize("path", _files(), ids=lambda path: path.name)
def test_a_silent_reviewer_pays_every_page(db_path, path):
    result = _run(db_path, path, _handler(path))
    assert result["outcome"] == "approve", path.name
    assert result["payment"].status == "paid"


def test_a_quoted_critique_holds_2140(db_path):
    path = JUDGMENT / "invoice_2140.txt"
    quote = "Partial shipment: 10 of 15 WidgetA delivered on 2026-09-11, balance on backorder"
    body = _critique("All 15 WidgetA are billed but only 10 were delivered.", quote)
    result = _run(db_path, path, _handler(path, critique=body))
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "CRITIQUE_HOLD"
    assert result["payment"].status == "not_paid"
    message = next(f.message for f in result["findings"] if f.code == "CRITIQUE_HOLD")
    assert quote in message
    logged = _stage(result, "critique")
    assert logged["dropped_concerns"] == []
    assert logged["quotes"] == [quote]
    row = grade(result, KEY["data/judgment_invoices/invoice_2140.txt"])
    assert row["outcome_ok"] and row["reason_ok"]


def test_a_quote_not_on_the_page_is_dropped_and_2140_pays(db_path):
    path = JUDGMENT / "invoice_2140.txt"
    body = _critique("Billed for goods not delivered.", "Only 5 of 15 WidgetA arrived, rest cancelled")
    result = _run(db_path, path, _handler(path, critique=body))
    assert result["outcome"] == "approve"
    assert "CRITIQUE_HOLD" not in _codes(result)
    assert _stage(result, "critique")["dropped_concerns"][0]["rule"] == "quote_not_on_page"


def test_a_missing_innocent_reading_drops_the_concern(db_path):
    path = JUDGMENT / "invoice_2140.txt"
    body = _critique("Billed for goods not delivered.", "balance on backorder", innocent="")
    result = _run(db_path, path, _handler(path, critique=body))
    assert result["outcome"] == "approve"
    assert _stage(result, "critique")["dropped_concerns"][0]["rule"] == "no_innocent_reading"


def test_a_date_line_quote_on_2106_is_structured_only(db_path):
    path = ROOT / "data" / "extra_invoices" / "invoice_2106.txt"
    body = _critique("The invoice date is in the future.", "Date: 2026-04-16")
    result = _run(db_path, path, _handler(path, critique=body))
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert _stage(result, "critique")["dropped_concerns"][0]["rule"] == "quote_structured_only"


def test_an_item_line_quote_is_structured_only(db_path):
    path = JUDGMENT / "invoice_2144.txt"
    body = _critique("GadgetX is billed.", "GadgetX    qty: 4     unit price: $750.00")
    result = _run(db_path, path, _handler(path, critique=body))
    assert result["outcome"] == "approve"
    assert _stage(result, "critique")["dropped_concerns"][0]["rule"] == "quote_structured_only"


def test_a_quoted_audit_escalation_holds_2146_and_a_fake_quote_pays(db_path):
    path = JUDGMENT / "invoice_2146.txt"
    quote = "Partial shipment: 3 of 6 WidgetB delivered"
    escalate = {
        "verdict": "ESCALATE",
        "feedback": [{"text": "All 6 WidgetB billed but only 3 delivered.", "quote": quote}],
        "notes": "billed for undelivered goods",
        "cited_codes": [],
        "innocent_reading": INNOCENT,
    }
    held = _run(db_path, path, _handler(path, audit=escalate))
    assert "critique" not in [row["stage"] for row in held["logs"]]
    assert held["outcome"] == "hold"
    assert held["reason_code"] == "AUDIT_ESCALATED"
    message = next(f.message for f in held["findings"] if f.code == "AUDIT_ESCALATED")
    assert quote in message
    row = grade(held, KEY["data/judgment_invoices/invoice_2146.txt"])
    assert row["outcome_ok"] and row["reason_ok"]

    fake = dict(escalate, feedback=[{"text": "billed twice", "quote": "Second copy of INV-2146 already paid"}])
    paid = _run(db_path, path, _handler(path, audit=fake))
    assert paid["outcome"] == "approve"
    assert paid["payment"].status == "paid"
    logged = _stage(paid, "audit")
    assert logged["model_verdict"] == "ESCALATE"
    assert logged["verdict"] == "PASS"
    assert logged["dropped_concerns"][0]["rule"] == "quote_not_on_page"


def test_old_replies_without_quote_or_innocent_reading_still_load():
    draft = CritiqueDraft.model_validate({"concerns": ["amount is high"], "stricter": False})
    assert draft.innocent_reading == "" and draft.concerns[0].quote == ""
    audit = AuditDraft.model_validate({"verdict": "PASS", "feedback": [], "notes": None})
    assert audit.innocent_reading == "" and audit.notes == ""
    nulls = CritiqueDraft.model_validate(
        {"concerns": [{"text": "x", "quote": None}], "stricter": True, "innocent_reading": None}
    )
    assert nulls.concerns[0].quote == "" and nulls.innocent_reading == ""
