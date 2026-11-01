"""Critique tools. No network. The model is scripted."""

import hashlib
import json
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from db import create_database
from graph import run_invoice
from llm_client import TOOL_ROUND_CAP
from main import _result_line, tool_calls_of
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    ExtractedInvoice,
    LineItem,
    PipelineState,
    SourceInfo,
)
from pipeline.critique import TOOLS_LINE, critique
from pipeline.fact_guard import CRITIQUE_CLAIMS, VERIFIED_FACTS
from pipeline.tools import get_policy_rule
from decision.outcome import HOLD_CODES, REJECT_CODES, WARNING_CODES
from scripts.eval import format_tool_summary
from decision.wording import today_line
from tests.fakes import FakeClock, FakeLLM, ScriptedToolModel, ZeroRandom
from tests.test_tool_loop import text_step, tool_step

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 31)
REASON = json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
AUDIT = json.dumps({"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []})
CLEAN = json.dumps({"concerns": [], "stricter": False})


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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extraction() -> str:
    def line(item, qty, price, amount):
        return {
            "item": item,
            "description": item,
            "quantity": qty,
            "unit_price": price,
            "amount": amount,
            "note": None,
        }

    return json.dumps(
        {
            "vendor": "Keelson Manufacturing",
            "invoice_number": "INV-2106",
            "invoice_date": "2026-04-16",
            "due_date": "2026-05-16",
            "currency": "USD",
            "line_items": [
                line("WidgetA", 15, "250", "3750"),
                line("WidgetB", 8, "500", "4000"),
                line("GadgetX", 5, "750", "3750"),
            ],
            "subtotal": "11500",
            "tax_amount": 0,
            "shipping": 0,
            "total": "11500",
            "payment_terms": "Net 45",
            "notes": None,
        }
    )


def _invoice_file(directory: Path) -> Path:
    path = directory / "invoice_2106.txt"
    path.write_text(
        "Vendor: Keelson Manufacturing\n"
        "Invoice Number: INV-2106\n"
        "Date: 2026-04-16\n"
        "Due Date: 2026-05-16\n"
        "WidgetA qty 15\n"
        "WidgetB qty 8\n"
        "GadgetX qty 5\n"
        "Total: 11500.00\n",
        encoding="utf-8",
    )
    return path


def _run(path, db_path, steps, monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", "1")
    model = ScriptedToolModel(steps)
    clock = FakeClock()
    result = run_invoice(
        path,
        db_path,
        model,
        clock=clock,
        rng=ZeroRandom(),
        max_attempts=3,
        as_of=AS_OF,
    )
    return result, model, clock


def _stage(result, name) -> dict:
    return next(row["result"] for row in result["logs"] if row["stage"] == name)


def _baseline_system(as_of) -> str:
    """The critique system prompt with the tools sentence left out."""

    return (
        "TASK critique\n"
        f"{today_line(as_of)}\n"
        "Review this approval. Return JSON with keys concerns (list of objects) and "
        "stricter (boolean). You may only make the decision stricter. "
        "Being over the scrutiny threshold is not a risk. Do not set stricter only "
        "because the total is over it. "
        "You cannot approve a failure and you cannot clear a non-USD hold. "
        f"{VERIFIED_FACTS} {CRITIQUE_CLAIMS} Return JSON only."
    )


def _state() -> PipelineState:
    invoice = ExtractedInvoice(
        vendor="Widgets Inc.",
        currency="USD",
        invoice_date="2026-04-16",
        due_date="2026-05-16",
        total=Decimal("4000"),
        line_items=[
            LineItem(
                item="WidgetB",
                description="WidgetB",
                quantity=Decimal(8),
                unit_price=Decimal(500),
                amount=Decimal(4000),
            )
        ],
        source=SourceInfo(path="x.txt", filename="x.txt", format="txt"),
    )
    return PipelineState(
        invoice_path="x.txt",
        filename="x.txt",
        raw_text="Total: 4000.00\n",
        invoice=invoice,
        decision=ApprovalDecision(
            decision="approve",
            reasoning=ApprovalReasoning(
                rules_applied=["stock covered"],
                evidence=["total 4000.00 USD"],
                risks_considered=["no extra risk"],
                conclusion="The checks passed.",
            ),
            amount=Decimal(4000),
            currency="USD",
        ),
    )


def test_critique_tools_return_2106_facts_and_the_log(tmp_path, db_path, monkeypatch):
    before = _sha(db_path)
    path = _invoice_file(tmp_path)
    calls = tool_step(
        ("stock", "check_stock", json.dumps({"item": "Widget B"})),
        ("price", "get_reference_price", json.dumps({"item": "WidgetB"})),
        ("rule", "get_policy_rule", json.dumps({"code": "REVISED_INVOICE"})),
    )
    result, model, clock = _run(
        path,
        db_path,
        [
            text_step(_extraction()),
            text_step(REASON),
            calls,
            text_step(CLEAN),
            text_step(REASON),
            text_step(AUDIT),
        ],
        monkeypatch,
    )
    assert clock.slept == []
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert _sha(db_path) == before
    source = (ROOT / "pipeline" / "tools.py").read_text(encoding="utf-8")
    assert "create_database" not in source
    assert "sqlite3" not in source

    logged = _stage(result, "critique")["tool_calls"]
    by_name = {row["name"]: row for row in logged}
    stock = by_name["check_stock"]
    assert stock["agent"] == "critique"
    assert stock["round"] == 1
    assert stock["attempt"] == 1
    assert stock["call_id"] == "stock"
    assert stock["error"] is None
    assert isinstance(stock["latency_ms"], int) and stock["latency_ms"] >= 0
    assert stock["result"] == {
        "item": "Widget B",
        "matched_item": "WidgetB",
        "available": 10,
        "requested_on_invoice": "8",
        "covered": True,
    }
    assert by_name["get_reference_price"]["result"] == {
        "item": "WidgetB",
        "matched_item": "WidgetB",
        "reference_price": "500.00",
        "currency": "USD",
    }
    rule = by_name["get_policy_rule"]["result"]
    assert rule["code"] == "REVISED_INVOICE"
    assert rule["class"] == "hold"
    assert rule["rule"] == (
        "A revised invoice is a hold until someone confirms it replaces an unpaid original."
    )
    assert rule["action"] == "Confirm it replaces the original and that the original was not paid."
    assert [row["agent"] for row in logged] == ["critique", "critique", "critique"]

    line = _result_line(path, result)
    assert line["tool_calls"] == logged
    assert tool_calls_of(result) == logged
    names = [item["function"]["name"] for item in model.tool_requests[0]["tools"]]
    assert names == ["check_stock", "get_reference_price", "get_policy_rule"]
    bodies = [
        json.loads(message["content"])
        for message in model.tool_requests[1]["messages"]
        if message["role"] == "tool"
    ]
    assert bodies[0]["available"] == 10
    system = model.tool_requests[0]["messages"][0]["content"]
    assert TOOLS_LINE in system
    assert "verified_by_code" in model.tool_requests[0]["messages"][1]["content"]


def test_unknown_item_and_code_go_back_to_the_model(tmp_path, db_path, monkeypatch):
    path = _invoice_file(tmp_path)
    bad = tool_step(
        ("c1", "check_stock", json.dumps({"item": "WidgitA"})),
        ("c2", "get_policy_rule", json.dumps({"code": "NOT_A_CODE"})),
    )
    result, model, _clock = _run(
        path,
        db_path,
        [
            text_step(_extraction()),
            text_step(REASON),
            bad,
            text_step(CLEAN),
            text_step(REASON),
            text_step(AUDIT),
        ],
        monkeypatch,
    )
    assert result["outcome"] == "approve"
    logged = _stage(result, "critique")["tool_calls"]
    assert logged[0]["error"] == "unknown item: WidgitA"
    assert logged[0]["result"] is None
    assert logged[0]["name"] == "check_stock"
    assert logged[0]["arguments"] == {"item": "WidgitA"}
    assert logged[1]["error"] == "argument code is not a finding code"
    assert logged[1]["result"] is None
    code_schema = next(
        tool["function"]["parameters"]
        for tool in model.tool_requests[0]["tools"]
        if tool["function"]["name"] == "get_policy_rule"
    )
    assert code_schema["properties"]["code"]["enum"] == (
        list(REJECT_CODES) + list(HOLD_CODES) + list(WARNING_CODES)
    )
    assert "ITEM_NOT_IN_SOURCE" in code_schema["properties"]["code"]["enum"]
    bodies = [
        json.loads(message["content"])
        for message in model.tool_requests[1]["messages"]
        if message["role"] == "tool"
    ]
    assert bodies == [
        {"error": "unknown item: WidgitA"},
        {"error": "argument code is not a finding code"},
    ]


def test_known_warning_without_rule_text_is_not_an_input_error():
    rule = get_policy_rule({"code": "noncommercial_address"})
    assert rule == {
        "code": "NONCOMMERCIAL_ADDRESS",
        "class": "warning",
        "rule": None,
        "action": None,
    }


def test_critique_cap_is_a_system_error_hold_and_nothing_is_paid(tmp_path, db_path, monkeypatch):
    assert TOOL_ROUND_CAP == 3
    path = _invoice_file(tmp_path)
    one = ("c1", "check_stock", json.dumps({"item": "WidgetB"}))
    steps = [text_step(_extraction()), text_step(REASON)]
    steps.extend(tool_step(one) for _ in range(TOOL_ROUND_CAP + 1))
    result, model, clock = _run(path, db_path, steps, monkeypatch)
    assert clock.slept == []
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert result.get("authorization") is None
    assert result["payment"].status == "not_paid"
    assert result["payment"].mock_response is None
    assert "payment" not in [row["stage"] for row in result["logs"]]
    logged = _stage(result, "critique")
    assert len(logged["attempts"]) == 1
    assert logged["attempts"][0]["error_type"] == "tool_cap"
    assert logged["attempts"][0]["retryable"] is False
    assert logged["attempts"][0]["backoff_seconds"] is None
    assert len(logged["tool_calls"]) == TOOL_ROUND_CAP + 1
    assert logged["tool_calls"][-1]["error"] == "blocked: tool_cap"
    assert logged["tool_calls"][-1]["result"] is None
    assert len(result["attempts"]) == 1
    assert [row["agent"] for row in logged["tool_calls"]] == ["critique"] * (TOOL_ROUND_CAP + 1)
    assert _result_line(path, result)["tool_calls"] == logged["tool_calls"]
    assert len(model.tool_requests) == TOOL_ROUND_CAP + 1


def test_fact_guard_still_drops_a_covered_stock_concern(tmp_path, db_path, monkeypatch):
    path = _invoice_file(tmp_path)
    concern = {
        "concerns": [
            {"category": "stock", "item": "WidgetB", "text": "WidgetB requested 8 vs available 10"}
        ],
        "stricter": True,
    }
    result, model, _clock = _run(
        path,
        db_path,
        [
            text_step(_extraction()),
            text_step(REASON),
            tool_step(("c1", "check_stock", json.dumps({"item": "WidgetB"}))),
            text_step(json.dumps(concern)),
            text_step(REASON),
            text_step(AUDIT),
        ],
        monkeypatch,
    )
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert "CRITIQUE_HOLD" not in [finding.code for finding in result["findings"]]
    logged = _stage(result, "critique")
    assert logged["stricter"] is False
    assert logged["dropped_concerns"][0]["rule"] == "stock_sufficient"
    assert logged["dropped_concerns"][0]["detail"] == (
        "WidgetB requested 8 and available stock is 10."
    )
    assert logged["tool_calls"][0]["result"]["covered"] is True
    assert logged["tool_calls"][0]["result"]["available"] == 10
    system = model.tool_requests[0]["messages"][0]["content"]
    user = model.tool_requests[0]["messages"][1]["content"]
    assert "verified by code" in system
    facts = json.loads(user)["verified_by_code"]
    widget_b = next(row for row in facts["stock"] if row["item"] == "WidgetB")
    assert widget_b == {"item": "WidgetB", "requested": "8", "available": 10}


def test_critique_payload_matches_main_when_tools_are_off(db_path, monkeypatch):
    state = _state()
    monkeypatch.delenv("XAI_TOOLS", raising=False)
    seen = []

    def handler(messages):
        seen.append(messages)
        return CLEAN

    critique(state, FakeLLM(handler), db_path, FakeClock(), ZeroRandom(), 3, as_of=AS_OF)
    assert seen[0][0]["content"] == _baseline_system(AS_OF)
    assert "verified_by_code" in seen[0][1]["content"]

    monkeypatch.setenv("XAI_TOOLS", "0")
    quiet = ScriptedToolModel([text_step(CLEAN)])
    critique(state, quiet, db_path, FakeClock(), ZeroRandom(), 3, as_of=AS_OF)
    assert quiet.tool_requests == []
    assert quiet.requests[0]["messages"] == seen[0]

    monkeypatch.setenv("XAI_TOOLS", "1")
    model = ScriptedToolModel([text_step(CLEAN)])
    critique(state, model, db_path, FakeClock(), ZeroRandom(), 3, as_of=AS_OF)
    system = model.tool_requests[0]["messages"][0]["content"]
    assert system == _baseline_system(AS_OF).replace(
        " Return JSON only.",
        f" {TOOLS_LINE} Return JSON only.",
    )
    assert model.tool_requests[0]["messages"][1] == seen[0][1]


def test_eval_summary_counts_calls_and_latency():
    result = {
        "logs": [
            {
                "stage": "critique",
                "result": {
                    "tool_calls": [{"latency_ms": 4}, {"latency_ms": 6}],
                    "attempts": [{"tool_calls": [{"latency_ms": 4}, {"latency_ms": 6}]}],
                },
            }
        ]
    }
    assert tool_calls_of(result) == [{"latency_ms": 4}, {"latency_ms": 6}]
    assert format_tool_summary(2, 10) == "tools calls=2 total_latency_ms=10 avg_latency_ms=5.0"
    assert format_tool_summary(0, 0) == "tools calls=0 total_latency_ms=0 avg_latency_ms=0.0"
