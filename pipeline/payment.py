"""Payment. The mock gateway runs only with an unused PASS-audit authorization."""

import sqlite3
from decimal import Decimal
from pathlib import Path

from models import ExtractedInvoice, Finding, PaymentAuthorization, PaymentRecord, PipelineState
from pipeline.release_record import remember
from pipeline.stage_log import stage_log
from decision.invoice_fields import non_usd_message
from decision.pass_list import failed_checks, pass_checks
from decision.outcome import HOLD_CODES, PAYMENT_REFUSED, REJECT_CODES, finding_codes
from decision.policy import POLICY
from decision.wording import explain
from ledger import arrival_findings, record_payment, settle_payment
from ledger.payments import authorization_used, record_revision_difference, revision_difference

# Keys from payment() calls that passed no ledger. A ledger file keeps its own.
_NO_LEDGER_KEYS: set[str] = set()
PAYMENT_UNCONFIRMED = "payment did not confirm; ledger row marked failed"


def clear_keys_without_ledger() -> None:
    """Forget keys remembered only because payment() had no ledger file.

    Does not change any payments.db. A ledger stores its own used keys, and
    deleting that file drops them.
    """

    _NO_LEDGER_KEYS.clear()


def payment(state: PipelineState, ledger_path: str | Path | None = None, *, release: bool = False) -> dict:
    invoice = state.invoice
    findings = list(state.findings)
    try:
        payable = _payable_amount(state, ledger_path, release)
    except sqlite3.Error as exc:
        message = f"payments ledger unreadable: {exc}"
        return _hold_unreadable_ledger(state, findings, message, release)
    blocked = "" if release else _payment_blocked(state)
    refusal = blocked or _authorization_refusal(state, ledger_path, payable)
    auth = state.authorization
    if refusal or invoice is None or not isinstance(auth, PaymentAuthorization) or payable is None:
        if refusal and not blocked:
            findings.append(Finding(code=PAYMENT_REFUSED, message=refusal, severity="blocking"))
        record = not_paid_record(invoice, refusal or "payment blocked")
        return _log_payment(state, record, findings, release)
    return _pay(state, invoice, auth, findings, ledger_path, release, payable)


def _pay(
    state: PipelineState,
    invoice: ExtractedInvoice,
    auth: PaymentAuthorization,
    findings: list,
    ledger_path,
    release: bool,
    payable: Decimal,
) -> dict:
    """The steps after a valid authorization: release record, ledger row, mock gateway."""

    # The release record is written before the ledger row. A failed write
    # pays nothing and does not raise.
    if not release:
        noted = remember(state, ledger_path)
        if noted is not True:
            return _hold_unreadable_ledger(
                state, findings, noted or "release record could not be written", release
            )
    wrote = _write_ledger(state, ledger_path, auth.key, payable)
    if wrote is False:
        return _refuse_ledger(state, findings, ledger_path, "DUPLICATE_INVOICE", release)
    if wrote == "SPLIT_INVOICE":
        return _refuse_ledger(state, findings, ledger_path, "SPLIT_INVOICE", release)
    if wrote == "USED_AUTHORIZATION":
        detail = "payment refused: authorization key was already used"
        findings.append(Finding(code=PAYMENT_REFUSED, message=detail, severity="blocking"))
        return _log_payment(state, not_paid_record(invoice, detail), findings, release)
    if wrote is not True:
        return _hold_unreadable_ledger(state, findings, wrote, release)
    # No ledger file: remember the key here. A file stored it in the same write.
    if ledger_path is None:
        _NO_LEDGER_KEYS.add(auth.key)
    try:
        response = mock_payment(invoice.vendor, payable)
    except Exception:
        response = None
    confirmed = isinstance(response, dict) and response.get("status") == "success"
    if ledger_path is not None:
        confirmed = confirmed and settle_payment(ledger_path, auth.key, "paid")
        if not confirmed:
            settle_payment(ledger_path, auth.key, "failed")
    if not confirmed:
        return _hold_unreadable_ledger(state, findings, PAYMENT_UNCONFIRMED, release)
    record = PaymentRecord(
        status="paid",
        vendor=invoice.vendor,
        amount=payable,
        currency=invoice.currency,
        detail="mock payment accepted",
        mock_response=response,
    )
    return _log_payment(state, record, findings, release)


def not_paid_record(invoice: ExtractedInvoice | None, detail: str) -> PaymentRecord:
    return PaymentRecord(
        status="not_paid",
        vendor=None if invoice is None else invoice.vendor,
        amount=None if invoice is None else invoice.total,
        currency=None if invoice is None else invoice.currency,
        detail=detail,
    )


def _log_payment(state: PipelineState, record: PaymentRecord, findings: list, release: bool) -> dict:
    """The payment stage log when the outcome stays the one audit set."""

    result = {
        "payment": record.model_dump(mode="json"),
        "outcome": state.outcome,
        "codes": finding_codes(findings),
    }
    if not release:
        result["pass_list"] = pass_checks(state.invoice, findings, state.audit_verdict)
    return {
        "payment": record,
        "findings": findings,
        "logs": state.logs + [stage_log(state.filename, "payment", result, echo=not release)],
    }


def _log_explained(state: PipelineState, record: PaymentRecord, findings: list, release: bool) -> dict:
    """The payment stage log when the findings decide a new outcome."""

    outcome, code, reason, action = explain(findings, state.invoice)
    result = {
        "payment": record.model_dump(mode="json"),
        "outcome": outcome,
        "reason": reason,
        "reason_code": code,
        "codes": finding_codes(findings),
    }
    return {
        "payment": record,
        "findings": findings,
        "outcome": outcome,
        "reason_code": code,
        "action": action,
        "logs": state.logs + [stage_log(state.filename, "payment", result, echo=not release)],
    }


def _payable_amount(state: PipelineState, ledger_path, release: bool) -> Decimal | None:
    """What this call may pay. A higher revision pays the difference only."""

    invoice = state.invoice
    if invoice is None or invoice.total is None:
        return None
    if release and ledger_path is not None:
        owed = revision_difference(ledger_path, invoice, state.raw_text or "")
        if owed is not None:
            return owed
    return invoice.total


def _write_ledger(
    state: PipelineState, ledger_path, authorization_key: str, payable: Decimal
) -> bool | str:
    """True when there is no ledger, or the pending row was inserted.

    False is a duplicate. "SPLIT_INVOICE" is the split rule. Any other string
    is an unreadable ledger. None as the path keeps the direct unit tests on
    the authorization key. The graph always passes a path. A payable amount
    other than the invoice total is the unpaid difference of a revision.
    """

    if ledger_path is None:
        return True
    invoice = state.invoice
    if invoice.total is None or payable != invoice.total:
        return record_revision_difference(
            ledger_path,
            invoice,
            state.raw_text or "",
            payable,
            authorization_key,
            state.filename,
        )
    return record_payment(
        ledger_path,
        vendor=invoice.vendor,
        invoice_number=invoice.invoice_number,
        revision=invoice.revision,
        total=invoice.total,
        currency=invoice.currency,
        invoice_date=invoice.invoice_date,
        source_file=state.filename,
        raw_text=state.raw_text,
        authorization_key=authorization_key,
        status="pending",
    )


def _hold_unreadable_ledger(state: PipelineState, findings: list, message: str, release: bool = False) -> dict:
    """The ledger could not be written, or the gateway did not confirm. A SYSTEM_ERROR hold, not paid."""

    findings = list(findings) + [
        Finding(code="SYSTEM_ERROR", message=message, severity="warning")
    ]
    return _log_explained(state, not_paid_record(state.invoice, message), findings, release)


def _refuse_ledger(state, findings, ledger_path, code: str, release: bool) -> dict:
    """The write lock refused the row. Nothing is paid."""

    extra = arrival_findings(ledger_path, state.invoice, state.raw_text)
    if code == "DUPLICATE_INVOICE":
        detail = "payment refused: this vendor and invoice number are already in the payments ledger"
        fallback = "This vendor and invoice number are already in the payments ledger."
        item = "invoice_number"
    else:
        detail = "This invoice plus payments in the split window is over the approval threshold."
        fallback = detail
        item = "total"
    if not any(found.code == code for found in extra):
        extra = extra + [Finding(code=code, message=fallback, severity="warning", item=item)]
    elif code == "SPLIT_INVOICE":
        detail = next(found.message for found in extra if found.code == code)
    findings = list(findings) + list(extra)
    if code == "DUPLICATE_INVOICE":
        findings.append(Finding(code=PAYMENT_REFUSED, message=detail, severity="blocking"))
    return _log_explained(state, not_paid_record(state.invoice, detail), findings, release)


def _payment_blocked(state: PipelineState) -> str:
    if state.agent_error:
        return state.agent_error
    if state.outcome != "approve":
        return state.outcome or "not approved"
    if state.decision is None or state.decision.decision != "approve":
        return "decision is not approve"
    invoice = state.invoice
    if invoice is None:
        return "no invoice"
    if invoice.currency not in POLICY.allowed_currencies:
        return non_usd_message(invoice.currency)
    if state.validation is not None:
        for finding in state.validation.findings:
            if finding.code in REJECT_CODES or finding.code in HOLD_CODES:
                return finding.message
    failed = failed_checks(invoice, state.findings, state.audit_verdict)
    if failed:
        return "pass list failed: " + ", ".join(failed)
    return ""


def _key_was_used(ledger_path, key: str) -> bool:
    """True when this ledger, or the no-ledger bucket, has already accepted key."""

    if ledger_path is None:
        return key in _NO_LEDGER_KEYS
    return authorization_used(ledger_path, key)


def _authorization_refusal(state: PipelineState, ledger_path, payable: Decimal | None) -> str:
    """Empty when the PASS audit's sign-off matches this invoice and is unused."""

    auth = state.authorization
    if not isinstance(auth, PaymentAuthorization):
        return "payment refused: no audit authorization"
    invoice = state.invoice
    if invoice is None:
        return "payment refused: no invoice"
    if invoice.vendor != auth.vendor:
        return "payment refused: vendor does not match the audit authorization"
    if payable is None or payable != auth.amount:
        return "payment refused: amount does not match the audit authorization"
    if invoice.currency != auth.currency:
        return "payment refused: currency does not match the audit authorization"
    if _key_was_used(ledger_path, auth.key):
        return "payment refused: authorization key was already used"
    return ""


def mock_payment(vendor, amount):
    shown = f"{amount:.2f}" if isinstance(amount, Decimal) else amount
    print(f"Paid {shown} to {vendor}", flush=True)
    return {"status": "success"}
