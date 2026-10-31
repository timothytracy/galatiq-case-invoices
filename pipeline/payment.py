"""Payment. The mock gateway runs only when nothing is blocking an approval."""

from decimal import Decimal

from models import PaymentRecord, PipelineState
from pipeline.stage_log import stage_log
from rules import HARD_REJECT


def payment(state: PipelineState) -> dict:
    invoice = state.invoice
    blocked = _payment_blocked(state)
    if blocked or invoice is None:
        record = PaymentRecord(
            status="not_paid",
            vendor=None if invoice is None else invoice.vendor,
            amount=None if invoice is None else invoice.total,
            currency=None if invoice is None else invoice.currency,
            detail=blocked or "payment blocked",
        )
    else:
        response = mock_payment(invoice.vendor, invoice.total)
        record = PaymentRecord(
            status="paid",
            vendor=invoice.vendor,
            amount=invoice.total,
            currency=invoice.currency,
            detail="mock payment accepted",
            mock_response=response,
        )
    result = {"payment": record.model_dump(mode="json"), "outcome": state.outcome}
    return {
        "payment": record,
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
    if invoice.currency != "USD":
        return f"non-USD currency ({invoice.currency}), manual review required"
    if state.validation is not None:
        for finding in state.validation.findings:
            if finding.code in HARD_REJECT or finding.code in {"non_usd", "fraud_signal"}:
                return finding.message
    return ""


def mock_payment(vendor, amount):
    shown = f"{amount:.2f}" if isinstance(amount, Decimal) else amount
    print(f"Paid {shown} to {vendor}", flush=True)
    return {"status": "success"}
