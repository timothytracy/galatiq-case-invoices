"""Grok via the OpenAI SDK at https://api.x.ai/v1.

A missing XAI_API_KEY raises MissingAPIKey. Timeouts, 429, 5xx, network
errors, and JSON that is still invalid after the schema repair are retried.
Other 4xx errors are not.

XAI_TOOLS defaults on for audit. request() never sends tools. tool_request()
sends them unless the value is 0, false, off, or no. Any other token raises
ModeError at startup. Critique does not call tool_request.

XAI_MODE is live (default), record, or replay. Live sends the same payload
as before and does not open a recording. Record appends each reply after
the live call. Replay answers from that file and does not construct a
network client. A replay miss is a non-retryable error, never a live call.
"""

import json
import os
import random
import re
import time
from datetime import date
from pathlib import Path

from recording import (
    RecordStore,
    ReplayStore,
    estimate_cost,
    format_usage_line,
    parse_as_of,
    request_fingerprint,
)

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
_TOOLS_OFF = frozenset({"0", "false", "off", "no"})
_TOOLS_ON = frozenset({"", "1"})

_MISSING_KEY = (
    "XAI_API_KEY is not set. Add it to the environment or to a .env file "
    "(see .env.example). The pipeline does not run without a key."
)


class MissingAPIKey(RuntimeError):
    """Raised at startup when no key is available."""


class ModeError(ValueError):
    """XAI_MODE, the recording path, or --as-of cannot be used as given."""


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
    """Audit tools default on. 0, false, off, and no turn them off.

    The value is stripped and case-folded. Unset, empty, and 1 are on.
    Any other token raises ModeError. The tools-off request is unchanged.
    """

    raw = os.environ.get("XAI_TOOLS")
    if raw is None:
        return True
    value = raw.strip().lower()
    if value in _TOOLS_OFF:
        return False
    if value in _TOOLS_ON:
        return True
    raise ModeError(
        f"XAI_TOOLS={value!r} is not recognized. "
        "Unset, empty, and 1 turn audit tools on. "
        "0, false, off, and no turn them off."
    )


def resolve_run_mode(record: str | None, replay: str | None) -> tuple[str, str | None]:
    """CLI flags win. Otherwise XAI_MODE and XAI_RECORDING.

    Returns (live|record|replay, path or None). Raises ModeError on a bad pair.
    """

    if record and replay:
        raise ModeError("pass only one of --record and --replay")
    if record:
        return "record", record
    if replay:
        return "replay", replay
    raw = os.environ.get("XAI_MODE", "").strip().lower()
    if raw in ("", "live"):
        return "live", None
    if raw not in ("record", "replay"):
        raise ModeError("XAI_MODE must be live, record, or replay")
    path = os.environ.get("XAI_RECORDING", "").strip()
    if not path:
        raise ModeError("XAI_RECORDING is required when XAI_MODE is record or replay")
    return raw, path


def begin_client(
    mode: str, recording: str | None, as_of_text: str | None
) -> tuple["GrokClient", date | None]:
    """One client for a run, and the date the agents should be told.

    Live returns as_of only when as_of_text is set, so the default run
    still uses the wall clock. Record stores as_of_text or today's date
    in the header. Replay returns the header date and ignores the wall clock.
    """

    cli_day = parse_as_of(as_of_text) if as_of_text else None
    if mode == "live":
        return GrokClient(mode="live"), cli_day
    if mode == "record":
        day = cli_day or date.today()
        client = GrokClient(mode="record", recording=recording, as_of=day)
        return client, day
    if mode == "replay":
        client = GrokClient(mode="replay", recording=recording)
        if client.as_of is None:
            raise ModeError(f"recording header has no as_of date: {recording}")
        if cli_day is not None and cli_day != client.as_of:
            raise ModeError(
                f"--as-of {cli_day.isoformat()} does not match the recording "
                f"as_of {client.as_of.isoformat()}"
            )
        return client, client.as_of
    raise ModeError("mode must be live, record, or replay")


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
    # The cap already spent the tool budget, so this plain answer is the last
    # chance. A schema failure after the one repair is a hold and is not
    # retried. That keeps cost and latency bounded.
    if getattr(request, "forced_final_turn", False):
        raise LLMError(
            "malformed_output",
            False,
            error_text,
            log={"tool_calls": list(request.log)},
        )
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


def _normalize_mode(mode: str | None) -> str:
    """Explicit mode wins. Otherwise XAI_MODE. Unset is live."""

    raw = mode if mode is not None else os.environ.get("XAI_MODE", "")
    name = raw.strip().lower()
    if name in ("", "live"):
        return "live"
    if name in ("record", "replay"):
        return name
    raise ModeError("XAI_MODE must be live, record, or replay")


def _chosen_model(model: str | None) -> str:
    chosen = (model or os.environ.get("XAI_MODEL", "")).strip()
    return chosen or DEFAULT_MODEL


def _required_recording(recording: str | Path | None) -> Path:
    """The path for record or replay. Live never calls this, so it opens nothing."""

    if recording is not None and str(recording).strip():
        return Path(recording)
    env = os.environ.get("XAI_RECORDING", "").strip()
    if env:
        return Path(env)
    raise ModeError("XAI_RECORDING is required when XAI_MODE is record or replay")


def _usage_dict(usage) -> dict:
    """prompt, completion, and total tokens. Missing usage is recorded as zeros."""

    def grab(name: str) -> int:
        if usage is None:
            return 0
        if isinstance(usage, dict):
            return int(usage.get(name) or 0)
        return int(getattr(usage, name, 0) or 0)

    prompt = grab("prompt_tokens")
    completion = grab("completion_tokens")
    total = grab("total_tokens")
    if not total:
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _answer_from_message(message) -> dict:
    """The assistant message replay needs: content, and tool calls when present."""

    calls = []
    for item in getattr(message, "tool_calls", None) or []:
        call = _read_tool_call(item)
        calls.append(
            {
                "id": call["id"],
                "name": call["name"],
                "arguments": _argument_text(call.get("arguments")),
            }
        )
    return {
        "content": _message_text(getattr(message, "content", None)),
        "tool_calls": calls or None,
    }


class GrokClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        mode: str | None = None,
        recording: str | Path | None = None,
        as_of: date | None = None,
    ) -> None:
        self.mode = _normalize_mode(mode)
        self._record: RecordStore | None = None
        self._replay: ReplayStore | None = None
        self.as_of: date | None = None
        if self.mode == "replay":
            # No key and no network client. The header supplies model and as_of.
            store = ReplayStore(_required_recording(recording))
            self._replay = store
            self.api_key = ""
            self.model = store.model or _chosen_model(model)
            self.as_of = store.as_of
            return
        load_dotenv()
        if api_key is None:
            key = os.environ.get("XAI_API_KEY", "")
        else:
            key = api_key
        key = key.strip()
        if not key:
            raise MissingAPIKey(_MISSING_KEY)
        self.api_key = key
        self.model = _chosen_model(model)
        if self.mode == "record":
            day = as_of or date.today()
            self.as_of = day
            self._record = RecordStore(_required_recording(recording), day, self.model)

    def __repr__(self) -> str:
        return f"GrokClient(model={self.model!r})"

    def usage_summary(self) -> dict:
        """Tokens and estimated USD from the lines this client recorded or replayed."""

        rows: list[dict] = []
        if self._record is not None:
            rows = self._record.usages
        elif self._replay is not None:
            rows = self._replay.used
        prompt = sum(int(row["prompt_tokens"]) for row in rows)
        completion = sum(int(row["completion_tokens"]) for row in rows)
        total = sum(int(row["total_tokens"]) for row in rows)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cost_usd": estimate_cost(rows),
        }

    def usage_line(self) -> str:
        summary = self.usage_summary()
        return format_usage_line(summary["total_tokens"], summary["cost_usd"])

    def request(self, messages: list[dict], timeout: float = 60) -> str:
        """One completion, no tools. This payload stays as it is today."""

        return self._exchange(messages, timeout, tools=None)

    def request_tools(self, messages: list[dict], tools: list[dict], timeout: float = 60) -> dict:
        """One completion that may ask for tools. Used only when XAI_TOOLS is on.

        Returns {"content": str, "tool_calls": [{"id", "name", "arguments"}]}.
        arguments is the raw JSON string from the model.
        """

        return self._exchange(messages, timeout, tools=tools)

    def _exchange(self, messages, timeout, *, tools):
        """The one seam that sends a model request.

        Record writes the reply after this call. Replay returns before it
        and does not construct a network client. A new tool uses this same
        seam. Live, with tools omitted, sends model, messages, and
        temperature, and nothing else.
        """

        tool_choice = None if tools is None else "auto"
        if self.mode == "replay":
            return self._replay_answer(messages, tools, tool_choice, as_text=tools is None)

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=BASE_URL, timeout=timeout)
        payload = {"model": self.model, "messages": messages, "temperature": 0}
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = client.chat.completions.create(**payload)
        except Exception as exc:
            message = str(exc).replace(self.api_key, "[redacted]")
            classified = classify_exception(exc)
            raise LLMError(classified.error_type, classified.retryable, message) from None
        message = response.choices[0].message
        if self.mode == "record":
            self._record_message(
                messages,
                tools,
                tool_choice,
                message,
                getattr(response, "usage", None),
            )
        if tools is None:
            return _message_text(message.content)
        calls = [_read_tool_call(item) for item in (message.tool_calls or [])]
        return {"content": _message_text(message.content), "tool_calls": calls}

    def _replay_answer(self, messages, tools, tool_choice, as_text: bool):
        fingerprint = request_fingerprint(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=None,
            temperature=0,
        )
        answer = self._replay.take(fingerprint) if self._replay is not None else None
        if answer is None:
            short = fingerprint[:12]
            text = f"replay miss: no recording for fingerprint {short}"
            raise LLMError(text, False, text)
        if as_text:
            return _message_text(answer.get("content"))
        return {
            "content": _message_text(answer.get("content")),
            "tool_calls": list(answer.get("tool_calls") or []),
        }

    def _record_message(self, messages, tools, tool_choice, message, usage) -> None:
        if self._record is None:
            return
        fingerprint = request_fingerprint(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=None,
            temperature=0,
        )
        self._record.append(
            {
                "fingerprint": fingerprint,
                "model": self.model,
                "answer": _answer_from_message(message),
                "usage": _usage_dict(usage),
            }
        )


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
        if not tools_enabled():
            return self.llm.request(messages, timeout)
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

    Tools off: llm.request, the same payload as the tools-off path.
    Tools on: run tool calls until the model returns plain content, then
    return that text. The repair turn, if any, is repair() and sends no tools.
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
