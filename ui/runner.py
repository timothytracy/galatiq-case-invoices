"""Run one invoice with Grok or the fake LLM, and read a saved run back."""

import glob
import io
import json
import os
import tempfile
import time
from contextlib import redirect_stdout
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from db import create_database, data_dir, inventory_db_path, payments_db_path
from graph import run_invoice
from llm_client import GrokClient, load_dotenv
from tests.fake_llm_eval import AS_OF, handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
DEFAULT_DB = data_dir() / "inventory.db"
DEFAULT_RUN = ROOT / "output" / "runs" / "review_run.json"
PAYMENTS_DB = payments_db_path()
def grok_key_set() -> bool:
    """True when the CLI's Grok client would start. It reads XAI_API_KEY."""
    load_dotenv()
    return bool(os.environ.get("XAI_API_KEY", "").strip())
def invoice_files(folder: Path | None = None) -> list[Path]:
    directory = folder or INVOICES
    return sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES)
def resolve_path(text: str) -> Path:
    path = Path(text.strip())
    return path if path.is_absolute() else ROOT / path
def ensure_db(path: Path | None = None) -> Path:
    db_path = path or inventory_db_path(DEFAULT_DB)
    if not db_path.exists():
        create_database(db_path)
    return db_path

def to_jsonable(value):
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value

def is_real_payments_db(path: Path | str) -> bool:
    """True for the repo or working-directory payments.db, not a temp ledger."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    return resolved in {PAYMENTS_DB.resolve(), (Path.cwd() / "payments.db").resolve()}
def temporary_ledger() -> Path:
    """An empty ledger file that is not payments.db."""
    handle, name = tempfile.mkstemp(prefix="review-payments-", suffix=".db")
    os.close(handle)
    path = Path(name)
    if is_real_payments_db(path):
        path.unlink(missing_ok=True)
        raise RuntimeError("temporary ledger must not be payments.db")
    return path
def recording_files(folder: Path | None = None) -> list[Path]:
    """Committed replay recordings, sorted by file name."""
    directory = folder or (ROOT / "recordings")
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.jsonl") if path.is_file())


def run_one(
    path: Path,
    db_path: Path | None = None,
    *,
    live: bool = False,
    ledger_path: Path | None = None,
    replay: Path | None = None,
) -> dict:
    """One invoice. live=True uses GrokClient, the same client main.py constructs.

    The ledger is kept and returned on the entry. Live uses payments.db.
    Offline and replay use the given file, or a new temporary ledger that is
    not payments.db. The release sha is written into that same file, so
    approval must use it too. Replay sets XAI_MODE and XAI_RECORDING for this
    call only, then restores them.
    """
    database = ensure_db(db_path)
    env = None
    if replay is not None:
        recording = Path(replay)
        env = {"XAI_MODE": "replay", "XAI_RECORDING": str(recording)}
        ledger = _replay_ledger(ledger_path)
        kind = "replay"
    elif live:
        ledger = PAYMENTS_DB if ledger_path is None else Path(ledger_path)
        kind = "grok"
    else:
        ledger = temporary_ledger() if ledger_path is None else Path(ledger_path)
        kind = "fake"
    previous = {key: os.environ.get(key) for key in env} if env else None
    if env:
        os.environ.update(env)
    try:
        if kind == "fake":
            llm = FakeLLM(handler_for(path))
            extra = {"clock": FakeClock(), "rng": ZeroRandom(), "max_attempts": 5, "as_of": AS_OF}
        else:
            llm, extra = GrokClient(), {}
        extra["ledger_path"] = ledger
        started = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            result = run_invoice(path, database, llm, **extra)
        elapsed = time.perf_counter() - started
    finally:
        if previous is not None:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return {
        "elapsed_seconds": elapsed,
        "state": to_jsonable(result),
        "llm": kind,
        "ledger_path": str(ledger),
    }


def _replay_ledger(ledger_path: Path | None) -> Path:
    """A temporary ledger. Replay never opens payments.db."""
    if ledger_path is None or is_real_payments_db(ledger_path):
        return temporary_ledger()
    return Path(ledger_path)


def pack(entries: list[dict]) -> dict:
    modes = {entry.get("llm") for entry in entries if entry.get("llm")}
    llm = modes.pop() if len(modes) == 1 else "mixed"
    payload = {"record": "review_run", "llm": llm, "invoices": entries}
    if llm == "fake":
        payload["as_of"] = AS_OF.isoformat()
    return payload
def save_run(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack(entries), indent=2) + "\n", encoding="utf-8")
    return path

def load_run(path: Path) -> list[dict]:
    files = _run_files(path)
    if not files:
        if path.is_dir() or _is_glob(path):
            raise ValueError(f"No JSON files in {path}")
        raise ValueError(f"Run file not found: {path}")
    entries = []
    for item in files:
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{item.name} is not JSON") from exc
        entries.extend(parse_payload(payload, item.name))
    return entries

def _is_glob(path: Path) -> bool:
    return any(mark in str(path) for mark in "*?[")


def _is_release_record(path: Path) -> bool:
    """Payment release files sit next to run logs and are not runs."""
    return path.name.endswith(".release.json")


def _run_files(path: Path) -> list[Path]:
    """JSON run logs. A directory or glob skips *.release.json payment records."""
    if _is_glob(path):
        matches = [Path(item) for item in sorted(glob.glob(str(path)))]
        return [item for item in matches if item.is_file() and not _is_release_record(item)]
    if path.is_dir():
        return [
            item for item in sorted(path.glob("*.json"))
            if item.is_file() and not _is_release_record(item)
        ]
    if path.is_file():
        return [path]
    return []


def parse_payload(payload, name: str = "") -> list[dict]:
    if isinstance(payload, dict) and payload.get("record") == "review_run":
        mode = payload.get("llm") if payload.get("llm") in {"fake", "grok"} else None
        entries = []
        for row in payload.get("invoices") or []:
            if isinstance(row, dict):
                entries.append({
                    "elapsed_seconds": row.get("elapsed_seconds"),
                    "state": row["state"] if "state" in row else row,
                    "result_line": row.get("result_line"),
                    "llm": row.get("llm") or mode,
                })
        return entries
    # main.py only runs with GrokClient, so a pipeline log is a live run.
    if isinstance(payload, list) and _is_pipeline_log(payload):
        return [{"elapsed_seconds": None, "state": state_from_log(payload), "result_line": _result_record(payload), "llm": "grok"}]
    raise ValueError(f"{name or 'file'} is not a review run or a pipeline log")
def _is_pipeline_log(payload: list) -> bool:
    first = payload[0] if payload else None
    return isinstance(first, dict) and first.get("record") in {"stage", "result"}
def _result_record(records: list) -> dict:
    return next((row for row in records if isinstance(row, dict) and row.get("record") == "result"), {})

def state_from_log(records: list) -> dict:
    stages = [row for row in records if isinstance(row, dict) and row.get("record") == "stage"]
    result = _result_record(records)
    state = {"filename": result.get("file") or (stages[0].get("invoice_file") if stages else ""), "logs": stages}
    state.update({key: result.get(key) for key in ("outcome", "reason_code", "action") if key in result})
    for stage in stages:
        body, name = stage.get("result") or {}, stage.get("stage")
        if name == "ingestion" and isinstance(body.get("invoice"), dict):
            state["invoice"] = body["invoice"]
        for key in ("findings", "decision", "payment"):
            if isinstance(body.get(key), list if key == "findings" else dict):
                state[key] = body[key]
        if name == "audit" and body.get("verdict"):
            state["audit_verdict"] = body["verdict"]
    if "outcome" not in state:
        terminal = next((
            stage.get("result") or {} for stage in reversed(stages)
            if stage.get("stage") in {"reject_log", "hold", "payment", "audit"} and (stage.get("result") or {}).get("outcome")
        ), None)
        if terminal:
            state["outcome"] = terminal["outcome"]
            state.setdefault("reason_code", terminal.get("reason_code"))
            state.setdefault("action", terminal.get("action"))
    return state

def mode_label(entries: list[dict]) -> str:
    """How the shown run was produced. Empty when nothing has been loaded."""
    modes = {entry.get("llm") for entry in entries if entry.get("llm") in {"fake", "grok", "replay"}}
    if modes == {"fake"}:
        return "Offline (fake LLM)"
    if modes == {"grok"}:
        return "Live (Grok)"
    if modes == {"replay"}:
        return "Replay (recorded Grok)"
    if modes == {"fake", "grok"}:
        return "Live (Grok) and Offline (fake LLM)"
    if not modes:
        return ""
    names = {"grok": "Live (Grok)", "fake": "Offline (fake LLM)", "replay": "Replay (recorded Grok)"}
    return " and ".join(names[mode] for mode in ("grok", "fake", "replay") if mode in modes)
