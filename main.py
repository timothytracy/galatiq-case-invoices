"""Process one invoice: python3 main.py --invoice_path=data/invoices/invoice_1001.txt

Payments are remembered in payments.db, next to inventory.db, in the data
folder: the repository root, or INVOICE_DATA_DIR when set. Run logs go to
logs/ in that folder. The working directory does not matter.
inventory.db is created from the seed when it is missing.
--reset-ledger deletes payments.db. --reset-db rebuilds inventory.db.
--reset does both. recordings/, data/, and logs/ are left as they are.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from db import ensure_inventory_db, inventory_db_path, log_dir, payments_db_path, reset_inventory_db
from graph import run_invoice
from llm_client import (
    NEEDS_KEY, REPLAY_NOTICE, GrokClient, MissingAPIKey, ModeError, begin_client, resolve_run_mode,
)
from decision.outcome import result_line
from pipeline.ingestion import is_recorded

RECORDINGS = Path(__file__).resolve().parent / "recordings"
# ordered.jsonl is left out: its later answers depend on the ledger order of that scenario.
FALLBACK_RECORDINGS = ("samples.jsonl", "unseen.jsonl")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    path = Path(args.invoice_path)
    if path.is_dir():
        print("--invoice_path must be one invoice file.", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"Invoice file not found: {path}", file=sys.stderr)
        return 2

    opened = _open_client(path, args)
    if isinstance(opened, int):
        return opened
    client, as_of = opened

    db_path = inventory_db_path()
    if args.reset or args.reset_db:
        reset_inventory_db(db_path)
    else:
        ensure_inventory_db(db_path)
    # Next to inventory.db. Gitignored. A second run of the same file is a duplicate hold.
    ledger = payments_db_path()
    if args.reset or args.reset_ledger:
        ledger.unlink(missing_ok=True)

    result = run_invoice(path, db_path, client, as_of=as_of, ledger_path=ledger)
    _write_log(path, result)
    print(json.dumps(result_line(path, result)), flush=True)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process one invoice file.")
    parser.add_argument("--invoice_path", required=True, help="Path to one invoice file")
    parser.add_argument(
        "--reset-ledger",
        action="store_true",
        help=(
            "Delete payments.db in the data folder before this run, including the "
            "release records stored in it. A held invoice must be rerun before it can be "
            "approved. Does not change inventory.db, recordings/, data/, or logs/."
        ),
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help=(
            "Delete inventory.db in the data folder and rebuild it from the seed "
            "before this run. Does not change payments.db, recordings/, data/, or logs/."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Delete payments.db and inventory.db in the data folder, then rebuild "
            "inventory.db from the seed. Deleting payments.db drops its release records, "
            "so a held invoice must be rerun before it can be approved. "
            "Does not change recordings/, data/, or logs/."
        ),
    )
    mode_flags = parser.add_mutually_exclusive_group()
    mode_flags.add_argument("--record", metavar="PATH", help="Live call, then append to this JSONL file")
    mode_flags.add_argument("--replay", metavar="PATH", help="Answer from this JSONL file. No API key")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD", help="Date the agents are told is today")
    return parser


def _open_client(path: Path, args: argparse.Namespace):
    """(client, as_of), or an exit code. No key and no XAI_MODE falls back to a committed recording."""

    try:
        mode, recording = resolve_run_mode(args.record, args.replay)
        fallback = mode == "live" and not os.environ.get("XAI_MODE", "").strip()
        try:
            client, as_of = begin_client(mode, recording, args.as_of)
        except MissingAPIKey:
            if not fallback:
                raise
            print(REPLAY_NOTICE, file=sys.stderr, flush=True)
            mode, recording = "replay", _covering_recording(path)
            if recording is None:
                print(NEEDS_KEY.format(file=path.name), file=sys.stderr)
                return 1
            client, as_of = begin_client(mode, recording, args.as_of)
        if mode == "replay" and not is_recorded(path, client):
            print(NEEDS_KEY.format(file=path.name), file=sys.stderr)
            return 1
    except MissingAPIKey as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ModeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return client, as_of


def _covering_recording(path: Path) -> str | None:
    """The first committed recording that holds this invoice, or None."""

    for name in FALLBACK_RECORDINGS:
        recording = RECORDINGS / name
        if recording.is_file() and is_recorded(path, GrokClient(mode="replay", recording=recording)):
            return str(recording)
    return None


def _write_log(path: Path, result: dict) -> None:
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    payload = list(result["logs"])
    payload.append(result_line(path, result))
    destination = folder / f"{path.name}.json"
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
