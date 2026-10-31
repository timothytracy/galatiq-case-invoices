"""Grok client for later phases.

Uses the OpenAI SDK against https://api.x.ai/v1. The README's `from xai import Grok`
snippet is not a real client and is not used here.

Offline when XAI_API_KEY is missing, when INVOICE_OFFLINE is 1/true/yes/on,
or when offline=True. Offline calls do not import the SDK and do not use the network.
"""

import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

DEFAULT_MODEL = "grok-4.7"
BASE_URL = "https://api.x.ai/v1"
_OFFLINE_VALUES = {"1", "true", "yes", "on"}


class LLMResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    model: str
    offline: bool
    latency_ms: float


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines into the environment. Existing variables win. Never prints values."""

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


def _env_forces_offline() -> bool:
    return os.environ.get("INVOICE_OFFLINE", "").strip().lower() in _OFFLINE_VALUES


class GrokClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        offline: bool | None = None,
    ) -> None:
        load_dotenv()
        if api_key is None:
            self.api_key = os.environ.get("XAI_API_KEY", "").strip()
        else:
            self.api_key = api_key.strip()
        self.model = (model or os.environ.get("XAI_MODEL", "")).strip() or DEFAULT_MODEL
        # offline=False means "do not force", not "call the network without a key".
        self.offline = bool(offline) or _env_forces_offline() or not self.api_key

    def __repr__(self) -> str:
        return f"GrokClient(model={self.model!r}, offline={self.offline}, has_key={bool(self.api_key)})"

    def complete(self, messages: list[dict], timeout: float = 60) -> LLMResult:
        if self.offline:
            return LLMResult(text="", model=self.model, offline=True, latency_ms=0)

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=BASE_URL, timeout=timeout)
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
            )
        except Exception as exc:
            message = str(exc)
            if self.api_key:
                message = message.replace(self.api_key, "[redacted]")
            raise RuntimeError(f"Grok request failed: {message}") from None
        elapsed_ms = (time.perf_counter() - started) * 1000
        content = response.choices[0].message.content
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)
        return LLMResult(text=text, model=self.model, offline=False, latency_ms=elapsed_ms)
