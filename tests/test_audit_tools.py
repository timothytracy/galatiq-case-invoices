"""Audit's read-only tools. No network. The model is scripted."""

import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from db import create_database, load_stock
from graph import route_after_audit, run_invoice
from decision.outcome import result_line, tool_calls_of
from pipeline import audit
from pipeline.audit import LEDGER_LINE, TOOLS_LINE
from pipeline.payment import payment
from pipeline.tools import audit_registry
from decision.outcome import HOLD_CODES, REJECT_CODES, WARNING_CODES
from decision.wording import ACTION, RULE_TEXT
from tests.fakes import FakeClock, ScriptedToolModel, ZeroRandom, task_of
from tests.test_pipeline import extraction, scripted, write_invoice
from tests.test_tool_loop import _payable_state, text_step, tool_step

# Process codes, and any code with no rule text, stay out of the enum.
_PROCESS = {
    "CRITIQUE_HOLD",
    "AUDIT_ESCALATED",
    "SYSTEM_ERROR",
    "BAD_REASONING",
    "AUDIT_NOTE",
}
_RECORD = {
    "agent",
    "round",
    "attempt",
    "call_id",
    "name",
    "arguments",
    "result",
    "error",
    "latency_ms",
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "inventory.db"
    create_database(path)
    return path


def _block_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is disabled in tests")

    monkeypatch.setattr(socket, "create_connection", explode)


def _verdict(verdict: str) -> str:
    feedback = ["due_date is wrong"] if verdict == "SEND_BACK" else []
    return json.dumps(
        {"verdict": verdict, "feedback": feedback, "notes": verdict, "cited_codes": []}
    )


def _run(steps, db_path, monkeypatch):
    _block_network(monkeypatch)
    model = ScriptedToolModel(steps)
    out = audit(_payable_state(), model, db_path, FakeClock(), ZeroRandom(), 5)
    return out, model


def _audit_log(out):
    return next(row for row in out["logs"] if row["stage"] == "audit")


def _registry(db_path):
    state = _payable_state()
    return audit_registry(state.invoice, dict(load_stock(db_path)), state.raw_text), state


def test_audit_pass_with_tools_pays(db_path, monkeypatch):
    out, model = _run(
        [
            tool_step(("c1", "lookup_item", json.dumps({"item": "WidgetA"}))),
            tool_step(("c2", "recompute_totals", "{}")),
            tool_step(("c3", "get_policy_rule", json.dumps({"code": "STOCK_SHORTFALL"}))),
            text_step(_verdict("PASS")),
        ],
        db_path,
        monkeypatch,
    )
    assert out["outcome"] == "approve"
    assert out["audit_verdict"] == "PASS"
    assert out["authorization"] is not None
    assert out["authorization"].amount == Decimal("500")
    calls = _audit_log(out)["result"]["tool_calls"]
    assert [row["name"] for row in calls] == ["lookup_item", "recompute_totals", "get_policy_rule"]
    assert all(set(row) == _RECORD for row in calls)
    assert all(row["agent"] == "audit" and row["error"] is None and "repeat" not in row for row in calls)
    assert calls[0]["result"] == {
        "item": "WidgetA",
        "matched_item": "WidgetA",
        "available": 15,
        "requested_on_invoice": "2",
        "covered": True,
        "reference_price": "250.00",
        "currency": "USD",
    }
    assert calls[1]["result"] == {
        "reconciles": True,
        "printed_total": "500.00",
        "mismatches": [],
    }
    assert calls[2]["result"] == {
        "code": "STOCK_SHORTFALL",
        "class": "reject",
        "rule": RULE_TEXT["STOCK_SHORTFALL"],
        "action": ACTION["STOCK_SHORTFALL"],
    }
    assert TOOLS_LINE in model.tool_requests[0]["messages"][0]["content"]
    assert "verified_by_code" in model.tool_requests[0]["messages"][1]["content"]
    _tools, state = _registry(db_path)
    assert model.tool_requests[0]["tools"] == audit_registry(
        state.invoice, dict(load_stock(db_path)), state.raw_text
    ).schemas()
    stopped = _payable_state().model_copy(
        update={
            "outcome": out["outcome"],
            "authorization": out["authorization"],
            "audit_verdict": out["audit_verdict"],
            "findings": out["findings"],
            "logs": out["logs"],
        }
    )
    assert route_after_audit(stopped) == "payment"
    paid = payment(stopped)
    assert paid["payment"].status == "paid"
    line = result_line(Path("x.txt"), out)
    assert line["tool_calls"] == tool_calls_of(out) == calls
    assert "tool_cap_hit" not in line
    assert "forced_final_turn" not in line
    assert "tool_cap_hit" not in _audit_log(out)["result"]
    assert "payment" not in [row["stage"] for row in out["logs"]]


def test_audit_send_back_with_tools_does_not_pay(db_path, monkeypatch):
    out, _model = _run(
        [
            tool_step(("c1", "lookup_item", json.dumps({"item": "WidgetA"}))),
            text_step(_verdict("SEND_BACK")),
        ],
        db_path,
        monkeypatch,
    )
    assert out["audit_verdict"] == "SEND_BACK"
    assert out["outcome"] is None
    assert out["authorization"] is None
    assert out["feedback"] == ["due_date is wrong"]
    calls = _audit_log(out)["result"]["tool_calls"]
    assert calls[0]["name"] == "lookup_item"
    assert calls[0]["result"]["covered"] is True
    stopped = _payable_state().model_copy(
        update={
            "outcome": out["outcome"],
            "audit_verdict": out["audit_verdict"],
            "authorization": out["authorization"],
            "agent_error": out["agent_error"],
            "logs": out["logs"],
        }
    )
    assert route_after_audit(stopped) == "ingestion"
    assert "payment" not in [row["stage"] for row in out["logs"]]


def test_audit_repeat_is_on_the_stage_log(db_path, monkeypatch):
    out, _model = _run(
        [
            tool_step(
                ("c1", "lookup_item", json.dumps({"item": "WidgetA"})),
                ("c2", "lookup_item", json.dumps({"item": "WidgetA"})),
            ),
            text_step(_verdict("PASS")),
        ],
        db_path,
        monkeypatch,
    )
    calls = _audit_log(out)["result"]["tool_calls"]
    assert "repeat" not in calls[0]
    assert calls[1]["repeat"] is True
    assert calls[1]["result"] == calls[0]["result"]
    assert calls[1]["call_id"] == "c2"
    assert out["authorization"] is not None


def test_lookup_does_not_correct_an_unknown_item(db_path):
    registry, state = _registry(db_path)
    folded = registry.call("lookup_item", json.dumps({"item": "widget a"}))
    assert folded["item"] == "widget a"
    assert folded["matched_item"] == "WidgetA"
    assert folded["available"] == 15
    assert folded["requested_on_invoice"] == "2"
    missing = registry.call("lookup_item", json.dumps({"item": "SuperGizmo"}))
    assert missing == {"error": "unknown item: SuperGizmo"}
    assert "Likely meant" not in missing["error"]
    fake = registry.call("lookup_item", json.dumps({"item": "FakeItem"}))
    assert fake["matched_item"] == "FakeItem"
    assert fake["available"] == 0
    assert fake["requested_on_invoice"] == "0"
    assert fake["reference_price"] is None
    assert fake["currency"] is None
    bad = state.invoice.model_copy(update={"total": Decimal("999")})
    broken = audit_registry(bad, dict(load_stock(db_path)), state.raw_text)
    mismatch = broken.call("recompute_totals", "{}")
    assert mismatch["reconciles"] is False
    assert mismatch["printed_total"] == "999.00"
    assert mismatch["mismatches"] == [
        "Printed total 999.00 does not equal 500.00 (subtotal + tax + shipping)."
    ]


def test_policy_enum_excludes_process_codes_and_rejects_an_invalid_code(db_path, monkeypatch):
    registry, _state = _registry(db_path)
    schema = next(
        tool for tool in registry.schemas() if tool["function"]["name"] == "get_policy_rule"
    )
    enum = schema["function"]["parameters"]["properties"]["code"]["enum"]
    assert _PROCESS.isdisjoint(enum)
    assert enum == [
        code
        for code in (*REJECT_CODES, *HOLD_CODES, *WARNING_CODES)
        if code not in _PROCESS and code in RULE_TEXT
    ]
    assert "NEAR_THRESHOLD" not in enum
    for code in ("SYSTEM_ERROR", "AUDIT_NOTE", "MADE_UP"):
        body = registry.call("get_policy_rule", json.dumps({"code": code}))
        assert body == {"error": "argument code must be one of the allowed values"}
    warning = registry.call("get_policy_rule", json.dumps({"code": "FRAUD_SIGNAL"}))
    assert warning["class"] == "warning"
    assert warning["rule"] == RULE_TEXT["FRAUD_SIGNAL"]
    assert warning["action"] is None

    out, _model = _run(
        [
            tool_step(("bad", "get_policy_rule", json.dumps({"code": "NOT_A_CODE"}))),
            text_step(_verdict("PASS")),
        ],
        db_path,
        monkeypatch,
    )
    logged = _audit_log(out)["result"]["tool_calls"][0]
    assert logged["error"] == "argument code must be one of the allowed values"
    assert logged["result"] is None
    assert out["outcome"] == "approve"


def test_audit_request_sends_tools(db_path, monkeypatch):
    _block_network(monkeypatch)
    model = ScriptedToolModel([text_step(_verdict("PASS"))])
    out = audit(_payable_state(), model, db_path, FakeClock(), ZeroRandom(), 3)
    assert out["outcome"] == "approve"
    assert model.requests == []
    assert len(model.tool_requests) == 1
    system = model.tool_requests[0]["messages"][0]["content"]
    assert TOOLS_LINE in system
    assert LEDGER_LINE in system
    names = [tool["function"]["name"] for tool in model.tool_requests[0]["tools"]]
    assert names == [
        "lookup_item",
        "recompute_totals",
        "get_policy_rule",
        "check_payment_ledger",
    ]
    assert "tool_calls" not in _audit_log(out)["result"]


def test_critique_sends_no_tools(tmp_path, db_path, monkeypatch):
    _block_network(monkeypatch)
    path = write_invoice(
        tmp_path, "big.txt", "WidgetA 15, WidgetB 10, GadgetX 5\nTotal: 12500.00\n"
    )
    body = extraction(
        line_items=[
            {
                "item": "WidgetA",
                "description": "WidgetA",
                "quantity": 15,
                "unit_price": 250,
                "amount": 3750,
                "note": None,
            },
            {
                "item": "WidgetB",
                "description": "WidgetB",
                "quantity": 10,
                "unit_price": 500,
                "amount": 5000,
                "note": None,
            },
            {
                "item": "GadgetX",
                "description": "GadgetX",
                "quantity": 5,
                "unit_price": 750,
                "amount": 3750,
                "note": None,
            },
        ],
        subtotal=12500,
        total=12500,
    )
    handler, counts = scripted(body)
    seen = {"plain": [], "tools": []}

    class Model:
        def request(self, messages, timeout=60):
            name = task_of(messages)
            seen["plain"].append(name)
            if name == "critique":
                assert TOOLS_LINE not in messages[0]["content"]
                assert "lookup_item" not in messages[0]["content"]
            return handler(messages)

        def request_tools(self, messages, tools, timeout=60):
            name = task_of(messages)
            seen["tools"].append(name)
            assert name == "audit"
            assert TOOLS_LINE in messages[0]["content"]
            assert [tool["function"]["name"] for tool in tools] == [
                "lookup_item",
                "recompute_totals",
                "get_policy_rule",
                "check_payment_ledger",
            ]
            return {"content": handler(messages), "tool_calls": []}

    result = run_invoice(
        path, db_path, Model(), clock=FakeClock(), rng=ZeroRandom(), max_attempts=3
    )
    assert result["outcome"] == "approve"
    assert counts["critique"] == 1
    assert "critique" in seen["plain"]
    assert seen["tools"] == ["audit"]
    critique_log = next(row for row in result["logs"] if row["stage"] == "critique")
    assert "tool_calls" not in critique_log["result"]
    paid = result["payment"]
    assert (paid.status if not isinstance(paid, dict) else paid["status"]) == "paid"
