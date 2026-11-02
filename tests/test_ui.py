"""Review screen: fake-LLM runs, saved logs, hold notes, and the Streamlit boot."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import create_database
from decision.policy import APPROVAL_THRESHOLD_USD
from llm_client import TOOL_CALL_CAP, TOOL_ROUND_CAP
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
    tool_call_view,
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


def test_tool_call_view_shapes_stage_attempts_and_gaps(tmp_path):
    loaded = invoice_view(load_run(ROOT / "tests" / "fixtures" / "tool_calls_review.json")[0])
    shaped = loaded["tool_calls"]
    calls = shaped["calls"]
    assert [row["name"] for row in calls] == [
        "lookup_item",
        "lookup_item",
        "get_policy_rule",
        "recompute_totals",
        "lookup_item",
        "recompute_totals",
        "get_policy_rule",
        "lookup_item",
    ]
    success, repeat, blocked, error, raw, sparse, extra, failed = calls
    assert success["who"] == "audit"
    assert success["round"] == "1"
    assert success["attempt"] == "1"
    assert success["arguments"] == "Item WidgetB"
    assert success["detail"] == "Item WidgetB, Stock 4, Unit price 25.00"
    assert success["latency"] == "12 ms"
    assert success["repeat"] is False
    assert success["blocked"] is False
    assert success["error"] is False
    assert success["failed_attempt"] is False
    assert repeat["repeat"] is True
    assert repeat["detail"] == success["detail"]
    assert blocked["blocked"] is True
    assert blocked["error"] is False
    assert blocked["detail"] == "Stopped by the tool cap"
    assert blocked["latency"] == "0 ms"
    assert blocked["attempt"] == "2"
    assert error["error"] is True
    assert error["blocked"] is False
    assert error["detail"] == "item not found"
    assert error["arguments"] == "Invoice INV-1042"
    assert error["arguments_raw"] == ""
    assert raw["arguments"] == ""
    assert raw["arguments_raw"] == "{item: WidgetB}"
    assert raw["error"] is True
    assert raw["detail"] == "arguments are not valid JSON"
    assert success["arguments_raw"] == ""
    assert sparse["who"] == "audit"
    assert sparse["round"] == ""
    assert sparse["attempt"] == ""
    assert sparse["arguments"] == ""
    assert sparse["detail"] == ""
    assert sparse["latency"] == ""
    assert sparse["extra"] == ""
    assert sparse["error"] is False
    assert extra["extra"] == "Batch id b-9"
    assert failed["failed_attempt"] is True
    assert failed["attempt"] == "1"
    assert failed["arguments"] == "Item WidgetC"
    assert failed["who"] == "audit"
    cap = (
        f"The tool cap was hit on rounds (limit {TOOL_ROUND_CAP}). "
        "A final no-tools answer was forced."
    )
    assert shaped["extras"].count(cap) == 1
    assert "Audit: Trace id t-1" in shaped["extras"]
    assert "Audit attempt 1: Queue id q-1" in shaped["extras"]
    rendered = json.dumps(shaped)
    assert "null" not in rendered
    assert "None" not in rendered
    assert "[]" not in rendered

    assert tool_call_view([{"stage": "audit", "result": {}}]) == {"calls": [], "extras": []}
    assert tool_call_view([{"stage": "audit", "result": {"tool_calls": []}}]) == {"calls": [], "extras": []}
    assert tool_call_view([{"stage": "audit", "result": {"tool_calls": None}}]) == {"calls": [], "extras": []}
    assert tool_call_view(None)["calls"] == []
    assert tool_call_view([{"stage": "audit", "result": {"tool_calls": [None, "skip", {}]}}])["calls"] == [
        {
            "who": "audit",
            "round": "",
            "attempt": "",
            "name": "",
            "arguments": "",
            "arguments_raw": "",
            "detail": "",
            "latency": "",
            "repeat": False,
            "blocked": False,
            "error": False,
            "failed_attempt": False,
            "extra": "",
        }
    ]
    other = tool_call_view(
        [
            {
                "stage": "critique",
                "result": {
                    "tool_calls": [
                        {
                            "agent": "clerk",
                            "name": "lookup_item",
                            "call_id": "c1",
                            "round": 2,
                            "attempt": 3,
                            "latency_ms": 0,
                            "result": {"ok": True},
                            "error": None,
                            "mystery_flag": True,
                        }
                    ],
                    "another_flag": False,
                },
            }
        ]
    )
    row = other["calls"][0]
    assert row["who"] == "clerk"
    assert row["round"] == "2"
    assert row["attempt"] == "3"
    assert row["latency"] == "0 ms"
    assert row["detail"] == "Ok Yes"
    assert row["extra"] == "Mystery flag Yes"
    assert other["extras"] == ["Critique: Another flag No"]
    almost = tool_call_view(
        [
            {
                "stage": "audit",
                "result": {
                    "tool_calls": [
                        {
                            "name": "lookup_item",
                            "call_id": "c2",
                            "error": "blocked: tool_cap extra",
                            "result": None,
                        }
                    ]
                },
            }
        ]
    )["calls"][0]
    assert almost["blocked"] is False
    assert almost["error"] is True
    assert almost["detail"] == "blocked: tool_cap extra"
    bare = tool_call_view(
        [
            {
                "stage": "audit",
                "result": {
                    "tool_calls": [
                        {
                            "name": "lookup_item",
                            "call_id": "bare",
                            "arguments": None,
                            "result": None,
                            "error": None,
                        }
                    ]
                },
            }
        ]
    )["calls"][0]
    assert bare["arguments"] == ""
    assert bare["arguments_raw"] == ""
    assert bare["detail"] == ""
    assert "null" not in json.dumps(bare)
    rounds_only = tool_call_view([{"stage": "audit", "result": {"tool_cap_hit": "rounds"}}])
    assert rounds_only["calls"] == []
    assert rounds_only["extras"] == [f"The tool cap was hit on rounds (limit {TOOL_ROUND_CAP})."]
    calls_only = tool_call_view(
        [{"stage": "critique", "result": {"tool_cap_hit": "calls", "forced_final_turn": True}}]
    )
    assert calls_only["extras"] == [
        f"The tool cap was hit on calls (limit {TOOL_CALL_CAP}). A final no-tools answer was forced."
    ]
    forced_only = tool_call_view([{"stage": "audit", "result": {"forced_final_turn": True}}])
    assert forced_only["extras"] == ["A final no-tools answer was forced."]
    from_line = tool_call_view(
        [{"stage": "audit", "result": {}}],
        {"record": "result", "tool_cap_hit": "calls", "forced_final_turn": True},
    )
    assert from_line["calls"] == []
    assert from_line["extras"] == calls_only["extras"]
    shared = {
        "agent": "audit",
        "round": 1,
        "attempt": 1,
        "call_id": "same",
        "name": "lookup_item",
        "arguments": {"item": "WidgetB"},
        "result": {"item": "WidgetB"},
        "error": None,
        "latency_ms": 1,
    }
    duplicate = tool_call_view(
        [{"stage": "audit", "result": {"tool_calls": [shared], "attempts": []}}],
        {"tool_calls": [shared]},
        [{"attempt": 4, "tool_calls": [{"name": "lookup_item", "call_id": "only-attempt", "attempt": 4}]}],
    )
    assert [row["name"] for row in duplicate["calls"]] == ["lookup_item", "lookup_item"]
    assert duplicate["calls"][0]["failed_attempt"] is False
    assert duplicate["calls"][1]["failed_attempt"] is True
    assert duplicate["calls"][1]["attempt"] == "4"

    stage_call = {
        "agent": "clerk",
        "round": 1,
        "attempt": 1,
        "call_id": "call_1_1",
        "name": "lookup_item",
        "arguments": {"item": "Bolt"},
        "result": {"item": "Bolt"},
        "error": None,
        "latency_ms": 4,
    }
    failed_call = {
        "round": 1,
        "attempt": 2,
        "call_id": "call_2_1",
        "name": "get_policy_rule",
        "arguments": {"rule": "threshold"},
        "result": None,
        "error": "blocked: tool_cap",
        "latency_ms": 0,
    }
    log_path = tmp_path / "note.txt.json"
    log_path.write_text(
        json.dumps(
            [
                {
                    "record": "stage",
                    "stage": "critique",
                    "invoice_file": "note.txt",
                    "timestamp": "2026-10-31T00:00:00Z",
                    "result": {
                        "tool_calls": [stage_call],
                        "attempts": [
                            {
                                "attempt": 2,
                                "error_type": "tool_cap",
                                "retryable": False,
                                "tool_calls": [failed_call],
                            }
                        ],
                    },
                },
                {
                    "record": "result",
                    "file": "note.txt",
                    "outcome": "hold",
                    "reason_code": "SYSTEM_ERROR",
                    "payment_status": "not_paid",
                    "tool_calls": [stage_call],
                },
            ]
        ),
        encoding="utf-8",
    )
    from_log = invoice_view(load_run(log_path)[0])
    logged = from_log["tool_calls"]["calls"]
    assert from_log["file"] == "note.txt"
    assert [row["name"] for row in logged] == ["lookup_item", "get_policy_rule"]
    assert logged[0]["who"] == "clerk"
    assert logged[1]["who"] == "critique"
    assert logged[1]["blocked"] is True
    assert logged[1]["failed_attempt"] is True
    assert logged[1]["attempt"] == "2"


def test_tool_call_view_renders(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=90)
    app.run()
    assert not app.exception, app.exception
    run_file = next(item for item in app.text_input if item.label == "Run file")
    run_file.set_value("tests/fixtures/tool_calls_review.json").run()
    next(button for button in app.button if button.label == "Load run file").click().run()
    assert not app.exception, app.exception
    _show(app, "Invoice detail")
    chosen = next(item for item in app.selectbox if item.label == "Invoice")
    assert chosen.value == "invoice_1042.txt"
    detail = _visible(app)
    assert "Tool calls" in detail
    assert "lookup_item" in detail
    assert "Repeat" in detail
    assert "Blocked by cap" in detail
    assert "Stopped by the tool cap" in detail
    assert "Failed attempt" in detail
    assert "item not found" in detail
    assert "WidgetC" in detail
    assert "Batch id b-9" in detail
    assert "Trace id t-1" in detail
    assert "Raw arguments: {item: WidgetB}" in detail
    assert f"The tool cap was hit on rounds (limit {TOOL_ROUND_CAP})." in detail
    assert "A final no-tools answer was forced." in detail
    assert "does not count toward the cap" in detail
    assert "No tool calls." not in detail
    assert "null" not in detail.lower()
    assert "None" not in detail
    assert "[]" not in detail


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
    shown = view.get("tool_calls") or {}
    parts.extend(shown.get("extras") or [])
    for call in shown.get("calls") or []:
        parts.extend(
            str(call.get(name) or "")
            for name in (
                "who", "round", "attempt", "name", "arguments", "arguments_raw",
                "detail", "extra", "latency",
            )
        )
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
