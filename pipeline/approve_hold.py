"""Pay a held invoice after a reviewer approves it.

The caller's dict only names the invoice. Payment uses the pipeline record
in `INVOICE_RELEASE_DIR` (default `logs/`), keyed by the repo-relative path.
The review screen does not call this yet.

    python3 -m pipeline.approve_hold --demo
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from models import ExtractedInvoice, Finding, PipelineState
from pipeline.audit import amount_in_text, issue_authorization
from pipeline.payment import payment
from decision.findings import _name_in_source
from decision.invoice_fields import non_usd_message
from decision.outcome import review_release_block
from decision.policy import POLICY

ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()
_FILLERS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u180e\u3164\uffa0\u115f\u1160\u00ad\u200e\u200f"))
MISSING_REVIEWER = "MISSING_REVIEWER"
MISSING_NOTE = "MISSING_NOTE"
NOT_PAYABLE = "NOT_PAYABLE"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"


def approve_and_pay(
    result: dict | PipelineState,
    *,
    reviewer: str,
    note: str,
    amount,
    ledger_path: str | Path | None = None,
    log_path: str | Path | None = None,
    demo: bool = False,
    now=None,
) -> dict:
    """Release one held invoice. `result` selects the pipeline record.

    reviewer and note need two Unicode letters after fillers are removed.
    amount must equal the recorded total with no rounding. demo=True uses a
    temporary ledger and log. The caller's result is not modified.
    """

    temps = []
    if demo:
        ledger = _scratch("payments-", ".db")
        temps.append(ledger)
    else:
        ledger = Path(ledger_path) if ledger_path is not None else Path("payments.db")
    if demo and log_path is None:
        log = _scratch("approve-", ".jsonl")
        temps.append(log)
    else:
        log = Path(log_path) if log_path is not None else Path("output") / "reviews" / "approve_pay.jsonl"
    try:
        return _approve(result, reviewer, note, amount, ledger, log, now)
    finally:
        for path in temps:
            path.unlink(missing_ok=True)


def remember(result) -> bool:
    """Write the release record. False on a disk error. Never raises.

    A source outside the repository is not a release record. Payment of that
    file still proceeds. A failed write of an in-repo record pays nothing.
    """

    try:
        rel = _repo_path(_source_of(result))
        if rel is None:
            return True
        invoice = _invoice(_pick(result, "invoice"))
        if invoice is None:
            return False
        raw = _pick(result, "raw_text") or invoice.source.raw_text or ""
        findings = _finding_list(_pick(result, "findings") or [])
        record = {
            "record": "release",
            "file": rel.as_posix(),
            "outcome": _pick(result, "outcome"),
            "codes": [item.code for item in findings],
            "reason_code": _pick(result, "reason_code"),
            "critique_concerns": list(_pick(result, "critique_concerns") or []),
            "raw_text": raw,
            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "source": rel.as_posix(),
            "invoice": invoice.model_dump(mode="json"),
            "findings": [_finding_row(item) for item in findings],
        }
        path = _record_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _approve(result, reviewer, note, amount, ledger, log, now) -> dict:
    name, text = _plain(reviewer), _plain(note)
    when = _timestamp(now)
    filename = _repo_key(result) or "unknown"

    def finish(decision, code, message, saved=None):
        invoice = None if saved is None else saved.get("invoice")
        record = {
            "time": when,
            "invoice": filename,
            "invoice_number": None if not invoice else invoice.invoice_number,
            "vendor": None if not invoice else invoice.vendor,
            "amount": None if not invoice or invoice.total is None else format(invoice.total, "f"),
            "currency": None if not invoice else invoice.currency,
            "pipeline_outcome": None if saved is None else saved.get("outcome"),
            "pipeline_codes": [] if saved is None else list(saved.get("codes") or []),
            "reviewer": name,
            "note": text,
            "critique_concerns": [] if saved is None else list(saved.get("critique_concerns") or []),
            "decision": decision,
            "reason_code": code,
            "reason": message,
        }
        _append(log, record)
        paid = decision == "paid"
        return {
            "result": "paid" if paid else "held",
            "reason_code": code,
            "reason": message,
            "pipeline_outcome": record["pipeline_outcome"],
            "pipeline_codes": record["pipeline_codes"],
            "pipeline_findings": [] if saved is None else list(saved.get("findings") or []),
            "critique_concerns": record["critique_concerns"],
            "invoice": filename,
            "payment_status": "paid" if paid else "not_paid",
            "record": record,
        }

    if sum(ch.isalpha() for ch in name) < 2:
        return finish("held", MISSING_REVIEWER, "A reviewer name is required.")
    if sum(ch.isalpha() for ch in text) < 2:
        return finish("held", MISSING_NOTE, "A note is required.")
    saved = _load(filename)
    if saved is None:
        return finish("held", NOT_PAYABLE, "No pipeline record for this invoice.")
    invoice = _invoice(saved.get("invoice"))
    saved["invoice"] = invoice
    mismatch = _differs(result, saved)
    if mismatch:
        return finish("held", NOT_PAYABLE, mismatch, saved)
    source = _source_problem(saved)
    if source:
        return finish("held", NOT_PAYABLE, source, saved)
    fresh = _recheck(saved, invoice, ledger)
    if isinstance(fresh, str):
        return finish("held", "SYSTEM_ERROR", fresh, saved)
    blocked = review_release_block(
        saved.get("outcome"),
        list(saved.get("findings") or []) + fresh,
        saved.get("reason_code"),
    )
    if blocked:
        return finish("held", NOT_PAYABLE, blocked, saved)
    if invoice is None or not _grounded(invoice, _file_text(ROOT / _repo_path(saved.get("source")))):
        return finish("held", NOT_PAYABLE, "The vendor, invoice number, or total is not in the source file.", saved)
    if invoice.total is None or invoice.total <= 0:
        return finish("held", NOT_PAYABLE, "The total must be greater than zero.", saved)
    if invoice.currency not in POLICY.allowed_currencies:
        return finish("held", "NON_USD", non_usd_message(invoice.currency), saved)
    offered = _money(amount)
    if offered is None or offered != invoice.total:
        shown = "missing" if offered is None else format(offered, "f")
        return finish("held", AMOUNT_MISMATCH, f"Amount {shown} does not equal the extracted total {format(invoice.total, 'f')}.", saved)
    finish("attempt", None, "Paying after the guards passed.", saved)
    state = _pay_state(saved, invoice)
    auth = issue_authorization(state)
    if auth is None:
        return finish("held", NOT_PAYABLE, "The invoice has no vendor or total to pay.", saved)
    paid = payment(state.model_copy(update={"authorization": auth}), ledger, release=True)
    if paid["payment"].status == "paid":
        return finish("paid", None, "Reviewer approved this hold for payment.", saved)
    return finish("held", paid.get("reason_code") or "PAYMENT_REFUSED", paid["payment"].detail, saved)


def _differs(result, saved) -> str | None:
    """Refuse when the caller sent facts that are not the pipeline record."""

    outcome = _pick(result, "outcome", missing=True)
    if outcome is not _MISSING and outcome != saved.get("outcome"):
        return "The request does not match the pipeline record."
    sent = _pick(result, "findings", missing=True)
    if sent is not _MISSING and [item.code for item in _finding_list(sent or [])] != list(saved.get("codes") or []):
        return "The request does not match the pipeline record."
    claimed = _invoice(_pick(result, "invoice", missing=True))
    recorded = saved.get("invoice")
    if claimed is None or recorded is None:
        return None if claimed is None else "The request does not match the pipeline record."
    pairs = (
        claimed.vendor != recorded.vendor,
        claimed.invoice_number != recorded.invoice_number,
        claimed.total != recorded.total,
        claimed.currency != recorded.currency,
        _lines(claimed) != _lines(recorded),
    )
    if any(pairs):
        return "The request does not match the pipeline record."
    return None


def _source_problem(saved) -> str | None:
    rel = _repo_path(saved.get("source"))
    if rel is None:
        return "The source file is outside the repository."
    path = ROOT / rel
    if not path.is_file():
        return "The source file is missing."
    try:
        on_disk = _file_text(path)
    except (OSError, ValueError):
        return "The source file could not be read."
    recorded = saved.get("raw_text") or ""
    if on_disk != recorded or hashlib.sha256(on_disk.encode()).hexdigest() != saved.get("raw_sha256"):
        return "The source file does not match the pipeline record."
    return None


def _grounded(invoice: ExtractedInvoice, raw: str) -> bool:
    """Vendor, number, total, and each line name and quantity, as the file prints them."""

    body = "\n".join(
        line for line in raw.splitlines()
        if not re.match(r"\s*(to|bill to|attn)\b", line, re.I)
    )
    vendor = re.sub(r"\s+", " ", (invoice.vendor or "").casefold()).strip()
    if not vendor or vendor not in re.sub(r"\s+", " ", body.casefold()):
        return False
    parts = re.findall(r"[a-z]+|\d+", (invoice.invoice_number or "").casefold())
    token = r"(?<![a-z0-9-])" + r"[\s-]*".join(map(re.escape, parts)) + r"(?![ \t-]*[a-z0-9])"
    if not parts or not re.search(token, raw.casefold()):
        return False
    if invoice.total is None or not amount_in_text(invoice.total, raw):
        return False
    for line in invoice.line_items:
        shown = format(line.quantity, "f")
        qty = shown.rstrip("0").rstrip(".") if "." in shown else shown
        if not _name_in_source(line.item, raw) or not re.search(rf"(?<!\d){re.escape(qty)}(?!\d)", raw):
            return False
    return True


def _recheck(saved, invoice, ledger):
    """validate() on the saved invoice, the live file, and the live inventory."""

    if invoice is None:
        return "The invoice result could not be read."
    rel = _repo_path(saved.get("source"))
    if rel is None:
        return "The source file is outside the repository."
    try:
        live = _file_text(ROOT / rel)
    except (OSError, ValueError):
        return "The source file could not be read."
    state = _pay_state(saved, invoice).model_copy(update={"raw_text": live})
    from pipeline.validation import validate
    with redirect_stdout(io.StringIO()):
        checked = validate(state, ROOT / "inventory.db", ledger_path=ledger)
    fresh = list(checked.get("findings") or [])
    for finding in fresh:
        if finding.code == "SYSTEM_ERROR":
            return finding.message
    return fresh


def _file_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from parsers.pdf_invoice import pdf_text
        return pdf_text(path)
    return path.read_text(encoding="utf-8")


def _pay_state(saved, invoice: ExtractedInvoice) -> PipelineState:
    return PipelineState(
        invoice_path=saved.get("source") or invoice.source.path,
        filename=Path(saved["file"]).name,
        raw_text=saved.get("raw_text") or "",
        invoice=invoice,
        findings=_finding_list(saved.get("findings") or []),
        outcome="hold",
        critique_concerns=list(saved.get("critique_concerns") or []),
    )


def _record_dir() -> Path:
    raw = os.environ.get("INVOICE_RELEASE_DIR", "").strip()
    return Path(raw) if raw else ROOT / "logs"


def _record_path(rel: Path) -> Path:
    return _record_dir() / (rel.as_posix().replace("/", "__") + ".release.json")


def _repo_path(path) -> Path | None:
    """Repo-relative path, or None when it is outside the repository."""

    if not path or path is _MISSING:
        return None
    candidate = Path(str(path))
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        return candidate.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None


def _repo_key(result) -> str | None:
    rel = _repo_path(_source_of(result))
    return None if rel is None else rel.as_posix()


def _source_of(result):
    invoice = _pick(result, "invoice")
    if isinstance(invoice, ExtractedInvoice):
        return invoice.source.path
    if isinstance(invoice, dict):
        return (invoice.get("source") or {}).get("path")
    return _pick(result, "invoice_path")


def _load(filename: str):
    rel = _repo_path(filename)
    path = _record_path(rel) if rel is not None else None
    if path is None or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) and record.get("record") == "release" else None


def _pick(result, key, missing=False):
    if isinstance(result, PipelineState):
        return getattr(result, key)
    if isinstance(result, dict) and key in result:
        return result[key]
    return _MISSING if missing else None


def _invoice(value):
    if value is None or value is _MISSING:
        return None
    if isinstance(value, ExtractedInvoice):
        return value
    if isinstance(value, dict):
        return ExtractedInvoice.model_validate(value)
    return None


def _finding_list(value) -> list[Finding]:
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


def _lines(invoice: ExtractedInvoice):
    return [(line.item, line.quantity, line.unit_price, line.amount) for line in invoice.line_items]


def _plain(value) -> str:
    return (value or "").translate(_FILLERS).strip()


def _money(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except Exception:
        return None
    return amount if amount.is_finite() else None


def _timestamp(now) -> str:
    moment = now() if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scratch(prefix, suffix) -> Path:
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(handle)
    return Path(name)


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()


def _offline(path: Path, records: Path):
    """Replay one saved invoice. No test double and no API key."""

    from db import create_database
    from graph import run_invoice
    from llm_client import begin_client

    os.environ["INVOICE_RELEASE_DIR"] = str(records)
    database = ROOT / "inventory.db"
    if not database.is_file():
        create_database(database)
    client, as_of = begin_client("replay", str(ROOT / "recordings" / "samples_tools.jsonl"), None)
    with redirect_stdout(io.StringIO()):
        return run_invoice(path, database, client, as_of=as_of)


def _run_demo() -> int:
    records = Path(tempfile.mkdtemp(prefix="releases-"))
    previous = os.environ.get("INVOICE_RELEASE_DIR")
    os.environ["INVOICE_RELEASE_DIR"] = str(records)
    try:
        hold = _offline(ROOT / "data" / "invoices" / "invoice_1012.txt", records)
        rejected = _offline(ROOT / "data" / "invoices" / "invoice_1002.txt", records)
        first = approve_and_pay(
            hold, reviewer="Avery Chen", note="Confirmed this is the same company.",
            amount=hold["invoice"].total, demo=True,
        )
        second = approve_and_pay(
            rejected, reviewer="Avery Chen", note="Stock is short, so this cannot be paid.",
            amount=rejected["invoice"].total, demo=True,
        )
    finally:
        if previous is None:
            os.environ.pop("INVOICE_RELEASE_DIR", None)
        else:
            os.environ["INVOICE_RELEASE_DIR"] = previous
    print(json.dumps(first["record"]), flush=True)
    print(json.dumps(second["record"]), flush=True)
    return 0 if first["result"] == "paid" and second["result"] == "held" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve a held invoice for payment.")
    parser.add_argument("--demo", action="store_true", help="Run two offline invoices, then release them.")
    args = parser.parse_args(argv)
    if not args.demo:
        print("Pass --demo.", file=sys.stderr)
        return 2
    return _run_demo()


if __name__ == "__main__":
    raise SystemExit(main())
