"""Release records. What the pipeline saw, kept for a reviewer to release later.

A record is written before the payment ledger row. Its sha is stored in the
ledger when the outcome could be released.
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from db import log_dir
from models import ExtractedInvoice, Finding
from decision.outcome import review_release_block
from ledger.payments import save_release_sha

ROOT = Path(__file__).resolve().parents[1]


def remember(result, ledger_path=None) -> bool | str:
    """Write the release record after its sha row. False on a disk or database error."""

    data = as_view(result)
    rel = repo_path(_source_of(data))
    if rel is None:
        return True
    try:
        invoice = invoice_of(data.get("invoice"))
        if invoice is None:
            return False
        raw = data.get("raw_text") or invoice.source.raw_text or ""
        findings = finding_list(data.get("findings") or [])
        record = {
            "record": "release",
            "file": rel.as_posix(),
            "outcome": data.get("outcome"),
            "codes": [item.code for item in findings],
            "reason_code": data.get("reason_code"),
            "critique_concerns": list(data.get("critique_concerns") or []),
            "raw_text": raw,
            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "source": rel.as_posix(),
            "invoice": invoice.model_dump(mode="json"),
            "findings": [_finding_row(item) for item in findings],
        }
        text = json.dumps(record) + "\n"
        payable = review_release_block(record["outcome"], findings, record["reason_code"]) is None
        digest = hashlib.sha256(text.encode()).hexdigest() if payable else None
        if ledger_path is not None:
            save_release_sha(ledger_path, rel.as_posix(), digest)
        path = record_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except sqlite3.Error as exc:
        return f"payments ledger unreadable: {exc}"
    except OSError:
        return False
    return True


def _record_dir() -> Path:
    raw = os.environ.get("INVOICE_RELEASE_DIR", "").strip()
    return Path(raw) if raw else log_dir()


def record_path(rel: Path) -> Path:
    return _record_dir() / (quote(rel.as_posix(), safe="") + ".release.json")


def repo_path(path) -> Path | None:
    """Repo-relative path, or None when it is outside the repository."""

    if not path:
        return None
    candidate = Path(str(path))
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        return candidate.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None


def repo_key(result) -> str | None:
    rel = repo_path(_source_of(result))
    return None if rel is None else rel.as_posix()


def _source_of(result):
    invoice = result.get("invoice")
    if isinstance(invoice, ExtractedInvoice):
        return invoice.source.path
    if isinstance(invoice, dict):
        return (invoice.get("source") or {}).get("path")
    return result.get("invoice_path")


def load_record(filename: str):
    rel = repo_path(filename)
    if rel is None:
        return None
    path = record_path(rel)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) and record.get("record") == "release" else None


def as_view(result) -> dict:
    if isinstance(result, dict):
        return result
    return {
        "invoice": result.invoice,
        "invoice_path": result.invoice_path,
        "raw_text": result.raw_text,
        "findings": list(result.findings or []),
        "outcome": result.outcome,
        "reason_code": result.reason_code,
        "critique_concerns": list(result.critique_concerns or []),
    }


def invoice_of(value):
    if value is None:
        return None
    if isinstance(value, ExtractedInvoice):
        return value
    if isinstance(value, dict):
        return ExtractedInvoice.model_validate(value)
    return None


def finding_list(value) -> list[Finding]:
    found = []
    for item in value or []:
        if isinstance(item, Finding):
            found.append(item)
        elif isinstance(item, dict):
            found.append(Finding.model_validate(item))
    return found


def _finding_row(item: Finding) -> dict:
    row = {"code": item.code, "message": item.message, "severity": item.severity}
    if item.item:
        row["item"] = item.item
    if item.send_back_cap:
        row["send_back_cap"] = True
    return row
