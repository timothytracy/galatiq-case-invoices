"""Payment. The mock gateway runs only with an unused PASS-audit authorization."""

from decimal import Decimal

from models import Finding, PaymentAuthorization, PaymentRecord, PipelineState
from pipeline.stage_log import stage_log
from decision.invoice_fields import non_usd_message
from decision.outcome import HOLD_CODES, PAYMENT_REFUSED, REJECT_CODES, finding_codes
from decision.policy import POLICY

# Keys already accepted in this process.
_USED_KEYS: set[str] = set()


def payment(state: PipelineState) -> dict:
    invoice = state.invoice
    blocked = _payment_blocked(state)
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
        "logs": state.logs + [stage_log(state.filename, "payment", result)],
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
