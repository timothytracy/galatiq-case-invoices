"""Process one invoice.

    python3 main.py --invoice_path=data/invoices/invoice_1001.txt

A missing XAI_API_KEY stops the program before any invoice is touched.
main.py creates inventory.db only when that file is missing. The run itself
opens the database read-only.
"""

import argparse
import json
import sys
from pathlib import Path

from db import create_database
from graph import run_invoice
from llm_client import GrokClient, MissingAPIKey


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
    return {
        "record": "result",
        "file": path.name,
        "outcome": result.get("outcome"),
        "payment_status": payment_status(result),
    }


def payment_status(result: dict) -> str:
    payment = result.get("payment")
    if payment is None:
        return "not_paid"
    if isinstance(payment, dict):
        return payment.get("status", "not_paid")
    return payment.status


if __name__ == "__main__":
    raise SystemExit(main())
