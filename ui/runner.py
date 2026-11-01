"""Run invoices with the repo's fake LLM, and read a saved run back.

stdout from the stage log is captured here so the review screen does not
repeat it. The records stay on the graph result, under logs.
"""

import io
import json
import time
from contextlib import redirect_stdout
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from db import create_database
from graph import run_invoice
from tests.fake_llm_eval import AS_OF, handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
DEFAULT_DB = ROOT / "inventory.db"
DEFAULT_RUN = ROOT / "output" / "runs" / "review_run.json"


def invoice_files(folder: Path | None = None) -> list[Path]:
    """Invoice files in the same set the eval script walks."""

    directory = folder or INVOICES
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUFFIXES
    )


def resolve_path(text: str) -> Path:
    """A relative path is from the repo root, not the shell's cwd."""

    path = Path(text.strip())
    if not path.is_absolute():
        path = ROOT / path
    return path


def ensure_db(path: Path | None = None) -> Path:
    """Create inventory.db only when it is missing, same as main.py."""

    db_path = path or DEFAULT_DB
    if not db_path.exists():
        create_database(db_path)
    return db_path


def to_jsonable(value):
    """Graph result to JSON. Models keep the field names model_dump already uses."""

    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def run_one(path: Path, db_path: Path | None = None) -> dict:
    """One invoice through run_invoice. The model is the test fake, not Grok."""

    database = ensure_db(db_path)
    started = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        result = run_invoice(
            path,
            database,
            FakeLLM(handler_for(path)),
            clock=FakeClock(),
            rng=ZeroRandom(),
            max_attempts=5,
            as_of=AS_OF,
        )
    return {"elapsed_seconds": time.perf_counter() - started, "state": to_jsonable(result)}


def pack(entries: list[dict]) -> dict:
    return {
        "record": "review_run",
        "llm": "fake",
        "as_of": AS_OF.isoformat(),
        "invoices": entries,
    }


def save_run(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack(entries), indent=2) + "\n", encoding="utf-8")
    return path


def load_run(path: Path) -> list[dict]:
    """A review_run file, a main.py log, or a directory of those JSON files."""

    if path.is_dir():
        files = sorted(item for item in path.iterdir() if item.suffix.lower() == ".json")
        if not files:
            raise ValueError(f"No JSON files in {path}")
        entries = []
        for item in files:
            entries.extend(_load_file(item))
        return entries
    if not path.is_file():
        raise ValueError(f"Run file not found: {path}")
    return _load_file(path)


def _load_file(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not JSON") from exc
    return parse_payload(payload, path.name)


def parse_payload(payload, name: str = "") -> list[dict]:
    if isinstance(payload, dict) and payload.get("record") == "review_run":
        entries = []
        for row in payload.get("invoices") or []:
            if isinstance(row, dict) and "state" in row:
                entries.append(
                    {
                        "elapsed_seconds": row.get("elapsed_seconds"),
                        "state": row["state"],
                        "result_line": row.get("result_line"),
                    }
                )
            elif isinstance(row, dict):
                entries.append({"elapsed_seconds": None, "state": row})
        return entries
    if isinstance(payload, list) and _is_pipeline_log(payload):
        return [
            {
                "elapsed_seconds": None,
                "state": state_from_log(payload),
                "result_line": _result_record(payload),
            }
        ]
    label = name or "file"
    raise ValueError(f"{label} is not a review run or a pipeline log")


def _is_pipeline_log(payload: list) -> bool:
    first = payload[0] if payload else None
    return isinstance(first, dict) and first.get("record") in {"stage", "result"}


def _result_record(records: list) -> dict:
    for row in records:
        if isinstance(row, dict) and row.get("record") == "result":
            return row
    return {}


def state_from_log(records: list) -> dict:
    """Rebuild the fields the screen reads from a main.py log (stage records + result line)."""

    stages = [row for row in records if isinstance(row, dict) and row.get("record") == "stage"]
    result = _result_record(records)
    filename = result.get("file") or (stages[0].get("invoice_file") if stages else "")
    state: dict = {"filename": filename, "logs": stages}
    if "outcome" in result:
        state["outcome"] = result.get("outcome")
    if "reason_code" in result:
        state["reason_code"] = result.get("reason_code")
    if "action" in result:
        state["action"] = result.get("action")
    for stage in stages:
        body = stage.get("result") or {}
        name = stage.get("stage")
        if name == "ingestion" and isinstance(body.get("invoice"), dict):
            state["invoice"] = body["invoice"]
        if isinstance(body.get("findings"), list):
            state["findings"] = body["findings"]
        if isinstance(body.get("decision"), dict):
            state["decision"] = body["decision"]
        if isinstance(body.get("payment"), dict):
            state["payment"] = body["payment"]
        if name == "audit" and body.get("verdict"):
            state["audit_verdict"] = body["verdict"]
    if "outcome" not in state:
        for stage in reversed(stages):
            body = stage.get("result") or {}
            if stage.get("stage") in {"reject_log", "hold", "payment", "audit"} and body.get("outcome"):
                state["outcome"] = body["outcome"]
                state.setdefault("reason_code", body.get("reason_code"))
                state.setdefault("action", body.get("action"))
                break
    return state
