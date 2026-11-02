"""Pay a held invoice after a reviewer approves it.

The review screen calls approve_and_pay. It does not change the pipeline
outcome. A reject, a reject-class finding, or any hold that is not a review
hold is never paid. Currency, amount, and the payments ledger are checked
again, then the existing one-time authorization and payment step run.

Demo, with a temporary ledger and no model call:

    python3 -m pipeline.approve_hold --demo
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

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
from decision.invoice_fields import different, non_usd_message, show_money
from decision.outcome import review_release_block
from decision.policy import POLICY
from ledger import arrival_findings, paid_rows, split_window

DEFAULT_LOG = Path("output") / "reviews" / "approve_pay.jsonl"

# Stable codes for a release that does not pay. Ledger and currency reuse
# the pipeline's codes: NON_USD, DUPLICATE_INVOICE, SPLIT_INVOICE, SYSTEM_ERROR.
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

    reviewer and note must be non-empty. amount must equal the extracted
    total. demo=True uses a temporary ledger and deletes it before returning.
    Otherwise ledger_path defaults to payments.db in the working directory.
    The decision is appended to log_path (default output/reviews/approve_pay.jsonl).
    The caller's result is not modified, and its outcome is not rewritten.
    """

    ledger, temporary = _ledger(ledger_path, demo)
    log = Path(log_path) if log_path is not None else DEFAULT_LOG
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
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _approve(result, *, reviewer, note, amount, ledger_path, log_path, now) -> dict:
    name = (reviewer or "").strip()
    text = (note or "").strip()
    when = _timestamp(now)
    state, load_error = _state_from(result)
    invoice_name = _invoice_name(state)
    outcome = None if state is None else state.outcome
    findings = [] if state is None else [_public_finding(item) for item in state.findings]

    def finish(status: str, code: str | None, message: str) -> dict:
        record = {
            "reviewer": name,
            "note": text,
            "time": when,
            "invoice": invoice_name,
            "pipeline_outcome": outcome,
            "pipeline_findings": findings,
            "result": status,
            "reason_code": code,
            "reason": message,
        }
        _append(log_path, record)
        return {
            "result": status,
            "reason_code": code,
            "reason": message,
            "pipeline_outcome": outcome,
            "pipeline_findings": findings,
            "invoice": invoice_name,
            "payment_status": "paid" if status == "paid" else "not_paid",
            "record": record,
        }

    if not name:
        return finish("held", MISSING_REVIEWER, "A reviewer name is required.")
    if not text:
        return finish("held", MISSING_NOTE, "A note is required.")
    if load_error is not None or state is None or state.invoice is None:
        return finish("held", NOT_PAYABLE, load_error or "The invoice result could not be read.")

    blocked = review_release_block(state.outcome, state.findings, state.reason_code)
    if blocked is not None:
        return finish("held", NOT_PAYABLE, blocked)

    invoice = state.invoice
    if invoice.currency not in POLICY.allowed_currencies:
        return finish("held", "NON_USD", non_usd_message(invoice.currency))

    paid_amount = _money(amount)
    if invoice.total is None or paid_amount is None or different(paid_amount, invoice.total):
        shown = "missing" if paid_amount is None else show_money(paid_amount)
        extracted = "missing" if invoice.total is None else show_money(invoice.total)
        return finish(
            "held",
            AMOUNT_MISMATCH,
            f"Amount {shown} does not equal the extracted total {extracted}.",
        )

    try:
        rows = paid_rows(ledger_path)
        prior = arrival_findings(ledger_path, invoice, state.raw_text)
    except sqlite3.Error as exc:
        return finish("held", "SYSTEM_ERROR", f"payments ledger unreadable: {exc}")

    duplicate = next((item for item in prior if item.code == "DUPLICATE_INVOICE"), None)
    if duplicate is not None:
        return finish("held", "DUPLICATE_INVOICE", duplicate.message)
    _window, over = split_window(invoice, rows)
    if over:
        split = next((item for item in prior if item.code == "SPLIT_INVOICE"), None)
        message = (
            split.message
            if split is not None
            else "This invoice plus payments in the split window is over the approval threshold."
        )
        return finish("held", "SPLIT_INVOICE", message)

    # payment() still checks this sign-off against the invoice. The copy
    # exists so that step can run; the logged pipeline outcome stays hold.
    authorization = issue_authorization(state)
    if authorization is None:
        return finish("held", NOT_PAYABLE, "The invoice has no vendor or total to pay.")
    paid = payment(_for_payment(state, authorization), ledger_path)
    if paid["payment"].status == "paid":
        return finish("paid", None, "Reviewer approved this hold for payment.")
    code = paid.get("reason_code") or "PAYMENT_REFUSED"
    return finish("held", code, paid["payment"].detail)


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


def _state_from(result) -> tuple[PipelineState | None, str | None]:
    if isinstance(result, PipelineState):
        return result, None
    if not isinstance(result, dict):
        return None, "The invoice result could not be read."
    if "outcome" not in result and isinstance(result.get("state"), dict):
        result = result["state"]
    try:
        invoice = _invoice_from(result.get("invoice"))
        findings = [_finding_from(item) for item in result.get("findings") or []]
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
            outcome=result.get("outcome"),
            reason_code=result.get("reason_code"),
        ),
        None,
    )


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


def _timestamp(now) -> str:
    moment = now() if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(ledger_path, demo: bool) -> tuple[Path, Path | None]:
    if demo:
        handle, name = tempfile.mkstemp(prefix="payments-", suffix=".db")
        os.close(handle)
        path = Path(name)
        return path, path
    if ledger_path is None:
        return Path("payments.db"), None
    return Path(ledger_path), None


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


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


def _run_demo(log_path: Path | None) -> int:
    """One review hold is paid. One reject is refused. The ledger is temporary."""

    temporary = log_path is None
    if log_path is None:
        handle, name = tempfile.mkstemp(prefix="approve-", suffix=".jsonl")
        os.close(handle)
        log_path = Path(name)
    held = _demo_state(
        "demo_hold.txt",
        "INV-900",
        "hold",
        "VENDOR_RENAMED",
        "warning",
        "The vendor name says formerly.",
    )
    rejected = _demo_state(
        "demo_reject.txt",
        "INV-901",
        "reject",
        "STOCK_SHORTFALL",
        "blocking",
        "WidgetA quantity 20 exceeds stock of 5.",
    )
    try:
        first = approve_and_pay(
            held,
            reviewer="Avery Chen",
            note="Confirmed this is the same company.",
            amount="500.00",
            demo=True,
            log_path=log_path,
        )
        second = approve_and_pay(
            rejected,
            reviewer="Avery Chen",
            note="Stock is short, so this cannot be paid.",
            amount="500.00",
            demo=True,
            log_path=log_path,
        )
        print(json.dumps(first["record"]), flush=True)
        print(json.dumps(second["record"]), flush=True)
        return 0 if first["result"] == "paid" and second["result"] == "held" else 1
    finally:
        if temporary:
            log_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve a held invoice for payment.")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Pay one sample hold and refuse one sample reject on a temporary ledger.",
    )
    parser.add_argument("--result", help="Pipeline result JSON for one invoice")
    parser.add_argument("--reviewer", help="Reviewer name")
    parser.add_argument("--note", help="Why this hold is being released")
    parser.add_argument("--amount", help="Amount to pay. Must equal the extracted total")
    parser.add_argument("--ledger", help="Payments ledger. Defaults to payments.db")
    parser.add_argument("--log", help="Append-only decision log")
    args = parser.parse_args(argv)

    if args.demo:
        return _run_demo(Path(args.log) if args.log else None)
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
