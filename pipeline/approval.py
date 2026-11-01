"""Approval wording. The outcome is already fixed. Facts are filled in Python."""

import json
import re

from db import load_stock
from llm_client import AgentStepFailed, LLMError, call_with_retries
from models import ApprovalDecision, ApprovalReasoning, Finding, PipelineState, ReasoningDraft
from pipeline.stage_log import _stopped, agent_finding, stage_log
from decision.findings import evidence_lines
from decision.policy import POLICY
from decision.wording import PASSED_RULES, approval_conclusion, resolve_as_of, today_line

# Long enough to read the flagged sentence. Short enough to keep a log line small.
_LOG_LIMIT = 500


def approve(state: PipelineState, llm, db_path, clock, rng, max_attempts: int, as_of=None) -> dict:
    as_of = resolve_as_of(as_of)
    if state.agent_error:
        return {"outcome": "hold"}
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
    decision_name = "hold" if state.critique_stricter else "approve"

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
            _reason_messages(state, invoice, evidence, as_of),
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
            "hold",
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
    # The note above is not a fact. The sentence is the checks and the finding text.
    conclusion = approval_conclusion(
        invoice,
        list(report.findings),
        POLICY,
        passed_critique=state.critique_rounds > 0 and not state.critique_stricter,
    )
    if state.critique_stricter:
        conclusion = conclusion + " Critique made the decision stricter: hold, no payment."
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


# A payload refusal, or a fight with an approve decision. "approved" alone does not match.
_BAD_INPUT = (
    "not valid json", "invalid json", "invalid input", "cannot be parsed",
    "unparseable", "unable to parse", "unable to process", "cannot be reliably extracted",
)
_NOT_APPROVE = (
    "unable to approve",
    "not to approve",
    "not approve",
    "don't approve",
    "can't approve",
)
# "should/cannot/must not be approved" contains "not" before "approv".
_APPROVE_REFUSAL = re.compile(r"(?i)not\s+(?:be\s+)?approv")


def reasoning_rejection(draft: ReasoningDraft, decision: str) -> str | None:
    """Matched phrase, or 'approve-contradiction'. None when the note is fine.

    The approval sentence is written in code. This still checks the model note,
    and a conclusion field if the model sent one, for a refusal or a fight with approve.
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
        if decision == "approve" and _APPROVE_REFUSAL.search(lowered):
            return "approve-contradiction"
    return None


def reasoning_output_failed(draft: ReasoningDraft, decision: str) -> bool:
    """True when the wording refuses the input or contradicts an approve decision."""

    return reasoning_rejection(draft, decision) is not None


def _reason_messages(state, invoice, evidence, as_of=None) -> list[dict]:
    as_of = resolve_as_of(as_of)
    concerns = ""
    if state.critique_concerns:
        concerns = "Critique concerns to address:\n" + "\n".join(
            f"- {item}" for item in state.critique_concerns
        )
    payload = {
        "vendor": invoice.vendor,
        "total": None if invoice.total is None else str(invoice.total),
        "currency": invoice.currency,
        "evidence": evidence,
    }
    return [
        {
            "role": "system",
            "content": (
                "TASK reason\n"
                f"{today_line(as_of)}\n"
                "Write a short note as JSON with key risks_considered (list of strings). "
                "Do not describe the input format. "
                "The decision is already fixed by the rules. "
                "The user message is a valid JSON object of invoice facts. "
                "Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                part
                for part in (
                    "Invoice facts (a valid JSON object):\n" + json.dumps(payload),
                    concerns,
                )
                if part
            ),
        },
    ]
