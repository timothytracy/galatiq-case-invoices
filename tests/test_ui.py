"""Review screen: replay runs, saved logs, hold notes, and the Streamlit boot."""

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from decision.policy import APPROVAL_THRESHOLD_USD
from llm_client import TOOL_CALL_CAP, TOOL_ROUND_CAP
from ui.pay import (
    BACKEND_UNAVAILABLE,
    approve_and_pay,
    extracted_total,
    saved_result,
)
from ui.present import (
    INVOICE_FIELDS,
    LINE_FIELDS,
    shared_ledger_note,
    action_text,
    agent_trail,
    already_paid_notice,
    blank,
    can_pay,
    concern_text,
    confirm_label,
    format_amount,
    holds,
    invoice_view,
    not_required,
    pay_block,
    pay_message,
    payment_detail_text,
    payment_text,
    reason_code_text,
    show_text,
    summarize,
    table_rows,
    tool_call_view,
)
from ui.reviews import load_reviews, record_review
from ui.runner import (
    PAYMENTS_DB, ensure_db, grok_key_set, invoice_files, is_real_payments_db, load_run,
    paid_ledger_row, recording_files, run_one, save_run, stage_upload, temporary_ledger,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "recordings" / "samples.jsonl"


def test_replay_run_exposes_pipeline_fields(db_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    reject = run_one(ROOT / "data" / "invoices" / "invoice_1002.txt", db_path, replay=SAMPLES)
    approve = run_one(ROOT / "data" / "invoices" / "invoice_1010.txt", db_path, replay=SAMPLES)
    hold = run_one(ROOT / "data" / "invoices" / "invoice_1014.xml", db_path, replay=SAMPLES)

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
    entry = run_one(ROOT / "data" / "invoices" / "invoice_1002.txt", db_path, replay=SAMPLES)
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
    assert again["llm"] == "replay"
    assert json.loads(saved.read_text(encoding="utf-8"))["llm"] == "replay"
    assert load_run(log_path)[0]["llm"] == "grok"


def test_hold_review_does_not_change_the_pipeline_decision(db_path, tmp_path):
    entry = run_one(ROOT / "data" / "invoices" / "invoice_1014.xml", db_path, replay=SAMPLES)
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


def test_paid_hold_moves_from_held_to_released():
    hold = _review_hold("northwind.txt", "500.00", "USD", "VENDOR_RENAMED", "The vendor name says formerly.")
    euro = _review_hold("harbor.txt", "890.25", "EUR", "VENDOR_RENAMED", "The vendor name says formerly.")
    assert can_pay(invoice_view(hold)) is True
    before = summarize([hold, euro])
    assert before["counts"]["hold"] == 2
    assert before["dollars"]["hold"] == Decimal("500.00")
    assert before["released"]["count"] == 0

    hold["state"]["payment"] = {"status": "paid", "amount": "500.00", "currency": "USD"}
    euro["state"]["payment"] = {"status": "paid", "amount": "890.25", "currency": "EUR"}
    paid = invoice_view(hold)
    assert paid["outcome"] == "hold"
    assert can_pay(paid) is False
    assert pay_block(paid) == "Already paid after review."
    after = summarize([hold, euro])
    assert after["counts"]["hold"] == 0
    assert after["dollars"]["hold"] == Decimal("0")
    assert after["other"] == {}
    assert after["released"] == {
        "count": 2, "dollars": Decimal("500.00"), "other": {"EUR": Decimal("890.25")},
    }


def test_live_mode_uses_the_cli_client_and_needs_a_mode(monkeypatch, db_path):
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
    assert calls["kwargs"] == {"ledger_path": PAYMENTS_DB}
    assert entry["llm"] == "grok"
    with pytest.raises(ValueError, match="live=True or a replay recording"):
        run_one(ROOT / "data" / "invoices" / "invoice_1001.txt", db_path)


def test_screens_do_not_render_empty_literals(db_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    entries = [
        run_one(ROOT / "data" / "invoices" / name, db_path, replay=SAMPLES)
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
    assert mode.value == "Replay (recorded Grok)"
    assert list(mode.options) == ["Live (Grok)", "Replay (recorded Grok)"]

    app.text_input[0].set_value("data/invoices/invoice_1010.txt").run()
    next(button for button in app.button if button.label == "Process this invoice").click().run()
    assert not app.exception, app.exception
    rendered = _visible(app)
    assert "This run used Replay (recorded Grok)." in rendered
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


def test_approve_and_pay_shapes_stubbed_success_and_refusal(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    seen = []

    def backend(state, *, reviewer, note, amount, ledger_path=None, log_path=None, demo=False):
        seen.append(
            {
                "filename": state.get("filename"),
                "reviewer": reviewer,
                "note": note,
                "amount": amount,
                "ledger": ledger_path,
                "demo": demo,
            }
        )
        if note == "refuse":
            return {
                "result": "held",
                "reason_code": "NOT_PAYABLE",
                "reason": "A reject is never paid.",
                "payment_status": "not_paid",
                "pipeline_outcome": "reject",
                "pipeline_findings": [],
                "invoice": "rejected.txt",
                "record": {"result": "held", "reason_code": "NOT_PAYABLE", "reason": "A reject is never paid."},
            }
        return {
            "result": "paid",
            "reason_code": None,
            "reason": "Reviewer approved this hold for payment.",
            "payment_status": "paid",
            "pipeline_outcome": "hold",
            "pipeline_findings": [],
            "invoice": "held.txt",
            "record": {"result": "paid", "payment_status": "paid"},
        }

    monkeypatch.setattr(_approve_module(), "approve_and_pay", backend, raising=False)
    state = {"filename": "held.txt", "outcome": "hold"}
    paid = approve_and_pay(
        state, reviewer="Ada", note="release it", amount="500.00", demo=True, ledger_path=ROOT / "payments.db",
    )
    refused = approve_and_pay(
        state, reviewer="Ada", note="refuse", amount="500.00", demo=True,
    )
    assert paid["status"] == "paid"
    assert paid["result"] == "paid"
    assert paid["payment_status"] == "paid"
    assert paid["reason"] == "Reviewer approved this hold for payment."
    assert refused["status"] == "refused"
    assert refused["result"] == "held"
    assert refused["reason_code"] == "NOT_PAYABLE"
    assert refused["reason"] == "A reject is never paid."
    assert refused["payment_status"] == "not_paid"
    assert seen[0]["demo"] is True
    assert seen[0]["ledger"] == ROOT / "payments.db"
    assert seen[0]["amount"] == "500.00"
    assert seen[0]["reviewer"] == "Ada"
    kind, text = pay_message(
        {"file": "held.txt", "amount": "500.00", "currency": "USD"}, paid,
    )
    assert kind == "success"
    assert text == "Paid held.txt. Payment status: Paid. 500.00 USD."
    error_kind, error_text = pay_message({"file": "rejected.txt"}, refused)
    assert error_kind == "error"
    assert error_text == "NOT_PAYABLE. A reject is never paid."
    assert "null" not in text.lower()
    assert "None" not in text


def test_missing_backend_refuses_without_writing_a_ledger(monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    module = _approve_module()
    if hasattr(module, "approve_and_pay"):
        monkeypatch.delattr(module, "approve_and_pay")
    real = ROOT / "payments.db"
    cwd_ledger = tmp_path / "payments.db"
    before = real.read_bytes() if real.exists() else None
    monkeypatch.chdir(tmp_path)
    result = approve_and_pay(
        {"filename": "held.txt", "outcome": "hold"},
        reviewer="Ada",
        note="release it",
        amount="500.00",
        demo=True,
        ledger_path=real,
    )
    assert result["status"] == "refused"
    assert result["result"] == "held"
    assert result["reason"] == BACKEND_UNAVAILABLE
    assert result["payment"] is None
    assert not cwd_ledger.exists()
    assert (real.read_bytes() if real.exists() else None) == before


def test_adapter_passes_the_loaded_result_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    entry = _review_hold("held.txt", "500.00", "USD", "VENDOR_RENAMED", "The vendor name says formerly.")
    entry["state"]["critique_concerns"] = ["new bank account"]
    path = tmp_path / "review_run.json"
    save_run(path, [entry])
    loaded = load_run(path)[0]
    original = saved_result(loaded)
    assert original is loaded["state"]
    assert original is not invoice_view(loaded)
    before = json.loads(json.dumps(original))
    seen = []

    def backend(state, *, reviewer, note, amount, ledger_path=None, log_path=None, demo=False):
        seen.append({"state": state, "amount": amount, "demo": demo})
        return {
            "result": "held",
            "reason_code": "NOT_PAYABLE",
            "reason": "held for the test",
            "payment_status": "not_paid",
            "critique_concerns": state.get("critique_concerns"),
        }

    monkeypatch.setattr(_approve_module(), "approve_and_pay", backend, raising=False)
    total = extracted_total(original)
    shaped = approve_and_pay(
        original, reviewer="Ada", note="Same company.", amount=total, demo=True,
    )
    assert seen[0]["state"] is original
    assert seen[0]["amount"] is original["invoice"]["total"]
    assert seen[0]["demo"] is True
    assert loaded["state"] == before
    assert shaped["critique_concerns"] == ["new bank account"]
    assert concern_text(None) == ""
    assert concern_text(shaped["critique_concerns"]) == "Critique concerns: new bank account"
    kind, text = pay_message(invoice_view(loaded), shaped)
    assert kind == "error"
    assert text == "NOT_PAYABLE. held for the test Critique concerns: new bank account"
    assert invoice_view({"state": {"filename": "held.txt", "outcome": "hold"}})["critique_concerns"] == []


def test_loaded_run_without_a_record_is_refused(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert Path.cwd().resolve() == ROOT.resolve()
    assert "chdir" not in (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert "chdir" not in (ROOT / "ui" / "runner.py").read_text(encoding="utf-8")
    state = saved_result(_review_hold("northwind.txt", "500.00", "USD", "VENDOR_RENAMED", "formerly"))
    missing_ledger = approve_and_pay(
        state, reviewer="Ada", note="Same company.", amount=extracted_total(state), demo=True,
    )
    assert missing_ledger["result"] == "held"
    assert missing_ledger["reason_code"] == "NOT_PAYABLE"
    assert missing_ledger["reason"] == "A ledger is required."
    ledger = temporary_ledger()
    refused = approve_and_pay(
        state, reviewer="Ada", note="Same company.", amount=extracted_total(state),
        ledger_path=ledger, demo=True,
    )
    assert refused["result"] == "held"
    assert refused["reason_code"] == "NOT_PAYABLE"
    assert refused["reason"] == "No pipeline record for this invoice."
    assert pay_message(invoice_view({"state": state}), refused)[1] == (
        "NOT_PAYABLE. No pipeline record for this invoice."
    )
    from decision.outcome import SEND_BACK_CAP_MESSAGE

    capped = _review_hold("capped.txt", "500.00", "USD", "AUDIT_ESCALATED", SEND_BACK_CAP_MESSAGE)
    assert can_pay(invoice_view(capped)) is False
    assert "SEND_BACK_CAP_MESSAGE" not in (ROOT / "ui" / "app.py").read_text(encoding="utf-8")


def test_replay_process_writes_a_release_record_and_can_pay(db_path, monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert Path.cwd().resolve() == ROOT.resolve()
    releases = tmp_path / "releases"
    monkeypatch.setenv("INVOICE_RELEASE_DIR", str(releases))
    logs = ROOT / "logs"
    before = {path.name for path in logs.glob("*.release.json")} if logs.is_dir() else set()
    real = ROOT / "payments.db"
    pay_before = real.read_bytes() if real.exists() else None
    ledger = temporary_ledger()
    assert not is_real_payments_db(ledger)
    held = run_one(
        ROOT / "data" / "invoices" / "invoice_1012.txt", db_path, replay=SAMPLES, ledger_path=ledger,
    )
    state = saved_result(held)
    assert held["llm"] == "replay"
    assert state["outcome"] == "hold"
    assert Decimal(str(extracted_total(state))) == Decimal("9975.00")
    written = list(releases.glob("*.release.json"))
    assert len(written) == 1
    record = json.loads(written[0].read_text(encoding="utf-8"))
    assert record["record"] == "release"
    assert Decimal(str(record["invoice"]["total"])) == Decimal("9975.00")
    after = {path.name for path in logs.glob("*.release.json")} if logs.is_dir() else set()
    assert after == before
    assert held["ledger_path"] == str(ledger)
    assert ledger.is_file()
    paid = approve_and_pay(
        state, reviewer="Ada", note="Same company.", amount=extracted_total(state),
        ledger_path=ledger, demo=True,
    )
    assert paid["result"] == "paid"
    assert paid["payment_status"] == "paid"
    assert paid["payment"]["reviewer"] == "Ada"
    assert paid["payment"]["time"]
    assert paid["payment"]["amount"]
    assert paid["payment"]["currency"] == "USD"
    again = approve_and_pay(
        state, reviewer="Ada", note="Same company.", amount=extracted_total(state),
        ledger_path=ledger, demo=True,
    )
    assert again["result"] == "held"
    assert again["reason_code"] == "NOT_PAYABLE"
    number = state["invoice"]["invoice_number"]
    assert again["reason"] == f"Invoice {number} from this vendor is already paid."
    rejected = run_one(
        ROOT / "data" / "invoices" / "invoice_1002.txt", db_path, replay=SAMPLES, ledger_path=ledger,
    )
    assert saved_result(rejected)["outcome"] == "reject"
    unpaid = approve_and_pay(
        saved_result(rejected),
        reviewer="Ada",
        note="Stock is short.",
        amount=extracted_total(saved_result(rejected)),
        ledger_path=ledger,
        demo=True,
    )
    assert unpaid["result"] == "held"
    assert unpaid["reason_code"] == "NOT_PAYABLE"
    assert unpaid["reason"] == "A reject is never paid."
    assert unpaid["payment_status"] == "not_paid"
    assert (real.read_bytes() if real.exists() else None) == pay_before
    assert not (tmp_path / "payments.db").exists()
    ledger.unlink(missing_ok=True)


def test_pay_action_is_only_for_review_holds():
    payable = invoice_view(
        _review_hold("held.txt", "1500.00", "USD", "VENDOR_RENAMED", "The vendor name says formerly.")
    )
    blocked = invoice_view(
        _review_hold("euros.txt", "80.00", "EUR", "NON_USD", "Non-USD currency (EUR).")
    )
    reject = invoice_view(_entry("reject", "80.00", "USD", 1, "rejected.txt"))
    approved = invoice_view(_entry("approve", "10.00", "USD", 1, "paid.txt"))
    assert can_pay(payable) is True
    assert can_pay(blocked) is False
    assert pay_block(blocked) == "NON_USD is not a review hold and is never paid."
    assert can_pay(reject) is False
    assert can_pay(approved) is False
    assert confirm_label(payable) == "Confirm payment of 1,500.00 USD for held.txt"
    queued = holds(
        [
            _review_hold("held.txt", "1500.00", "USD", "VENDOR_RENAMED", "formerly"),
            _entry("reject", "80.00", "USD", 1, "rejected.txt"),
            _review_hold("euros.txt", "80.00", "EUR", "NON_USD", "euros"),
        ]
    )
    assert [view["file"] for view in queued] == ["held.txt", "euros.txt"]


def test_replay_run_does_not_write_payments_db(db_path, monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    real = ROOT / "payments.db"
    before = real.read_bytes() if real.exists() else None
    created = []
    original = temporary_ledger

    def spy():
        path = original()
        created.append(path)
        return path

    monkeypatch.setattr("ui.runner.temporary_ledger", spy)
    entry = run_one(ROOT / "data" / "invoices" / "invoice_1010.txt", db_path, replay=SAMPLES)
    assert entry["state"]["outcome"] == "approve"
    assert entry["llm"] == "replay"
    assert created
    ledger = created[0]
    assert entry["ledger_path"] == str(ledger)
    assert ledger.is_file()
    assert ledger.name != "payments.db"
    assert not is_real_payments_db(ledger)
    assert not (tmp_path / "payments.db").exists()
    assert (real.read_bytes() if real.exists() else None) == before


def test_hold_queue_pay_requires_reviewer_note_and_confirm(monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert Path.cwd().resolve() == ROOT.resolve()
    real = ROOT / "payments.db"
    before = real.read_bytes() if real.exists() else None
    reviews = tmp_path / "hold_reviews.json"
    monkeypatch.setattr("ui.reviews.REVIEW_PATH", reviews)
    payable = _review_hold(
        "northwind.txt", "500.00", "USD", "VENDOR_RENAMED", "The vendor name says formerly.",
    )
    refused = _review_hold(
        "harbor.txt", "890.25", "EUR", "VENDOR_RENAMED", "The vendor name says formerly.",
    )
    blocked = _review_hold("blocked.txt", "40.00", "USD", "NON_USD", "Non-USD currency (USD).")
    reject = _entry("reject", "80.00", "USD", 1.0, "rejected.txt")
    reject["llm"] = "replay"
    run_path = tmp_path / "review_run.json"
    save_run(run_path, [payable, refused, blocked, reject])

    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=90)
    app.run()
    assert not app.exception, app.exception
    mode = next(radio for radio in app.sidebar.radio if radio.label == "Mode")
    assert mode.value == "Replay (recorded Grok)"
    run_file = next(item for item in app.text_input if item.label == "Run file")
    run_file.set_value(str(run_path)).run()
    next(button for button in app.button if button.label == "Load run file").click().run()
    assert not app.exception, app.exception

    for screen in ("Results", "Invoice detail", "Metrics", "Run or load"):
        _show(app, screen)
        assert not _has_pay(app), screen
    _show(app, "Hold queue")
    assert not app.exception, app.exception
    visible = _visible(app)
    assert "northwind.txt" in visible
    assert "harbor.txt" in visible
    assert "blocked.txt" in visible
    assert "rejected.txt" not in visible
    assert "NON_USD is not a review hold and is never paid." in visible
    assert len(_pay_buttons(app)) == 2
    assert app.radio(key="choice-0-northwind.txt").value is None
    confirm = app.checkbox(key="pay-confirm-0-northwind.txt")
    assert confirm.value is False
    assert confirm.label == "Confirm payment of 500.00 USD for northwind.txt"
    assert not app.text_input(key="pay-reviewer-0-northwind.txt").value
    assert not any(box.label.startswith("Confirm payment") and "blocked.txt" in box.label for box in app.checkbox)

    app.radio(key="choice-0-northwind.txt").set_value("approve")
    app.text_area(key="note-0-northwind.txt").set_value("Finance confirmed the dollars.")
    _submit(app, "Save review")
    saved = _visible(app)
    assert "Recorded approve for northwind.txt." in saved
    assert "nothing was paid" in saved
    assert "Pipeline outcome is still hold." in saved
    assert not (tmp_path / "payments.db").exists()

    _submit_pay(app, "northwind.txt", 0)
    missing = _visible(app)
    assert "A reviewer name is required." in missing
    assert "A note is required." in missing
    assert "Confirm payment of 500.00 USD for northwind.txt before paying." in missing

    app.text_input(key="pay-reviewer-0-northwind.txt").set_value("A")
    app.text_area(key="pay-note-0-northwind.txt").set_value("x")
    app.checkbox(key="pay-confirm-0-northwind.txt").set_value(True)
    _submit_pay(app, "northwind.txt", 0)
    short = _visible(app)
    assert "A reviewer name is required." in short
    assert "A note is required." in short
    assert "Payment status:" not in short

    app.text_input(key="pay-reviewer-0-northwind.txt").set_value("Ada")
    app.text_area(key="pay-note-0-northwind.txt").set_value("Release this hold.")
    app.checkbox(key="pay-confirm-0-northwind.txt").set_value(False)
    _submit_pay(app, "northwind.txt", 0)
    unconfirmed = _visible(app)
    assert "Confirm payment of 500.00 USD for northwind.txt before paying." in unconfirmed
    assert "A reviewer name is required." not in unconfirmed
    assert "A note is required." not in unconfirmed
    assert "Payment status:" not in unconfirmed

    app.text_input(key="pay-reviewer-0-northwind.txt").set_value("Ada")
    app.text_area(key="pay-note-0-northwind.txt").set_value("Same company.")
    app.checkbox(key="pay-confirm-0-northwind.txt").set_value(True)
    _submit_pay(app, "northwind.txt", 0)
    paid = _visible(app)
    assert "NOT_PAYABLE. No pipeline record for this invoice." in paid
    assert "Paid northwind.txt." not in paid
    assert not app.exception
    assert (real.read_bytes() if real.exists() else None) == before


def test_paid_status_after_approve_and_pay(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=90)
    app.run()
    _process_path(app, "data/invoices/invoice_1012.txt")
    _show(app, "Hold queue")
    app.text_input(key="pay-reviewer-0-invoice_1012.txt").set_value("Ada")
    app.text_area(key="pay-note-0-invoice_1012.txt").set_value("Same company.")
    app.checkbox(key="pay-confirm-0-invoice_1012.txt").set_value(True)
    _submit_pay(app, "invoice_1012.txt", 0)
    queued = _visible(app)
    assert "Paid" in queued
    assert "9,975.00" in queued
    assert "Not paid" not in queued
    assert "Payment did not happen" not in queued
    assert "Already paid after review." in queued
    assert not _pay_buttons(app)
    _show(app, "Results")
    assert "Paid after review" in _visible(app)
    _show(app, "Metrics")
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Hold"] == "0"
    assert metrics["Dollars held"] == "0.00 USD"
    assert metrics["Released after review"] == "1"
    assert metrics["Dollars released after review"] == "9,975.00 USD"
    _show(app, "Invoice detail")
    detail = _visible(app)
    assert "Paid" in detail
    assert "9,975.00" in detail
    assert "Not paid" not in detail
    assert "Payment did not happen" not in detail
    assert "Payment happened." in detail
    view = invoice_view(_session_entry(app, "invoice_1012.txt"))
    text = payment_detail_text(view)
    assert view["reason"]
    assert view["reason"] not in text
    assert "formerly" not in text
    assert "Reviewer Ada" in text
    assert "Amount 9,975.00 USD" in text
    assert "Time " in text
    assert "Authorization " in text
    assert "detail" not in (view.get("payment") or {})
    assert "Reviewer Ada" in detail
    assert "Authorization " in detail


def test_payment_detail_uses_facts_and_skips_the_hold_reason():
    entry = _review_hold(
        "northwind.txt", "500.00", "USD", "VENDOR_RENAMED", "The vendor name says formerly.",
    )
    entry["state"]["payment"]["detail"] = "The vendor name says formerly."
    unpaid = invoice_view(entry)
    assert unpaid["payment_status"] == "not_paid"
    assert payment_detail_text(unpaid) == ""
    entry["state"]["payment"] = {
        "status": "paid",
        "detail": "The vendor name says formerly.",
        "amount": "500.00",
        "currency": "USD",
        "reviewer": "Ada",
        "time": "2026-11-02T00:00:00Z",
        "authorization": "data/invoices/northwind.txt:abc",
    }
    paid = invoice_view(entry)
    text = payment_detail_text(paid)
    assert "formerly" not in text
    assert text == (
        "Amount 500.00 USD. Reviewer Ada. Time 2026-11-02T00:00:00Z. "
        "Authorization data/invoices/northwind.txt:abc."
    )


def test_shared_ledger_changes_later_invoices(db_path, monkeypatch):
    """One ledger for the folder. Each invoice alone is the README sample score."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    ledger = temporary_ledger()
    shared = [
        run_one(path, db_path, replay=SAMPLES, ledger_path=ledger) for path in invoice_files()
    ]
    assert summarize(shared)["counts"] == {"approve": 6, "reject": 9, "hold": 5}
    duplicate = invoice_view(next(
        entry for entry in shared if entry["state"]["filename"] == "invoice_1011.txt"
    ))
    assert duplicate["outcome"] == "hold"
    assert duplicate["reason_code"] == "DUPLICATE_INVOICE"
    alone = invoice_view(run_one(
        ROOT / "data" / "invoices" / "invoice_1011.txt", db_path, replay=SAMPLES,
    ))
    assert alone["outcome"] == "approve"
    assert alone["payment_status"] == "paid"
    revised = invoice_view(next(
        entry for entry in shared if entry["state"]["filename"] == "invoice_1004_revised.json"
    ))
    assert revised["outcome"] == "hold"
    assert revised["reason_code"] == "DUPLICATE_INVOICE"
    revised_alone = invoice_view(run_one(
        ROOT / "data" / "invoices" / "invoice_1004_revised.json", db_path, replay=SAMPLES,
    ))
    assert revised_alone["reason_code"] == "REVISED_INVOICE"
    paid = invoice_view(next(
        entry for entry in shared if entry["state"]["filename"] == "invoice_1001.txt"
    ))
    facts = payment_detail_text(paid)
    assert "Amount 5,000.00 USD" in facts
    assert "Time " in facts
    assert "Authorization " in facts
    assert "mock payment accepted" not in facts
    assert paid["reason"] not in facts
    held = invoice_view(next(
        entry for entry in shared if entry["state"]["filename"] == "invoice_1012.txt"
    ))
    assert held["payment_status"] == "not_paid"
    assert payment_detail_text(held) == ""
    assert held["reason"] in held["payment"]["detail"]
    note = shared_ledger_note()
    assert "6 approve, 9 reject, and 5 hold" in note
    assert "7 approve, 9 reject, and 4 hold" in note
    assert "6 approve" not in (ROOT / "ui" / "present.py").read_text(encoding="utf-8")
    ledger.unlink(missing_ok=True)


def test_pay_form_hides_when_the_ledger_already_paid_the_invoice(db_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=90)
    app.run()
    assert os.environ.get("INVOICE_RELEASE_DIR")
    ledger = temporary_ledger()
    pdf = run_one(
        ROOT / "data" / "invoices" / "invoice_1012.pdf",
        db_path, replay=SAMPLES, ledger_path=ledger,
    )
    text = run_one(
        ROOT / "data" / "invoices" / "invoice_1012.txt",
        db_path, replay=SAMPLES, ledger_path=ledger,
    )
    assert invoice_view(pdf)["outcome"] == "hold"
    assert invoice_view(text)["outcome"] == "hold"
    before = ledger.read_bytes()
    assert paid_ledger_row(ledger, invoice_view(pdf)) is None
    assert ledger.read_bytes() == before
    app.session_state["entries"] = [pdf, text]
    app.run()
    _show(app, "Hold queue")
    assert len(_pay_buttons(app)) == 2
    app.text_input(key="pay-reviewer-0-invoice_1012.pdf").set_value("Ada")
    app.text_area(key="pay-note-0-invoice_1012.pdf").set_value("Same company.")
    app.checkbox(key="pay-confirm-0-invoice_1012.pdf").set_value(True)
    _submit_pay(app, "invoice_1012.pdf", 0)
    queued = _visible(app)
    assert "Paid invoice_1012.pdf." in queued
    assert "Already paid after review." in queued
    assert not _pay_buttons(app)
    other = invoice_view(next(
        entry for entry in app.session_state["entries"]
        if entry["state"]["filename"] == "invoice_1012.txt"
    ))
    row = paid_ledger_row(ledger, other)
    assert row is not None
    assert row["source_file"] == "invoice_1012.pdf"
    assert row["status"] == "paid"
    assert already_paid_notice(other, row) in queued
    assert can_pay(other) is True
    ledger.unlink(missing_ok=True)


def test_upload_uses_the_existing_run_path(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    known = (ROOT / "data" / "invoices" / "invoice_1001.txt").read_bytes()
    assert stage_upload("invoice_1001.txt", known) == ROOT / "data" / "invoices" / "invoice_1001.txt"
    assert stage_upload("../../invoice_1001.txt", known) == ROOT / "data" / "invoices" / "invoice_1001.txt"
    staged = stage_upload("../invoice_9001.txt", b"not a recorded invoice\n")
    assert staged.name == "invoice_9001.txt"
    assert staged.is_file()
    assert not str(staged.resolve()).startswith(str((ROOT / "data").resolve()))

    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=90)
    app.run()
    uploader = next(item for item in app.file_uploader if item.label == "Upload an invoice")
    uploader.upload("invoice_9001.txt", b"not a recorded invoice\n").run()
    assert not app.exception, app.exception
    missing = _visible(app)
    assert "invoice_9001.txt is not in the recordings. Processing a new invoice needs XAI_API_KEY." in missing
    assert not app.session_state["entries"]

    uploader = next(item for item in app.file_uploader if item.label == "Upload an invoice")
    uploader.upload("invoice_1001.txt", known).run()
    assert not app.exception, app.exception
    entry = _session_entry(app, "invoice_1001.txt")
    assert entry["state"]["outcome"] == "approve"
    assert entry["state"]["payment"]["status"] == "paid"
    assert entry["llm"] == "replay"
    shown = _visible(app)
    assert "Processed invoice_1001.txt." in shown
    assert shared_ledger_note() in shown


def test_run_screen_explains_the_shared_ledger_counts(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=120)
    app.run()
    assert not app.exception, app.exception
    assert shared_ledger_note() in _visible(app)
    next(button for button in app.button if button.label == "Process data/invoices").click().run()
    assert not app.exception, app.exception
    shown = _visible(app)
    assert "Processed 20 invoices." in shown
    assert "approve 6, reject 9, hold 5" in shown
    assert shared_ledger_note() in shown
    assert "7 approve, 9 reject, and 4 hold" in shown


def test_processing_the_same_invoice_twice_matches(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=120)
    app.run()
    _process_path(app, "data/invoices/invoice_1001.txt")
    first = _session_entry(app, "invoice_1001.txt")
    assert first["state"]["outcome"] == "approve"
    assert first["state"]["payment"]["status"] == "paid"
    _process_path(app, "data/invoices/invoice_1001.txt")
    shown = _visible(app)
    assert "PAYMENT_REFUSED" not in shown
    assert "already used" not in shown
    second = _session_entry(app, "invoice_1001.txt")
    assert second["state"]["outcome"] == first["state"]["outcome"]
    assert second["state"]["payment"]["status"] == first["state"]["payment"]["status"]
    assert second["ledger_path"] != first["ledger_path"]
    assert not is_real_payments_db(second["ledger_path"])
    body = json.dumps(second["state"])
    assert "PAYMENT_REFUSED" not in body
    assert "authorization key was already used" not in body


def test_replay_mode_lists_recordings_and_runs_without_a_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    names = [path.name for path in recording_files()]
    assert {"ordered.jsonl", "samples.jsonl", "unseen.jsonl"} <= set(names)
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=120)
    app.run()
    mode = next(radio for radio in app.sidebar.radio if radio.label == "Mode")
    assert "Replay (recorded Grok)" in mode.options
    mode.set_value("Replay (recorded Grok)").run()
    assert not app.exception, app.exception
    visible = _visible(app)
    assert "Replay of a recorded Grok run" in visible
    assert "no live model calls" in visible
    recording = next(item for item in app.selectbox if item.label == "Recording")
    assert "samples.jsonl" in recording.options
    assert "ordered.jsonl" in recording.options
    assert "unseen.jsonl" in recording.options
    _process_path(app, "data/invoices/invoice_1002.txt")
    played = _session_entry(app, "invoice_1002.txt")
    assert played["llm"] == "replay"
    assert played["state"]["outcome"] == "reject"
    assert played["state"]["reason_code"] == "STOCK_SHORTFALL"
    assert not is_real_payments_db(played["ledger_path"])
    assert os.environ.get("XAI_MODE") != "replay"
    assert not os.environ.get("XAI_API_KEY", "").strip()
    assert "No XAI_API_KEY set. Using replay" in visible


def test_replay_stops_before_running_an_unrecorded_invoice(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    created = []
    monkeypatch.setattr("ui.runner.temporary_ledger", lambda: created.append(1))
    ran = []
    monkeypatch.setattr("ui.runner.run_invoice", lambda *args, **kwargs: ran.append(args))
    app = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=120)
    app.run()
    _process_path(app, "data/extra_invoices/invoice_2108.txt")
    shown = _visible(app)
    assert "invoice_2108.txt is not in the recordings. Processing a new invoice needs XAI_API_KEY." in shown
    assert "replay miss" not in shown.lower()
    assert not app.session_state["entries"]
    assert not ran
    assert not created


def test_load_run_skips_release_records(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    log = folder / "invoice_1002.txt.json"
    log.write_text(
        json.dumps([
            {
                "record": "stage",
                "stage": "reject_log",
                "invoice_file": "invoice_1002.txt",
                "result": {"outcome": "reject", "reason": "stock is short"},
            },
            {
                "record": "result",
                "file": "invoice_1002.txt",
                "outcome": "reject",
                "reason_code": "STOCK_SHORTFALL",
                "action": "Send it back.",
                "payment_status": "not_paid",
            },
        ]),
        encoding="utf-8",
    )
    release = folder / "data%2Finvoices%2Finvoice_1002.txt.release.json"
    release.write_text(
        json.dumps({"record": "release", "file": "data/invoices/invoice_1002.txt", "outcome": "reject"}),
        encoding="utf-8",
    )
    loaded = load_run(folder)
    assert len(loaded) == 1
    assert loaded[0]["state"]["filename"] == "invoice_1002.txt"
    assert loaded[0]["state"]["outcome"] == "reject"
    assert loaded[0]["state"]["reason_code"] == "STOCK_SHORTFALL"
    globbed = load_run(folder / "*.json")
    assert len(globbed) == 1
    assert globbed[0]["state"]["outcome"] == "reject"
    assert globbed[0]["state"]["filename"] == "invoice_1002.txt"


def test_ensure_db_honors_inventory_db(monkeypatch, tmp_path):
    target = tmp_path / "custom-inventory.db"
    monkeypatch.setenv("INVENTORY_DB", str(target))
    found = ensure_db()
    assert found == target
    assert target.is_file()


def _process_path(app, path: str) -> None:
    app.text_input(key="invoice_path").set_value(path).run()
    next(button for button in app.button if button.label == "Process this invoice").click().run()
    assert not app.exception, app.exception


def _session_entry(app, filename: str) -> dict:
    found = [
        entry for entry in app.session_state["entries"]
        if (entry.get("state") or {}).get("filename") == filename
    ]
    assert found, filename
    return found[-1]


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


def _approve_module():
    import importlib

    return importlib.import_module("pipeline.approve_hold")


def _submit_pay(app, filename: str, index: int) -> None:
    label = "Approve and pay"
    matches = [
        button for button in app.form_submit_button
        if button.label == label and filename in (button.key or "")
    ]
    button = matches[0] if matches else _pay_buttons(app)[index]
    button.click().run()
    assert not app.exception, app.exception


def _has_pay(app) -> bool:
    labels = [button.label for button in app.button]
    boxes = [box.label for box in app.checkbox]
    return any(label == "Approve and pay" for label in labels) or any(
        label.startswith("Confirm payment") for label in boxes
    )


def _pay_buttons(app):
    return [button for button in app.form_submit_button if button.label == "Approve and pay"]


def _submit(app, label: str) -> None:
    next(button for button in app.form_submit_button if button.label == label).click().run()
    assert not app.exception, app.exception


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


def _review_hold(filename, total, currency, code, message):
    entry = _entry("hold", total, currency, 0.2, filename)
    entry["llm"] = "replay"
    entry["state"]["reason_code"] = code
    entry["state"]["action"] = "Confirm with the vendor."
    entry["state"]["raw_text"] = f"{filename}\nWidgetA\n"
    entry["state"]["findings"] = [{"code": code, "message": message, "severity": "warning"}]
    entry["state"]["invoice"] = {
        "vendor": "Northwind Supply",
        "invoice_number": "INV-900",
        "invoice_date": "2026-04-15",
        "due_date": "2026-05-15",
        "currency": currency,
        "subtotal": total,
        "total": total,
        "line_items": [{
            "item": "WidgetA",
            "description": "WidgetA",
            "quantity": "2",
            "unit_price": "250.00",
            "amount": total,
        }],
        "source": {
            "path": filename,
            "filename": filename,
            "format": "txt",
            "parser": "grok",
            "raw_text": "WidgetA",
        },
    }
    return entry


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
