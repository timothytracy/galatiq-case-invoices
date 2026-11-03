"""Pay a held invoice after a reviewer approves it.

The caller's dict only names the invoice. Payment uses the pipeline record
in `INVOICE_RELEASE_DIR` (default `logs/` in the data folder), keyed by the repo-relative path.
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

from db import data_dir, ensure_inventory_db, inventory_db_path, payments_db_path
from ledger.payments import revision_difference, stored_release_sha
from models import ExtractedInvoice, PipelineState
from parsers.pdf_invoice import pdf_text
from pipeline.audit import amount_in_text, issue_authorization
from pipeline.payment import payment
from pipeline.release_record import (
    ROOT,
    as_view,
    finding_list,
    invoice_of,
    load_record,
    record_path,
    repo_key,
    repo_path,
)
from pipeline.validation import validate
from decision.findings import name_in_source
from decision.invoice_fields import non_usd_message
from decision.outcome import blocks_release, finding_codes, review_release_block
from decision.policy import POLICY
from timestamps import utc_stamp

_FILLERS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u180e\u3164\uffa0\u115f\u1160\u00ad\u200e\u200f"))
_NOT_LATEST = "release record is not the latest"
_NEW_SPLIT = (
    "SPLIT_INVOICE: this vendor's paid invoices in the split window now cross the approval "
    "threshold, and the recorded hold did not show that. Rerun the invoice so the split is reviewed."
)
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
    reviewer and note need two letters. amount must equal the recorded total.
    demo=True keeps ledger_path and, when log_path is omitted, writes the
    decision log to a temporary file. demo=True without a ledger is refused.
    The caller's result is not modified.
    """

    result = as_view(result)
    temps = []
    if demo and log_path is None:
        log = _scratch("approve-", ".jsonl")
        temps.append(log)
    else:
        log = Path(log_path) if log_path is not None else data_dir() / "output" / "reviews" / "approve_pay.jsonl"
    ledger = None if demo and ledger_path is None else (
        Path(ledger_path) if ledger_path is not None else payments_db_path()
    )
    try:
        return _approve(result, reviewer, note, amount, ledger, log, now)
    finally:
        for path in temps:
            path.unlink(missing_ok=True)


def _approve(result, reviewer, note, amount, ledger, log, now) -> dict:
    name, text = _plain(reviewer), _plain(note)
    when = _timestamp(now)
    filename = repo_key(result) or "unknown"

    def finish(decision, code, message, saved=None, paid_amount=None):
        invoice = None if saved is None else saved.get("invoice")
        if paid_amount is not None:
            shown_amount = format(paid_amount, "f")
        elif invoice is None or invoice.total is None:
            shown_amount = None
        else:
            shown_amount = format(invoice.total, "f")
        record = {
            "time": when,
            "invoice": filename,
            "invoice_number": None if invoice is None else invoice.invoice_number,
            "vendor": None if invoice is None else invoice.vendor,
            "amount": shown_amount,
            "currency": None if invoice is None else invoice.currency,
            "pipeline_outcome": None if saved is None else saved.get("outcome"),
            "pipeline_codes": [] if saved is None else list(saved.get("codes") or []),
            "reviewer": name,
            "note": text,
            "critique_concerns": [] if saved is None else list(saved.get("critique_concerns") or []),
            "decision": decision,
            "reason_code": code,
            "reason": message,
        }
        if decision == "paid" and saved is not None and "SPLIT_INVOICE" in (saved.get("codes") or []):
            record["split_released"] = True
        _append(log, record)
        paid = decision == "paid"
        body = {
            "result": "paid" if paid else "held",
            "payment_status": "paid" if paid else "not_paid",
            "pipeline_findings": [] if saved is None else list(saved.get("findings") or []),
            "record": record,
        }
        body.update({
            key: record[key]
            for key in ("reason_code", "reason", "pipeline_outcome", "pipeline_codes", "critique_concerns", "invoice")
        })
        return body

    if sum(ch.isalpha() for ch in name) < 2:
        return finish("held", MISSING_REVIEWER, "A reviewer name is required.")
    if sum(ch.isalpha() for ch in text) < 2:
        return finish("held", MISSING_NOTE, "A note is required.")
    if ledger is None:
        return finish("held", NOT_PAYABLE, "A ledger is required.")
    if repo_key(result) is None:
        return finish(
            "held",
            NOT_PAYABLE,
            "The invoice file is outside the repository, so it has no release record.",
        )
    saved = load_record(filename)
    if saved is None:
        return finish("held", NOT_PAYABLE, "No pipeline record for this invoice.")
    invoice = invoice_of(saved.get("invoice"))
    saved["invoice"] = invoice
    early = review_release_block(
        saved.get("outcome"), list(saved.get("findings") or []), saved.get("reason_code"),
    )
    if early:
        return finish("held", NOT_PAYABLE, early, saved)
    on_disk = record_path(repo_path(filename))
    try:
        digest = hashlib.sha256(on_disk.read_bytes()).hexdigest()
    except OSError:
        digest = None
    if digest is None or digest != stored_release_sha(ledger, filename):
        return finish("held", "SYSTEM_ERROR", _NOT_LATEST, saved)
    mismatch = _differs(result, saved)
    if mismatch:
        return finish("held", NOT_PAYABLE, mismatch, saved)
    source = _source_problem(saved)
    if source:
        return finish("held", NOT_PAYABLE, source, saved)
    fresh = _recheck(saved, invoice, ledger)
    if isinstance(fresh, str):
        return finish("held", "SYSTEM_ERROR", fresh, saved)
    if "SPLIT_INVOICE" in finding_codes(fresh) and "SPLIT_INVOICE" not in (saved.get("codes") or []):
        return finish("held", NOT_PAYABLE, _NEW_SPLIT, saved)
    blocked = review_release_block(
        saved.get("outcome"),
        list(saved.get("findings") or []) + fresh,
        saved.get("reason_code"),
    )
    if blocked:
        # The saved record already passed. A DUPLICATE_INVOICE from the recheck
        # means this vendor and invoice number already have a paid row.
        if _fresh_block_code(fresh) == "DUPLICATE_INVOICE":
            number = "" if invoice is None or not invoice.invoice_number else invoice.invoice_number
            blocked = f"Invoice {number} from this vendor is already paid."
        return finish("held", NOT_PAYABLE, blocked, saved)
    if invoice is None or not _grounded(invoice, _file_text(ROOT / repo_path(saved.get("source")))):
        return finish("held", NOT_PAYABLE, "The vendor, invoice number, or total is not in the source file.", saved)
    if invoice.total is None or invoice.total <= 0:
        return finish("held", NOT_PAYABLE, "The total must be greater than zero.", saved)
    if invoice.currency not in POLICY.allowed_currencies:
        return finish("held", "NON_USD", non_usd_message(invoice.currency), saved)
    try:
        owed = revision_difference(ledger, invoice, saved.get("raw_text") or "")
    except sqlite3.Error as exc:
        return finish("held", "SYSTEM_ERROR", f"payments ledger unreadable: {exc}", saved)
    payable = invoice.total if owed is None else owed
    offered = _money(amount)
    if offered is None or offered != payable:
        shown = "missing" if offered is None else format(offered, "f")
        if owed is None:
            mismatch = (
                f"Amount {shown} does not equal the extracted total {format(invoice.total, 'f')}."
            )
        else:
            mismatch = (
                f"Amount {shown} does not equal the unpaid difference {format(payable, 'f')}."
            )
        return finish("held", AMOUNT_MISMATCH, mismatch, saved)
    finish("attempt", None, "Paying after the guards passed.", saved, payable)
    state = _pay_state(saved, invoice)
    auth = issue_authorization(state, payable)
    if auth is None:
        return finish("held", NOT_PAYABLE, "The invoice has no vendor or total to pay.", saved)
    paid = payment(state.model_copy(update={"authorization": auth}), ledger, release=True)
    if paid["payment"].status == "paid":
        return finish("paid", None, "Reviewer approved this hold for payment.", saved, payable)
    return finish(
        "held",
        paid.get("reason_code") or "PAYMENT_REFUSED",
        paid["payment"].detail,
        saved,
        payable,
    )


def _fresh_block_code(fresh) -> str | None:
    """The first recheck code that blocks a release. None when nothing new blocks."""


    for finding, code in zip(fresh, finding_codes(fresh)):
        if blocks_release(code, finding):
            return code
    return None


def _differs(result, saved) -> str | None:
    """Refuse when the caller sent facts that are not the pipeline record."""

    if "outcome" in result and result.get("outcome") != saved.get("outcome"):
        return "The request does not match the pipeline record."
    if "findings" in result and [item.code for item in finding_list(result.get("findings") or [])] != list(saved.get("codes") or []):
        return "The request does not match the pipeline record."
    if "invoice" not in result or result.get("invoice") is None:
        return None
    claimed = invoice_of(result.get("invoice"))
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
    rel = repo_path(saved.get("source"))
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
    """Sanity check that the vendor, number, total, and each line appear in the file.

    Text that is already in the file passes. This does not show the record is unchanged.
    """

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
        if not name_in_source(line.item, raw) or not re.search(rf"(?<!\d){re.escape(qty)}(?!\d)", raw):
            return False
    return True


def _recheck(saved, invoice, ledger):
    """validate() on the saved invoice, the live file, and the live inventory."""

    if invoice is None:
        return "The invoice result could not be read."
    rel = repo_path(saved.get("source"))
    if rel is None:
        return "The source file is outside the repository."
    try:
        live = _file_text(ROOT / rel)
    except (OSError, ValueError):
        return "The source file could not be read."
    state = _pay_state(saved, invoice).model_copy(update={"raw_text": live})
    database = inventory_db_path()
    try:
        ensure_inventory_db(database)
        with redirect_stdout(io.StringIO()):
            checked = validate(state, database, ledger_path=ledger)
    except (OSError, sqlite3.Error) as exc:
        return f"inventory database could not be opened: {exc}"
    fresh = list(checked.get("findings") or [])
    for finding in fresh:
        if finding.code == "SYSTEM_ERROR":
            return finding.message
    return fresh


def _file_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return pdf_text(path)
    return path.read_text(encoding="utf-8")


def _pay_state(saved, invoice: ExtractedInvoice) -> PipelineState:
    return PipelineState(
        invoice_path=saved.get("source") or invoice.source.path,
        filename=Path(saved["file"]).name,
        raw_text=saved.get("raw_text") or "",
        invoice=invoice,
        findings=finding_list(saved.get("findings") or []),
        outcome="hold",
        critique_concerns=list(saved.get("critique_concerns") or []),
    )


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
    return utc_stamp(now() if now is not None else None)


def _scratch(prefix, suffix) -> Path:
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(handle)
    return Path(name)


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
