"""Audit. Compare the extraction with the raw text, then refuse to loosen it.

The model returns PASS, SEND_BACK, or ESCALATE. Python recomputes the hard
rules and keeps whichever result is stricter.
"""

import json
import re
from decimal import Decimal

from db import load_stock
from llm_client import AgentStepFailed, call_with_retries
from models import ApprovalDecision, AuditDraft, PipelineState
from pipeline.decisions import _tighten
from pipeline.stage_log import _stopped, stage_log
from rules import HARD_REJECT, MAX_SEND_BACKS, assess, show_money


def audit(state: PipelineState, llm, db_path, clock, rng, max_attempts: int) -> dict:
    invoice = state.invoice
    decision = state.decision
    if invoice is None or decision is None:
        return _stopped(state, "manual_review", "Audit had no decision.", "audit", [], agent_error=state.agent_error)

    step_attempts: list[dict] = []
    try:
        draft = call_with_retries(
            llm.request,
            _audit_messages(state, invoice, decision),
            AuditDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(state, "manual_review", str(exc), "audit", step_attempts, agent_error=str(exc))

    stock = dict(load_stock(db_path))
    fresh = assess(invoice, state.raw_text, stock)
    guarded = _guard_audit(state, decision, fresh.findings, draft.verdict, list(draft.feedback))
    new_decision = decision
    if guarded["verdict"] != "SEND_BACK" and guarded["outcome"] not in (None, decision.decision):
        new_decision = _tighten(decision, guarded["outcome"], guarded["conclusion"], guarded["evidence"])

    result = {
        "model_verdict": draft.verdict,
        "verdict": guarded["verdict"],
        "outcome": guarded["outcome"],
        "feedback": guarded["feedback"],
        "notes": draft.notes,
        "attempts": step_attempts,
        "decision": new_decision.model_dump(mode="json"),
    }
    update = {
        "audit_verdict": guarded["verdict"],
        "decision": new_decision,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "audit", result)],
    }
    if guarded["verdict"] == "SEND_BACK":
        update["outcome"] = None
        update["feedback"] = guarded["feedback"]
        update["send_backs"] = state.send_backs + 1
        update["decision"] = None
        update["critique_rounds"] = 0
        update["critique_concerns"] = []
        update["critique_stricter"] = False
    else:
        update["outcome"] = guarded["outcome"]
    return update


def _audit_messages(state, invoice, decision) -> list[dict]:
    findings = []
    if state.validation is not None:
        findings = [finding.model_dump(mode="json") for finding in state.validation.findings]
    payload = {
        "extracted": invoice.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "findings": findings,
        "critique_concerns": list(state.critique_concerns),
        "raw_text": state.raw_text,
    }
    return [
        {
            "role": "system",
            "content": (
                "TASK audit\n"
                "Compare the extracted fields with the raw text. Check the decision against the "
                "findings. Re-check the line math and any fraud signals. Check that critique "
                "concerns were addressed. Return JSON with verdict (PASS, SEND_BACK, or ESCALATE), "
                "feedback (list of field notes, required for SEND_BACK), and notes (string). "
                "You can only make the outcome stricter. Never turn a hard failure or a non-USD "
                "invoice into a payment. Return JSON only."
            ),
        },
        {"role": "user", "content": json.dumps(payload)},
    ]


def concerns_addressed(concerns: list[str], decision: ApprovalDecision | None) -> bool:
    if not concerns:
        return True
    if decision is None:
        return False
    blob = " ".join(
        [
            decision.reasoning.conclusion,
            " ".join(decision.reasoning.risks_considered),
            " ".join(decision.reasoning.evidence),
            " ".join(item.conclusion for item in decision.revisions),
        ]
    ).lower()
    return all(concern.lower() in blob for concern in concerns if concern)


def amount_in_text(amount: Decimal, raw: str) -> bool:
    quantized = show_money(amount)
    if quantized in raw:
        return True
    with_commas = f"{Decimal(quantized):,.2f}"
    if with_commas in raw:
        return True
    whole = quantized.split(".")[0]
    if Decimal(quantized) == Decimal(whole):
        return _whole_number_in_text(whole, raw)
    return False


def _whole_number_in_text(whole: str, raw: str) -> bool:
    """A whole-dollar total can be printed without the trailing .00."""

    return re.search(rf"(?<![0-9]){re.escape(whole)}(?![0-9])", raw) is not None


def _guard_audit(state, decision, findings, model_verdict, feedback) -> dict:
    """Apply the audit checks. The result is never looser than the rules or the model."""

    hard = [finding for finding in findings if finding.code in HARD_REJECT]
    non_usd = [finding for finding in findings if finding.code == "non_usd"]
    fraud = [finding for finding in findings if finding.code == "fraud_signal"]
    text_notes = []
    if state.invoice is not None and state.invoice.total is not None:
        if not amount_in_text(state.invoice.total, state.raw_text or ""):
            text_notes.append(
                f"total {show_money(state.invoice.total)} was not found in the raw text"
            )

    if hard or decision.decision == "reject":
        # The model cannot loosen a hard failure. PASS means "do not send this
        # back"; the outcome stays reject, so the router logs it and does not pay.
        return {
            "verdict": "PASS",
            "outcome": "reject",
            "feedback": [],
            "conclusion": " ".join(finding.message for finding in hard) or decision.reasoning.conclusion,
            "evidence": [finding.message for finding in hard],
        }
    if non_usd:
        return {
            "verdict": "ESCALATE",
            "outcome": "manual_review",
            "feedback": [],
            "conclusion": non_usd[0].message,
            "evidence": [finding.message for finding in non_usd],
        }
    if fraud and decision.decision == "approve":
        return {
            "verdict": "ESCALATE",
            "outcome": "manual_review",
            "feedback": [],
            "conclusion": " ".join(finding.message for finding in fraud),
            "evidence": [finding.message for finding in fraud],
        }
    if not concerns_addressed(state.critique_concerns, decision):
        return {
            "verdict": "ESCALATE",
            "outcome": "manual_review",
            "feedback": [],
            "conclusion": "Critique concerns were not addressed.",
            "evidence": [f"Unaddressed critique concern: {item}" for item in state.critique_concerns],
        }

    verdict = model_verdict
    if text_notes and verdict == "PASS":
        verdict = "SEND_BACK"
        feedback = text_notes + feedback
    if verdict == "SEND_BACK":
        if state.send_backs >= MAX_SEND_BACKS:
            return {
                "verdict": "ESCALATE",
                "outcome": "manual_review",
                "feedback": feedback,
                "conclusion": "Audit send-back cap reached. Manual review, no payment.",
                "evidence": feedback or ["send-back cap reached"],
            }
        used = feedback or ["re-check the extracted fields against the raw text"]
        return {
            "verdict": "SEND_BACK",
            "outcome": None,
            "feedback": used,
            "conclusion": decision.reasoning.conclusion,
            "evidence": [],
        }
    if verdict == "ESCALATE" or decision.decision == "manual_review":
        outcome = "manual_review"
        return {
            "verdict": "ESCALATE",
            "outcome": outcome,
            "feedback": [],
            "conclusion": decision.reasoning.conclusion,
            "evidence": [],
        }
    # PASS. An approve stays approve. Anything else stays as strict as it was.
    if decision.decision == "approve":
        return {
            "verdict": "PASS",
            "outcome": "approve",
            "feedback": [],
            "conclusion": decision.reasoning.conclusion,
            "evidence": [],
        }
    return {
        "verdict": "PASS",
        "outcome": decision.decision,
        "feedback": [],
        "conclusion": decision.reasoning.conclusion,
        "evidence": [],
    }
