"""The tool loop. No network. Audit is the only agent given a registry."""

import importlib
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from db import create_database
from graph import route_after_audit, run_invoice
from decision.outcome import result_line
from llm_client import (
    TOOL_CALL_CAP,
    TOOL_ROUND_CAP,
    AgentStepFailed,
    GrokClient,
    LLMError,
    call_with_retries,
    tool_request,
)
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    ExtractedInvoice,
    LineItem,
    PipelineState,
    SourceInfo,
    ValidationReport,
)
from pipeline import audit
from pipeline.audit import LEDGER_LINE, TOOLS_LINE
from pipeline.payment import payment
from pipeline.tools import Tool, ToolInputError, ToolRegistry, example_registry
from tests.fakes import FakeClock, FakeLLM, ScriptedToolModel, ZeroRandom, task_of
from tests.test_pipeline import extraction, scripted, write_invoice

ROOT = Path(__file__).resolve().parents[1]
ANSWER = '{"echoed": "phase-1"}'
AUDIT_PASS = json.dumps({"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []})
_RECORD_FIELDS = {
    "round",
    "attempt",
    "call_id",
    "name",
    "arguments",
    "result",
    "error",
    "latency_ms",
}


class Echoed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    echoed: str


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


def tool_step(*calls, content=""):
    return {
        "content": content,
        "tool_calls": [
            {"id": call_id, "name": name, "arguments": arguments}
            for call_id, name, arguments in calls
        ],
    }


def text_step(content):
    return {"content": content, "tool_calls": []}


def _echo(note="phase-1", call_id="c1"):
    return tool_step((call_id, "echo_note", json.dumps({"note": note})))


def _finish(steps, monkeypatch, registry=None):
    model = ScriptedToolModel(steps)
    log = []
    clock = FakeClock()
    parsed = call_with_retries(
        tool_request(model, registry or example_registry(), log),
        [{"role": "user", "content": "Answer with JSON."}],
        Echoed,
        max_attempts=5,
        clock=clock,
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert clock.slept == []
    return parsed, log, model


def _assert_record(record):
    assert set(record) == _RECORD_FIELDS
    assert "ok" not in record
    assert "arguments_raw" not in record
    assert isinstance(record["attempt"], int) and record["attempt"] >= 1
    assert isinstance(record["latency_ms"], int)
    assert record["latency_ms"] >= 0


def test_one_tool_round_trip_returns_final_json(monkeypatch):
    step = _echo()
    step["content"] = "not the answer"
    parsed, log, model = _finish(
        [step, text_step(ANSWER)],
        monkeypatch,
    )
    assert parsed.echoed == "phase-1"
    assert len(log) == 1
    _assert_record(log[0])
    assert log[0]["round"] == 1
    assert log[0]["call_id"] == "c1"
    assert log[0]["name"] == "echo_note"
    assert log[0]["arguments"] == {"note": "phase-1"}
    assert log[0]["result"] == {"note": "phase-1", "echo": "phase-1"}
    assert log[0]["attempt"] == 1
    assert log[0]["error"] is None
    sent = model.tool_requests[1]["messages"]
    assert sent[-2]["role"] == "assistant"
    assert sent[-2]["content"] == "not the answer"
    assert sent[-2]["tool_calls"][0]["id"] == "c1"
    assert sent[-1]["role"] == "tool"
    assert sent[-1]["tool_call_id"] == "c1"
    assert json.loads(sent[-1]["content"]) == log[0]["result"]
    assert model.tool_requests[0]["tools"] == example_registry().schemas()


def test_parallel_calls_all_run_and_each_counts(monkeypatch):
    parsed, log, model = _finish(
        [
            tool_step(
                ("c1", "echo_note", json.dumps({"note": "one"})),
                ("c2", "echo_note", json.dumps({"note": "two"})),
            ),
            text_step(ANSWER),
        ],
        monkeypatch,
    )
    assert parsed.echoed == "phase-1"
    assert [row["call_id"] for row in log] == ["c1", "c2"]
    assert [row["round"] for row in log] == [1, 1]
    assert [row["arguments"]["note"] for row in log] == ["one", "two"]
    assert all(row["error"] is None for row in log)
    assert [row["attempt"] for row in log] == [1, 1]
    sent = model.tool_requests[1]["messages"]
    tool_ids = [message["tool_call_id"] for message in sent if message["role"] == "tool"]
    assert tool_ids == ["c1", "c2"]


def test_identical_repeat_does_not_count_or_rerun(monkeypatch):
    runs = {"n": 0}

    def echo(arguments):
        runs["n"] += 1
        return {"note": arguments["note"], "echo": arguments["note"]}

    registry = ToolRegistry(
        [
            Tool(
                name="echo_note",
                description="Echo.",
                parameters={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                },
                function=echo,
            )
        ]
    )
    same = json.dumps({"note": "phase-1"})
    model = ScriptedToolModel(
        [
            tool_step(("c1", "echo_note", same), ("c2", "echo_note", same)),
            text_step(ANSWER),
        ]
    )
    step = tool_request(model, registry)
    parsed = call_with_retries(
        step,
        [{"role": "user", "content": "Answer with JSON."}],
        Echoed,
        max_attempts=3,
        clock=FakeClock(),
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert parsed.echoed == "phase-1"
    assert runs["n"] == 1
    assert step.calls == 1
    assert step.rounds == 1
    assert "repeat" not in step.log[0]
    assert step.log[1]["repeat"] is True
    assert step.log[1]["result"] == step.log[0]["result"]
    assert step.log[1]["error"] is None
    assert step.log[1]["call_id"] == "c2"
    body = json.loads(model.tool_requests[1]["messages"][-1]["content"])
    assert body == step.log[0]["result"]


def test_repeat_does_not_spend_the_call_cap_but_rounds_still_count(monkeypatch):
    runs = {"n": 0}

    def echo(arguments):
        runs["n"] += 1
        return {"note": arguments["note"], "echo": arguments["note"]}

    registry = ToolRegistry(
        [
            Tool(
                name="echo_note",
                description="Echo.",
                parameters={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                },
                function=echo,
            )
        ]
    )
    eight = [
        (f"c{index}", "echo_note", json.dumps({"note": str(index)}))
        for index in range(TOOL_CALL_CAP)
    ]
    model = ScriptedToolModel(
        [tool_step(*eight), _echo(note="0", call_id="again"), text_step(ANSWER)]
    )
    step = tool_request(model, registry)
    parsed = call_with_retries(
        step,
        [{"role": "user", "content": "Answer with JSON."}],
        Echoed,
        max_attempts=3,
        clock=FakeClock(),
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert parsed.echoed == "phase-1"
    assert runs["n"] == TOOL_CALL_CAP
    assert step.calls == TOOL_CALL_CAP
    assert step.rounds == 2
    assert step.log[-1]["repeat"] is True
    assert step.log[-1]["result"] == step.log[0]["result"]
    assert step.log[-1]["name"] == "echo_note"

    runs["n"] = 0
    blocked = ScriptedToolModel(
        [_echo(note="same", call_id=f"r{index}") for index in range(TOOL_ROUND_CAP + 1)]
    )
    blocked_step = tool_request(blocked, registry)
    with pytest.raises(LLMError) as caught:
        blocked_step([{"role": "user", "content": "Answer with JSON."}])
    assert caught.value.error_type == "tool_cap"
    assert runs["n"] == 1
    assert [row.get("repeat", False) for row in blocked_step.log] == [False, True, True, False]
    assert blocked_step.log[-1]["error"] == "blocked: tool_cap"
    assert blocked_step.log[-1]["result"] is None
    assert blocked_step.calls == 1


def test_unknown_tool_returns_an_error_and_continues(monkeypatch):
    parsed, log, model = _finish(
        [tool_step(("c1", "nope", "{}")), text_step(ANSWER)],
        monkeypatch,
    )
    assert parsed.echoed == "phase-1"
    assert log[0]["error"] == "unknown tool: nope"
    assert log[0]["result"] is None
    body = json.loads(model.tool_requests[1]["messages"][-1]["content"])
    assert body == {"error": "unknown tool: nope"}


def test_bad_args_return_an_error_and_the_loop_continues(monkeypatch):
    cases = [
        ("not-json", "arguments are not valid JSON"),
        ("{}", "missing argument: note"),
        ('{"note": 1}', "argument note must be a string"),
        ("[]", "arguments must be a JSON object"),
    ]
    for arguments, error in cases:
        parsed, log, model = _finish(
            [tool_step(("c1", "echo_note", arguments)), text_step(ANSWER)],
            monkeypatch,
        )
        assert parsed.echoed == "phase-1"
        assert log[0]["error"] == error
        assert log[0]["result"] is None
        assert log[0]["name"] == "echo_note"
        if arguments == "not-json":
            assert log[0]["arguments"] is None
            assert log[0]["arguments_raw"] == "not-json"
        else:
            assert "arguments_raw" not in log[0]
        body = json.loads(model.tool_requests[1]["messages"][-1]["content"])
        assert body == {"error": error}


def _payable_state() -> PipelineState:
    invoice = ExtractedInvoice(
        vendor="Widgets Inc.",
        currency="USD",
        invoice_date="2026-01-01",
        due_date="2026-02-01",
        total=Decimal("500"),
        subtotal=Decimal("500"),
        tax_amount=Decimal("0"),
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("2"),
                unit_price=Decimal("250"),
                amount=Decimal("500"),
            )
        ],
        source=SourceInfo(path="x", filename="x.txt", format="txt", raw_text="Total: 500.00"),
    )
    decision = ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(
            rules_applied=["stock covered"],
            evidence=["total 500.00 USD"],
            risks_considered=["no extra risk"],
            conclusion="The checks passed.",
        ),
        amount=Decimal("500"),
        currency="USD",
    )
    return PipelineState(
        invoice_path="x",
        filename="x.txt",
        raw_text="WidgetA\nTotal: 500.00\n",
        invoice=invoice,
        validation=ValidationReport(passed=True, findings=[]),
        decision=decision,
    )


def _cap_hold(steps, db_path, monkeypatch, registry=None):
    """Drive audit() with a scripted model. The registry stands in for the audit tools."""

    chosen = registry or example_registry()
    # pipeline/__init__.py binds the name audit to the function. Load the module by name.
    audit_module = importlib.import_module("pipeline.audit")
    monkeypatch.setattr(
        audit_module,
        "audit_registry",
        lambda invoice, stock, raw_text, ledger_path=None: chosen,
    )
    model = ScriptedToolModel(steps)
    clock = FakeClock()
    out = audit(
        _payable_state(),
        model,
        db_path,
        clock,
        ZeroRandom(),
        5,
    )
    stage = next(row for row in out["logs"] if row["stage"] == "audit")
    return out, clock, list(stage["result"].get("tool_calls") or []), model


def _assert_system_error_hold(out, clock, executed, error_type="tool_cap"):
    assert out["outcome"] == "hold"
    assert out["reason_code"] == "SYSTEM_ERROR"
    assert out["findings"][-1].code == "SYSTEM_ERROR"
    assert "Rerun the invoice" in (out["action"] or "")
    assert len(out["attempts"]) == 1
    assert out["attempts"][0]["error_type"] == error_type
    assert out["attempts"][0]["retryable"] is False
    assert out["attempts"][0]["backoff_seconds"] is None
    assert len(out["attempts"][0]["tool_calls"]) == executed
    assert clock.slept == []
    assert "payment" not in [row["stage"] for row in out["logs"]]
    stopped = _payable_state().model_copy(
        update={
            "outcome": out["outcome"],
            "reason_code": out["reason_code"],
            "action": out["action"],
            "agent_error": out["agent_error"],
            "findings": out["findings"],
            "attempts": out["attempts"],
            "logs": out["logs"],
        }
    )
    assert stopped.authorization is None
    assert route_after_audit(stopped) == "reject_log"
    paid = payment(stopped)
    assert paid["payment"].status == "not_paid"
    assert paid["payment"].mock_response is None


def test_round_cap_then_a_plain_pass_still_approves(db_path, monkeypatch):
    assert TOOL_ROUND_CAP == 3
    steps = [_echo(note=str(index), call_id=f"c{index}") for index in range(TOOL_ROUND_CAP + 1)]
    steps.append(text_step(AUDIT_PASS))
    out, clock, log, model = _cap_hold(steps, db_path, monkeypatch)
    assert out["outcome"] == "approve"
    assert out["audit_verdict"] == "PASS"
    assert out["authorization"] is not None
    assert clock.slept == []
    assert [row["round"] for row in log] == [1, 2, 3, 4]
    assert [row["error"] for row in log] == [None, None, None, "blocked: tool_cap"]
    assert log[-1]["result"] is None
    assert log[-1]["name"] == "echo_note"
    stage = next(row for row in out["logs"] if row["stage"] == "audit")
    assert stage["result"]["tool_cap_hit"] == "rounds"
    assert stage["result"]["forced_final_turn"] is True
    assert stage["result"]["tool_calls"][-1]["error"] == "blocked: tool_cap"
    final = model.requests[-1]
    assert "tools" not in final
    assert "tool_choice" not in final
    assert any(message.get("role") == "tool" for message in final["messages"])
    assert "verified_by_code" in final["messages"][1]["content"]
    assert final["messages"][-1]["content"].startswith("The tool cap is reached.")
    assert len(model.tool_requests) == TOOL_ROUND_CAP + 1
    assert len(model.requests) == 1
    line = result_line(Path("cap.txt"), out)
    assert line["tool_cap_hit"] == "rounds"
    assert line["forced_final_turn"] is True
    stopped = _payable_state().model_copy(
        update={
            "outcome": out["outcome"],
            "audit_verdict": out["audit_verdict"],
            "authorization": out["authorization"],
            "logs": out["logs"],
        }
    )
    assert route_after_audit(stopped) == "payment"
    assert payment(stopped)["payment"].status == "paid"


def test_call_cap_then_a_bad_final_answer_holds_without_payment(db_path, monkeypatch):
    assert TOOL_CALL_CAP == 8
    eight = [
        (f"c{index}", "echo_note", json.dumps({"note": str(index)}))
        for index in range(TOOL_CALL_CAP)
    ]
    steps = [tool_step(*eight), _echo(call_id="over"), text_step("not-json"), text_step("nope")]
    out, clock, log, model = _cap_hold(steps, db_path, monkeypatch)
    _assert_system_error_hold(out, clock, executed=TOOL_CALL_CAP + 1, error_type="malformed_output")
    assert log[-1]["error"] == "blocked: tool_cap"
    assert log[-1]["result"] is None
    assert log[-1]["call_id"] == "over"
    assert log[-1]["round"] == 2
    assert {row["round"] for row in log[:-1]} == {1}
    assert all(row["error"] is None for row in log[:-1])
    stage = next(row for row in out["logs"] if row["stage"] == "audit")
    assert stage["result"]["tool_cap_hit"] == "calls"
    assert stage["result"]["forced_final_turn"] is True
    assert [row["call_id"] for row in stage["result"]["tool_calls"]] == [
        *(f"c{index}" for index in range(TOOL_CALL_CAP)),
        "over",
    ]
    assert len(model.tool_requests) == 2
    assert len(model.requests) == 2
    assert all("tools" not in sent and "tool_choice" not in sent for sent in model.requests)
    assert model.requests[1]["messages"][-1]["content"].startswith("The JSON failed validation:")
    line = result_line(Path("cap.txt"), out)
    assert line["tool_cap_hit"] == "calls"
    assert line["forced_final_turn"] is True
    assert line["payment_status"] == "not_paid"


def test_calls_cap_then_a_plain_pass_still_approves(db_path, monkeypatch):
    eight = [
        (f"c{index}", "echo_note", json.dumps({"note": str(index)}))
        for index in range(TOOL_CALL_CAP)
    ]
    steps = [tool_step(*eight), _echo(call_id="over"), text_step(AUDIT_PASS)]
    out, clock, _log, model = _cap_hold(steps, db_path, monkeypatch)
    assert out["outcome"] == "approve"
    assert out["audit_verdict"] == "PASS"
    assert out["authorization"] is not None
    assert clock.slept == []
    assert len(model.requests) == 1
    final = model.requests[0]
    assert "tools" not in final
    assert "tool_choice" not in final
    stage = next(row for row in out["logs"] if row["stage"] == "audit")
    assert stage["result"]["tool_cap_hit"] == "calls"
    assert stage["result"]["forced_final_turn"] is True
    line = result_line(Path("cap.txt"), out)
    assert line["tool_cap_hit"] == "calls"
    assert line["forced_final_turn"] is True
    stopped = _payable_state().model_copy(
        update={
            "outcome": out["outcome"],
            "audit_verdict": out["audit_verdict"],
            "authorization": out["authorization"],
            "logs": out["logs"],
        }
    )
    assert route_after_audit(stopped) == "payment"
    assert payment(stopped)["payment"].status == "paid"


def test_rounds_cap_then_a_bad_final_answer_holds_without_payment(db_path, monkeypatch):
    steps = [_echo(note=str(index), call_id=f"c{index}") for index in range(TOOL_ROUND_CAP + 1)]
    steps.extend([text_step("not-json"), text_step("nope")])
    out, clock, _log, model = _cap_hold(steps, db_path, monkeypatch)
    _assert_system_error_hold(out, clock, executed=TOOL_ROUND_CAP + 1, error_type="malformed_output")
    assert len(out["attempts"]) == 1
    assert clock.slept == []
    assert len(model.requests) == 2
    assert all("tools" not in sent and "tool_choice" not in sent for sent in model.requests)
    stage = next(row for row in out["logs"] if row["stage"] == "audit")
    assert stage["result"]["tool_cap_hit"] == "rounds"
    assert stage["result"]["forced_final_turn"] is True
    line = result_line(Path("cap.txt"), out)
    assert line["tool_cap_hit"] == "rounds"
    assert line["forced_final_turn"] is True
    assert line["payment_status"] == "not_paid"


def test_api_error_on_the_final_turn_is_a_hold_without_retry(db_path, monkeypatch):
    steps = [_echo(note=str(index), call_id=f"c{index}") for index in range(TOOL_ROUND_CAP + 1)]
    steps.append(LLMError("http_429", True, "busy"))
    out, clock, _log, model = _cap_hold(steps, db_path, monkeypatch)
    _assert_system_error_hold(out, clock, executed=TOOL_ROUND_CAP + 1, error_type="http_429")
    assert len(out["attempts"]) == 1
    assert out["attempts"][0]["retryable"] is False
    assert out["attempts"][0]["backoff_seconds"] is None
    assert clock.slept == []
    assert len(model.requests) == 1
    assert "tools" not in model.requests[0]
    assert "tool_choice" not in model.requests[0]
    assert len(model.tool_requests) == TOOL_ROUND_CAP + 1
    line = result_line(Path("cap.txt"), out)
    assert line["tool_cap_hit"] == "rounds"
    assert line["forced_final_turn"] is True
    assert line["payment_status"] == "not_paid"


def test_repair_turn_sends_no_tools(monkeypatch):
    steps = [_echo(note=str(index), call_id=f"c{index}") for index in range(TOOL_ROUND_CAP)]
    steps.append(text_step("not-json"))
    steps.append(text_step(ANSWER))
    model = ScriptedToolModel(steps)
    step = tool_request(model, example_registry())
    clock = FakeClock()
    parsed = call_with_retries(
        step,
        [{"role": "user", "content": "Answer with JSON."}],
        Echoed,
        max_attempts=5,
        clock=clock,
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert parsed.echoed == "phase-1"
    assert clock.slept == []
    assert len(step.log) == TOOL_ROUND_CAP
    assert step.calls == TOOL_ROUND_CAP
    assert len(model.requests) == 1
    repair = model.requests[0]["messages"]
    assert repair[-1]["content"].startswith("The JSON failed validation:")
    assert all(message.get("role") != "tool" for message in repair)
    assert all(
        "The JSON failed validation:" not in (message.get("content") or "")
        for sent in model.tool_requests
        for message in sent["messages"]
    )


def test_retry_keeps_counting_toward_the_cap(monkeypatch):
    steps = [_echo(note=str(index), call_id=f"c{index}") for index in range(2)]
    steps.append(LLMError("http_429", True, "busy"))
    steps.append(_echo(note="after", call_id="after"))
    steps.append(text_step(ANSWER))
    model = ScriptedToolModel(steps)
    log = []
    clock = FakeClock()
    attempts = []
    parsed = call_with_retries(
        tool_request(model, example_registry(), log),
        [{"role": "user", "content": "Answer with JSON."}],
        Echoed,
        max_attempts=5,
        clock=clock,
        rng=ZeroRandom(),
        attempt_log=attempts,
    )
    assert parsed.echoed == "phase-1"
    assert len(attempts) == 1
    assert attempts[0]["error_type"] == "http_429"
    assert len(attempts[0]["tool_calls"]) == 2
    assert clock.slept == [1.0]
    assert [row["round"] for row in log] == [1, 2, 3]
    assert [row["attempt"] for row in log] == [1, 1, 2]


def test_retry_then_cap_logs_each_call_once(db_path, monkeypatch):
    steps = [
        _echo(note="a", call_id="a"),
        _echo(note="b", call_id="b"),
        LLMError("http_429", True, "busy"),
        _echo(note="c", call_id="c"),
        _echo(note="d", call_id="d"),
        text_step(AUDIT_PASS),
    ]
    out, clock, log, _model = _cap_hold(steps, db_path, monkeypatch)
    assert clock.slept == [1.0]
    assert out["outcome"] == "approve"
    assert [row["error_type"] for row in out["attempts"]] == ["http_429"]
    assert [row["call_id"] for row in out["attempts"][0]["tool_calls"]] == ["a", "b"]
    logged = out["logs"][-1]["result"]["tool_calls"]
    assert [row["call_id"] for row in logged] == ["a", "b", "c", "d"]
    assert logged[-1]["error"] == "blocked: tool_cap"
    assert logged[-1]["result"] is None
    assert [row["call_id"] for row in log] == ["a", "b", "c", "d"]
    assert out["logs"][-1]["result"]["tool_cap_hit"] == "rounds"
    assert out["logs"][-1]["result"]["forced_final_turn"] is True


def test_retry_does_not_reset_the_round_cap(monkeypatch):
    steps = [_echo(note=str(index), call_id=f"c{index}") for index in range(TOOL_ROUND_CAP)]
    steps.append(LLMError("http_429", True, "busy"))
    steps.append(_echo(note="over", call_id="over"))
    model = ScriptedToolModel(steps)
    log = []
    clock = FakeClock()
    attempts = []
    with pytest.raises(AgentStepFailed) as caught:
        call_with_retries(
            tool_request(model, example_registry(), log),
            [{"role": "user", "content": "Answer with JSON."}],
            Echoed,
            max_attempts=5,
            clock=clock,
            rng=ZeroRandom(),
            attempt_log=attempts,
        )
    assert caught.value.attempts == 2
    assert caught.value.error_type == "tool_cap"
    assert [row["error_type"] for row in attempts] == ["http_429", "tool_cap"]
    assert attempts[1]["retryable"] is False
    assert attempts[1]["cap"] == "rounds"
    assert clock.slept == [1.0]
    assert [row["error"] for row in log] == [None, None, None, "blocked: tool_cap"]
    assert [row["attempt"] for row in log[:-1]] == [1, 1, 1]
    assert log[-1]["attempt"] == 2
    assert log[-1]["result"] is None


def test_reusing_the_callable_for_a_second_step_starts_fresh(monkeypatch):
    steps = [_echo(note=f"a{index}", call_id=f"a{index}") for index in range(TOOL_ROUND_CAP)]
    steps.append(text_step(ANSWER))
    steps.append(_echo(note="b1", call_id="b1"))
    steps.append(text_step(ANSWER))
    model = ScriptedToolModel(steps)
    log = []
    request = tool_request(model, example_registry(), log)
    messages = [{"role": "user", "content": "Answer with JSON."}]
    clock = FakeClock()
    first = call_with_retries(
        request,
        messages,
        Echoed,
        max_attempts=3,
        clock=clock,
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert first.echoed == "phase-1"
    assert len(log) == TOOL_ROUND_CAP
    second = call_with_retries(
        request,
        messages,
        Echoed,
        max_attempts=3,
        clock=clock,
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert second.echoed == "phase-1"
    assert clock.slept == []
    assert len(log) == 1
    assert log[0]["round"] == 1
    assert log[0]["attempt"] == 1
    assert log[0]["call_id"] == "b1"


def test_a_new_tool_step_starts_fresh(monkeypatch):
    spent = ScriptedToolModel(
        [_echo(note=str(index), call_id=f"c{index}") for index in range(TOOL_ROUND_CAP + 1)]
    )
    spent_log = []
    with pytest.raises(LLMError) as caught:
        tool_request(spent, example_registry(), spent_log)(
            [{"role": "user", "content": "Answer with JSON."}]
        )
    assert caught.value.error_type == "tool_cap"
    assert [row["error"] for row in spent_log] == [None, None, None, "blocked: tool_cap"]

    fresh = ScriptedToolModel([_echo(call_id="fresh"), text_step(ANSWER)])
    fresh_log = []
    text = tool_request(fresh, example_registry(), fresh_log)(
        [{"role": "user", "content": "Answer with JSON."}]
    )
    assert text == ANSWER
    assert len(fresh_log) == 1
    assert fresh_log[0]["round"] == 1
    assert fresh_log[0]["attempt"] == 1
    assert fresh_log[0]["call_id"] == "fresh"


def _registry(name, function):
    return ToolRegistry(
        [
            Tool(
                name=name,
                description="Test tool.",
                parameters={"type": "object", "properties": {}},
                function=function,
            )
        ]
    )


def test_input_error_goes_back_to_the_model(monkeypatch):
    def missing(_arguments):
        raise ToolInputError("item not found")

    parsed, log, model = _finish(
        [tool_step(("c1", "lookup", "{}")), text_step(ANSWER)],
        monkeypatch,
        registry=_registry("lookup", missing),
    )
    assert parsed.echoed == "phase-1"
    assert log[0]["error"] == "item not found"
    assert log[0]["result"] is None
    assert log[0]["attempt"] == 1
    body = json.loads(model.tool_requests[1]["messages"][-1]["content"])
    assert body == {"error": "item not found"}


def test_infrastructure_error_is_a_system_error_hold(db_path, monkeypatch):
    def broken(_arguments):
        raise RuntimeError("database is locked")

    out, clock, log, model = _cap_hold(
        [tool_step(("c1", "broken", "{}")), text_step(ANSWER)],
        db_path,
        monkeypatch,
        registry=_registry("broken", broken),
    )
    _assert_system_error_hold(out, clock, executed=1, error_type="tool_error")
    assert log[0]["name"] == "broken"
    assert log[0]["arguments"] == {}
    assert log[0]["error"] == "database is locked"
    assert log[0]["result"] is None
    audit_log = next(row for row in out["logs"] if row["stage"] == "audit")
    assert audit_log["result"]["tool_calls"][0]["error"] == "database is locked"
    assert len(model.tool_requests) == 1
    assert model.steps[0]["content"] == ANSWER


def test_decimal_or_date_result_does_not_crash_the_node(monkeypatch):
    from datetime import date

    def priced(_arguments):
        return {"amount": Decimal("1.50"), "when": date(2026, 1, 2)}

    parsed, log, model = _finish(
        [tool_step(("c1", "priced", "{}")), text_step(ANSWER)],
        monkeypatch,
        registry=_registry("priced", priced),
    )
    assert parsed.echoed == "phase-1"
    assert log[0]["error"] is None
    assert log[0]["result"] == {"amount": "1.50", "when": "2026-01-02"}
    body = json.loads(model.tool_requests[1]["messages"][-1]["content"])
    assert body == log[0]["result"]


def _install_openai(monkeypatch, captured, content='{"echoed": "x"}'):
    import openai

    class Completions:
        def create(self, **kwargs):
            captured.append(kwargs)
            message = type("Msg", (), {"content": content, "tool_calls": None})()
            choice = type("Choice", (), {"message": message})()
            return type("Resp", (), {"choices": [choice]})()

    class Client:
        def __init__(self, **_kwargs):
            self.chat = type("Chat", (), {"completions": Completions()})()

    monkeypatch.setattr(openai, "OpenAI", Client)


def test_tool_request_sends_tools_on_the_wire(monkeypatch):
    captured = []
    _install_openai(monkeypatch, captured, content=ANSWER)
    client = GrokClient(api_key="test-key")
    text = tool_request(client, example_registry())(
        [{"role": "user", "content": "Answer with JSON."}]
    )
    assert text == ANSWER
    assert captured[0]["tools"] == example_registry().schemas()
    assert captured[0]["tool_choice"] == "auto"
    assert captured[0]["temperature"] == 0
    assert captured[0]["model"] == client.model


def test_only_audit_is_wired_to_the_tool_loop():
    audit_text = (ROOT / "pipeline/audit.py").read_text(encoding="utf-8")
    assert "tool_request" in audit_text
    assert "final_turn_on_cap=True" in audit_text
    for relative in ("llm_client.py", "tool_loop.py"):
        client_text = (ROOT / relative).read_text(encoding="utf-8")
        assert '== "audit"' not in client_text
        assert "== 'audit'" not in client_text
    critique = (ROOT / "pipeline/critique.py").read_text(encoding="utf-8")
    assert "tool_request" not in critique
    assert "request_tools" not in critique
    assert "audit_registry" not in critique
    for relative in (
        "pipeline/approval.py",
        "pipeline/ingestion.py",
        "graph.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "tool_request" not in text
        assert "request_tools" not in text


def test_only_the_audit_prompt_names_the_tools(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA\nTotal: 500.00\n")
    handler, _counts = scripted(extraction())
    fake = FakeLLM(handler)
    result = run_invoice(path, db_path, fake, clock=FakeClock(), rng=ZeroRandom(), max_attempts=3)
    assert result["outcome"] == "approve"
    for call in fake.calls:
        system = call[0]["content"]
        if task_of(call) == "audit":
            assert system.endswith(f"{TOOLS_LINE} {LEDGER_LINE} Return JSON only.")
        else:
            assert TOOLS_LINE not in system
            assert "check_payment_ledger" not in system


def test_final_turn_follows_the_flag_not_the_agent_name(monkeypatch):
    messages = [{"role": "user", "content": "Answer with JSON."}]

    def drive(agent, flag):
        steps = [_echo(note=str(index), call_id=f"c{index}") for index in range(TOOL_ROUND_CAP + 1)]
        steps.append(text_step(ANSWER))
        model = ScriptedToolModel(steps)
        step = tool_request(
            model, example_registry(), agent=agent, final_turn_on_cap=flag
        )
        return step, model

    named, named_model = drive("audit", False)
    with pytest.raises(LLMError) as caught:
        named(list(messages))
    assert caught.value.error_type == "tool_cap"
    assert named.forced_final_turn is False
    assert named_model.requests == []

    flagged, flagged_model = drive("other", True)
    assert flagged(list(messages)) == ANSWER
    assert flagged.forced_final_turn is True
    final = flagged_model.requests[-1]
    assert "tools" not in final
    assert "tool_choice" not in final
    assert final["messages"][-1]["content"].startswith("The tool cap is reached.")
