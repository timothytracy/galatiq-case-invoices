"""Critique may only make an approval stricter. The graph caps the loop at one round."""

from llm_client import AgentStepFailed, call_with_retries
from models import ApprovalDecision, CritiqueDraft, Finding, PipelineState
from pipeline.stage_log import _stopped, agent_finding, stage_log
from rules import resolve_as_of, threshold_only_concern, today_line


def critique(state: PipelineState, llm, clock, rng, max_attempts: int, as_of=None) -> dict:
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
    step_attempts: list[dict] = []
    try:
        draft = call_with_retries(
            llm.request,
            _critique_messages(decision, as_of),
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

    concerns = [item for item in draft.concerns if not threshold_only_concern(item)]
    # Being over $10,000 is not a risk. A stricter flag that only says so is ignored.
    stricter = bool(draft.stricter) and decision.decision == "approve" and bool(concerns)
    result = {
        "concerns": concerns,
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


def _critique_messages(decision: ApprovalDecision, as_of) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "TASK critique\n"
                f"{today_line(as_of)}\n"
                "Review this approval. Return JSON with keys concerns (list of strings) and "
                "stricter (boolean). You may only make the decision stricter. "
                "Being over the scrutiny threshold is not a risk. Do not set stricter only "
                "because the total is over it. "
                "You cannot approve a failure and you cannot clear a non-USD hold. Return JSON only."
            ),
        },
        {"role": "user", "content": decision.model_dump_json()},
    ]
