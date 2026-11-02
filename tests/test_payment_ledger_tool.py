"""check_payment_ledger. Offline. The model is scripted. No network.

The tool is bound to the current invoice. A hit can hold. It cannot pay.
"""

import hashlib
import json
import socket
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from db import create_database, load_stock
from graph import route_after_audit
from ledger import paid_rows, record_payment
from ledger.scenario import run_scenario
from llm_client import TOOL_CALL_CAP
from pipeline import audit
from pipeline.payment import payment
from pipeline.tools import audit_registry
from decision.invoice_fields import show_money
from decision.policy import POLICY
from tests.fake_llm_eval import AS_OF, handler_for
from tests.fakes import FakeClock, ScriptedToolModel, ZeroRandom
from tests.test_audit_tools import _verdict
from tests.test_tool_loop import _payable_state, text_step, tool_step

# Tools-off audit messages for _payable_state at 2026-10-31, canonical JSON.
# Captured from main 22acca19. A new request key or prompt word changes it.
_MAIN_TOOLS_OFF_AUDIT = "5f9b40f0a389058955136ed1ffcfd4f3acdf9c6fc773f2c28670b86d82e0da3a"
_USER_KEYS = {
    "extracted",
    "decision",
    "findings",
    "critique_concerns",
    "raw_text",
    "verified_by_code",
}
ROOT = Path(__file__).resolve().parents[1]


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


def _numbered(number="INV-1001", vendor="Widgets Inc.", when="2026-01-15"):
    state = _payable_state()
    invoice = state.invoice.model_copy(
        update={"invoice_number": number, "vendor": vendor, "invoice_date": when}
    )
    return state.model_copy(update={"invoice": invoice})


def _registry(db_path, state, ledger):
    return audit_registry(
        state.invoice,
        dict(load_stock(db_path)),
        state.raw_text,
        ledger_path=ledger,
    )


def _pay(path, **overrides):
    fields = {
        "vendor": "Widgets Inc.",
        "invoice_number": "INV-9002",
        "revision": None,
        "total": Decimal("9600.00"),
        "currency": "USD",
        "invoice_date": "2026-01-10",
        "source_file": "other.txt",
        "raw_text": "other bill",
    }
    fields.update(overrides)
    assert record_payment(path, **fields) is True


def _clear(body):
    assert body == {
        "already_paid": False,
        "match_date": None,
        "match_total": None,
        "same_vendor_paid_in_window": "0.00",
        "this_total": "500.00",
        "combined": "500.00",
        "split_window_days": POLICY.split_window_days,
        "scrutiny_threshold": show_money(POLICY.scrutiny_threshold),
        "over_split_threshold": False,
    }


def _run(state, steps, db_path, monkeypatch, ledger):
    monkeypatch.setenv("XAI_TOOLS", "1")
    model = ScriptedToolModel(steps)
    out = audit(
        state,
        model,
        db_path,
        FakeClock(),
        ZeroRandom(),
        3,
        as_of=date(2026, 10, 31),
        ledger_path=ledger,
    )
    return out, model


def _carry(source, out):
    fields = {
        "outcome": out["outcome"],
        "reason_code": out["reason_code"],
        "action": out.get("action"),
        "agent_error": out.get("agent_error"),
        "findings": out["findings"],
        "logs": out["logs"],
        "attempts": out.get("attempts") or [],
    }
    if "audit_verdict" in out:
        fields["audit_verdict"] = out["audit_verdict"]
    if "authorization" in out:
        fields["authorization"] = out["authorization"]
    if "decision" in out:
        fields["decision"] = out["decision"]
    return source.model_copy(update=fields)


def _assert_not_paid(source, out):
    assert out.get("authorization") is None
    stopped = _carry(source, out)
    assert stopped.authorization is None
    assert route_after_audit(stopped) == "reject_log"
    assert payment(stopped)["payment"].status == "not_paid"
    assert "payment" not in [row["stage"] for row in out["logs"]]


def test_fresh_ledger_reports_no_prior_payment(db_path, tmp_path):
    state = _numbered()
    missing = tmp_path / "absent.db"
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    for ledger in (None, missing, empty):
        body = _registry(db_path, state, ledger).call("check_payment_ledger", "{}")
        _clear(body)
    assert not missing.exists()
    assert empty.read_bytes() == b""
    schema = next(
        tool
        for tool in _registry(db_path, state, None).schemas()
        if tool["function"]["name"] == "check_payment_ledger"
    )
    assert schema["function"]["parameters"] == {"type": "object", "properties": {}}
    assert "another vendor" in schema["function"]["description"]


def test_paid_invoice_is_a_hit(db_path, tmp_path):
    ledger = tmp_path / "payments.db"
    _pay(
        ledger,
        invoice_number="1001",
        total=Decimal("500.00"),
        invoice_date="2026-01-02",
        source_file="paid.txt",
        raw_text="already paid",
    )
    before = paid_rows(ledger)
    state = _numbered(number="INV 1001", vendor="widgets  inc.", when="2026-01-15")
    body = _registry(db_path, state, ledger).call("check_payment_ledger", "{}")
    assert body["already_paid"] is True
    assert body["match_date"] == "2026-01-02"
    assert body["match_total"] == "500.00"
    assert body["over_split_threshold"] is False
    assert paid_rows(ledger) == before


def test_split_window_total_and_scope(db_path, tmp_path):
    ledger = tmp_path / "payments.db"
    _pay(ledger, vendor="Other Co", invoice_number="INV-1001", total=Decimal("50000.00"))
    _pay(ledger, invoice_number="INV-9002", total=Decimal("9600.00"), invoice_date="2026-01-10")
    _pay(
        ledger,
        invoice_number="INV-9003",
        total=Decimal("50000.00"),
        invoice_date="2025-11-01",
        source_file="old.txt",
        raw_text="outside the window",
    )
    _pay(
        ledger,
        invoice_number="INV-9004",
        total=Decimal("50000.00"),
        currency="EUR",
        source_file="euro.txt",
        raw_text="other currency",
    )
    _pay(
        ledger,
        invoice_number="INV-9005",
        total=Decimal("50000.00"),
        invoice_date="yesterday",
        source_file="nodate.txt",
        raw_text="no printed date",
    )
    state = _numbered()
    widened = json.dumps({"vendor": "Other Co", "invoice_number": "INV-1001"})
    registry = _registry(db_path, state, ledger)
    plain = registry.call("check_payment_ledger", "{}")
    asked = registry.call("check_payment_ledger", widened)
    assert plain == asked
    assert plain["already_paid"] is False
    assert plain["match_date"] is None
    assert plain["same_vendor_paid_in_window"] == "9600.00"
    assert plain["this_total"] == "500.00"
    assert plain["combined"] == "10100.00"
    assert plain["split_window_days"] == 30
    assert plain["scrutiny_threshold"] == "10000.00"
    assert plain["over_split_threshold"] is True
    assert len(paid_rows(ledger)) == 5


def test_corrupt_ledger_is_a_system_error_hold(db_path, tmp_path, monkeypatch):
    ledger = tmp_path / "payments.db"
    ledger.write_bytes(b"this is not a sqlite database")
    state = _numbered()
    out, model = _run(
        state,
        [tool_step(("c1", "check_payment_ledger", "{}"))],
        db_path,
        monkeypatch,
        ledger,
    )
    assert out["outcome"] == "hold"
    assert out["reason_code"] == "SYSTEM_ERROR"
    assert out["findings"][-1].code == "SYSTEM_ERROR"
    assert out["attempts"][0]["error_type"] == "tool_error"
    assert out["attempts"][0]["retryable"] is False
    logged = out["logs"][-1]["result"]["tool_calls"][0]
    assert logged["name"] == "check_payment_ledger"
    assert logged["result"] is None
    assert "file is not a database" in logged["error"]
    assert model.tool_requests
    assert len(model.requests) == 0
    _assert_not_paid(state, out)


def test_locked_ledger_is_a_system_error_hold(db_path, tmp_path, monkeypatch):
    ledger = tmp_path / "payments.db"
    _pay(ledger, invoice_number="INV-1001", total=Decimal("500.00"), source_file="row.txt")

    def nowait(path):
        name = Path(path).resolve().as_posix()
        return sqlite3.connect(f"file:{name}?mode=ro", uri=True, timeout=0)

    monkeypatch.setattr("ledger.payments._connect_readonly", nowait)
    holder = sqlite3.connect(ledger, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        state = _numbered()
        out, _model = _run(
            state,
            [tool_step(("c1", "check_payment_ledger", "{}"))],
            db_path,
            monkeypatch,
            ledger,
        )
    finally:
        holder.close()
    assert out["outcome"] == "hold"
    assert out["reason_code"] == "SYSTEM_ERROR"
    logged = out["logs"][-1]["result"]["tool_calls"][0]
    assert logged["result"] is None
    assert "database is locked" in logged["error"]
    _assert_not_paid(state, out)


def test_a_duplicate_hit_cannot_pay(db_path, tmp_path, monkeypatch):
    ledger = tmp_path / "payments.db"
    _pay(
        ledger,
        invoice_number="INV-1001",
        total=Decimal("500.00"),
        invoice_date="2026-01-02",
        source_file="paid.txt",
        raw_text="already paid",
    )
    state = _numbered(number="INV 1001")
    out, _model = _run(
        state,
        [
            tool_step(("c1", "check_payment_ledger", "{}")),
            text_step(_verdict("PASS")),
        ],
        db_path,
        monkeypatch,
        ledger,
    )
    assert out["audit_verdict"] == "ESCALATE"
    assert out["outcome"] == "hold"
    assert out["reason_code"] == "DUPLICATE_INVOICE"
    assert out["decision"].decision == "hold"
    assert [finding.code for finding in out["findings"]] == ["DUPLICATE_INVOICE"]
    _assert_not_paid(state, out)


def test_a_split_hit_cannot_pay(db_path, tmp_path, monkeypatch):
    ledger = tmp_path / "payments.db"
    _pay(ledger)
    state = _numbered()
    out, _model = _run(
        state,
        [
            tool_step(("c1", "check_payment_ledger", json.dumps({"vendor": "Other Co"}))),
            text_step(_verdict("PASS")),
        ],
        db_path,
        monkeypatch,
        ledger,
    )
    assert out["outcome"] == "hold"
    assert out["reason_code"] == "SPLIT_INVOICE"
    assert "DUPLICATE_INVOICE" not in [finding.code for finding in out["findings"]]
    assert out["authorization"] is None
    _assert_not_paid(state, out)


def test_two_ledger_hits_become_one_finding(db_path, tmp_path, monkeypatch):
    """Different arguments are not a repeat. The same code is still one finding."""

    ledger = tmp_path / "payments.db"
    _pay(
        ledger,
        invoice_number="INV-1001",
        total=Decimal("500.00"),
        invoice_date="2026-01-02",
        source_file="paid.txt",
        raw_text="already paid",
    )
    state = _numbered()
    out, _model = _run(
        state,
        [
            tool_step(
                ("c1", "check_payment_ledger", "{}"),
                ("c2", "check_payment_ledger", json.dumps({"vendor": "Other Co"})),
            ),
            text_step(_verdict("PASS")),
        ],
        db_path,
        monkeypatch,
        ledger,
    )
    calls = out["logs"][-1]["result"]["tool_calls"]
    assert calls[0]["result"]["already_paid"] is True
    assert "repeat" not in calls[0]
    assert "repeat" not in calls[1]
    assert calls[1]["result"]["already_paid"] is True
    assert [finding.code for finding in out["findings"]] == ["DUPLICATE_INVOICE"]
    assert out["outcome"] == "hold"
    assert out["reason_code"] == "DUPLICATE_INVOICE"
    _assert_not_paid(state, out)


def test_ledger_tool_counts_toward_the_cap_and_repeats_reuse(db_path, monkeypatch):
    assert TOOL_CALL_CAP == 8
    codes = [
        "STOCK_SHORTFALL",
        "TOTAL_MISMATCH",
        "UNKNOWN_ITEM",
        "NON_USD",
        "FRAUD_SIGNAL",
        "PRICE_SPIKE",
        "REVISED_INVOICE",
    ]
    first = [
        ("ledger", "check_payment_ledger", "{}"),
        ("ledger-again", "check_payment_ledger", "{}"),
        *[
            (f"p{index}", "get_policy_rule", json.dumps({"code": code}))
            for index, code in enumerate(codes)
        ],
    ]
    state = _payable_state()
    out, model = _run(
        state,
        [
            tool_step(*first),
            tool_step(("over", "lookup_item", json.dumps({"item": "WidgetA"}))),
            text_step(_verdict("PASS")),
        ],
        db_path,
        monkeypatch,
        None,
    )
    calls = out["logs"][-1]["result"]["tool_calls"]
    assert [row["name"] for row in calls[:2]] == ["check_payment_ledger", "check_payment_ledger"]
    assert "repeat" not in calls[0]
    assert calls[0]["result"]["already_paid"] is False
    assert calls[1]["repeat"] is True
    assert calls[1]["result"] == calls[0]["result"]
    assert calls[1]["call_id"] == "ledger-again"
    assert all(row["round"] == 1 and row["error"] is None for row in calls[:-1])
    assert len(calls) == TOOL_CALL_CAP + 2
    assert calls[-1]["error"] == "blocked: tool_cap"
    assert calls[-1]["result"] is None
    assert calls[-1]["round"] == 2
    assert out["logs"][-1]["result"]["tool_cap_hit"] == "calls"
    assert out["logs"][-1]["result"]["forced_final_turn"] is True
    final = model.requests[-1]
    assert "tools" not in final
    assert "tool_choice" not in final
    assert final["messages"][-1]["content"].startswith("The tool cap is reached.")
    assert out["outcome"] == "approve"
    assert out["authorization"] is not None


def test_tools_off_payload_matches_main(db_path, monkeypatch):
    monkeypatch.delenv("XAI_TOOLS", raising=False)
    model = ScriptedToolModel([text_step(_verdict("PASS"))])
    out = audit(
        _payable_state(),
        model,
        db_path,
        FakeClock(),
        ZeroRandom(),
        3,
        as_of=date(2026, 10, 31),
    )
    assert out["outcome"] == "approve"
    assert model.tool_requests == []
    assert len(model.requests) == 1
    request = model.requests[0]
    assert set(request) == {"messages", "timeout"}
    assert "tools" not in request
    assert "tool_choice" not in request
    system = request["messages"][0]["content"]
    assert "check_payment_ledger" not in system
    assert "prior payment" not in system
    assert set(json.loads(request["messages"][1]["content"])) == _USER_KEYS
    blob = json.dumps(request["messages"], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(blob).hexdigest() == _MAIN_TOOLS_OFF_AUDIT


def test_ordered_scenario_with_tools_on(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", "1")
    ledger = tmp_path / "payments.db"

    class Model:
        def __init__(self, handler):
            self.handler = handler

        def request(self, messages, timeout=60):
            return self.handler(messages)

        def request_tools(self, messages, tools, timeout=60):
            names = [tool["function"]["name"] for tool in tools]
            assert "check_payment_ledger" in names
            assert "vendor" not in json.dumps(tools[-1]["function"]["parameters"])
            if not any(message.get("role") == "tool" for message in messages):
                return {
                    "content": "",
                    "tool_calls": [
                        {"id": "ledger", "name": "check_payment_ledger", "arguments": "{}"}
                    ],
                }
            return {"content": self.handler(messages), "tool_calls": []}

    report = run_scenario(
        ROOT / "data" / "ordered_scenario.json",
        db_path,
        ledger,
        lambda path: Model(handler_for(path)),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=5,
        as_of=AS_OF,
    )
    missed = [step for step in report["steps"] if not step["ok"]]
    assert missed == [], missed
    assert report["ledger_ok"]
    first = report["steps"][0]["tool_calls"]
    ledger_calls = [row for row in first if row["name"] == "check_payment_ledger"]
    assert ledger_calls[0]["result"]["already_paid"] is False
    duplicate = report["steps"][1]
    assert duplicate["reason_code"] == "DUPLICATE_INVOICE"
    assert duplicate["paid"] is False
    assert not any(row["name"] == "check_payment_ledger" for row in duplicate["tool_calls"])
