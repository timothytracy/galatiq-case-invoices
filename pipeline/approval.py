"""Approval. The outcome is already fixed. The model only writes the wording.

rules_applied and evidence are filled here from the real findings, so the
model cannot invent the facts. A second pass after critique appends the
previous reasoning.
"""

import json

from db import load_stock
from llm_client import AgentStepFailed, call_with_retries
from models import ApprovalDecision, ApprovalReasoning, PipelineState, ReasoningDraft
from pipeline.stage_log import _stopped, stage_log
from rules import PASSED_RULES, evidence_lines


def approve(state: PipelineState, llm, db_path, clock, rng, max_attempts: int) -> dict:
    if state.agent_error:
        return {"outcome": "manual_review"}
    invoice = state.invoice
    report = state.validation
    if invoice is None or report is None:
        return _stopped(state, "reject", "Approval had no invoice.", "approval", [], agent_error=None)

    stock = dict(load_stock(db_path))
    step_attempts: list[dict] = []
    evidence = evidence_lines(invoice, stock, report.findings)
    try:
        draft = call_with_retries(
            llm.request,
            _reason_messages(state, invoice, report, evidence),
            ReasoningDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(state, "manual_review", str(exc), "approval", step_attempts, agent_error=str(exc))

    decision_name = "manual_review" if state.critique_stricter else "approve"
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
                "Cite only the evidence you are given. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                part
                for part in (json.dumps(payload), prior, concerns)
                if part
            ),
        },
    ]
