"""Process one invoice: python3 main.py --invoice_path=data/invoices/invoice_1001.txt

Payments are remembered in payments.db, next to inventory.db.
--reset-ledger deletes that file first so a repeated demo is not a duplicate hold.
"""

import argparse
import json
import sys
from pathlib import Path

from db import create_database
from graph import run_invoice
from llm_client import MissingAPIKey, ModeError, begin_client, resolve_run_mode, tools_enabled
from decision.outcome import result_codes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process one invoice file.")
    parser.add_argument("--invoice_path", required=True, help="Path to one invoice file")
    parser.add_argument(
        "--reset-ledger",
        action="store_true",
        help="Delete payments.db before this run so a repeated demo file is not a duplicate hold.",
    )
    mode_flags = parser.add_mutually_exclusive_group()
    mode_flags.add_argument("--record", metavar="PATH", help="Live call, then append to this JSONL file")
    mode_flags.add_argument("--replay", metavar="PATH", help="Answer from this JSONL file. No API key")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD", help="Date the agents are told is today")
    args = parser.parse_args(argv)

    path = Path(args.invoice_path)
    if path.is_dir():
        print("--invoice_path must be one invoice file.", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"Invoice file not found: {path}", file=sys.stderr)
        return 2

    try:
        mode, recording = resolve_run_mode(args.record, args.replay)
        tools_enabled()
        client, as_of = begin_client(mode, recording, args.as_of)
    except MissingAPIKey as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ModeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    db_path = Path("inventory.db")
    if not db_path.exists():
        create_database(db_path)
    # Next to inventory.db. Gitignored. A second run of the same file is a duplicate hold.
    ledger = Path("payments.db")
    if args.reset_ledger:
        ledger.unlink(missing_ok=True)

    if mode == "live" and as_of is None:
        result = run_invoice(path, db_path, client, ledger_path=ledger)
    else:
        result = run_invoice(path, db_path, client, as_of=as_of, ledger_path=ledger)
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
    for row in reversed(result.get("logs") or []):
        if not isinstance(row, dict) or row.get("stage") != "audit":
            continue
        body = row.get("result") or {}
        if body.get("tool_cap_hit"):
            line["tool_cap_hit"] = body["tool_cap_hit"]
        if body.get("forced_final_turn"):
            line["forced_final_turn"] = True
        break
    return line


def tool_calls_of(result: dict) -> list:
    """Tool calls recorded on stage results. Empty when a stage made none.

    Audit stores them on logs[].result.tool_calls where stage is audit. The
    result line copies that list. A missing key means the stage made no tool call.
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
