"""Grok via the OpenAI SDK at https://api.x.ai/v1.

A missing XAI_API_KEY raises MissingAPIKey. Timeouts, 429, 5xx, network
errors, and JSON that is still invalid after the schema repair are retried.
Other 4xx errors are not.

XAI_TOOLS defaults off. request() never sends tools. tool_request() sends
them only when that flag is on. Critique calls tool_request when the flag is on.
"""

import json
import os
import random
import re
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from decision.policy import POLICY

DEFAULT_MODEL = "grok-4.20-0309-non-reasoning"
BASE_URL = "https://api.x.ai/v1"
DEFAULT_MAX_ATTEMPTS = POLICY.model_retry_attempts
SCHEMA_REPAIR_LIMIT = 2
# Not in policy.toml: Policy rejects unknown keys. A round is one model
# reply that contains tool calls. One ToolStep owns the counts for the
# whole step: the repair turn and every retry share them.
TOOL_ROUND_CAP = 3
TOOL_CALL_CAP = 8
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
_TOOLS_ON = {"1", "true", "yes", "on"}

_MISSING_KEY = (
    "XAI_API_KEY is not set. Add it to the environment or to a .env file "
    "(see .env.example). The pipeline does not run without a key."
)


class MissingAPIKey(RuntimeError):
    """Raised at startup when no key is available."""


class LLMError(Exception):
    """One failed model call. log is copied onto the attempt record."""

    def __init__(
        self,
        error_type: str,
        retryable: bool,
        message: str = "",
        *,
        log: dict | None = None,
    ) -> None:
        super().__init__(message or error_type)
        self.error_type = error_type
        self.retryable = retryable
        self.log = {} if log is None else log


class AgentStepFailed(Exception):
    """The retries are finished, or the error cannot be retried."""

    def __init__(self, attempts: int, error_type: str) -> None:
        self.attempts = attempts
        self.error_type = error_type
        super().__init__(f"agent step failed after {attempts} attempts: {error_type}")


class SleepClock:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines. Existing variables win."""

    env_path = path or Path(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_max_attempts() -> int:
    raw = os.environ.get("XAI_MAX_ATTEMPTS", "").strip()
    if not raw:
        return DEFAULT_MAX_ATTEMPTS
    value = int(raw)
    if value < 1:
        raise ValueError("XAI_MAX_ATTEMPTS must be at least 1")
    return value


def tools_enabled() -> bool:
    """XAI_TOOLS is the switch and the rollback. Unset and anything else are off."""

    return os.environ.get("XAI_TOOLS", "").strip().lower() in _TOOLS_ON


def backoff_seconds(failed_attempt: int, rng: random.Random) -> float:
    """1, 2, 4, 8, 16 seconds, plus up to 25% jitter."""

    index = min(failed_attempt - 1, len(BACKOFF_SECONDS) - 1)
    base = BACKOFF_SECONDS[index]
    jitter = rng.uniform(0, base * 0.25)
    return base + jitter


def classify_exception(exc: BaseException) -> LLMError:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    text = str(exc)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return LLMError(f"http_{status}", True, text)
    if isinstance(status, int) and 400 <= status < 500:
        return LLMError(f"http_{status}", False, text)
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return LLMError("timeout", True, text)
    if "connection" in name or isinstance(exc, (TimeoutError, ConnectionError)):
        return LLMError("network", True, text)
    return LLMError(name or "unknown", False, text)


def parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped, parse_float=_decimal)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(stripped[start : end + 1], parse_float=_decimal)
    if not isinstance(value, dict):
        raise ValueError("model output must be a JSON object")
    return value


def structured_call(request, messages: list[dict], schema: type[BaseModel], check=None) -> BaseModel:
    """One repair turn if the JSON does not validate. check may raise LLMError.

    A tool step's repair() is a plain completion: no tools, so it cannot spend
    the tool budget. Any other request is called the same way on both turns.
    """

    conversation = list(messages)
    error_text = "empty response"
    plain = getattr(request, "repair", None)
    for turn in range(SCHEMA_REPAIR_LIMIT):
        if turn and plain is not None:
            text = plain(conversation)
        else:
            text = request(conversation)
        try:
            parsed = schema.model_validate(parse_json_object(text))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            error_text = str(exc)[:2000]
            conversation = conversation + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "The JSON failed validation: "
                        + error_text
                        + ". Return only a JSON object that matches the schema."
                    ),
                },
            ]
            continue
        if check is not None:
            check(parsed)
        return parsed
    raise LLMError("malformed_output", True, error_text)


def call_with_retries(
    request,
    messages: list[dict],
    schema: type[BaseModel],
    *,
    max_attempts: int,
    clock,
    rng: random.Random,
    attempt_log: list[dict],
    check=None,
) -> BaseModel:
    """Retry a structured call. Sleep goes through clock so tests can fake time."""

    last_type = "unknown"
    for attempt in range(1, max_attempts + 1):
        mark_attempt = getattr(request, "set_attempt", None)
        if mark_attempt is not None:
            mark_attempt(attempt)
        try:
            return structured_call(request, messages, schema, check=check)
        except LLMError as exc:
            last_type = exc.error_type
            give_up = (not exc.retryable) or attempt == max_attempts
            delay = None if give_up else backoff_seconds(attempt, rng)
            entry = {
                "attempt": attempt,
                "error_type": exc.error_type,
                "backoff_seconds": delay,
                "retryable": exc.retryable,
            }
            entry.update(exc.log)
            attempt_log.append(entry)
            print(json.dumps({"record": "attempt", **entry}), flush=True)
            if give_up:
                raise AgentStepFailed(attempt, exc.error_type) from None
            clock.sleep(delay)
    raise AgentStepFailed(max_attempts, last_type)


class GrokClient:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        load_dotenv()
        if api_key is None:
            key = os.environ.get("XAI_API_KEY", "")
        else:
            key = api_key
        key = key.strip()
        if not key:
            raise MissingAPIKey(_MISSING_KEY)
        self.api_key = key
        chosen = (model or os.environ.get("XAI_MODEL", "")).strip()
        self.model = chosen or DEFAULT_MODEL

    def __repr__(self) -> str:
        return f"GrokClient(model={self.model!r})"

    def request(self, messages: list[dict], timeout: float = 60) -> str:
        """One completion, no tools. This payload stays as it is today."""

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=BASE_URL, timeout=timeout)
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
            )
        except Exception as exc:
            message = str(exc).replace(self.api_key, "[redacted]")
            classified = classify_exception(exc)
            raise LLMError(classified.error_type, classified.retryable, message) from None
        return _message_text(response.choices[0].message.content)

    def request_tools(self, messages: list[dict], tools: list[dict], timeout: float = 60) -> dict:
        """One completion that may ask for tools. Used only when XAI_TOOLS is on.

        Returns {"content": str, "tool_calls": [{"id", "name", "arguments"}]}.
        arguments is the raw JSON string from the model.
        """

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=BASE_URL, timeout=timeout)
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:
            message = str(exc).replace(self.api_key, "[redacted]")
            classified = classify_exception(exc)
            raise LLMError(classified.error_type, classified.retryable, message) from None
        message = response.choices[0].message
        calls = [_read_tool_call(item) for item in (message.tool_calls or [])]
        return {"content": _message_text(message.content), "tool_calls": calls}


class ToolStep:
    """Caps and the tool log for one agent step.

    Retries share the counts. set_attempt(1) starts a new step and a new log.
    The schema repair goes through repair(), which sends no tools.
    """

    def __init__(self, llm, registry, agent: str | None = None) -> None:
        self.llm = llm
        self.registry = registry
        self.agent = agent
        self.log = []
        self.rounds = 0
        self.calls = 0
        self.attempt = 1

    def set_attempt(self, attempt: int) -> None:
        """call_with_retries reports the attempt. 1 starts a step. A retry does not."""

        if attempt == 1:
            self.rounds = 0
            self.calls = 0
            # A new list. clear() would wipe a list the caller still holds.
            self.log = []
        self.attempt = attempt

    def repair(self, messages, timeout=60) -> str:
        """Plain JSON fix. No tools, so this turn cannot hit the tool cap."""

        try:
            return self.llm.request(messages, timeout)
        except LLMError as exc:
            exc.log.setdefault("tool_calls", list(self.log))
            raise

    def __call__(self, messages, timeout=60) -> str:
        if not tools_enabled():
            return self.llm.request(messages, timeout)
        return _tool_loop(self, messages, timeout)


def tool_request(llm, registry, agent: str | None = None) -> ToolStep:
    """A request(messages) -> str that structured_call already knows how to use.

    Flag off: llm.request, the same payload as today.
    Flag on: run tool calls until the model returns plain content, then return
    that text. The repair turn, if any, is repair() and sends no tools.
    Read step.log after the call.
    """

    return ToolStep(llm, registry, agent)


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
        next_calls = step.calls + len(calls)
        if next_round > TOOL_ROUND_CAP or next_calls > TOOL_CALL_CAP:
            which = "rounds" if next_round > TOOL_ROUND_CAP else "calls"
            for call in calls:
                step.log.append(
                    _record(step, call, next_round, None, "blocked: tool_cap", time.perf_counter())
                )
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
            try:
                record, tool_message = _run_call(step, call, next_round)
            except LLMError as exc:
                exc.log.setdefault("tool_calls", list(step.log))
                raise
            step.log.append(record)
            conversation.append(tool_message)


def _normalize_call(call: dict, round_number: int, index: int) -> dict:
    return {
        "id": str(call.get("id") or f"call_{round_number}_{index}"),
        "name": str(call.get("name") or ""),
        "arguments": _argument_text(call.get("arguments")),
    }


def _argument_text(raw) -> str:
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


def _run_call(step: ToolStep, call: dict, round_number: int) -> tuple[dict, dict]:
    started = time.perf_counter()
    try:
        payload = step.registry.call(call["name"], call["arguments"])
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


def _record(step: ToolStep, call: dict, round_number: int, result, error, started: float) -> dict:
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


def _message_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def _read_tool_call(item) -> dict:
    function = item.function
    return {
        "id": str(item.id or ""),
        "name": str(function.name or ""),
        "arguments": function.arguments,
    }


def _decimal(value: str):
    from decimal import Decimal

    return Decimal(value)
