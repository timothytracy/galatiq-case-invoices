"""Validation. Python rules only. A blocking outcome skips approval."""

from db import load_stock
from models import PipelineState, ValidationReport
from pipeline.stage_log import _stopped, stage_log
from rules import assess


def validate(state: PipelineState, db_path) -> dict:
    invoice = state.invoice
    if invoice is None:
        return _stopped(state, "reject", "No invoice was extracted.", "validation", [], agent_error=state.agent_error)
    stock = dict(load_stock(db_path))
    assessment = assess(invoice, state.raw_text, stock)
    if assessment.currency != invoice.currency:
        invoice = invoice.model_copy(update={"currency": assessment.currency})
    report = ValidationReport(
        passed=assessment.outcome == "approve",
        findings=assessment.findings,
    )
    outcome = None if assessment.outcome == "approve" else assessment.outcome
    result = {
        "passed": report.passed,
        "outcome": assessment.outcome,
        "reason": assessment.reason,
        "currency": assessment.currency,
        "findings": [finding.model_dump(mode="json") for finding in report.findings],
    }
    return {
        "invoice": invoice,
        "validation": report,
        "outcome": outcome,
        "logs": state.logs + [stage_log(state.filename, "validation", result)],
    }
