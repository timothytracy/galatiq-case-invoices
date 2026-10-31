"""Critique may only make an approval stricter. The graph caps the loop at one round."""

from llm_client import AgentStepFailed, call_with_retries
from models import ApprovalDecision, CritiqueDraft, PipelineState
from pipeline.stage_log import _stopped, stage_log


def critique(state: PipelineState, llm, clock, rng, max_attempts: int) -> dict:
    decision = state.decision
    if decision is None:
        return _stopped(state, "reject", "Critique had no decision.", "critique", [], agent_error=None)
    step_attempts: list[dict] = []
    try:
        draft = call_with_retries(
            llm.request,
            _critique_messages(decision),
            CritiqueDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(state, "manual_review", str(exc), "critique", step_attempts, agent_error=str(exc))

    stricter = bool(draft.stricter) and decision.decision == "approve"
    result = {
        "concerns": list(draft.concerns),
        "stricter": stricter,
        "round": state.critique_rounds + 1,
        "attempts": step_attempts,
    }
    return {
        "critique_rounds": state.critique_rounds + 1,
        "critique_concerns": list(draft.concerns),
        "critique_stricter": stricter,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "critique", result)],
    }


def _critique_messages(decision: ApprovalDecision) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "TASK critique\n"
                "Review this approval. Return JSON with keys concerns (list of strings) and "
                "stricter (boolean). You may only make the decision stricter. "
                "You cannot approve a failure and you cannot clear a non-USD hold. Return JSON only."
            ),
        },
        {"role": "user", "content": decision.model_dump_json()},
    ]
