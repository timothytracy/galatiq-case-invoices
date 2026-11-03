"""LangGraph wiring. Fraud review runs once after validation. Critique returns to approval.
Audit can return to ingestion."""

import os
import random
import tempfile
from datetime import date
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from db import ensure_inventory_db
from llm_client import SleepClock, read_max_attempts
from models import PipelineState
from pipeline import approve, audit, critique, fraud_review, ingest, payment, reject_log, validate
from decision.policy import CRITIQUE_CAP, POLICY
from decision.wording import resolve_as_of

RECURSION_LIMIT = 80


def build_graph(
    llm,
    db_path,
    *,
    clock=None,
    rng=None,
    max_attempts=None,
    as_of: date | None = None,
    ledger_path=None,
):
    clock = clock or SleepClock()
    rng = rng or random.Random()
    attempts = read_max_attempts() if max_attempts is None else max_attempts
    as_of = resolve_as_of(as_of)
    db_path = str(db_path)

    graph = StateGraph(PipelineState)
    graph.add_node("ingestion", lambda state: ingest(state, llm, clock, rng, attempts, as_of))
    graph.add_node(
        "validation",
        lambda state: validate(state, db_path, as_of, ledger_path),
    )
    graph.add_node(
        "fraud_review",
        lambda state: fraud_review(state, llm, clock, rng, attempts, as_of),
    )
    graph.add_node(
        "approval",
        lambda state: approve(state, llm, db_path, clock, rng, attempts, as_of),
    )
    graph.add_node(
        "critique",
        lambda state: critique(state, llm, db_path, clock, rng, attempts, as_of),
    )
    graph.add_node(
        "audit",
        lambda state: audit(
            state, llm, db_path, clock, rng, attempts, as_of, ledger_path=ledger_path
        ),
    )
    graph.add_node("payment", lambda state: payment(state, ledger_path))
    graph.add_node("reject_log", lambda state: reject_log(state, ledger_path))

    graph.add_edge(START, "ingestion")
    graph.add_conditional_edges(
        "ingestion",
        route_after_ingestion,
        {"validation": "validation", "reject_log": "reject_log"},
    )
    graph.add_conditional_edges(
        "validation",
        route_after_validation,
        {"fraud_review": "fraud_review", "approval": "approval", "reject_log": "reject_log"},
    )
    graph.add_conditional_edges(
        "fraud_review",
        route_after_fraud_review,
        {"approval": "approval", "reject_log": "reject_log"},
    )
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"critique": "critique", "audit": "audit", "reject_log": "reject_log"},
    )
    graph.add_conditional_edges(
        "critique",
        route_after_critique,
        {"approval": "approval", "reject_log": "reject_log"},
    )
    graph.add_conditional_edges(
        "audit",
        route_after_audit,
        {"ingestion": "ingestion", "payment": "payment", "reject_log": "reject_log"},
    )
    graph.add_edge("payment", END)
    graph.add_edge("reject_log", END)
    return graph.compile()


def route_after_ingestion(state: PipelineState) -> str:
    if state.agent_error or state.invoice is None:
        return "reject_log"
    return "validation"


def route_after_validation(state: PipelineState) -> str:
    if state.agent_error or state.outcome in ("reject", "hold"):
        return "reject_log"
    if state.fraud_reviewed:
        return "approval"
    return "fraud_review"


def route_after_fraud_review(state: PipelineState) -> str:
    if state.agent_error or state.outcome in ("reject", "hold"):
        return "reject_log"
    return "approval"


def route_after_approval(state: PipelineState) -> str:
    if state.agent_error or state.invoice is None or state.decision is None:
        return "reject_log"
    if state.invoice.currency in POLICY.allowed_currencies and state.critique_rounds < CRITIQUE_CAP:
        return "critique"
    return "audit"


def route_after_critique(state: PipelineState) -> str:
    if state.agent_error:
        return "reject_log"
    return "approval"


def route_after_audit(state: PipelineState) -> str:
    if state.agent_error:
        return "reject_log"
    if state.audit_verdict == "SEND_BACK" and state.outcome is None:
        return "ingestion"
    if state.outcome == "approve":
        return "payment"
    return "reject_log"


def run_invoice(
    path,
    db_path,
    llm,
    *,
    clock=None,
    rng=None,
    max_attempts=None,
    as_of: date | None = None,
    ledger_path=None,
) -> dict:
    """One invoice, one new graph. The state is not kept for the next file.

    ledger_path=None opens a fresh temporary payments ledger and deletes it
    when this call returns. Pass a path to remember payments for the next file.
    as_of is the date the agents are told is today. Tests inject it.
    A record or replay client carries that same date on as_of. Using it
    here means a replay on a later day still builds the recorded prompts.
    A live client leaves as_of unset, so the wall clock is still used.
    The inventory file is created from the seed when it is missing.
    An existing inventory file is not opened or changed here.
    """

    ensure_inventory_db(db_path)
    if as_of is None:
        as_of = getattr(llm, "as_of", None)
    temporary = ledger_path is None
    if temporary:
        handle, ledger_path = tempfile.mkstemp(prefix="payments-", suffix=".db")
        os.close(handle)
    try:
        app = build_graph(
            llm,
            db_path,
            clock=clock,
            rng=rng,
            max_attempts=max_attempts,
            as_of=as_of,
            ledger_path=ledger_path,
        )
        state = PipelineState(invoice_path=str(path), filename=Path(path).name)
        return app.invoke(state, config={"recursion_limit": RECURSION_LIMIT})
    finally:
        if temporary:
            Path(ledger_path).unlink(missing_ok=True)


def mermaid_text() -> str:
    app = build_graph(llm=None, db_path="inventory.db")
    return app.get_graph().draw_mermaid()
