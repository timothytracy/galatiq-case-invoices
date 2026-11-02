"""Offline demo: pay one hold and refuse one reject. No model call.

    python3 scripts/approve_hold_demo.py --demo
"""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import ensure_inventory_db, inventory_db_path  # noqa: E402
from graph import run_invoice  # noqa: E402
from llm_client import begin_client  # noqa: E402
from pipeline.approve_hold import approve_and_pay  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve a held invoice for payment.")
    parser.add_argument("--demo", action="store_true", help="Run two offline invoices, then release them.")
    args = parser.parse_args(argv)
    if not args.demo:
        print("Pass --demo.", file=sys.stderr)
        return 2
    return _run_demo()


def _run_demo() -> int:
    records = Path(tempfile.mkdtemp(prefix="releases-"))
    ledger_fd, ledger_name = tempfile.mkstemp(prefix="payments-", suffix=".db")
    log_fd, log_name = tempfile.mkstemp(prefix="approve-", suffix=".jsonl")
    os.close(ledger_fd)
    os.close(log_fd)
    ledger, log = Path(ledger_name), Path(log_name)
    previous = os.environ.get("INVOICE_RELEASE_DIR")
    os.environ["INVOICE_RELEASE_DIR"] = str(records)
    try:
        hold = _offline(ROOT / "data" / "invoices" / "invoice_1012.txt", ledger)
        rejected = _offline(ROOT / "data" / "invoices" / "invoice_1002.txt", ledger)
        first = approve_and_pay(
            hold, reviewer="Avery Chen", note="Confirmed this is the same company.",
            amount=hold["invoice"].total, ledger_path=ledger, log_path=log,
        )
        second = approve_and_pay(
            rejected, reviewer="Avery Chen", note="Stock is short, so this cannot be paid.",
            amount=rejected["invoice"].total, ledger_path=ledger, log_path=log,
        )
    finally:
        if previous is None:
            os.environ.pop("INVOICE_RELEASE_DIR", None)
        else:
            os.environ["INVOICE_RELEASE_DIR"] = previous
        shutil.rmtree(records, ignore_errors=True)
        ledger.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
    print(json.dumps(first["record"]), flush=True)
    print(json.dumps(second["record"]), flush=True)
    return 0 if first["result"] == "paid" and second["result"] == "held" else 1


def _offline(path: Path, ledger: Path):
    """Replay one saved invoice. No test double and no API key."""

    database = ensure_inventory_db(inventory_db_path())
    client, as_of = begin_client("replay", str(ROOT / "recordings" / "samples.jsonl"), None)
    with redirect_stdout(io.StringIO()):
        return run_invoice(path, database, client, as_of=as_of, ledger_path=ledger)


if __name__ == "__main__":
    raise SystemExit(main())
