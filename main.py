"""Process one invoice: python3 main.py --invoice_path=data/invoices/invoice_1001.txt"""

import argparse
import json
import sys
from pathlib import Path

from db import create_database
from graph import run_invoice
from llm_client import GrokClient, MissingAPIKey
from decision.outcome import result_codes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process one invoice file.")
    parser.add_argument("--invoice_path", required=True, help="Path to one invoice file")
    args = parser.parse_args(argv)

    path = Path(args.invoice_path)
    if path.is_dir():
        print("--invoice_path must be one invoice file.", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"Invoice file not found: {path}", file=sys.stderr)
        return 2

    try:
        client = GrokClient()
    except MissingAPIKey as exc:
        print(str(exc), file=sys.stderr)
        return 1

    db_path = Path("inventory.db")
    if not db_path.exists():
        create_database(db_path)

    result = run_invoice(path, db_path, client)
    _write_log(path, result)
    print(json.dumps(_result_line(path, result)), flush=True)
    return 0


def _write_log(path: Path, result: dict) -> None:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = list(result["logs"])
    payload.append(_result_line(path, result))
    destination = log_dir / f"{path.name}.json"
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _result_line(path: Path, result: dict) -> dict:
    line = {
        "record": "result",
        "file": path.name,
        "outcome": result.get("outcome"),
        "reason_code": result.get("reason_code"),
        "action": result.get("action"),
        "payment_status": payment_status(result),
        "codes": result_codes(result),
    }
    calls = tool_calls_of(result)
    if calls:
        line["tool_calls"] = calls
    return line


def tool_calls_of(result: dict) -> list:
    """Tool calls recorded on stage results. Empty when a stage made none.

    The review screen reads these on state.logs[].result.tool_calls and, for a
    pipeline log, on the result record's tool_calls field. A missing key means
    the stage made no tool call.
    """

    calls = []
    for row in result.get("logs") or []:
        if not isinstance(row, dict):
            continue
        body = row.get("result")
        if isinstance(body, dict) and body.get("tool_calls"):
            calls.extend(body["tool_calls"])
    return calls


def payment_status(result: dict) -> str:
    payment = result.get("payment")
    if payment is None:
        return "not_paid"
    if isinstance(payment, dict):
        return payment.get("status", "not_paid")
    return payment.status


if __name__ == "__main__":
    raise SystemExit(main())
