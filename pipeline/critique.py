"""Critique may only make an approval stricter. The graph caps the loop at one round."""

import json

from db import load_stock
from llm_client import AgentStepFailed, call_with_retries
from models import ApprovalDecision, CritiqueDraft, Finding, PipelineState
from pipeline.fact_guard import (
    VERIFIED_SENTENCE,
    concern_text,
    partition_concerns,
    verified_payload,
)
from pipeline.stage_log import _stopped, agent_finding, stage_log
from decision.wording import resolve_as_of, threshold_only_concern, today_line


def critique(state: PipelineState, llm, db_path, clock, rng, max_attempts: int, as_of=None) -> dict:
    as_of = resolve_as_of(as_of)
    decision = state.decision
    if decision is None:
        reason = "Critique had no decision."
        return _stopped(
            state,
            "reject",
            reason,
            "critique",
            [],
            agent_error=None,
            finding=Finding(code="MISSING_FIELD", message=reason, severity="blocking"),
        )
    stock: dict[str, int] = {}
    facts = None
    if state.invoice is not None:
        stock = dict(load_stock(db_path))
        facts = verified_payload(state.invoice, stock, as_of)

    step_attempts: list[dict] = []
    try:
        draft = call_with_retries(
            llm.request,
            _critique_messages(decision, as_of, facts),
            CritiqueDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(
            state,
            "hold",
            str(exc),
            "critique",
            step_attempts,
            agent_error=str(exc),
            finding=agent_finding(exc),
        )

    # Being over $10,000 is not a risk. A stricter flag that only says so is ignored.
    checkable = [item for item in draft.concerns if not threshold_only_concern(concern_text(item))]
    dropped: list[dict] = []
    if state.invoice is None:
        kept = checkable
    else:
        kept, dropped = partition_concerns(checkable, state.invoice, stock, as_of)
    concerns = [concern_text(item) for item in kept]
    stricter = bool(draft.stricter) and decision.decision == "approve" and bool(concerns)
    result = {
        "concerns": concerns,
        "dropped_concerns": dropped,
        "stricter": stricter,
        "round": state.critique_rounds + 1,
        "attempts": step_attempts,
    }
    update = {
        "critique_rounds": state.critique_rounds + 1,
        "critique_concerns": concerns,
        "critique_stricter": stricter,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "critique", result)],
    }
    if stricter:
        update["findings"] = list(state.findings) + [
            Finding(
                code="CRITIQUE_HOLD",
                message="Critique raised a concern other than the dollar threshold.",
                severity="warning",
            )
        ]
    return update


def _critique_messages(decision: ApprovalDecision, as_of, facts) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "TASK critique\n"
                f"{today_line(as_of)}\n"
                "Review this approval. Return JSON with keys concerns (list of objects) and "
                "stricter (boolean). You may only make the decision stricter. "
                "Being over the scrutiny threshold is not a risk. Do not set stricter only "
                "because the total is over it. "
                "You cannot approve a failure and you cannot clear a non-USD hold. "
                f"{VERIFIED_SENTENCE} Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "decision": decision.model_dump(mode="json"),
                    "verified_by_code": facts,
                }
            ),
        },
    ]
