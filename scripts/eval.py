"""Score the sample invoices against data/answer_key.json.

Each file is a new graph run. Nothing is remembered between files.
The exit code is 0 only when every outcome and primary reason code match.
Warning lines are printed and do not change the exit code.

    python3 scripts/eval.py
    python3 scripts/eval.py --help
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
from rules import KEY_REASON_TO_CODE, result_codes  # noqa: E402

INVOICES = ROOT / "data" / "invoices"
ANSWER_KEY = ROOT / "data" / "answer_key.json"
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


def load_answer_key(path: Path | None = None) -> dict:
    payload = json.loads((path or ANSWER_KEY).read_text(encoding="utf-8"))
    return payload["invoices"]


def grade(result: dict, expected: dict) -> dict:
    """Compare one graph result to one answer-key row.

    Outcome and primary reason code decide ok. Warnings are informational.
    """

    actual_outcome = str(result.get("outcome") or "").upper()
    expected_outcome = str(expected["outcome"]).upper()
    actual_code = result.get("reason_code")
    expected_name = expected["primary_reason"]
    if expected_name not in KEY_REASON_TO_CODE:
        expected_code = expected_name
        known = False
    else:
        expected_code = KEY_REASON_TO_CODE[expected_name]
        known = True
    codes = set(result_codes(result))
    messages = " ".join(_messages(result)).lower()
    expected_warnings = list(expected.get("warnings") or [])
    matched = [item for item in expected_warnings if _warning_hit(item, codes, messages)]
    return {
        "outcome_ok": actual_outcome == expected_outcome,
        "reason_ok": known and actual_code == expected_code,
        "actual_outcome": actual_outcome,
        "expected_outcome": expected_outcome,
        "actual_code": actual_code,
        "expected_code": expected_code,
        "warnings_matched": len(matched),
        "warnings_expected": len(expected_warnings),
    }


def _messages(result: dict) -> list[str]:
    messages = []
    for finding in result.get("findings") or []:
        if isinstance(finding, dict):
            messages.append(finding.get("message") or "")
        else:
            messages.append(finding.message)
    return messages


def _warning_hit(expected: str, codes: set[str], messages: str) -> bool:
    """True when a key warning is present in the findings. Informational only."""

    text = expected.lower()
    if "fraud signal" in text:
        return "FRAUD_SIGNAL" in codes
    if "within 2%" in text or "threshold" in text:
        return "NEAR_THRESHOLD" in codes
    if "descriptor" in text or "rush order" in text:
        return "DESCRIPTOR" in codes
    if "unit price" in text:
        return "PRICE_VARIANCE" in codes
    if "address" in text:
        return "NONCOMMERCIAL_ADDRESS" in codes
    if "ocr" in text:
        return "OCR_FIX" in codes
    if "not a date" in text or "due date reads" in text:
        return "UNPARSED_DATE" in codes
    if "missing vendor" in text or "negative total" in text or "total mismatch" in text:
        return bool(codes & {"TOTAL_MISMATCH", "MISSING_FIELD", "NEGATIVE_AMOUNT", "BAD_QUANTITY"})
    return text in messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score each sample invoice against data/answer_key.json."
    )
    parser.add_argument(
        "--invoices",
        default=str(INVOICES),
        help="Directory of invoice files (default: data/invoices)",
    )
    args = parser.parse_args(argv)
    try:
        GrokClient()
    except MissingAPIKey as exc:
        print(str(exc), file=sys.stderr)
        return 1

    invoices = Path(args.invoices)
    db_path = ROOT / "inventory.db"
    if not db_path.exists():
        create_database(db_path)

    key = load_answer_key()
    files = sorted(
        path for path in invoices.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES
    )
    correct = 0
    for path in files:
        result = run_invoice(path, db_path, GrokClient())
        print_bad_reasoning(path.name, result.get("attempts") or [])
        lookup = f"data/invoices/{path.name}"
        expected = key.get(lookup)
        if expected is None:
            print(f"{path.name}\toutcome={result.get('outcome')}\texpected=MISSING\tMISS", flush=True)
            continue
        row = grade(result, expected)
        ok = row["outcome_ok"] and row["reason_ok"]
        correct += int(ok)
        print(
            f"{path.name}\toutcome={row['actual_outcome']}\texpected={row['expected_outcome']}\t"
            f"reason={row['actual_code']}\texpected_reason={row['expected_code']}\t"
            f"payment={payment_status(result)}\t"
            f"warnings={row['warnings_matched']}/{row['warnings_expected']}\t"
            f"{'ok' if ok else 'MISS'}",
            flush=True,
        )
    print(f"score {correct}/{len(files)}", flush=True)
    return 0 if correct == len(files) and correct == len(key) else 1


if __name__ == "__main__":
    raise SystemExit(main())
