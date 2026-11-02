"""Pay a held invoice after a reviewer approves it.

The caller's dict only names the invoice. Payment uses the pipeline record
in `INVOICE_RELEASE_DIR` (default `logs/`), keyed by the repo-relative path.
The review screen does not call this yet.
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from models import ExtractedInvoice, Finding, PipelineState
from pipeline.audit import amount_in_text, issue_authorization
from pipeline.payment import payment
from decision.findings import _name_in_source
from decision.invoice_fields import non_usd_message
from decision.outcome import review_release_block
from decision.policy import POLICY

ROOT = Path(__file__).resolve().parents[1]
_FAILED: set[str] = set()
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

    result = _view(result)
    temps = []
    if demo:
        ledger = _scratch("payments-", ".db")
        temps.append(ledger)
    else:
        ledger = Path(ledger_path) if ledger_path is not None else ROOT / "payments.db"
    if demo and log_path is None:
        log = _scratch("approve-", ".jsonl")
        temps.append(log)
    else:
        log = Path(log_path) if log_path is not None else ROOT / "output" / "reviews" / "approve_pay.jsonl"
    try:
        return _approve(result, reviewer, note, amount, ledger, log, now)
    finally:
        for path in temps:
            path.unlink(missing_ok=True)


def remember(result) -> bool:
    """Write the release record. False on a disk error. Never raises.

    A source outside the repository is not a release record. Payment of that
    file still proceeds. A failed write removes the previous record. If the
    old file cannot be removed, the path is blocked until a later write works.
    """

    data = _view(result)
    rel = _repo_path(_source_of(data))
    if rel is None:
        return True
    try:
        invoice = _invoice(data.get("invoice"))
        if invoice is None:
            return False
        raw = data.get("raw_text") or invoice.source.raw_text or ""
        findings = _finding_list(data.get("findings") or [])
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
        path = _record_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    except OSError:
        _drop(rel)
        return False
    _FAILED.discard(rel.as_posix())
    _unblock(rel)
    return True


def _drop(rel: Path) -> None:
    """A failed write must not leave the previous record payable."""

    _FAILED.add(rel.as_posix())
    try:
        _record_path(rel).unlink(missing_ok=True)
    except OSError:
        pass
    if _record_path(rel).exists():
        _block(rel)


def _approve(result, reviewer, note, amount, ledger, log, now) -> dict:
    name, text = _plain(reviewer), _plain(note)
    when = _timestamp(now)
    filename = _repo_key(result) or "unknown"

    def finish(decision, code, message, saved=None):
        invoice = None if saved is None else saved.get("invoice")
        record = {
            "time": when,
            "invoice": filename,
            "invoice_number": None if invoice is None else invoice.invoice_number,
            "vendor": None if invoice is None else invoice.vendor,
            "amount": None if invoice is None or invoice.total is None else format(invoice.total, "f"),
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
    if _is_blocked(filename):
        return finish("held", "SYSTEM_ERROR", "The release record could not be replaced.")
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

    if "outcome" in result and result.get("outcome") != saved.get("outcome"):
        return "The request does not match the pipeline record."
    if "findings" in result and [item.code for item in _finding_list(result.get("findings") or [])] != list(saved.get("codes") or []):
        return "The request does not match the pipeline record."
    if "invoice" not in result or result.get("invoice") is None:
        return None
    claimed = _invoice(result.get("invoice"))
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
    try:
        with redirect_stdout(io.StringIO()):
            checked = validate(state, ROOT / "inventory.db", ledger_path=ledger)
    except (OSError, sqlite3.Error) as exc:
        return f"inventory database could not be opened: {exc}"
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


def _block_file() -> Path:
    """Beside the release folder, so a read-only folder cannot hide a failed rewrite."""

    directory = _record_dir()
    return directory.parent / f"{directory.name}.blocked"


def _blocked_names() -> set[str] | None:
    path = _block_file()
    if not path.exists():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return {line.strip() for line in text.splitlines() if line.strip()}


def _write_blocked(names: set[str]) -> None:
    path = _block_file()
    if not names:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _block(rel: Path) -> None:
    names = _blocked_names()
    if names is None:
        names = set()
    names.add(rel.as_posix())
    try:
        _write_blocked(names)
    except OSError:
        pass


def _unblock(rel: Path) -> None:
    names = _blocked_names()
    if not names:
        return
    names.discard(rel.as_posix())
    try:
        _write_blocked(names)
    except OSError:
        pass


def _is_blocked(filename: str) -> bool:
    rel = _repo_path(filename)
    if rel is None:
        return False
    if rel.as_posix() in _FAILED:
        return True
    names = _blocked_names()
    if names is None:
        return True
    return rel.as_posix() in names


def _record_path(rel: Path) -> Path:
    # quote keeps a__b/x.txt and a/b__x.txt from sharing one file.
    return _record_dir() / (quote(rel.as_posix(), safe="") + ".release.json")


def _repo_path(path) -> Path | None:
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


def _repo_key(result) -> str | None:
    rel = _repo_path(_source_of(result))
    return None if rel is None else rel.as_posix()


def _source_of(result):
    invoice = result.get("invoice")
    if isinstance(invoice, ExtractedInvoice):
        return invoice.source.path
    if isinstance(invoice, dict):
        return (invoice.get("source") or {}).get("path")
    return result.get("invoice_path")


def _load(filename: str):
    rel = _repo_path(filename)
    if rel is None or rel.as_posix() in _FAILED:
        return None
    path = _record_path(rel)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) and record.get("record") == "release" else None


def _view(result) -> dict:
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


def _invoice(value):
    if value is None:
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
