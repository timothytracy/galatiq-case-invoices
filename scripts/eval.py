"""Score sample invoices against data/answer_key.json. Exit 0 only on a full match.

A match is the outcome plus the primary reason code, or a listed alternate
code from the answer key. The unseen scorer uses that same rule.
invoice_2115.txt is the example: the deterministic parser rejects the misread
total '$3,5OO.00' as MISSING_FIELD, and the recorded model read gives the
listed alternate TOTAL_MISMATCH.

--ordered runs data/ordered_scenario.json on one shared ledger. The scored
path does not pass a ledger, so each invoice still gets a fresh one.
--verbose also prints each stage's JSON record. Without it, stdout is the
score lines only.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import ensure_inventory_db, inventory_db_path, reset_inventory_db  # noqa: E402
from graph import run_invoice  # noqa: E402
from ledger.scenario import run_scenario  # noqa: E402
from llm_client import (  # noqa: E402
    MissingAPIKey,
    ModeError,
    begin_client,
    resolve_run_mode,
)
from decision.outcome import payment_status, result_codes, tool_calls_of  # noqa: E402
from decision.wording import KEY_REASON_TO_CODE  # noqa: E402
from parsers import SUFFIXES  # noqa: E402

INVOICES = ROOT / "data" / "invoices"
ANSWER_KEY = ROOT / "data" / "answer_key.json"
ORDERED_SCENARIO = ROOT / "data" / "ordered_scenario.json"


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


def invoice_lookup(path: Path) -> str:
    """Repo-relative answer-key path, or data/invoices/<name> when it is outside the repo."""

    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return f"data/invoices/{path.name}"


def _accepted_codes(expected: dict) -> set:
    """Reason codes the row accepts. An unknown alternative is warned, not added."""

    names = [expected["primary_reason"]]
    for name in expected.get("also_accepted_reasons") or []:
        if name not in KEY_REASON_TO_CODE:
            print(
                f"warning: unknown reason name {name!r} in also_accepted_reasons",
                file=sys.stderr,
            )
        names.append(name)
    return {KEY_REASON_TO_CODE[name] for name in names if name in KEY_REASON_TO_CODE}


def grade(result: dict, expected: dict) -> dict:
    """Compare one graph result to one answer-key row.

    The unseen scorer checks the outcome plus the primary reason code, or a
    listed alternate code (also_accepted_reasons). invoice_2115.txt is the
    example: the deterministic parser rejects the misread total '$3,5OO.00' as
    MISSING_FIELD, and the recorded model read gives the listed alternate
    TOTAL_MISMATCH. An unknown name in that list is not a code, and it is
    printed rather than dropped quietly. Warnings are informational.

    also_accepted_outcomes lists other outcomes the row accepts, each with its
    own primary_reason. invoice_1004.json accepts a FRAUD_SUSPECTED hold.
    """

    actual_outcome = str(result.get("outcome") or "").upper()
    for alternative in expected.get("also_accepted_outcomes") or []:
        if str(alternative["outcome"]).upper() == actual_outcome:
            expected = {**expected, **alternative, "also_accepted_reasons": []}
            break
    expected_outcome = str(expected["outcome"]).upper()
    actual_code = result.get("reason_code")
    expected_name = expected["primary_reason"]
    if expected_name in KEY_REASON_TO_CODE:
        expected_code = KEY_REASON_TO_CODE[expected_name]
    else:
        expected_code = expected_name
    accepted = _accepted_codes(expected)
    codes = set(result_codes(result))
    messages = " ".join(_messages(result)).lower()
    expected_warnings = list(expected.get("warnings") or [])
    matched = [item for item in expected_warnings if _warning_hit(item, codes, messages)]
    return {
        "outcome_ok": actual_outcome == expected_outcome,
        "reason_ok": actual_code in accepted,
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
    parser.add_argument(
        "--answer-key",
        default=str(ANSWER_KEY),
        help="Path to the answer-key JSON (default: data/answer_key.json)",
    )
    parser.add_argument(
        "--ordered",
        nargs="?",
        const=str(ORDERED_SCENARIO),
        default=None,
        help=(
            "Run this scenario in order on one shared ledger "
            "(default file: data/ordered_scenario.json). Not the scored eval."
        ),
    )
    mode_flags = parser.add_mutually_exclusive_group()
    mode_flags.add_argument(
        "--record",
        metavar="PATH",
        help="Call the live model and append each request to this JSONL file",
    )
    mode_flags.add_argument(
        "--replay",
        metavar="PATH",
        help="Answer from this JSONL file. No network and no API key",
    )
    parser.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        help="Date the agents are told is today. Record stores it. Replay reads the file",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print each stage's JSON record and the mock payment line",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help=(
            "Delete the repository inventory.db and rebuild it from the seed before this run. "
            "Eval does not use payments.db. "
            "Does not change recordings/, data/, or logs/."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Rebuild the repository inventory.db from the seed before this run. "
            "Same as --reset-db. Eval does not use payments.db. "
            "Does not change recordings/, data/, or logs/."
        ),
    )
    args = parser.parse_args(argv)
    try:
        mode, recording = resolve_run_mode(args.record, args.replay)
    except ModeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if mode == "replay" and not Path(recording).is_file():
        print(f"replay recording not found: {recording}", file=sys.stderr)
        return 1
    invoices = Path(args.invoices)
    if args.ordered:
        if not Path(args.ordered).is_file():
            print(f"Scenario file not found: {args.ordered}", file=sys.stderr)
            return 2
    elif not invoices.is_dir():
        print(f"Invoice directory not found: {invoices}", file=sys.stderr)
        return 2
    elif not Path(args.answer_key).is_file():
        print(f"Answer key not found: {args.answer_key}", file=sys.stderr)
        return 2
    try:
        client, as_of = begin_client(mode, recording, args.as_of)
    except MissingAPIKey as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    db_path = inventory_db_path()
    if args.reset or args.reset_db:
        reset_inventory_db(db_path)
    else:
        ensure_inventory_db(db_path)

    with _release_dir():
        if args.ordered:
            return _ordered_main(Path(args.ordered), db_path, client, mode, as_of, args.verbose)
        return _scored_main(invoices, Path(args.answer_key), db_path, client, mode, as_of, args.verbose)


def _scored_main(
    invoices: Path, answer_key: Path, db_path: Path, client, mode: str, as_of, verbose: bool
) -> int:
    """Each invoice on its own graph, graded against the answer key."""

    key = load_answer_key(answer_key)
    files = sorted(
        path for path in invoices.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES
    )
    correct = 0
    tool_calls = 0
    repeats = 0
    for path in files:
        # One client for the run, so record mode writes a single file.
        # Live has no as_of: the graph still uses the wall clock.
        with stage_output(verbose):
            result = run_invoice(path, db_path, client, as_of=as_of)
        print_bad_reasoning(path.name, result.get("attempts") or [])
        calls = tool_calls_of(result)
        tool_calls, repeats = _tally(calls, tool_calls, repeats)
        lookup = invoice_lookup(path)
        expected = key.get(lookup)
        if expected is None:
            print(
                f"{path.name}\toutcome={result.get('outcome')}\texpected=MISSING\t"
                f"tools={len(calls)}\tMISS",
                flush=True,
            )
            continue
        row = grade(result, expected)
        ok = row["outcome_ok"] and row["reason_ok"]
        correct += int(ok)
        print(
            f"{path.name}\toutcome={row['actual_outcome']}\texpected={row['expected_outcome']}\t"
            f"reason={row['actual_code']}\texpected_reason={row['expected_code']}\t"
            f"payment={payment_status(result)}\t"
            f"warnings={row['warnings_matched']}/{row['warnings_expected']}\t"
            f"tools={len(calls)}\t"
            f"{_mark(ok)}",
            flush=True,
        )
        if not ok and result.get("agent_error"):
            print(f"{path.name}\terror={result['agent_error']}", flush=True)
    print(format_tool_summary(tool_calls, repeats), flush=True)
    print(f"score {correct}/{len(files)}", flush=True)
    if mode != "live":
        print(client.usage_line(), flush=True)
    return 0 if correct == len(files) and correct == len(key) else 1


@contextlib.contextmanager
def _release_dir():
    """Replay records stay out of logs/ unless INVOICE_RELEASE_DIR is already set."""

    if os.environ.get("INVOICE_RELEASE_DIR", "").strip():
        yield
        return
    folder = tempfile.mkdtemp(prefix="releases-")
    os.environ["INVOICE_RELEASE_DIR"] = folder
    try:
        yield
    finally:
        os.environ.pop("INVOICE_RELEASE_DIR", None)
        shutil.rmtree(folder, ignore_errors=True)


def _tally(calls: list[dict], tool_calls: int, repeats: int) -> tuple[int, int]:
    """Running totals of tool calls and repeated calls."""

    return tool_calls + len(calls), repeats + sum(1 for row in calls if row.get("repeat"))


def _mark(ok: bool) -> str:
    return "ok" if ok else "MISS"


def format_tool_summary(calls: int, repeats: int) -> str:
    """One line for a run. Repeats are included in calls."""

    return f"tools calls={calls} repeats={repeats}"


def stage_output(verbose: bool):
    """Pass stage records through, or swallow them so the score lines stand out."""

    if verbose:
        return contextlib.nullcontext()
    return contextlib.redirect_stdout(io.StringIO())


def _ordered_main(
    scenario_path: Path, db_path: Path, client, mode: str, as_of, verbose: bool = False
) -> int:
    """One temporary ledger for every file in the scenario. Not payments.db.

    One client covers the whole scenario, so --record appends every request
    to the same file. The ledger file name is not part of the model request.
    """

    handle, name = tempfile.mkstemp(prefix="payments-ordered-", suffix=".db")
    os.close(handle)
    try:
        with stage_output(verbose):
            report = run_scenario(
                scenario_path,
                db_path,
                name,
                lambda _path: client,
                as_of=as_of,
            )
    finally:
        Path(name).unlink(missing_ok=True)
    tool_calls = 0
    repeats = 0
    for step in report["steps"]:
        calls = step.get("tool_calls") or []
        tool_calls, repeats = _tally(calls, tool_calls, repeats)
        print(
            f"{step['file']}\toutcome={step['outcome']}\texpected={step['expected_outcome']}\t"
            f"reason={step['reason_code']}\texpected_reason={step['expected_reason']}\t"
            f"payment={'paid' if step['paid'] else 'not_paid'}\t"
            f"tools={len(calls)}\t"
            f"{_mark(step['ok'])}",
            flush=True,
        )
    files = ", ".join(report["ledger_files"])
    print(
        f"ledger {len(report['ledger_files'])} rows: {files}\t"
        f"expected {', '.join(report['expected_ledger_files'])}\t"
        f"{_mark(report['ledger_ok'])}",
        flush=True,
    )
    print(format_tool_summary(tool_calls, repeats), flush=True)
    if mode != "live":
        print(client.usage_line(), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
