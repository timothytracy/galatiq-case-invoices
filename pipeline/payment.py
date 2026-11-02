"""Payment. The mock gateway runs only with an unused PASS-audit authorization."""

from decimal import Decimal

from models import Finding, PaymentAuthorization, PaymentRecord, PipelineState
from pipeline.stage_log import stage_log
from decision.invoice_fields import non_usd_message
from decision.outcome import HOLD_CODES, PAYMENT_REFUSED, REJECT_CODES, finding_codes
from decision.policy import POLICY
from decision.wording import explain
from ledger import arrival_findings, record_payment

# Keys already accepted in this process.
_USED_KEYS: set[str] = set()


def payment(state: PipelineState, ledger_path=None, *, release: bool = False) -> dict:
    invoice = state.invoice
    blocked = "" if release else _payment_blocked(state)
    refusal = blocked or _authorization_refusal(state)
    auth = state.authorization
    findings = list(state.findings)
    if refusal or invoice is None or not isinstance(auth, PaymentAuthorization):
        if refusal and not blocked:
            findings.append(Finding(code=PAYMENT_REFUSED, message=refusal, severity="blocking"))
        record = PaymentRecord(
            status="not_paid",
            vendor=None if invoice is None else invoice.vendor,
            amount=None if invoice is None else invoice.total,
            currency=None if invoice is None else invoice.currency,
            detail=refusal or "payment blocked",
        )
    else:
        # The release record is written before the ledger row. A failed write
        # pays nothing and does not raise.
        if not release:
            from pipeline.approve_hold import remember
            noted = remember(state, ledger_path)
            if noted is not True:
                return _hold_unreadable_ledger(
                    state, findings, noted or "release record could not be written", release
                )
        wrote = _write_ledger(state, ledger_path)
        if wrote is False:
            return _refuse_ledger(state, findings, ledger_path, "DUPLICATE_INVOICE", release)
        if wrote == "SPLIT_INVOICE":
            return _refuse_ledger(state, findings, ledger_path, "SPLIT_INVOICE", release)
        if wrote is not True:
            return _hold_unreadable_ledger(state, findings, wrote, release)
        # Record the key before the mock call so a retry cannot pay twice.
        _USED_KEYS.add(auth.key)
        response = mock_payment(invoice.vendor, invoice.total)
        record = PaymentRecord(
            status="paid",
            vendor=invoice.vendor,
            amount=invoice.total,
            currency=invoice.currency,
            detail="mock payment accepted",
            mock_response=response,
        )
    result = {
        "payment": record.model_dump(mode="json"),
        "outcome": state.outcome,
        "codes": finding_codes(findings),
    }
    return {
        "payment": record,
        "findings": findings,
        "logs": state.logs + [stage_log(state.filename, "payment", result, echo=not release)],
    }


def _write_ledger(state: PipelineState, ledger_path) -> bool | str:
    """True when there is no ledger, or the row was inserted.

    False is a duplicate. "SPLIT_INVOICE" is the split rule. Any other string
    is an unreadable ledger. None as the path keeps the direct unit tests on
    the authorization key. The graph always passes a path.
    """

    if ledger_path is None:
        return True
    invoice = state.invoice
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
    )


def _hold_unreadable_ledger(state: PipelineState, findings: list, message: str, release: bool = False) -> dict:
    """The ledger file could not be written. Nothing is paid."""

    findings = list(findings) + [
        Finding(code="SYSTEM_ERROR", message=message, severity="warning")
    ]
    outcome, code, reason, action = explain(findings, state.invoice)
    record = PaymentRecord(
        status="not_paid",
        vendor=state.invoice.vendor,
        amount=state.invoice.total,
        currency=state.invoice.currency,
        detail=message,
    )
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
    outcome, reason_code, reason, action = explain(findings, state.invoice)
    record = PaymentRecord(
        status="not_paid",
        vendor=state.invoice.vendor,
        amount=state.invoice.total,
        currency=state.invoice.currency,
        detail=detail,
    )
    result = {
        "payment": record.model_dump(mode="json"),
        "outcome": outcome,
        "reason": reason,
        "reason_code": reason_code,
        "codes": finding_codes(findings),
    }
    return {
        "payment": record,
        "findings": findings,
        "outcome": outcome,
        "reason_code": reason_code,
        "action": action,
        "logs": state.logs + [stage_log(state.filename, "payment", result, echo=not release)],
    }


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
    return ""


def _authorization_refusal(state: PipelineState) -> str:
    """Empty when the PASS audit's sign-off matches this invoice and is unused."""

    auth = state.authorization
    if not isinstance(auth, PaymentAuthorization):
        return "payment refused: no audit authorization"
    invoice = state.invoice
    if invoice is None:
        return "payment refused: no invoice"
    if invoice.vendor != auth.vendor:
        return "payment refused: vendor does not match the audit authorization"
    if invoice.total != auth.amount:
        return "payment refused: amount does not match the audit authorization"
    if invoice.currency != auth.currency:
        return "payment refused: currency does not match the audit authorization"
    if auth.key in _USED_KEYS:
        return "payment refused: authorization key was already used"
    return ""


def mock_payment(vendor, amount):
    shown = f"{amount:.2f}" if isinstance(amount, Decimal) else amount
    print(f"Paid {shown} to {vendor}", flush=True)
    return {"status": "success"}
