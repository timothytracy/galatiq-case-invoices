"""Review screen: fake-LLM runs, saved logs, hold notes, and the Streamlit boot."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import create_database
from ui.present import agent_trail, invoice_view, summarize, table_rows
from ui.reviews import load_reviews, record_review
from ui.runner import load_run, run_one, save_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "inventory.db"
    create_database(path)
    return path


def test_fake_llm_run_exposes_pipeline_fields(db_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    reject = run_one(ROOT / "data" / "invoices" / "invoice_1002.txt", db_path)
    approve = run_one(ROOT / "data" / "invoices" / "invoice_1010.txt", db_path)
    hold = run_one(ROOT / "data" / "invoices" / "invoice_1014.xml", db_path)

    rejected = invoice_view(reject)
    assert rejected["outcome"] == "reject"
    assert rejected["reason_code"] == "STOCK_SHORTFALL"
    assert rejected["payment_status"] == "not_paid"
    assert rejected["action"]
    assert "exceeds stock" in rejected["reason"]
    assert rejected["amount"] is not None
    assert rejected["currency"] == "USD"
    trail = agent_trail(rejected["logs"])
    assert [row["stage"] for row in trail["rows"]] == ["ingestion", "validation", "reject_log"]
    assert "payment" in trail["missing"]
    assert "critique" in trail["missing"]
    rows = table_rows([reject, approve], outcome="reject")
    assert [row["file"] for row in rows] == ["invoice_1002.txt"]

    paid = invoice_view(approve)
    assert paid["outcome"] == "approve"
    assert paid["reason_code"] is None
    assert paid["payment_status"] == "paid"
    assert any(item["severity"] == "warning" for item in paid["findings"])
    paid_trail = agent_trail(paid["logs"])
    assert [row["stage"] for row in paid_trail["rows"]] == [
        "ingestion",
        "validation",
        "approval",
        "audit",
        "payment",
    ]
    assert paid_trail["missing"] == ["critique"]

    held = invoice_view(hold)
    assert held["outcome"] == "hold"
    assert held["reason_code"] == "NON_USD"
    assert held["currency"] == "EUR"
    assert held["payment_status"] == "not_paid"
    assert held["action"]
    summary = summarize([reject, approve, hold])
    assert summary["counts"] == {"approve": 1, "reject": 1, "hold": 1}
    assert summary["dollars"]["hold"] == Decimal("0")
    assert summary["other"]["EUR"]["hold"] == Decimal(str(held["amount"]))
    assert summary["dollars"]["approve"] == Decimal(str(paid["amount"]))
    assert summary["timing"] == "elapsed_seconds"
    assert summary["average_seconds"] > 0


def test_pipeline_log_and_review_run_roundtrip(db_path, tmp_path):
    entry = run_one(ROOT / "data" / "invoices" / "invoice_1002.txt", db_path)
    state = entry["state"]
    log_path = tmp_path / "invoice_1002.txt.json"
    payload = list(state["logs"])
    payload.append(
        {
            "record": "result",
            "file": state["filename"],
            "outcome": state["outcome"],
            "reason_code": state["reason_code"],
            "action": state["action"],
            "payment_status": state["payment"]["status"],
            "codes": [finding["code"] for finding in state["findings"]],
        }
    )
    log_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = invoice_view(load_run(log_path)[0])
    assert loaded["file"] == "invoice_1002.txt"
    assert loaded["outcome"] == "reject"
    assert loaded["reason_code"] == "STOCK_SHORTFALL"
    assert loaded["payment_status"] == "not_paid"
    assert loaded["invoice"]["vendor"]
    assert "exceeds stock" in loaded["reason"]

    saved = tmp_path / "review_run.json"
    save_run(saved, [entry])
    again = load_run(saved)[0]
    assert again["state"]["outcome"] == "reject"
    assert again["elapsed_seconds"] == entry["elapsed_seconds"]


def test_hold_review_does_not_change_the_pipeline_decision(db_path, tmp_path):
    entry = run_one(ROOT / "data" / "invoices" / "invoice_1014.xml", db_path)
    before = json.dumps(entry)
    view = invoice_view(entry)
    path = tmp_path / "hold_reviews.json"
    with pytest.raises(ValueError, match="note"):
        record_review(
            file=view["file"],
            pipeline_outcome=view["outcome"],
            pipeline_reason_code=view["reason_code"],
            pipeline_action=view["action"],
            reviewer_outcome="approve",
            note="  ",
            path=path,
        )
    saved = record_review(
        file=view["file"],
        pipeline_outcome=view["outcome"],
        pipeline_reason_code=view["reason_code"],
        pipeline_action=view["action"],
        reviewer_outcome="approve",
        note="Finance confirmed the euros.",
        path=path,
    )
    assert json.dumps(entry) == before
    assert view["outcome"] == "hold"
    assert view["payment_status"] == "not_paid"
    assert saved["pipeline_outcome"] == "hold"
    assert saved["reviewer_outcome"] == "approve"
    assert load_reviews(path) == [saved]
    with pytest.raises(ValueError, match="only a hold"):
        record_review(
            file="invoice_1001.txt",
            pipeline_outcome="approve",
            pipeline_reason_code=None,
            pipeline_action=None,
            reviewer_outcome="reject",
            note="nope",
            path=path,
        )


def test_metrics_from_timestamps_and_currency():
    usd = _entry("approve", "5000.00", "USD", 1.5, "a.txt")
    eur = _entry("hold", "4125.00", "EUR", 2.5, "b.xml")
    rejected = _entry("reject", "100.50", "USD", 0.5, "c.txt")
    blank = _entry("hold", None, "USD", None, "d.txt")
    summary = summarize([usd, eur, rejected, blank])
    assert summary["counts"] == {"approve": 1, "reject": 1, "hold": 2}
    assert summary["dollars"]["approve"] == Decimal("5000.00")
    assert summary["dollars"]["reject"] == Decimal("100.50")
    assert summary["dollars"]["hold"] == Decimal("0")
    assert summary["other"]["EUR"]["hold"] == Decimal("4125.00")
    assert summary["average_seconds"] == pytest.approx(1.5)
    assert summary["timed_invoices"] == 3

    stamped = {
        "elapsed_seconds": None,
        "state": {
            "filename": "t.txt",
            "outcome": "approve",
            "invoice": {"total": "10", "currency": "USD"},
            "payment": {"status": "paid"},
            "findings": [],
            "decision": {"reasoning": {"conclusion": "All checks passed."}},
            "logs": [
                {"timestamp": "2026-10-31T00:00:00Z", "stage": "ingestion", "result": {}},
                {"timestamp": "2026-10-31T00:00:02Z", "stage": "payment", "result": {}},
            ],
        },
    }
    timed = summarize([stamped])
    assert timed["average_seconds"] == 2.0
    assert timed["timing"] == "timestamps"
    assert table_rows([stamped], outcome="reject") == []


def test_review_app_starts():
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=90)
    app.run()
    assert not app.exception, app.exception
    labels = [button.label for button in app.button]
    assert "Process this invoice" in labels
    assert "Process data/invoices" in labels
    assert "Load run file" in labels


def _entry(outcome, total, currency, elapsed, filename):
    return {
        "elapsed_seconds": elapsed,
        "state": {
            "filename": filename,
            "outcome": outcome,
            "reason_code": None,
            "action": None,
            "invoice": {"total": total, "currency": currency, "invoice_number": "INV-1", "line_items": []},
            "decision": {
                "amount": total,
                "currency": currency,
                "reasoning": {"conclusion": "Because."},
            },
            "payment": {"status": "paid" if outcome == "approve" else "not_paid"},
            "findings": [],
            "logs": [],
        },
    }
