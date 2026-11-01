"""Review screen: fake-LLM runs, saved logs, hold notes, and the Streamlit boot."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import create_database
from decision.policy import APPROVAL_THRESHOLD_USD
from ui.present import (
    INVOICE_FIELDS,
    LINE_FIELDS,
    action_text,
    agent_trail,
    blank,
    format_amount,
    invoice_view,
    not_required,
    payment_text,
    reason_code_text,
    show_text,
    summarize,
    table_rows,
)
from ui.reviews import load_reviews, record_review
from ui.runner import grok_key_set, load_run, run_one, save_run

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
    trail = agent_trail(rejected["logs"], rejected["amount"])
    assert [row["stage"] for row in trail["rows"]] == ["ingestion", "validation", "reject_log"]
    assert "payment" in trail["missing"]
    assert "critique" not in trail["missing"]
    assert trail["critique"] == "Did not run"
    rows = table_rows([reject, approve], outcome="reject")
    assert [row["file"] for row in rows] == ["invoice_1002.txt"]

    paid = invoice_view(approve)
    assert paid["outcome"] == "approve"
    assert paid["reason_code"] is None
    assert paid["payment_status"] == "paid"
    assert any(item["severity"] == "warning" for item in paid["findings"])
    paid_trail = agent_trail(paid["logs"], paid["amount"])
    assert [row["stage"] for row in paid_trail["rows"]] == [
        "ingestion",
        "validation",
        "approval",
        "audit",
        "payment",
    ]
    assert paid_trail["missing"] == []
    assert paid_trail["critique"] == not_required()
    assert f"{APPROVAL_THRESHOLD_USD:,.0f}" in paid_trail["critique"]

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
    assert again["llm"] == "fake"
    assert json.loads(saved.read_text(encoding="utf-8"))["llm"] == "fake"
    assert load_run(log_path)[0]["llm"] == "grok"


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


def test_live_mode_uses_the_cli_client_and_offline_does_not(monkeypatch, db_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert grok_key_set() is False
    monkeypatch.setenv("XAI_API_KEY", "not-a-real-key")
    assert grok_key_set() is True

    calls = {}

    class Stub:
        def request(self, messages, timeout=60):
            raise AssertionError("live mode must not call the network in this test")

    def run(path, db, llm, **kwargs):
        calls["client"] = type(llm).__name__
        calls["kwargs"] = kwargs
        return {"filename": Path(path).name, "outcome": "hold", "logs": [], "findings": []}

    monkeypatch.setattr("ui.runner.run_invoice", run)
    monkeypatch.setattr("ui.runner.GrokClient", lambda: Stub())
    entry = run_one(ROOT / "data" / "invoices" / "invoice_1001.txt", db_path, live=True)
    assert calls["client"] == "Stub"
    assert calls["kwargs"] == {}
    assert entry["llm"] == "grok"


def test_screens_do_not_render_empty_literals(db_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    entries = [
        run_one(ROOT / "data" / "invoices" / name, db_path)
        for name in ("invoice_1002.txt", "invoice_1010.txt", "invoice_1014.xml")
    ]
    text = "\n".join(_screen_text(entry) for entry in entries)
    assert "null" not in text.lower()
    assert "None" not in text
    assert "[]" not in text
    assert "Approved, no action needed" in text
    critique = agent_trail(entries[1]["state"]["logs"], invoice_view(entries[1])["amount"])["critique"]
    assert critique == not_required()
    ran = {
        "stage": "critique",
        "timestamp": "2026-10-31T00:00:00Z",
        "result": {"concerns": ["new bank account"], "stricter": True, "round": 1},
    }
    summary = agent_trail([ran], "15000")["rows"][0]["summary"]
    assert "new bank account" in summary
    assert "Stricter" in summary


def test_review_app_starts(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=90)
    app.run()
    assert not app.exception, app.exception
    labels = [button.label for button in app.button]
    assert "Process this invoice" in labels
    assert "Process data/invoices" in labels
    assert "Load run file" in labels
    mode = next(radio for radio in app.sidebar.radio if radio.label == "Mode")
    assert mode.value == "Offline (fake LLM)"
    assert "Live (Grok)" in mode.options

    app.text_input[0].set_value("data/invoices/invoice_1010.txt").run()
    next(button for button in app.button if button.label == "Process this invoice").click().run()
    assert not app.exception, app.exception
    rendered = _visible(app)
    assert "This run used Offline (fake LLM)." in rendered
    assert "null" not in rendered.lower()
    assert "None" not in rendered
    assert "[]" not in rendered

    _show(app, "Invoice detail")
    detail = _visible(app)
    assert "Approved, no action needed" in detail
    assert not_required() in detail
    assert "null" not in detail.lower()

    _show(app, "Run or load")
    app.text_input[0].set_value("data/invoices/invoice_1014.xml").run()
    next(button for button in app.button if button.label == "Process this invoice").click().run()
    assert not app.exception, app.exception

    for screen in ("Results", "Hold queue", "Metrics"):
        _show(app, screen)
        shown = _visible(app)
        assert "null" not in shown.lower(), screen
        assert "None" not in shown, screen
        assert "[]" not in shown, screen
        if screen == "Hold queue":
            choice = next(radio for radio in app.radio if radio.label == "Reviewer choice")
            assert choice.value is None


def _screen_text(entry: dict) -> str:
    view = invoice_view(entry)
    trail = agent_trail(view["logs"], view["amount"])
    invoice = view.get("invoice") or {}
    parts = [
        view["file"], reason_code_text(view), action_text(view), view.get("reason") or "",
        format_amount(view["amount"], view["currency"]), payment_text(view["payment_status"]),
        trail["critique"],
        " ".join(row["summary"] for row in trail["rows"]),
    ]
    parts.extend(show_text(invoice[name]) for name in INVOICE_FIELDS if not blank(invoice.get(name)))
    for line in invoice.get("line_items") or []:
        parts.extend(show_text(line.get(name)) for name in LINE_FIELDS if not blank(line.get(name)))
    for finding in view["findings"]:
        parts.extend([finding.get("code") or "", finding.get("message") or ""])
        if not blank(finding.get("item")):
            parts.append(str(finding["item"]))
    if view.get("raw_text"):
        parts.append(view["raw_text"])
    return "\n".join(parts)


def _show(app, screen: str) -> None:
    radio = next(item for item in app.sidebar.radio if item.label == "Screen")
    radio.set_value(screen).run()
    assert not app.exception, app.exception


def _visible(app) -> str:
    chunks = []
    blocks = [app, app.sidebar]
    names = (
        "markdown", "caption", "text", "info", "success", "warning", "error",
        "header", "subheader", "title", "metric", "badge", "write",
    )
    for block in blocks:
        for name in names:
            for item in getattr(block, name, []):
                chunks.append(str(getattr(item, "value", "") or ""))
                chunks.append(str(getattr(item, "label", "") or ""))
                chunks.append(str(getattr(item, "body", "") or ""))
    return "\n".join(chunks)


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
