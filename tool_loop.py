"""The audit tool loop. Caps, repeats, and the tool log for one agent step.

tool_request() wraps a client so structured_call can use it like request().
Only audit calls it.
"""

import json
import time

from llm_errors import LLMError

# Not in policy.toml: Policy rejects unknown keys. A round is one model
# reply that contains tool calls. One ToolStep owns the counts for the
# whole step: the repair turn and every retry share them.
TOOL_ROUND_CAP = 3
TOOL_CALL_CAP = 8


class ToolStep:
    """Caps and the tool log for one agent step.

    Retries share the counts. set_attempt(1) starts a new step.
    The schema repair goes through repair(), which sends no tools.
    final_turn_on_cap asks for one plain reply at the cap instead of failing.
    """

    def __init__(
        self,
        llm,
        registry,
        tool_log: list | None = None,
        agent: str | None = None,
        *,
        final_turn_on_cap: bool = False,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.agent = agent
        self.final_turn_on_cap = final_turn_on_cap
        self.log = [] if tool_log is None else tool_log
        self.rounds = 0
        self.calls = 0
        self.attempt = 1
        self.cap_hit = None
        self.forced_final_turn = False

    def set_attempt(self, attempt: int) -> None:
        """call_with_retries reports the attempt. 1 starts a step. A retry does not."""

        if attempt == 1:
            self.cap_hit = None
            self.forced_final_turn = False
            if self.rounds or self.calls:
                self.rounds = 0
                self.calls = 0
                self.log.clear()
        self.attempt = attempt

    def repair(self, messages, timeout=60) -> str:
        """Plain JSON fix. No tools, so this turn cannot hit the tool cap."""

        try:
            return self.llm.request(messages, timeout)
        except LLMError as exc:
            exc.log.setdefault("tool_calls", list(self.log))
            if self.forced_final_turn:
                exc.retryable = False
            raise

    def __call__(self, messages, timeout=60) -> str:
        return _tool_loop(self, messages, timeout)


def tool_request(
    llm,
    registry,
    tool_log: list | None = None,
    agent: str | None = None,
    *,
    final_turn_on_cap: bool = False,
) -> ToolStep:
    """A request(messages) -> str that structured_call already knows how to use.

    Run tool calls until the model returns plain content, then return that
    text. The repair turn, if any, is repair() and sends no tools.
    final_turn_on_cap takes one plain reply when the cap blocks the next call.
    """

    return ToolStep(
        llm,
        registry,
        tool_log,
        agent,
        final_turn_on_cap=final_turn_on_cap,
    )


def _tool_loop(step: ToolStep, messages, timeout) -> str:
    conversation = list(messages)
    schemas = step.registry.schemas()
    while True:
        try:
            reply = step.llm.request_tools(conversation, schemas, timeout)
        except LLMError as exc:
            exc.log.setdefault("tool_calls", list(step.log))
            raise
        # A reply that still has tool calls is a tool turn. Its text is not the answer.
        pending = reply.get("tool_calls") or []
        calls = [
            _normalize_call(item, step.rounds + 1, index)
            for index, item in enumerate(pending, start=1)
        ]
        if not calls:
            return reply.get("content") or ""
        next_round = step.rounds + 1
        # An identical repeat does not count. The round still does.
        fresh = _fresh_call_count(step, calls)
        next_calls = step.calls + fresh
        if next_round > TOOL_ROUND_CAP or next_calls > TOOL_CALL_CAP:
            which = "rounds" if next_round > TOOL_ROUND_CAP else "calls"
            for call in calls:
                step.log.append(
                    _record(step, call, next_round, None, "blocked: tool_cap", time.perf_counter())
                )
            step.cap_hit = which
            if step.final_turn_on_cap:
                return _forced_final_turn(step, conversation, timeout)
            raise LLMError(
                "tool_cap",
                False,
                f"tool {which} cap reached",
                log={"tool_calls": list(step.log), "cap": which},
            )
        step.rounds = next_round
        step.calls = next_calls
        conversation.append(_assistant_tool_message(reply, calls))
        for call in calls:
            prior = _prior_call(step, call)
            if prior is not None:
                record = _record(
                    step,
                    call,
                    next_round,
                    prior.get("result"),
                    prior.get("error"),
                    time.perf_counter(),
                    repeat=True,
                )
                step.log.append(record)
                conversation.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": _cached_body(prior)}
                )
                continue
            try:
                record, tool_message = _run_call(step, call, next_round)
            except LLMError as exc:
                exc.log.setdefault("tool_calls", list(step.log))
                raise
            step.log.append(record)
            conversation.append(tool_message)


def _forced_final_turn(step: ToolStep, conversation: list, timeout) -> str:
    """One plain completion after the cap. The payload has no tools."""

    step.forced_final_turn = True
    messages = list(conversation) + [
        {
            "role": "user",
            "content": (
                "The tool cap is reached. Use the tool results and verified facts above. "
                "Return the audit verdict JSON now."
            ),
        }
    ]
    try:
        return step.llm.request(messages, timeout)
    except LLMError as exc:
        # An API error on that same last chance is a hold too, not another attempt.
        exc.retryable = False
        exc.log.setdefault("tool_calls", list(step.log))
        raise


def _normalize_call(call: dict, round_number: int, index: int) -> dict:
    return {
        "id": str(call.get("id") or f"call_{round_number}_{index}"),
        "name": str(call.get("name") or ""),
        "arguments": argument_text(call.get("arguments")),
    }


def argument_text(raw) -> str:
    """The one place a non-string tool argument becomes JSON text."""

    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, default=str)


def _assistant_tool_message(reply: dict, calls: list[dict]) -> dict:
    text = reply.get("content") or ""
    return {
        "role": "assistant",
        "content": text if text else None,
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in calls
        ],
    }


def _fresh_call_count(step: ToolStep, calls: list[dict]) -> int:
    """Calls in this round that are not an identical repeat.

    Same name and same arguments as a call already logged, or as an earlier
    call in this round, does not count. Invalid JSON is never a repeat.
    """

    seen = []
    fresh = 0
    for call in calls:
        parsed, error, _raw = parse_tool_arguments(call["arguments"])
        if error is None and (
            _prior_call(step, call) is not None or (call["name"], parsed) in seen
        ):
            continue
        fresh += 1
        if error is None:
            seen.append((call["name"], parsed))
    return fresh


def _prior_call(step: ToolStep, call: dict) -> dict | None:
    """The earlier run of this same name and arguments, if this step already has one."""

    parsed, error, _raw = parse_tool_arguments(call["arguments"])
    if error is not None:
        return None
    for row in step.log:
        # A repeat is a copy of an earlier run. The run is the result to return.
        if row.get("repeat"):
            continue
        if row.get("name") == call["name"] and row.get("arguments") == parsed:
            return row
    return None


def _cached_body(row: dict) -> str:
    if row.get("error"):
        return json.dumps({"error": row["error"]})
    return json.dumps(row.get("result"), default=str)


def _run_call(step: ToolStep, call: dict, round_number: int) -> tuple[dict, dict]:
    started = time.perf_counter()
    try:
        payload = step.registry.call(call["name"], call["arguments"])
    except LLMError:
        raise
    except Exception as exc:
        # A lookup miss is already {"error": ...}. Anything else is infrastructure.
        # The call is logged before the hold, including its name, arguments, and error.
        record = _record(step, call, round_number, None, str(exc), started)
        step.log.append(record)
        raise LLMError(
            "tool_error",
            False,
            str(exc),
            log={"tool_calls": list(step.log)},
        ) from None
    body = _json_ready(payload)
    payload = json.loads(body)
    error = _error_text(payload)
    record = _record(step, call, round_number, None if error else payload, error, started)
    return record, {"role": "tool", "tool_call_id": call["id"], "content": body}


def _record(step: ToolStep, call: dict, round_number: int, result, error, started: float, repeat: bool = False) -> dict:
    parsed, _parse_error, invalid_raw = parse_tool_arguments(call["arguments"])
    record = {
        "round": round_number,
        "attempt": step.attempt,
        "call_id": call["id"],
        "name": call["name"],
        "arguments": parsed,
        "result": result,
        "error": error,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }
    if step.agent:
        record = {"agent": step.agent, **record}
    if invalid_raw is not None:
        record["arguments_raw"] = invalid_raw
    if repeat:
        record["repeat"] = True
    return record


def parse_tool_arguments(raw: str) -> tuple[dict | None, str | None, str | None]:
    """Return (object, error, raw_if_the_JSON_itself_was_invalid).

    An empty string is an empty object. A JSON array is an error, and the
    raw text is not kept. Only invalid JSON sets the third value.
    """

    if raw.strip() == "":
        return {}, None, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, "arguments are not valid JSON", raw
    if not isinstance(value, dict):
        return None, "arguments must be a JSON object", None
    return value, None, None


def _json_ready(payload: dict) -> str:
    """JSON text for the model. The caller parses it once for the log.

    Decimal and date become strings. A value that still will not serialize
    becomes an error result, so the node does not crash.
    """

    try:
        return json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return json.dumps({"error": "tool result is not JSON"})


def _error_text(payload: dict) -> str | None:
    """A failed tool result is exactly {"error": "<reason>"}."""

    if list(payload) == ["error"] and isinstance(payload.get("error"), str):
        return payload["error"]
    return None
