"""Score labeled invoices.

Each file is a new graph run. Nothing is remembered between files.
Run from the repo root:

    python3 scripts/eval.py
    python3 scripts/eval.py --set extra
    python3 scripts/eval.py --set all

The default is the original 20 files in data/invoices. --set extra scores
data/extra_invoices against data/extra_labels.json. --set all scores both
and prints a score line for each set.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import create_database  # noqa: E402
from graph import run_invoice  # noqa: E402
from llm_client import GrokClient, MissingAPIKey  # noqa: E402
from main import payment_status  # noqa: E402

INVOICES = ROOT / "data" / "invoices"
LABELS = ROOT / "data" / "labels.json"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
SETS = {
    "samples": (INVOICES, LABELS),
    "extra": (ROOT / "data" / "extra_invoices", ROOT / "data" / "extra_labels.json"),
}


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


def resolve_sets(argv: list[str] | None = None) -> list[str]:
    """Which labeled folders to score. The default is the original samples."""

    parser = argparse.ArgumentParser(description="Score invoices against a label file.")
    parser.add_argument(
        "--set",
        choices=("samples", "extra", "all"),
        default="samples",
        dest="which",
        help="samples (default, the original 20), extra, or all.",
    )
    args = parser.parse_args(argv)
    if args.which == "all":
        return ["samples", "extra"]
    return [args.which]


def score_directory(invoice_dir: Path, labels_path: Path, db_path: Path) -> tuple[int, int]:
    labels = {
        row["file"]: row["outcome"]
        for row in json.loads(labels_path.read_text(encoding="utf-8"))["invoices"]
    }
    files = sorted(
        path for path in invoice_dir.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES
    )
    correct = 0
    for path in files:
        result = run_invoice(path, db_path, GrokClient())
        print_bad_reasoning(path.name, result.get("attempts") or [])
        actual = result.get("outcome")
        expected = labels.get(path.name)
        ok = actual == expected
        correct += int(ok)
        print(
            f"{path.name}\toutcome={actual}\texpected={expected}\t"
            f"payment={payment_status(result)}\t{'ok' if ok else 'MISS'}",
            flush=True,
        )
    return correct, len(files)


def main(argv: list[str] | None = None) -> int:
    names = resolve_sets(argv)
    try:
        GrokClient()
    except MissingAPIKey as exc:
        print(str(exc), file=sys.stderr)
        return 1

    db_path = ROOT / "inventory.db"
    if not db_path.exists():
        create_database(db_path)

    show_name = names != ["samples"]
    failed = False
    for name in names:
        invoice_dir, labels_path = SETS[name]
        correct, total = score_directory(invoice_dir, labels_path, db_path)
        if show_name:
            print(f"score {name} {correct}/{total}", flush=True)
        else:
            print(f"score {correct}/{total}", flush=True)
        if correct != total:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
