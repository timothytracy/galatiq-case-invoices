"""One JSON object per stage, printed and returned so the saved log matches stdout."""

import json
from datetime import datetime, timezone

from models import Finding, StageRecord
from rules import finding_codes, outcome_from_findings


def stage_log(filename: str, stage: str, result: dict) -> dict:
    record = StageRecord(
        stage=stage,
        invoice_file=filename,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        result=result,
    )
    payload = record.model_dump(mode="json")
    print(json.dumps(payload), flush=True)
    return payload


def agent_finding(exc) -> Finding:
    """The finding for a model step that will not be retried again."""

    code = "BAD_REASONING" if getattr(exc, "error_type", "") == "bad_reasoning" else "AGENT_FAILED"
    return Finding(code=code, message=str(exc), severity="warning")


def _stopped(state, outcome, reason, stage, step_attempts, agent_error, finding=None) -> dict:
    findings = list(state.findings)
    if finding is not None:
        findings.append(finding)
        outcome = outcome_from_findings(findings)
    result = {
        "outcome": outcome,
        "reason": reason,
        "attempts": step_attempts,
        "codes": finding_codes(findings),
    }
    return {
        "outcome": outcome,
        "findings": findings,
        "agent_error": agent_error,
        "attempts": state.attempts + step_attempts,
        "logs": state.logs + [stage_log(state.filename, stage, result)],
    }
