"""Approval. The outcome is already fixed. The model only writes the wording.

rules_applied and evidence are filled here from the real findings, so the
model cannot invent the facts. A second pass after critique appends the
previous reasoning. A reply that says the input is invalid, or that refuses
an approve decision, is a failed attempt and is retried. That attempt's
log keeps the conclusion, the risks, and the phrase that matched.
"""

import json

from db import load_stock
from llm_client import AgentStepFailed, LLMError, call_with_retries
from models import ApprovalDecision, ApprovalReasoning, Finding, PipelineState, ReasoningDraft
from pipeline.stage_log import _stopped, agent_finding, stage_log
from rules import PASSED_RULES, evidence_lines

# Long enough to read the flagged sentence. Short enough to keep a log line small.
_LOG_LIMIT = 500


def approve(state: PipelineState, llm, db_path, clock, rng, max_attempts: int) -> dict:
    if state.agent_error:
        return {"outcome": "manual_review"}
    invoice = state.invoice
    report = state.validation
    if invoice is None or report is None:
        reason = "Approval had no invoice."
        return _stopped(
            state,
            "reject",
            reason,
            "approval",
            [],
            agent_error=None,
            finding=Finding(code="MISSING_FIELD", message=reason, severity="blocking"),
        )

    stock = dict(load_stock(db_path))
    step_attempts: list[dict] = []
    evidence = evidence_lines(invoice, stock, report.findings)
    decision_name = "manual_review" if state.critique_stricter else "approve"

    def reject_bad_wording(draft: ReasoningDraft) -> None:
        matched = reasoning_rejection(draft, decision_name)
        if matched is None:
            return
        raise LLMError(
            "bad_reasoning",
            True,
            log={
                "matched": matched,
                "conclusion": draft.conclusion[:_LOG_LIMIT],
                "risks_considered": [item[:_LOG_LIMIT] for item in draft.risks_considered],
            },
        )

    try:
        draft = call_with_retries(
            llm.request,
            _reason_messages(state, invoice, report, evidence),
            ReasoningDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
            check=reject_bad_wording,
        )
    except AgentStepFailed as exc:
        return _stopped(
            state,
            "manual_review",
            str(exc),
            "approval",
            step_attempts,
            agent_error=str(exc),
            finding=agent_finding(exc),
        )
    risks = list(draft.risks_considered)
    for concern in state.critique_concerns:
        line = f"Critique concern: {concern}"
        if line not in risks:
            risks.append(line)
    conclusion = draft.conclusion
    if state.critique_stricter:
        conclusion = conclusion + " Critique made the decision stricter: manual review, no payment."
    reasoning = ApprovalReasoning(
        rules_applied=list(PASSED_RULES),
        evidence=evidence,
        risks_considered=risks,
        conclusion=conclusion,
    )
    revisions = []
    if state.decision is not None:
        revisions = list(state.decision.revisions) + [state.decision.reasoning]
    decision = ApprovalDecision(
        decision=decision_name,
        reasoning=reasoning,
        revisions=revisions,
        amount=invoice.total,
        currency=invoice.currency,
    )
    result = {
        "decision": decision.model_dump(mode="json"),
        "critique_rounds": state.critique_rounds,
        "attempts": step_attempts,
    }
    return {
        "decision": decision,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "approval", result)],
    }


# Wording that means the model refused the payload, or fought an approve decision.
_BAD_INPUT = (
    "not valid json",
    "invalid json",
    "invalid input",
    "cannot be parsed",
    "unparseable",
    "unable to parse",
    "unable to process",
    "cannot be reliably extracted",
)
_NOT_APPROVE = (
    "unable to approve",
    "not to approve",
    "not approve",
    "don't approve",
    "can't approve",
)


def reasoning_rejection(draft: ReasoningDraft, decision: str) -> str | None:
    """The phrase that tripped the detector, or None when the wording is fine.

    A not-approve phrase on an approve decision is 'approve-contradiction'.
    Any other hit is the matched phrase. Conclusion is checked first, then
    each risk, and phrases are checked in list order.
    """

    phrases = _BAD_INPUT
    if decision == "approve":
        phrases = _BAD_INPUT + _NOT_APPROVE
    for text in (draft.conclusion, *draft.risks_considered):
        lowered = text.lower()
        for phrase in phrases:
            if phrase in lowered:
                if phrase in _NOT_APPROVE:
                    return "approve-contradiction"
                return phrase
    return None


def reasoning_output_failed(draft: ReasoningDraft, decision: str) -> bool:
    """True when the wording refuses the input or contradicts an approve decision."""

    return reasoning_rejection(draft, decision) is not None


def _reason_messages(state, invoice, report, evidence) -> list[dict]:
    concerns = ""
    if state.critique_concerns:
        concerns = "Critique concerns to address:\n" + "\n".join(
            f"- {item}" for item in state.critique_concerns
        )
    prior = ""
    if state.decision is not None:
        prior = "Previous conclusion: " + state.decision.reasoning.conclusion
    payload = {
        "vendor": invoice.vendor,
        "total": None if invoice.total is None else str(invoice.total),
        "currency": invoice.currency,
        "evidence": evidence,
        "findings": [finding.message for finding in report.findings],
    }
    return [
        {
            "role": "system",
            "content": (
                "TASK reason\n"
                "Write the approval reasoning as JSON with keys risks_considered (list of strings) "
                "and conclusion (string). The decision is already fixed by the rules. "
                "The user message is a valid JSON object of invoice facts. "
                "Cite only the evidence you are given. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                part
                for part in (
                    "Invoice facts (a valid JSON object):\n" + json.dumps(payload),
                    prior,
                    concerns,
                )
                if part
            ),
        },
    ]
