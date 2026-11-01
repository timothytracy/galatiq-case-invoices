"""Validation. Python rules only. A blocking outcome skips approval."""

from db import load_stock
from models import Finding, PipelineState, ValidationReport
from pipeline.stage_log import _stopped, stage_log
from decision.outcome import finding_codes
from decision.wording import assess, resolve_as_of, today_line


def validate(state: PipelineState, db_path, as_of=None) -> dict:
    as_of = resolve_as_of(as_of)
    invoice = state.invoice
    if invoice is None:
        reason = "No invoice was extracted."
        return _stopped(
            state,
            "reject",
            reason,
            "validation",
            [],
            agent_error=state.agent_error,
            finding=Finding(code="MISSING_FIELD", message=reason, severity="blocking"),
        )
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
        "reason_code": assessment.reason_code,
        "action": assessment.action,
        "currency": assessment.currency,
        "as_of": as_of.isoformat(),
        "date_context": today_line(as_of),
        "findings": [finding.model_dump(mode="json") for finding in report.findings],
        "codes": finding_codes(report.findings),
    }
    return {
        "invoice": invoice,
        "validation": report,
        "findings": list(assessment.findings),
        "outcome": outcome,
        "reason_code": assessment.reason_code,
        "action": assessment.action,
        "logs": state.logs + [stage_log(state.filename, "validation", result)],
    }
