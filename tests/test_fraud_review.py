"""Fraud review: one model call after validation that can only add a hold. No network."""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from decision.outcome import PAYABLE_HOLD_CODES, review_release_block
from graph import run_invoice
from ledger.scenario import run_scenario
from llm_errors import LLMError
from parsers import SUFFIXES, parse_file
from pipeline.fraud_review import FRAUD_PROMPT, normalize_quote
from scripts.eval import grade, load_answer_key
from tests.fake_llm_eval import handler_for
from tests.fakes import FRAUD_PASS, FakeClock, FakeLLM, ZeroRandom, scripted, extraction, write_invoice

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
FRAUD = ROOT / "data" / "fraud_invoices"
KEY = load_answer_key(ROOT / "data" / "fraud_answer_key.json")
ACTION = "Verify payment details with the vendor using contact details already on file."


def _hold(quote, innocent="A vendor that prefers wire payments.", why="Verification is blocked."):
    return json.dumps({"verdict": "hold", "innocent_reading": innocent, "quote": quote, "why": why})


def _run(db_path, path, fraud, handler=None):
    fake = FakeLLM(handler or handler_for(path), fraud=fraud)
    result = run_invoice(path, db_path, fake, clock=FakeClock(), rng=ZeroRandom(), max_attempts=3)
    return result, fake


def _codes(result) -> list[str]:
    return [finding.code for finding in result["findings"]]


def _stages(result) -> list[str]:
    return [row["stage"] for row in result["logs"]]


def _fraud_log(result) -> dict:
    return next(row["result"] for row in result["logs"] if row["stage"] == "fraud_review")


def _key(path: Path) -> dict:
    return KEY[f"data/fraud_invoices/{path.name}"]


def _graded_ok(result, path: Path) -> bool:
    row = grade(result, _key(path))
    return row["outcome_ok"] and row["reason_ok"]


def test_a_quoted_hold_is_a_releasable_fraud_suspected_hold(db_path):
    path = FRAUD / "invoice_2121.txt"
    result, fake = _run(db_path, path, _hold("Do not call our office to confirm payment details."))
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "FRAUD_SUSPECTED"
    assert result["action"] == ACTION
    assert "FRAUD_SUSPECTED" in PAYABLE_HOLD_CODES
    assert review_release_block(result["outcome"], result["findings"], result["reason_code"]) is None
    assert _graded_ok(result, path)
    assert _stages(result) == ["ingestion", "validation", "fraud_review", "hold"]
    assert len(fake.fraud_calls) == 1
    assert {name for name in _task_names(fake)} == {"extract"}


def _task_names(fake):
    from tests.fakes import task_of

    return [task_of(call) for call in fake.calls]


def test_the_hold_keeps_the_keyword_fraud_signal_beside_it(db_path):
    path = FRAUD / "invoice_2121.txt"
    result, _fake = _run(db_path, path, _hold("Do not call our office to confirm payment details."))
    assert _codes(result) == ["FRAUD_SIGNAL", "FRAUD_SUSPECTED"]
    held = next(item for item in result["findings"] if item.code == "FRAUD_SUSPECTED")
    assert held.message.startswith('Fraud review: "Do not call our office to confirm payment details."')


def test_a_hold_can_rest_on_a_page_with_no_keyword_finding(db_path):
    path = FRAUD / "invoice_2124.txt"
    quote = "Please settle this invoice to Northgate rather than to Corbel."
    result, _fake = _run(db_path, path, _hold(quote, innocent="A factoring arrangement."))
    assert _codes(result) == ["FRAUD_SUSPECTED"]
    assert _graded_ok(result, path)


def test_the_quote_is_matched_after_whitespace_and_case_are_normalized(db_path):
    path = FRAUD / "invoice_2125.txt"
    quote = "PLEASE update your records so that payments go to   the remit-to party shown above."
    result, _fake = _run(db_path, path, _hold(quote))
    assert result["reason_code"] == "FRAUD_SUSPECTED"
    assert normalize_quote(" A \n b ") == "a b"


@pytest.mark.parametrize(
    "reply, reason",
    [
        (_hold(""), "The quote is shorter than 12 characters."),
        (_hold("Pay to Northgate Partners today."), "The quote is not in the invoice text."),
        (_hold("Wire only"), "The quote is shorter than 12 characters."),
        (_hold("Do not call our office to confirm payment details.", innocent="  "), "No innocent reading was given."),
    ],
)
def test_a_hold_code_cannot_verify_is_dropped_and_the_invoice_is_paid(db_path, reply, reason):
    path = FRAUD / "invoice_2121.txt"
    result, fake = _run(db_path, path, reply)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert "FRAUD_SUSPECTED" not in _codes(result)
    assert _codes(result) == ["FRAUD_SIGNAL"]
    assert _fraud_log(result)["fraud_note"] == f"Fraud review hold dropped: {reason}"
    assert "audit" in _task_names(fake)


def test_a_pass_changes_nothing(db_path):
    path = FRAUD / "invoice_2123.txt"
    passed, with_review = _run(db_path, path, None)
    assert passed["outcome"] == "approve"
    assert _codes(passed) == ["FRAUD_SIGNAL"]
    assert _fraud_log(passed)["fraud_verdict"] == "pass"
    assert _graded_ok(passed, path)
    for call in with_review.calls:
        text = json.dumps(call)
        assert "An ordinary bill from the vendor." not in text
        assert "fraud_verdict" not in text


@pytest.mark.parametrize(
    "fraud",
    [
        LLMError("rate_limit", True, "rate limited"),
        "not json at all",
        LLMError("replay miss: no recording for fingerprint abc", False, "replay miss"),
    ],
    ids=["retries_used_up", "schema_failure", "replay_miss"],
)
def test_a_failed_fraud_call_is_a_system_error_hold_and_nothing_after_it_runs(db_path, fraud):
    path = FRAUD / "invoice_2126.txt"
    result, fake = _run(db_path, path, fraud)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert result["payment"].status == "not_paid"
    assert review_release_block(result["outcome"], result["findings"], result["reason_code"])
    assert _task_names(fake) == ["extract"]
    assert "approval" not in _stages(result)
    assert "audit" not in _stages(result)


def test_a_send_back_does_not_run_the_fraud_review_again(tmp_path, db_path):
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA 2\nTotal: 500.00\n")
    handler, counts = scripted(extraction(), audits=["SEND_BACK", "PASS"])
    result, fake = _run(db_path, path, None, handler=handler)
    assert result["outcome"] == "approve"
    assert counts["extract"] == 2
    assert counts["audit"] == 2
    assert len(fake.fraud_calls) == 1
    assert _stages(result).count("fraud_review") == 1


def _sample(name: str) -> Path:
    for folder in ("invoices", "extra_invoices"):
        candidate = ROOT / "data" / folder / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError(name)


@pytest.mark.parametrize(
    "name, code",
    [
        ("invoice_2112.txt", "FRAUD_CLUSTER"),
        ("invoice_1002.txt", "STOCK_SHORTFALL"),
        ("invoice_1003.txt", "STOCK_SHORTFALL"),
    ],
)
def test_validation_holds_and_rejects_never_reach_the_fraud_review(db_path, name, code):
    path = _sample(name)
    result, fake = _run(db_path, path, AssertionError("fraud review must not run"))
    assert result["reason_code"] == code
    assert fake.fraud_calls == []
    assert "fraud_review" not in _stages(result)


def test_1003_keeps_its_reject_reason_and_both_fraud_signals(db_path):
    result, _fake = _run(db_path, _sample("invoice_1003.txt"), None)
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "STOCK_SHORTFALL"
    assert _codes(result).count("FRAUD_SIGNAL") == 2
    final = result["logs"][-1]["result"]
    assert "fraud signal" in final["reason"].lower()


def test_fake_model_scores_the_fraud_set_when_the_model_holds_the_labeled_holds(db_path):
    quotes = {
        "invoice_2121.txt": "Do not call our office to confirm payment details.",
        "invoice_2124.txt": "Please settle this invoice to Northgate rather than to Corbel.",
        "invoice_2125.txt": "Please update your records so that payments go to the remit-to party shown above.",
    }
    for path in sorted(FRAUD.iterdir()):
        reply = _hold(quotes[path.name]) if path.name in quotes else None
        result, _fake = _run(db_path, path, reply)
        assert _graded_ok(result, path), path.name


def _invoice_texts():
    for folder in sorted((ROOT / "data").iterdir()):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in SUFFIXES:
                continue
            if path.suffix.lower() == ".pdf":
                yield path, parse_file(path).source.raw_text or ""
            else:
                yield path, path.read_text(encoding="utf-8", errors="ignore")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", text.lower())


def test_the_prompt_borrows_no_four_word_phrase_from_any_invoice():
    prompt = " ".join(_words(FRAUD_PROMPT))
    leaks = []
    for path, text in _invoice_texts():
        words = _words(text)
        for start in range(len(words) - 3):
            phrase = " ".join(words[start : start + 4])
            if f" {phrase} " in f" {prompt} ":
                leaks.append((path.name, phrase))
    assert leaks == []


def test_1004_grades_as_an_approve_or_a_fraud_hold_and_nothing_else():
    row = load_answer_key()["data/invoices/invoice_1004.json"]
    approved = grade({"outcome": "approve", "reason_code": None, "findings": []}, row)
    held = grade({"outcome": "hold", "reason_code": "FRAUD_SUSPECTED", "findings": []}, row)
    other_hold = grade({"outcome": "hold", "reason_code": "PRICE_SPIKE", "findings": []}, row)
    rejected = grade({"outcome": "reject", "reason_code": "STOCK_SHORTFALL", "findings": []}, row)
    assert approved["outcome_ok"] and approved["reason_ok"]
    assert held["outcome_ok"] and held["reason_ok"]
    assert not other_hold["reason_ok"]
    assert not rejected["outcome_ok"]


def test_the_ordered_scenario_accepts_a_held_1004_with_two_ledger_rows(db_path, tmp_path):
    ledger = tmp_path / "payments.db"

    def fraud(messages):
        page = json.loads(messages[-1]["content"])["invoice_text"]
        if "742 Evergreen Terrace" in page and "Revised" not in page:
            return _hold("742 Evergreen Terrace, Springfield, IL 62704")
        return FRAUD_PASS

    report = run_scenario(
        ROOT / "data" / "ordered_scenario.json",
        db_path,
        ledger,
        lambda path: FakeLLM(handler_for(path), fraud=fraud),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=5,
        as_of=date(2026, 11, 1),
    )
    missed = [step for step in report["steps"] if not step["ok"]]
    assert missed == [], missed
    assert report["ok"]
    assert report["ledger_files"] == ["invoice_1001.txt", "invoice_1011.pdf"]
    steps = {step["file"]: step for step in report["steps"]}
    assert steps["invoice_1004.json"]["reason_code"] == "FRAUD_SUSPECTED"
    assert steps["invoice_1004_revised.json"]["reason_code"] == "REVISED_INVOICE"


def test_the_ordered_scenario_accepts_exactly_the_paid_and_the_held_ledgers():
    scenario = json.loads((ROOT / "data" / "ordered_scenario.json").read_text(encoding="utf-8"))
    accepted = [scenario["ledger_files"], *scenario["also_accepted_ledger_files"]]
    assert ["invoice_1001.txt", "invoice_1011.pdf", "invoice_1004.json"] in accepted
    assert ["invoice_1001.txt", "invoice_1011.pdf"] in accepted
    assert len(accepted) == 2
