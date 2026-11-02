"""Pay a held invoice after a reviewer approves it.

The caller's dict only names the invoice. Payment uses the pipeline record
written at the end of the run (`logs/<file>.release.json`). The review screen
does not call this yet.

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
_LOGS = ROOT / "logs"
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

    ledger, temp_ledger = _temp_ledger(ledger_path, demo)
    log, temp_log = _temp_log(log_path, demo)
    try:
        return _approve(result, reviewer, note, amount, ledger, log, now)
    finally:
        if temp_ledger is not None:
            temp_ledger.unlink(missing_ok=True)
        if temp_log is not None:
            temp_log.unlink(missing_ok=True)


def remember(result) -> None:
    """Write the release record for one finished run. The pipeline calls this."""

    filename = _filename(result)
    if not filename:
        return
    invoice = _invoice(_pick(result, "invoice"))
    raw = _pick(result, "raw_text") or ""
    if not raw and invoice is not None and invoice.source.raw_text:
        raw = invoice.source.raw_text
    findings = _finding_list(_pick(result, "findings") or [])
    record = {
        "record": "release",
        "file": filename,
        "outcome": _pick(result, "outcome"),
        "codes": [item.code for item in findings],
        "reason_code": _pick(result, "reason_code"),
        "critique_concerns": list(_pick(result, "critique_concerns") or []),
        "raw_text": raw,
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "source": None if invoice is None else invoice.source.path,
        "invoice": None if invoice is None else invoice.model_dump(mode="json"),
        "findings": [_finding_row(item) for item in findings],
    }
    _LOGS.mkdir(parents=True, exist_ok=True)
    (_LOGS / f"{filename}.release.json").write_text(json.dumps(record) + "\n", encoding="utf-8")


def _approve(result, reviewer, note, amount, ledger, log, now) -> dict:
    name, text = _plain(reviewer), _plain(note)
    when = _timestamp(now)
    filename = _filename(result) or "unknown"

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
    blocked = review_release_block(saved.get("outcome"), saved.get("findings") or [], saved.get("reason_code"))
    if blocked:
        return finish("held", NOT_PAYABLE, blocked, saved)
    if invoice is None or invoice.total is None or invoice.total <= 0:
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
    invoice = saved.get("invoice")
    path = _saved_path(None if invoice is None else invoice.source.path)
    if path is None or not path.is_file():
        return "The source file is missing."
    try:
        on_disk = _file_text(path)
    except (OSError, ValueError):
        return "The source file could not be read."
    recorded = saved.get("raw_text") or ""
    if on_disk != recorded or hashlib.sha256(on_disk.encode()).hexdigest() != saved.get("raw_sha256"):
        return "The source file does not match the pipeline record."
    if invoice is None or not _grounded(invoice, on_disk):
        return "The vendor, invoice number, or total is not in the source file."
    return None


def _grounded(invoice: ExtractedInvoice, raw: str) -> bool:
    folded = re.sub(r"[\s,\-]", "", raw.casefold())
    vendor = re.sub(r"[\s,\-]", "", (invoice.vendor or "").casefold())
    number = re.sub(r"[\s,\-]", "", (invoice.invoice_number or "").casefold())
    digits = re.search(r"(\d+)", invoice.invoice_number or "")
    number_ok = bool(number and number in folded) or bool(
        digits and re.search(rf"(?<!\d){digits.group(1)}(?!\d)", raw)
    )
    if not vendor or vendor not in folded or not number_ok:
        return False
    if invoice.total is None or not amount_in_text(invoice.total, raw):
        return False
    return all(_name_in_source(line.item, raw) for line in invoice.line_items)


def _saved_path(path) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate


def _file_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from parsers.pdf_invoice import pdf_text
        return pdf_text(path)
    return path.read_text(encoding="utf-8")


def _pay_state(saved, invoice: ExtractedInvoice) -> PipelineState:
    return PipelineState(
        invoice_path=invoice.source.path,
        filename=saved["file"],
        raw_text=saved.get("raw_text") or "",
        invoice=invoice,
        findings=_finding_list(saved.get("findings") or []),
        outcome="hold",
        critique_concerns=list(saved.get("critique_concerns") or []),
    )


def _load(filename: str):
    path = _LOGS / f"{filename}.release.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) and record.get("record") == "release" else None


def _filename(result) -> str | None:
    if isinstance(result, PipelineState):
        name = result.filename
    elif isinstance(result, dict):
        name = result.get("filename") or result.get("file")
        if not name and isinstance(result.get("invoice"), dict):
            name = (result["invoice"].get("source") or {}).get("filename")
    else:
        name = None
    name = Path(str(name)).name if name else ""
    return name or None


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
    from decision.outcome import SEND_BACK_CAP_MESSAGE
    row = {"code": item.code, "message": item.message, "severity": item.severity}
    if item.item:
        row["item"] = item.item
    if item.send_back_cap or item.message == SEND_BACK_CAP_MESSAGE:
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


def _temp_ledger(path, demo):
    if demo:
        return _scratch("payments-", ".db")
    return (Path(path) if path is not None else Path("payments.db"), None)


def _temp_log(path, demo):
    if demo and path is None:
        return _scratch("approve-", ".jsonl")
    return (Path(path) if path is not None else Path("output") / "reviews" / "approve_pay.jsonl", None)


def _scratch(prefix, suffix):
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(handle)
    path = Path(name)
    return path, path


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()


def _offline(path: Path):
    from db import create_database
    from graph import run_invoice
    from tests.fake_llm_eval import AS_OF, handler_for
    from tests.fakes import FakeClock, FakeLLM, ZeroRandom

    database = ROOT / "inventory.db"
    if not database.is_file():
        create_database(database)
    with redirect_stdout(io.StringIO()):
        return run_invoice(
            path, database, FakeLLM(handler_for(path)),
            clock=FakeClock(), rng=ZeroRandom(), max_attempts=5, as_of=AS_OF,
        )


def _run_demo() -> int:
    hold = _offline(ROOT / "data" / "invoices" / "invoice_1012.txt")
    rejected = _offline(ROOT / "data" / "invoices" / "invoice_1002.txt")
    first = approve_and_pay(
        hold, reviewer="Avery Chen", note="Confirmed this is the same company.",
        amount=hold["invoice"].total, demo=True,
    )
    second = approve_and_pay(
        rejected, reviewer="Avery Chen", note="Stock is short, so this cannot be paid.",
        amount=rejected["invoice"].total, demo=True,
    )
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
