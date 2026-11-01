"""Grok via the OpenAI SDK at https://api.x.ai/v1.

A missing XAI_API_KEY raises MissingAPIKey. Timeouts, 429, 5xx, network
errors, and JSON that is still invalid after the schema repair are retried.
Other 4xx errors are not.
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
BACKOFF_SECONDS = (1, 2, 4, 8, 16)

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
    """One repair turn if the JSON does not validate. check may raise LLMError."""

    conversation = list(messages)
    error_text = "empty response"
    for _ in range(SCHEMA_REPAIR_LIMIT):
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
        content = response.choices[0].message.content
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        return str(content)


def _decimal(value: str):
    from decimal import Decimal

    return Decimal(value)
