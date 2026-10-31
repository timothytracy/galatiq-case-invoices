"""Score the sample invoices against data/labels.json.

Each file is a new graph run. Nothing is remembered between files.
Run from the repo root:

    python3 scripts/eval.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import create_database  # noqa: E402
from graph import run_invoice  # noqa: E402
from llm_client import GrokClient, MissingAPIKey  # noqa: E402
from main import payment_status  # noqa: E402
from rules import result_codes  # noqa: E402

INVOICES = ROOT / "data" / "invoices"
LABELS = ROOT / "data" / "labels.json"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}


def print_bad_reasoning(filename: str, attempts: list) -> None:
    """One readable line per rejected wording: invoice, attempt, phrase, text."""

    for row in attempts:
        if row.get("error_type") != "bad_reasoning":
            continue
        print(
            f"{filename}\tbad_reasoning\tattempt={row.get('attempt')}\t"
            f"matched={row.get('matched')}\t"
            f"conclusion={row.get('conclusion')}\t"
            f"risks={json.dumps(row.get('risks_considered'))}",
            flush=True,
        )


def main() -> int:
    try:
        GrokClient()
    except MissingAPIKey as exc:
        print(str(exc), file=sys.stderr)
        return 1

    db_path = ROOT / "inventory.db"
    if not db_path.exists():
        create_database(db_path)

    labels = {
        row["file"]: row["outcome"]
        for row in json.loads(LABELS.read_text(encoding="utf-8"))["invoices"]
    }
    files = sorted(
        path for path in INVOICES.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES
    )
    correct = 0
    for path in files:
        result = run_invoice(path, db_path, GrokClient())
        print_bad_reasoning(path.name, result.get("attempts") or [])
        actual = result.get("outcome")
        expected = labels.get(path.name)
        ok = actual == expected
        correct += int(ok)
        codes = ",".join(result_codes(result))
        print(
            f"{path.name}\toutcome={actual}\texpected={expected}\t"
            f"payment={payment_status(result)}\tcodes={codes}\t{'ok' if ok else 'MISS'}",
            flush=True,
        )
    print(f"score {correct}/{len(files)}", flush=True)
    return 0 if correct == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
