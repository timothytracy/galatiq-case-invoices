"""Run the ordered payments scenario on one shared ledger."""

import json
from pathlib import Path

from graph import run_invoice
from ledger.payments import paid_rows, supplement_rows
from decision.outcome import payment_status, tool_calls_of
from decision.outcome import result_codes

ROOT = Path(__file__).resolve().parents[1]


def run_scenario(
    scenario_path,
    db_path,
    ledger_path,
    llm_for,
    *,
    clock=None,
    rng=None,
    max_attempts=None,
    as_of=None,
) -> dict:
    """Files in file order, one ledger. llm_for(path) builds that file's client."""

    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    steps = []
    for expected in scenario["steps"]:
        path = ROOT / expected["file"]
        result = run_invoice(
            path,
            db_path,
            llm_for(path),
            clock=clock,
            rng=rng,
            max_attempts=max_attempts,
            as_of=as_of,
            ledger_path=ledger_path,
        )
        steps.append(_grade(path.name, result, expected))
    found = [row.source_file for row in paid_rows(ledger_path)]
    found.extend(row.source_file for row in supplement_rows(ledger_path))
    expected_files = list(scenario["ledger_files"])
    for alternative in scenario.get("also_accepted_ledger_files") or []:
        if found == list(alternative):
            expected_files = list(alternative)
    ledger_ok = found == expected_files and len(found) == len(set(found))
    return {
        "ok": ledger_ok and all(step["ok"] for step in steps),
        "steps": steps,
        "ledger_files": found,
        "expected_ledger_files": expected_files,
        "ledger_ok": ledger_ok,
    }


def _grade(name: str, result: dict, expected: dict) -> dict:
    for alternative in expected.get("also_accepted") or []:
        if result.get("outcome") == alternative["outcome"]:
            expected = {**expected, **alternative}
            break
    outcome = result.get("outcome")
    code = result.get("reason_code")
    paid = payment_status(result) == "paid"
    calls = tool_calls_of(result)
    codes = result_codes(result)
    ok = (
        outcome == expected["outcome"]
        and code == expected["reason_code"]
        and paid is expected["paid"]
    )
    for extra in expected.get("also_codes") or []:
        if extra not in codes:
            ok = False
    for missing in expected.get("absent_codes") or []:
        if missing in codes:
            ok = False
    return {
        "file": name,
        "ok": ok,
        "outcome": outcome,
        "expected_outcome": expected["outcome"],
        "reason_code": code,
        "expected_reason": expected["reason_code"],
        "paid": paid,
        "codes": codes,
        "tool_calls": calls,
    }
