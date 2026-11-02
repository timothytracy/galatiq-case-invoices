"""Validation. Python rules only. A blocking outcome skips approval."""

import sqlite3

from db import load_stock
from models import Finding, PipelineState, ValidationReport
from pipeline.stage_log import _stopped, stage_log
from decision.findings import assess
from decision.outcome import finding_codes
from decision.wording import explain, resolve_as_of, today_line
from ledger import arrival_findings


def validate(state: PipelineState, db_path, as_of=None, ledger_path=None) -> dict:
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
    # Ledger is read-only here. A match is a hold, so payment never sees it.
    # No ledger path, or an empty file, adds nothing and leaves the assessment as it was.
    findings = list(assessment.findings)
    if ledger_path is not None:
        try:
            findings.extend(arrival_findings(ledger_path, invoice, state.raw_text))
        except sqlite3.Error as exc:
            findings.append(
                Finding(
                    code="SYSTEM_ERROR",
                    message=f"payments ledger unreadable: {exc}",
                    severity="warning",
                )
            )
    if len(findings) == len(assessment.findings):
        outcome_name = assessment.outcome
        reason = assessment.reason
        reason_code = assessment.reason_code
        action = assessment.action
    else:
        outcome_name, reason_code, reason, action = explain(findings, invoice, stock)
    report = ValidationReport(
        passed=outcome_name == "approve",
        findings=findings,
    )
    outcome = None if outcome_name == "approve" else outcome_name
    result = {
        "passed": report.passed,
        "outcome": outcome_name,
        "reason": reason,
        "reason_code": reason_code,
        "action": action,
        "currency": assessment.currency,
        "as_of": as_of.isoformat(),
        "date_context": today_line(as_of),
        "findings": [finding.model_dump(mode="json") for finding in report.findings],
        "codes": finding_codes(report.findings),
    }
    return {
        "invoice": invoice,
        "validation": report,
        "findings": findings,
        "outcome": outcome,
        "reason_code": reason_code,
        "action": action,
        "logs": state.logs + [stage_log(state.filename, "validation", result)],
    }
