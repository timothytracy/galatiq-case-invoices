"""The tool loop. No network. No agent is given a registry."""

import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from db import create_database
from graph import route_after_audit, run_invoice
from llm_client import (
    DEFAULT_MODEL,
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
from pipeline.payment import _USED_KEYS, payment
from pipeline.tools import Tool, ToolInputError, ToolRegistry, example_registry
from tests.fakes import FakeClock, FakeLLM, ScriptedToolModel, ZeroRandom
from tests.test_pipeline import extraction, scripted, write_invoice

ROOT = Path(__file__).resolve().parents[1]
ANSWER = '{"echoed": "phase-1"}'
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
    monkeypatch.setenv("XAI_TOOLS", "1")
    model = ScriptedToolModel(steps)
    step = tool_request(model, registry or example_registry())
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
    assert clock.slept == []
    return parsed, step.log, model


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
        raw_text="Total: 500.00\n",
        invoice=invoice,
        validation=ValidationReport(passed=True, findings=[]),
        decision=decision,
    )


class _LoopLLM:
    """audit() calls llm.request. The request here is the tool loop."""

    def __init__(self, request):
        self.request = request


def _cap_hold(steps, db_path, monkeypatch, registry=None):
    monkeypatch.setenv("XAI_TOOLS", "1")
    model = ScriptedToolModel(steps)
    step = tool_request(model, registry or example_registry())
    clock = FakeClock()
    out = audit(
        _payable_state(),
        _LoopLLM(step),
        db_path,
        clock,
        ZeroRandom(),
        5,
    )
    return out, clock, step.log, model


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


def test_round_cap_is_a_system_error_hold_without_retry_or_payment(db_path, monkeypatch):
    assert TOOL_ROUND_CAP == 3
    steps = [_echo(call_id=f"c{index}") for index in range(TOOL_ROUND_CAP + 1)]
    out, clock, log, model = _cap_hold(steps, db_path, monkeypatch)
    _assert_system_error_hold(out, clock, executed=TOOL_ROUND_CAP + 1)
    assert out["attempts"][0]["cap"] == "rounds"
    assert [row["round"] for row in log] == [1, 2, 3, 4]
    assert [row["error"] for row in log] == [None, None, None, "blocked: tool_cap"]
    assert log[-1]["result"] is None
    assert log[-1]["name"] == "echo_note"
    assert len(model.tool_requests) == TOOL_ROUND_CAP + 1
    assert model.requests == []


def test_call_cap_is_a_system_error_hold_without_retry_or_payment(db_path, monkeypatch):
    assert TOOL_CALL_CAP == 8
    eight = [
        (f"c{index}", "echo_note", json.dumps({"note": str(index)}))
        for index in range(TOOL_CALL_CAP)
    ]
    steps = [tool_step(*eight), _echo(call_id="over")]
    out, clock, log, model = _cap_hold(steps, db_path, monkeypatch)
    _assert_system_error_hold(out, clock, executed=TOOL_CALL_CAP + 1)
    assert out["attempts"][0]["cap"] == "calls"
    assert log[-1]["error"] == "blocked: tool_cap"
    assert log[-1]["result"] is None
    assert log[-1]["call_id"] == "over"
    assert log[-1]["round"] == 2
    assert {row["round"] for row in log[:-1]} == {1}
    assert all(row["error"] is None for row in log[:-1])
    assert len(model.tool_requests) == 2
    assert model.requests == []


def test_repair_turn_sends_no_tools(monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", "1")
    steps = [_echo(call_id=f"c{index}") for index in range(TOOL_ROUND_CAP)]
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
    monkeypatch.setenv("XAI_TOOLS", "1")
    steps = [_echo(call_id=f"c{index}") for index in range(2)]
    steps.append(LLMError("http_429", True, "busy"))
    steps.append(_echo(call_id="after"))
    steps.append(text_step(ANSWER))
    model = ScriptedToolModel(steps)
    step = tool_request(model, example_registry())
    clock = FakeClock()
    attempts = []
    parsed = call_with_retries(
        step,
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
    assert [row["round"] for row in step.log] == [1, 2, 3]
    assert [row["attempt"] for row in step.log] == [1, 1, 2]


def test_retry_then_cap_logs_each_call_once(db_path, monkeypatch):
    steps = [
        _echo(call_id="a"),
        _echo(call_id="b"),
        LLMError("http_429", True, "busy"),
        _echo(call_id="c"),
        _echo(call_id="d"),
    ]
    out, clock, log, _model = _cap_hold(steps, db_path, monkeypatch)
    assert clock.slept == [1.0]
    assert [row["error_type"] for row in out["attempts"]] == ["http_429", "tool_cap"]
    assert [row["call_id"] for row in out["attempts"][0]["tool_calls"]] == ["a", "b"]
    logged = out["logs"][-1]["result"]["tool_calls"]
    assert [row["call_id"] for row in logged] == ["a", "b", "c", "d"]
    assert logged[-1]["error"] == "blocked: tool_cap"
    assert logged[-1]["result"] is None
    assert [row["call_id"] for row in log] == ["a", "b", "c", "d"]


def test_retry_does_not_reset_the_round_cap(monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", "1")
    steps = [_echo(call_id=f"c{index}") for index in range(TOOL_ROUND_CAP)]
    steps.append(LLMError("http_429", True, "busy"))
    steps.append(_echo(call_id="over"))
    model = ScriptedToolModel(steps)
    step = tool_request(model, example_registry())
    clock = FakeClock()
    attempts = []
    with pytest.raises(AgentStepFailed) as caught:
        call_with_retries(
            step,
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
    assert [row["error"] for row in step.log] == [None, None, None, "blocked: tool_cap"]
    assert [row["attempt"] for row in step.log[:-1]] == [1, 1, 1]


def test_reusing_the_callable_for_a_second_step_starts_fresh(monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", "1")
    steps = [_echo(call_id=f"a{index}") for index in range(TOOL_ROUND_CAP)]
    steps.append(text_step(ANSWER))
    steps.append(_echo(call_id="b1"))
    steps.append(text_step(ANSWER))
    model = ScriptedToolModel(steps)
    request = tool_request(model, example_registry())
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
    first_log = request.log
    assert len(first_log) == TOOL_ROUND_CAP
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
    assert first_log is not request.log
    assert len(first_log) == TOOL_ROUND_CAP
    assert len(request.log) == 1
    assert request.log[0]["round"] == 1
    assert request.log[0]["attempt"] == 1
    assert request.log[0]["call_id"] == "b1"


def test_calling_the_step_twice_does_not_crash(monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", "1")
    model = ScriptedToolModel([text_step(ANSWER), text_step(ANSWER)])
    step = tool_request(model, example_registry())
    messages = [{"role": "user", "content": "Answer with JSON."}]
    assert step(messages) == ANSWER
    assert step(messages) == ANSWER


def test_a_new_tool_step_starts_fresh(monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", "1")
    spent = ScriptedToolModel([_echo(call_id=f"c{index}") for index in range(TOOL_ROUND_CAP + 1)])
    spent_step = tool_request(spent, example_registry())
    with pytest.raises(LLMError) as caught:
        spent_step([{"role": "user", "content": "Answer with JSON."}])
    assert caught.value.error_type == "tool_cap"
    assert [row["error"] for row in spent_step.log] == [None, None, None, "blocked: tool_cap"]

    fresh = ScriptedToolModel([_echo(call_id="fresh"), text_step(ANSWER)])
    fresh_step = tool_request(fresh, example_registry())
    text = fresh_step([{"role": "user", "content": "Answer with JSON."}])
    assert text == ANSWER
    assert len(fresh_step.log) == 1
    assert fresh_step.log[0]["round"] == 1
    assert fresh_step.log[0]["attempt"] == 1
    assert fresh_step.log[0]["call_id"] == "fresh"


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


def _canon(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_flag_off_payload_is_byte_identical_to_request(monkeypatch):
    captured = []
    _install_openai(monkeypatch, captured)
    client = GrokClient(api_key="test-key")
    messages = [{"role": "user", "content": "Return a JSON object."}]
    monkeypatch.delenv("XAI_TOOLS", raising=False)
    client.request(messages)
    plain = _canon(captured[0])
    for value in (None, "0"):
        fresh = [{"role": "user", "content": "Return a JSON object."}]
        if value is None:
            monkeypatch.delenv("XAI_TOOLS", raising=False)
        else:
            monkeypatch.setenv("XAI_TOOLS", value)
        tool_request(client, example_registry())(fresh)
    assert len(captured) == 3
    assert _canon(captured[1]) == plain
    assert _canon(captured[2]) == plain
    assert "tools" not in captured[0]
    assert "tool_choice" not in captured[0]
    assert captured[0]["temperature"] == 0
    assert captured[0]["messages"] == messages
    assert captured[0]["model"] == DEFAULT_MODEL


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_flag_off_sends_no_tools(value, monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", value)
    model = ScriptedToolModel([text_step(ANSWER), _echo()])
    step = tool_request(model, example_registry())
    text = step([{"role": "user", "content": "Answer with JSON."}])
    assert text == ANSWER
    assert step.log == []
    assert model.tool_requests == []
    assert model.requests[0]["messages"][0]["content"] == "Answer with JSON."
    assert len(model.steps) == 1
    assert model.steps[0]["tool_calls"]


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_flag_on_sends_tools_on_the_wire(value, monkeypatch):
    monkeypatch.setenv("XAI_TOOLS", value)
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


def test_only_critique_is_wired_to_the_tool_loop():
    critique = (ROOT / "pipeline/critique.py").read_text(encoding="utf-8")
    assert "tool_request" in critique
    for relative in (
        "pipeline/audit.py",
        "pipeline/approval.py",
        "pipeline/ingestion.py",
        "graph.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "tool_request" not in text
        assert "request_tools" not in text
        assert "XAI_TOOLS" not in text


def test_flag_does_not_change_agent_prompts(tmp_path, db_path, monkeypatch):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA\nTotal: 500.00\n")
    seen = []
    for value in (None, "0", "1"):
        _USED_KEYS.clear()
        if value is None:
            monkeypatch.delenv("XAI_TOOLS", raising=False)
        else:
            monkeypatch.setenv("XAI_TOOLS", value)
        handler, _counts = scripted(extraction())
        fake = FakeLLM(handler)
        result = run_invoice(
            path,
            db_path,
            fake,
            clock=FakeClock(),
            rng=ZeroRandom(),
            max_attempts=3,
        )
        assert result["outcome"] == "approve"
        seen.append(fake.calls)
    assert seen[0] == seen[1] == seen[2]
    encoded = [
        json.dumps(calls, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for calls in seen
    ]
    assert encoded[0] == encoded[1] == encoded[2]
