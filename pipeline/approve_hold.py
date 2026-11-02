"""Pay a held invoice after a reviewer approves it.

approve_and_pay is the release function. The review screen does not call it
yet. The pipeline outcome is left as recorded. A reject, a reject-class
finding, or any hold that is not a review hold is never paid. The invoice is
validated again before the existing authorization and payment step run.

Demo, on a temporary ledger and log, with no model call:

    python3 -m pipeline.approve_hold --demo
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from pydantic import ValidationError

from db import create_database
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    ExtractedInvoice,
    Finding,
    LineItem,
    PipelineState,
    SourceInfo,
)
from pipeline.audit import issue_authorization
from pipeline.payment import payment
from pipeline.validation import validate
from decision.invoice_fields import non_usd_message, show_money
from decision.outcome import (
    HOLD_CODES,
    PAYABLE_HOLD_CODES,
    REJECT_CODES,
    review_release_block,
)
from decision.policy import POLICY

DEFAULT_LOG = Path("output") / "reviews" / "approve_pay.jsonl"
_OUTCOMES = {"approve", "reject", "hold"}
_CENT = Decimal("0.01")
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u180e]")

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
    """Approve one held invoice for payment.

    reviewer and note need at least two letters after whitespace and
    zero-width characters are removed. amount must match the extracted total
    to the cent. demo=True uses a temporary ledger and a temporary log, and
    deletes both before returning. Otherwise ledger_path defaults to
    payments.db and log_path to output/reviews/approve_pay.jsonl.
    The caller's result is not modified.
    """

    ledger, temp_ledger = _ledger(ledger_path, demo)
    log, temp_log = _log(log_path, demo)
    try:
        return _approve(
            result,
            reviewer=reviewer,
            note=note,
            amount=amount,
            ledger_path=ledger,
            log_path=log,
            now=now,
        )
    finally:
        if temp_ledger is not None:
            temp_ledger.unlink(missing_ok=True)
        if temp_log is not None:
            temp_log.unlink(missing_ok=True)


def _approve(result, *, reviewer, note, amount, ledger_path, log_path, now) -> dict:
    name = _plain(reviewer)
    text = _plain(note)
    when = _timestamp(now)
    concerns: list[str] = []
    state, load_error = _state_from(result)
    invoice_name = _invoice_name(state)
    outcome = _stated_outcome(result) if state is None else state.outcome
    codes = [] if state is None else [item.code for item in state.findings]
    if state is not None:
        concerns = list(state.critique_concerns)

    def finish(decision: str, code: str | None, message: str, *, status: str | None = None) -> dict:
        kept = decision if status is None else status
        record = _entry(
            when, invoice_name, state, outcome, codes, name, text, concerns, kept, code, message
        )
        _append(log_path, record)
        paid = kept == "paid"
        return {
            "result": "paid" if paid else "held",
            "reason_code": code,
            "reason": message,
            "pipeline_outcome": outcome,
            "pipeline_findings": [] if state is None else [_public_finding(item) for item in state.findings],
            "critique_concerns": concerns,
            "invoice": invoice_name,
            "payment_status": "paid" if paid else "not_paid",
            "record": record,
        }

    if not _two_letters(name):
        return finish("held", MISSING_REVIEWER, "A reviewer name is required.")
    if not _two_letters(text):
        return finish("held", MISSING_NOTE, "A note is required.")
    if outcome not in _OUTCOMES:
        return finish("held", NOT_PAYABLE, "Only a held invoice can be approved for payment.")
    if load_error is not None or state is None or state.invoice is None:
        return finish("held", NOT_PAYABLE, load_error or "The invoice result could not be read.")

    blocked = review_release_block(state.outcome, state.findings, state.reason_code)
    if blocked is not None:
        return finish("held", NOT_PAYABLE, blocked)

    mismatch = _source_mismatch(state)
    if mismatch is not None:
        return finish("held", NOT_PAYABLE, mismatch)

    invoice = state.invoice
    if invoice.total is None or invoice.total <= 0:
        return finish("held", NOT_PAYABLE, "The total must be greater than zero.")
    if invoice.currency not in POLICY.allowed_currencies:
        return finish("held", "NON_USD", non_usd_message(invoice.currency))

    paid_amount = _money(amount)
    if paid_amount is None or _cents(paid_amount) != _cents(invoice.total):
        shown = "missing" if paid_amount is None else show_money(paid_amount)
        return finish(
            "held",
            AMOUNT_MISMATCH,
            f"Amount {shown} does not equal the extracted total {show_money(invoice.total)}.",
        )

    checked = validate(state, _stock_db(), ledger_path=ledger_path)
    blocked = _validation_block(checked.get("findings") or [])
    if blocked is not None:
        return finish("held", blocked[0], blocked[1])

    # The attempt line is on disk before payment() runs.
    _append(log_path, _entry(
        when, invoice_name, state, outcome, codes, name, text, concerns,
        "attempt", None, "Paying after the guards passed.",
    ))
    authorization = issue_authorization(state)
    if authorization is None:
        return finish("held", NOT_PAYABLE, "The invoice has no vendor or total to pay.")
    # Re-validation already refused rejects. This copy only lets payment()
    # run its authorization and ledger checks.
    paid = payment(_for_payment(state, authorization), ledger_path)
    if paid["payment"].status == "paid":
        return finish("paid", None, "Reviewer approved this hold for payment.")
    code = paid.get("reason_code") or "PAYMENT_REFUSED"
    return finish("held", code, paid["payment"].detail)


def _validation_block(findings) -> tuple[str, str] | None:
    """A code from a fresh validate() that means do not pay."""

    for finding in findings:
        if finding.code == "SYSTEM_ERROR":
            return "SYSTEM_ERROR", finding.message
    for code in ("DUPLICATE_INVOICE", "SPLIT_INVOICE"):
        for finding in findings:
            if finding.code == code:
                return code, finding.message
    for finding in findings:
        if finding.code in REJECT_CODES:
            return NOT_PAYABLE, f"{finding.code} is a reject-class finding and is never paid."
        if finding.code in HOLD_CODES and finding.code not in PAYABLE_HOLD_CODES:
            return NOT_PAYABLE, f"{finding.code} is not a review hold and is never paid."
    return None


def _for_payment(state: PipelineState, authorization) -> PipelineState:
    """A copy the payment step can accept. The original state is not changed."""

    invoice = state.invoice
    decision = ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(
            rules_applied=["A reviewer released a review hold."],
            evidence=[f"total {show_money(invoice.total)} {invoice.currency}"],
            risks_considered=["The reviewer approved this held invoice for payment."],
            conclusion="Reviewer release. The pipeline outcome is unchanged.",
        ),
        amount=invoice.total,
        currency=invoice.currency,
    )
    return state.model_copy(
        update={
            "outcome": "approve",
            "agent_error": None,
            "authorization": authorization,
            "decision": decision,
            "validation": None,
        }
    )


def _entry(when, invoice_name, state, outcome, codes, name, text, concerns, decision, code, message) -> dict:
    invoice = None if state is None else state.invoice
    return {
        "time": when,
        "invoice": invoice_name,
        "invoice_number": None if invoice is None else invoice.invoice_number,
        "vendor": None if invoice is None else invoice.vendor,
        "amount": None if invoice is None or invoice.total is None else show_money(invoice.total),
        "currency": None if invoice is None else invoice.currency,
        "pipeline_outcome": outcome,
        "pipeline_codes": list(codes),
        "reviewer": name,
        "note": text,
        "critique_concerns": list(concerns),
        "decision": decision,
        "reason_code": code,
        "reason": message,
    }


def _stated_outcome(result):
    if isinstance(result, PipelineState):
        return result.outcome
    if isinstance(result, dict):
        body = result["state"] if "outcome" not in result and isinstance(result.get("state"), dict) else result
        return body.get("outcome")
    return None


def _state_from(result) -> tuple[PipelineState | None, str | None]:
    if isinstance(result, PipelineState):
        return result, None
    if not isinstance(result, dict):
        return None, "The invoice result could not be read."
    if "outcome" not in result and isinstance(result.get("state"), dict):
        result = result["state"]
    outcome = result.get("outcome")
    if outcome is not None and outcome not in _OUTCOMES:
        return None, "Only a held invoice can be approved for payment."
    try:
        invoice = _invoice_from(result.get("invoice"))
        findings = [_finding_from(item) for item in result.get("findings") or []]
        concerns = [str(item) for item in result.get("critique_concerns") or []]
    except (TypeError, ValidationError, ValueError):
        return None, "The invoice result could not be read."
    filename = result.get("filename") or (invoice.source.filename if invoice is not None else "")
    raw = result.get("raw_text") or ""
    if not raw and invoice is not None and invoice.source.raw_text:
        raw = invoice.source.raw_text
    return (
        PipelineState(
            invoice_path=str(result.get("invoice_path") or filename or "invoice"),
            filename=str(filename or "invoice"),
            raw_text=raw,
            invoice=invoice,
            findings=findings,
            outcome=outcome,
            reason_code=result.get("reason_code"),
            critique_concerns=concerns,
        ),
        None,
    )


def _source_mismatch(state: PipelineState) -> str | None:
    """The carried text must match the file when that file is present."""

    path = None
    if state.invoice is not None and state.invoice.source.path:
        path = Path(state.invoice.source.path)
    elif state.invoice_path:
        path = Path(state.invoice_path)
    if path is None or not path.is_file():
        return None
    try:
        on_disk = _file_text(path)
    except (OSError, ValueError):
        return "The source file could not be read."
    carried = state.raw_text or ""
    if carried != on_disk:
        return "The invoice text does not match the source file."
    return None


def _file_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from parsers.pdf_invoice import pdf_text
        return pdf_text(path)
    return path.read_text(encoding="utf-8")


def _invoice_from(value) -> ExtractedInvoice | None:
    if value is None:
        return None
    if isinstance(value, ExtractedInvoice):
        return value
    if isinstance(value, dict):
        return ExtractedInvoice.model_validate(value)
    raise TypeError("invoice")


def _finding_from(value) -> Finding:
    if isinstance(value, Finding):
        return value
    if isinstance(value, dict):
        return Finding.model_validate(value)
    raise TypeError("finding")


def _public_finding(finding: Finding) -> dict:
    row = {"code": finding.code, "message": finding.message, "severity": finding.severity}
    if finding.item:
        row["item"] = finding.item
    return row


def _invoice_name(state: PipelineState | None) -> str:
    if state is None:
        return "unknown"
    if state.filename:
        return state.filename
    if state.invoice is not None and state.invoice.invoice_number:
        return state.invoice.invoice_number
    return "unknown"


def _plain(value) -> str:
    return _INVISIBLE.sub("", value or "").strip()


def _two_letters(value: str) -> bool:
    return sum(character.isalpha() for character in value) >= 2


def _money(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value).strip())
        except Exception:
            return None
    if not amount.is_finite():
        return None
    return amount


def _cents(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _timestamp(now) -> str:
    moment = now() if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _temp(prefix: str, suffix: str) -> Path:
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(handle)
    return Path(name)


def _ledger(ledger_path, demo: bool) -> tuple[Path, Path | None]:
    if demo:
        path = _temp("payments-", ".db")
        return path, path
    if ledger_path is None:
        return Path("payments.db"), None
    return Path(ledger_path), None


def _log(log_path, demo: bool) -> tuple[Path, Path | None]:
    if demo and log_path is None:
        path = _temp("approve-", ".jsonl")
        return path, path
    if log_path is None:
        return DEFAULT_LOG, None
    return Path(log_path), None


def _stock_db() -> Path:
    root = Path(__file__).resolve().parents[1] / "inventory.db"
    if root.is_file():
        return root
    local = Path("inventory.db")
    if local.is_file():
        return local
    return create_database(root)


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()


def _demo_state(filename: str, number: str, outcome: str, code: str, severity: str, message: str):
    total = Decimal("500.00")
    invoice = ExtractedInvoice(
        vendor="Northwind Supply",
        invoice_number=number,
        invoice_date="2026-04-15",
        due_date="2026-05-15",
        currency="USD",
        total=total,
        subtotal=total,
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("2"),
                unit_price=Decimal("250.00"),
                amount=total,
            )
        ],
        source=SourceInfo(path=filename, filename=filename, format="txt", raw_text="WidgetA"),
    )
    return PipelineState(
        invoice_path=filename,
        filename=filename,
        raw_text=f"{filename}\nWidgetA\n",
        invoice=invoice,
        findings=[Finding(code=code, message=message, severity=severity)],
        outcome=outcome,
        reason_code=code,
    )


def _run_demo() -> int:
    """One review hold is paid. One reject is refused. Ledger and log are temporary."""

    first = approve_and_pay(
        _demo_state(
            "demo_hold.txt", "INV-900", "hold", "VENDOR_RENAMED", "warning",
            "The vendor name says formerly.",
        ),
        reviewer="Avery Chen",
        note="Confirmed this is the same company.",
        amount="500.00",
        demo=True,
    )
    second = approve_and_pay(
        _demo_state(
            "demo_reject.txt", "INV-901", "reject", "STOCK_SHORTFALL", "blocking",
            "WidgetA quantity 20 exceeds stock of 5.",
        ),
        reviewer="Avery Chen",
        note="Stock is short, so this cannot be paid.",
        amount="500.00",
        demo=True,
    )
    print(json.dumps(first["record"]), flush=True)
    print(json.dumps(second["record"]), flush=True)
    return 0 if first["result"] == "paid" and second["result"] == "held" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve a held invoice for payment.")
    parser.add_argument("--demo", action="store_true", help="Pay one sample hold and refuse one reject.")
    parser.add_argument("--result", help="Pipeline result JSON for one invoice")
    parser.add_argument("--reviewer", help="Reviewer name")
    parser.add_argument("--note", help="Why this hold is being released")
    parser.add_argument("--amount", help="Amount to pay. Must equal the extracted total")
    parser.add_argument("--ledger", help="Payments ledger. Defaults to payments.db")
    parser.add_argument("--log", help="Append-only decision log")
    args = parser.parse_args(argv)
    if args.demo:
        return _run_demo()
    if not args.result or args.reviewer is None or args.note is None or args.amount is None:
        print("Need --result, --reviewer, --note, and --amount. Or pass --demo.", file=sys.stderr)
        return 2
    path = Path(args.result)
    if not path.is_file():
        print(f"Result file not found: {path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Result file is not JSON: {exc}", file=sys.stderr)
        return 2
    decision = approve_and_pay(
        payload,
        reviewer=args.reviewer,
        note=args.note,
        amount=args.amount,
        ledger_path=args.ledger,
        log_path=args.log,
    )
    print(json.dumps(decision["record"]), flush=True)
    return 0 if decision["result"] == "paid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
