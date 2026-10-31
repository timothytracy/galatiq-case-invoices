"""One JSON object per stage, printed and returned so the saved log matches stdout."""

import json
from datetime import datetime, timezone

from models import StageRecord


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


def _stopped(state, outcome, reason, stage, step_attempts, agent_error) -> dict:
    result = {"outcome": outcome, "reason": reason, "attempts": step_attempts}
    return {
        "outcome": outcome,
        "agent_error": agent_error,
        "attempts": state.attempts + step_attempts,
        "logs": state.logs + [stage_log(state.filename, stage, result)],
    }
