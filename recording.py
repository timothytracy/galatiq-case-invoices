"""JSONL recordings of model requests. No network and no API key.

A file is one header line, then one line per model request. The header
stores the as_of date the prompts used. Replay reads that date back so a
later calendar day still hashes the same requests.
"""

import hashlib
import json
import re
import tempfile
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Published token rates for grok-4.20-0309-non-reasoning.
# https://docs.x.ai/developers/pricing lists $1.25 input and $2.50 output
# per 1M tokens below 200k prompt tokens, and $2.50 / $5.00 at or above
# that. prompt_tokens is not split into cached input, so the whole prompt
# is billed at the input rate.
INPUT_RATE = Decimal("1.25")
OUTPUT_RATE = Decimal("2.50")
LONG_CONTEXT_TOKENS = 200_000
LONG_INPUT_RATE = Decimal("2.50")
LONG_OUTPUT_RATE = Decimal("5.00")
_MILLION = Decimal("1000000")
_CENT = Decimal("0.000001")


def request_fingerprint(
    *,
    model: str,
    messages: list,
    tools: list | None,
    tool_choice: str | None,
    response_format: dict | None,
    temperature: float | int | None,
) -> str:
    """SHA-256 hex of one model request, as canonical JSON.

    The hashed object has exactly these keys, and no others:
    model, messages, tools, tool_choice, response_format, temperature.

    tools is null when the request offers no tools. tool_choice is null
    when it is not sent. response_format is the schema identity sent on
    the request. This client does not send one, so the value is null.
    temperature is the sampling value actually sent. No other sampling
    parameter is sent, so none is included.

    Canonical JSON sorts every object's keys, keeps list order (message
    order and tool order stay meaningful), uses separators (',', ':'),
    and is UTF-8. That removes whitespace and key-order differences.
    messages is the full conversation, including tool-result turns. A
    string is hashed as the characters the model sees, so a tool result
    that is already JSON text changes the hash when that text changes.
    A payments ledger file under the system temp directory
    (payments-*.db or payments-ordered-*.db) is replaced with the token
    payments-ledger.db before hashing. The ordered eval creates a new
    temp file on every run, and that name is not part of the request.
    The API key, timeouts, timestamps, and request ids are not keys of
    this object and are not hashed.
    """

    body = scrub_temp_paths(
        {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "response_format": response_format,
            "temperature": temperature,
        }
    )
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def scrub_temp_paths(value):
    """Replace a temp payments-ledger path with a fixed token.

    Record and replay each create their own file. The hash uses the token
    so the two runs still match. Any other text is left as it is.
    """

    root = Path(tempfile.gettempdir()).resolve().as_posix().rstrip("/")
    pattern = re.compile(re.escape(root) + r"/payments(?:-ordered)?-[A-Za-z0-9_]+\.db")
    return _scrub(value, pattern)


def _scrub(value, pattern: re.Pattern):
    if isinstance(value, str):
        return pattern.sub("payments-ledger.db", value)
    if isinstance(value, list):
        return [_scrub(item, pattern) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item, pattern) for key, item in value.items()}
    return value


def stable_invoice_path(path: Path, root: Path | None = None) -> Path:
    """Repo-relative posix path, or the original path when it is outside root.

    Record and replay put this string in the audit prompt. Live does not
    call this, so a live prompt still carries the path the caller passed.
    A file outside the repo cannot be made portable, so it is left as given.
    """

    base = (root or ROOT).resolve()
    try:
        relative = path.resolve().relative_to(base)
    except ValueError:
        return path
    return Path(relative.as_posix())


def estimate_cost(usages: list[dict]) -> Decimal:
    """USD estimate from prompt and completion token counts. Six decimal places."""

    total = Decimal("0")
    for usage in usages:
        prompt = int(usage["prompt_tokens"])
        completion = int(usage["completion_tokens"])
        if prompt >= LONG_CONTEXT_TOKENS:
            in_rate, out_rate = LONG_INPUT_RATE, LONG_OUTPUT_RATE
        else:
            in_rate, out_rate = INPUT_RATE, OUTPUT_RATE
        total += (Decimal(prompt) * in_rate + Decimal(completion) * out_rate) / _MILLION
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)


def format_usage_line(total_tokens: int, cost: Decimal) -> str:
    """One line under the score. cost is already quantized."""

    shown = cost.quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"tokens total={total_tokens} cost_usd={shown:.6f}"


def parse_as_of(text: str) -> date:
    """YYYY-MM-DD. A bad value raises ValueError naming --as-of."""

    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("--as-of must be YYYY-MM-DD") from exc


class RecordStore:
    """Create the file, write the header, append one request line at a time."""

    def __init__(self, path: Path, as_of: date, model: str) -> None:
        self.path = path
        self.as_of = as_of
        self.model = model
        self.usages: list[dict] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {"record": "header", "as_of": as_of.isoformat(), "model": model}
        path.write_text(_line(header), encoding="utf-8")

    def append(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_line(row))
        self.usages.append(row["usage"])


class ReplayStore:
    """Read-only. Each fingerprint is a queue so a repeated request stays in order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.as_of: date | None = None
        self.model: str | None = None
        self._queues: dict[str, list[dict]] = {}
        self.used: list[dict] = []
        self._load(path)

    def _load(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"replay recording not found: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row.get("record") == "header":
                if self.as_of is None:
                    self.as_of = date.fromisoformat(row["as_of"])
                    self.model = row.get("model") or None
                continue
            fingerprint = row["fingerprint"]
            self._queues.setdefault(fingerprint, []).append(row)
        if self.as_of is None:
            raise ValueError(f"recording header has no as_of date: {path}")

    def has(self, fingerprint: str) -> bool:
        """True when an answer is still queued. Does not consume it."""

        return bool(self._queues.get(fingerprint))

    def take(self, fingerprint: str) -> dict | None:
        """The next saved answer, or None when this fingerprint was not recorded."""

        queue = self._queues.get(fingerprint)
        if not queue:
            return None
        row = queue.pop(0)
        self.used.append(row["usage"])
        return row["answer"]


def _line(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
