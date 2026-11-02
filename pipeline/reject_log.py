"""Reject log. The other exit. Nothing here is paid."""

from models import PaymentRecord, PipelineState
from pipeline.decisions import decision_from_validation, failure_decision, tighten
from pipeline.stage_log import stage_log
from decision.outcome import finding_codes
from decision.wording import ACTION, explain


def _stage_name(outcome: str) -> str:
    """Holds are logged as hold. Rejects stay on reject_log. The graph node is shared."""

    return "hold" if outcome == "hold" else "reject_log"


def reject_log(state: PipelineState, ledger_path=None) -> dict:
    findings = list(state.findings)
    outcome, code, reason, action = explain(findings, state.invoice)
    if state.agent_error and code not in {"SYSTEM_ERROR", "BAD_REASONING"}:
        outcome, code, action = "hold", "SYSTEM_ERROR", ACTION["SYSTEM_ERROR"]
        reason = f"{state.agent_error} {action}"
    elif state.outcome in {"reject", "hold"}:
        outcome = state.outcome
    if state.agent_error:
        decision = failure_decision(state, reason)
    elif state.decision is not None:
        decision = state.decision
        if decision.decision != outcome:
            decision = tighten(decision, outcome, reason, [reason])
        else:
            decision = decision.model_copy(
                update={
                    "reasoning": decision.reasoning.model_copy(update={"conclusion": reason}),
                }
            )
    else:
        _, decision = decision_from_validation(state, outcome)
        decision = decision.model_copy(
            update={"reasoning": decision.reasoning.model_copy(update={"conclusion": reason})}
        )
    decision = decision.model_copy(update={"decision": outcome, "reason_code": code, "action": action})
    from pipeline.approve_hold import remember
    saved = state.model_copy(update={"outcome": outcome, "reason_code": code, "decision": decision})
    if remember(saved, ledger_path) is not True:
        outcome, code, action = "hold", "SYSTEM_ERROR", ACTION["SYSTEM_ERROR"]
        reason = f"release record could not be written {action}"
        decision = decision.model_copy(update={"decision": outcome, "reason_code": code, "action": action})

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
        "reason_code": code,
        "action": action,
        "decision": decision,
        "payment": record,
        "logs": state.logs + [stage_log(state.filename, _stage_name(outcome), result)],
    }
