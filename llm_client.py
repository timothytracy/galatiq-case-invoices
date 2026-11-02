"""Grok via the OpenAI SDK at https://api.x.ai/v1.

A missing XAI_API_KEY raises MissingAPIKey. Timeouts, 429, 5xx, network
errors, and JSON that is still invalid after the schema repair are retried.
Other 4xx errors are not.

request() never sends tools. tool_request() always does. Only audit calls
tool_request. The tool loop lives in tool_loop.py.

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
from llm_errors import LLMError
from tool_loop import (  # noqa: F401  re-exported for callers that import them from here
    TOOL_CALL_CAP,
    TOOL_ROUND_CAP,
    ToolStep,
    argument_text,
    parse_tool_arguments,
    tool_request,
)

DEFAULT_MODEL = "grok-4.20-0309-non-reasoning"
BASE_URL = "https://api.x.ai/v1"
DEFAULT_MAX_ATTEMPTS = POLICY.model_retry_attempts
SCHEMA_REPAIR_LIMIT = 2
BACKOFF_SECONDS = (1, 2, 4, 8, 16)

_MISSING_KEY = (
    "XAI_API_KEY is not set. Add it to the environment or to a .env file "
    "(see .env.example). The pipeline does not run without a key."
)


class MissingAPIKey(RuntimeError):
    """Raised at startup when no key is available."""


class ModeError(ValueError):
    """XAI_MODE, the recording path, or --as-of cannot be used as given."""


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
                "arguments": argument_text(call.get("arguments")),
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
        """One completion that may ask for tools. Audit calls this.

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
