"""Decision objects built in Python.

The model does not write rules_applied or evidence. Audit and the reject log
share the tighten step so a revision cannot drift between those two exits.
"""

from models import ApprovalDecision, ApprovalReasoning, PipelineState
from rules import RULE_TEXT, show_money


def _tighten(decision: ApprovalDecision, outcome: str, conclusion: str, extra: list[str]) -> ApprovalDecision:
    reasoning = ApprovalReasoning(
        rules_applied=list(decision.reasoning.rules_applied) + ["Audit and critique can only make the decision stricter."],
        evidence=list(decision.reasoning.evidence) + (extra or ["The audit kept the stricter outcome."]),
        risks_considered=list(decision.reasoning.risks_considered),
        conclusion=conclusion,
    )
    return ApprovalDecision(
        decision=outcome,
        reasoning=reasoning,
        revisions=list(decision.revisions) + [decision.reasoning],
        amount=decision.amount,
        currency=decision.currency,
    )


def _failure_decision(state: PipelineState, reason: str) -> ApprovalDecision:
    invoice = state.invoice
    return ApprovalDecision(
        decision="manual_review",
        reasoning=ApprovalReasoning(
            rules_applied=["A failed agent step does not continue to payment."],
            evidence=[reason],
            risks_considered=[reason],
            conclusion=reason,
        ),
        amount=None if invoice is None else invoice.total,
        currency=None if invoice is None else invoice.currency,
    )


def _decision_from_validation(state: PipelineState, outcome: str) -> tuple[str, ApprovalDecision]:
    findings = [] if state.validation is None else list(state.validation.findings)
    invoice = state.invoice
    fired = []
    for finding in findings:
        text = RULE_TEXT.get(finding.code)
        if text and text not in fired:
            fired.append(text)
    if not fired:
        fired = ["The invoice did not pass the deterministic rules."]
    evidence = [finding.message for finding in findings] or ["No finding text was recorded."]
    if invoice is not None and invoice.total is not None:
        evidence = [f"total {show_money(invoice.total)} {invoice.currency}"] + evidence
    if findings:
        reason = " ".join(finding.message for finding in findings)
    else:
        reason = "The invoice was not approved."
    decision = ApprovalDecision(
        decision=outcome,
        reasoning=ApprovalReasoning(
            rules_applied=fired,
            evidence=evidence,
            risks_considered=[finding.message for finding in findings] or ["No extra risk was recorded."],
            conclusion=reason,
        ),
        amount=None if invoice is None else invoice.total,
        currency=None if invoice is None else invoice.currency,
    )
    return reason, decision
