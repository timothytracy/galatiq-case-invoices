"""LangGraph wiring. Critique returns to approval. Audit can return to ingestion."""

import random
from datetime import date
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from llm_client import SleepClock, read_max_attempts
from models import PipelineState
from pipeline import approve, audit, critique, ingest, payment, reject_log, validate
from decision.policy import APPROVAL_THRESHOLD_USD, CRITIQUE_CAP, POLICY
from decision.wording import resolve_as_of

RECURSION_LIMIT = 80


def build_graph(llm, db_path, *, clock=None, rng=None, max_attempts=None, as_of: date | None = None):
    clock = clock or SleepClock()
    rng = rng or random.Random()
    attempts = read_max_attempts() if max_attempts is None else max_attempts
    as_of = resolve_as_of(as_of)
    db_path = str(db_path)

    graph = StateGraph(PipelineState)
    graph.add_node("ingestion", lambda state: ingest(state, llm, clock, rng, attempts, as_of))
    graph.add_node("validation", lambda state: validate(state, db_path, as_of))
    graph.add_node(
        "approval",
        lambda state: approve(state, llm, db_path, clock, rng, attempts, as_of),
    )
    graph.add_node(
        "critique",
        lambda state: critique(state, llm, db_path, clock, rng, attempts, as_of),
    )
    graph.add_node("audit", lambda state: audit(state, llm, db_path, clock, rng, attempts, as_of))
    graph.add_node("payment", payment)
    graph.add_node("reject_log", reject_log)

    graph.add_edge(START, "ingestion")
    graph.add_conditional_edges(
        "ingestion",
        route_after_ingestion,
        {"validation": "validation", "reject_log": "reject_log"},
    )
    graph.add_conditional_edges(
        "validation",
        route_after_validation,
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
    return "approval"


def route_after_approval(state: PipelineState) -> str:
    if state.agent_error or state.invoice is None or state.decision is None:
        return "reject_log"
    total = state.invoice.total
    over = (
        state.invoice.currency in POLICY.allowed_currencies
        and total is not None
        and total > APPROVAL_THRESHOLD_USD
        and state.critique_rounds < CRITIQUE_CAP
    )
    if over:
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


def run_invoice(path, db_path, llm, *, clock=None, rng=None, max_attempts=None, as_of: date | None = None) -> dict:
    """One invoice, one new graph. Nothing is kept for the next file.

    as_of is the date the agents are told is today. Tests inject it.
    """

    app = build_graph(llm, db_path, clock=clock, rng=rng, max_attempts=max_attempts, as_of=as_of)
    state = PipelineState(invoice_path=str(path), filename=Path(path).name)
    return app.invoke(state, config={"recursion_limit": RECURSION_LIMIT})


def mermaid_text() -> str:
    app = build_graph(llm=None, db_path="inventory.db")
    return app.get_graph().draw_mermaid()