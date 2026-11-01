"""One JSON object per stage, printed and returned."""

import json
from datetime import datetime, timezone

from models import Finding, StageRecord
from decision.outcome import finding_codes
from decision.wording import explain


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

    code = "BAD_REASONING" if getattr(exc, "error_type", "") == "bad_reasoning" else "SYSTEM_ERROR"
    return Finding(code=code, message=str(exc), severity="warning")


def _stopped(state, outcome, reason, stage, step_attempts, agent_error, finding=None) -> dict:
    """Stop the graph. The finding list decides the outcome; outcome is the caller's hint."""

    findings = list(state.findings)
    if finding is not None:
        findings.append(finding)
    decided, code, explained, action = explain(findings, state.invoice)
    if not findings:
        decided = outcome
        explained = reason
    result = {
        "outcome": decided,
        "reason": explained,
        "reason_code": code,
        "action": action,
        "attempts": step_attempts,
        "codes": finding_codes(findings),
    }
    calls = []
    for row in step_attempts:
        if isinstance(row, dict) and row.get("tool_calls"):
            calls.extend(row["tool_calls"])
    if calls:
        result["tool_calls"] = calls
    return {
        "outcome": decided,
        "reason_code": code,
        "action": action,
        "findings": findings,
        "agent_error": agent_error,
        "attempts": state.attempts + step_attempts,
        "logs": state.logs + [stage_log(state.filename, stage, result)],
    }
