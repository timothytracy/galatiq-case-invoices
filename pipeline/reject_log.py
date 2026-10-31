"""Reject log. The other exit. Nothing on this path is paid."""

from models import PaymentRecord, PipelineState
from pipeline.decisions import _decision_from_validation, _failure_decision, _tighten
from pipeline.stage_log import stage_log
from rules import finding_codes


def reject_log(state: PipelineState) -> dict:
    outcome = "manual_review" if state.agent_error else (state.outcome or "reject")
    if state.agent_error:
        reason = state.agent_error
        decision = _failure_decision(state, reason)
    elif state.decision is not None:
        decision = state.decision
        if decision.decision != outcome:
            decision = _tighten(decision, outcome, decision.reasoning.conclusion, [])
        reason = decision.reasoning.conclusion
    else:
        reason, decision = _decision_from_validation(state, outcome)

    invoice = state.invoice
    record = PaymentRecord(
        status="not_paid",
        vendor=None if invoice is None else invoice.vendor,
        amount=None if invoice is None else invoice.total,
        currency=None if invoice is None else invoice.currency,
        detail=reason,
    )
    result = {
        "outcome": outcome,
        "reason": reason,
        "decision": decision.model_dump(mode="json"),
        "payment": record.model_dump(mode="json"),
        "codes": finding_codes(state.findings),
    }
    return {
        "outcome": outcome,
        "decision": decision,
        "payment": record,
        "logs": state.logs + [stage_log(state.filename, "reject_log", result)],
    }
