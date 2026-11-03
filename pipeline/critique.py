"""Critique may only make an approval stricter. The graph caps the loop at one round."""

import json
import re

from datetime import date
from pathlib import Path

from db import load_stock
from interfaces import LLM, Clock, Rng
from llm_client import CRITIQUE_MODEL, AgentStepFailed, call_with_retries, named_requester
from models import ApprovalDecision, CritiqueDraft, Finding, PipelineState
from pipeline.fact_guard import (
    CRITIQUE_CLAIMS,
    QUOTE_RULE,
    VERIFIED_FACTS,
    concern_text,
    partition_concerns,
    quote_gate,
    verified_payload,
)
from pipeline.stage_log import agent_stop, stage_log, stop_with_finding
from decision.wording import resolve_as_of, threshold_only_concern, today_line


def critique(
    state: PipelineState,
    llm: LLM,
    db_path: str | Path,
    clock: Clock,
    rng: Rng,
    max_attempts: int,
    as_of: date | None = None,
) -> dict:
    as_of = resolve_as_of(as_of)
    decision = state.decision
    if decision is None:
        reason = "Critique had no decision."
        return stop_with_finding(state, "critique", reason, "MISSING_FIELD")
    stock: dict[str, int] = {}
    facts = None
    if state.invoice is not None:
        stock = dict(load_stock(db_path))
        facts = verified_payload(state.invoice, stock, as_of)

    step_attempts: list[dict] = []
    try:
        draft = call_with_retries(
            named_requester(llm, CRITIQUE_MODEL),
            _critique_messages(decision, as_of, facts, state.raw_text or ""),
            CritiqueDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return agent_stop(state, "critique", step_attempts, exc)

    # Being over $10,000 is not a risk. A stricter flag that only says so is ignored.
    checkable = [item for item in draft.concerns if not threshold_only_concern(concern_text(item))]
    dropped: list[dict] = []
    if state.invoice is None:
        kept = checkable
    else:
        kept, dropped = partition_concerns(checkable, state.invoice, stock, as_of, "critique")
    kept, unquoted = quote_gate(
        kept, state.invoice, stock, as_of, state.raw_text or "", draft.innocent_reading
    )
    dropped = dropped + unquoted
    concerns = [concern_text(item) for item in kept if concern_text(item)]
    stricter = bool(draft.stricter) and decision.decision == "approve" and bool(concerns)
    result = {
        "concerns": concerns,
        "quotes": [item.quote for item in kept if item.quote],
        "innocent_reading": draft.innocent_reading,
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
                message=hold_message(kept),
                severity="warning",
            )
        ]
    return update


def hold_message(kept) -> str:
    quoted = [item for item in kept if item.quote.strip()]
    if not quoted:
        return "Critique raised a concern other than the dollar threshold."
    first = quoted[0]
    quote = re.sub(r"\s+", " ", first.quote).strip()
    return f'Critique: "{quote}". {concern_text(first)}'.strip()


def _critique_messages(decision: ApprovalDecision, as_of, facts, invoice_text: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "TASK critique\n"
                f"{today_line(as_of)}\n"
                "Review this approval. Return JSON with keys concerns (list of objects), "
                "stricter (boolean), and innocent_reading (string). You may only make the "
                "decision stricter. "
                "Being over the scrutiny threshold is not a risk. Do not set stricter only "
                "because the total is over it. "
                "You cannot approve a failure and you cannot clear a non-USD hold. "
                "invoice_text is the page as read. Compare it with the decision and "
                "verified_by_code, and raise a concern for anything the extraction missed or misread. "
                "Also compare what is billed with what the page itself says was ordered, "
                "delivered, priced, or returned; the numbers can all reconcile while the "
                "wording says the bill should not be paid as printed. Start from the most "
                "plausible innocent reading, and hold only when the page's wording makes it "
                "unlikely. "
                f"{VERIFIED_FACTS} {CRITIQUE_CLAIMS} Each concern also has quote. {QUOTE_RULE} "
                "Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "decision": decision.model_dump(mode="json"),
                    "verified_by_code": facts,
                    "invoice_text": invoice_text,
                }
            ),
        },
    ]
