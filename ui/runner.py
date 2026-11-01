"""Run one invoice with Grok or the fake LLM, and read a saved run back."""

import io
import json
import os
import time
from contextlib import redirect_stdout
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from db import create_database
from graph import run_invoice
from llm_client import GrokClient, load_dotenv
from tests.fake_llm_eval import AS_OF, handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
DEFAULT_DB = ROOT / "inventory.db"
DEFAULT_RUN = ROOT / "output" / "runs" / "review_run.json"
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
    db_path = path or DEFAULT_DB
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

def run_one(path: Path, db_path: Path | None = None, *, live: bool = False) -> dict:
    """One invoice. live=True uses GrokClient, the same client main.py constructs."""
    database = ensure_db(db_path)
    if live:
        llm, extra = GrokClient(), {}
    else:
        llm = FakeLLM(handler_for(path))
        extra = {"clock": FakeClock(), "rng": ZeroRandom(), "max_attempts": 5, "as_of": AS_OF}
    started = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        result = run_invoice(path, database, llm, **extra)
    return {"elapsed_seconds": time.perf_counter() - started, "state": to_jsonable(result), "llm": "grok" if live else "fake"}
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
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    if path.is_dir() and not files:
        raise ValueError(f"No JSON files in {path}")
    if not path.is_dir() and not path.is_file():
        raise ValueError(f"Run file not found: {path}")
    entries = []
    for item in files:
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{item.name} is not JSON") from exc
        entries.extend(parse_payload(payload, item.name))
    return entries

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
    modes = {entry.get("llm") for entry in entries if entry.get("llm") in {"fake", "grok"}}
    if modes == {"fake"}:
        return "Offline (fake LLM)"
    if modes == {"grok"}:
        return "Live (Grok)"
    return "Live (Grok) and Offline (fake LLM)" if modes else ""
