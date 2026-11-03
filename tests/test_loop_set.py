"""The loop set and the page-amount holds. Fake model, no network.

2117 is clean and over the threshold, so critique and audit run.
2118 prints one total in digits and another in words.
2120 states a balance due below its total after a deposit.
Each extraction below is a read Grok could make; code decides either way.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from db import SEED
from decision.findings import assess
from decision.outcome import PAYABLE_HOLD_CODES, review_release_block
from decision.printed_amounts import amount_in_words, balances_due, printed_totals, words_to_amount
from decision.wording import KEY_REASON_TO_CODE
from graph import run_invoice
from parsers import SUFFIXES, parse_file
from scripts.eval import grade, load_answer_key
from tests.fake_llm_eval import extraction_json, handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom, scripted, write_invoice

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "data" / "loop_invoices"
KEY = load_answer_key(ROOT / "data" / "loop_answer_key.json")
STOCK = dict(SEED)


def _run(db_path, path, handler):
    return run_invoice(
        path,
        db_path,
        FakeLLM(handler),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=5,
    )


def _read(name: str, **overrides) -> str:
    """The deterministic read of a loop invoice, with some fields changed."""

    payload = json.loads(extraction_json(LOOP / name))
    payload.update(overrides)
    return json.dumps(payload)


def _codes(result) -> dict:
    return {finding.code: finding.severity for finding in result["findings"]}


def _stages(result) -> list[str]:
    return [row["stage"] for row in result["logs"]]


def _row(result, name):
    return grade(result, KEY[f"data/loop_invoices/{name}"])


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Ten thousand two hundred fifty dollars and 00/100", Decimal("10250")),
        ("One hundred twenty-five dollars and fifty cents", Decimal("125.50")),
        ("twelve hundred dollars only", Decimal("1200")),
        ("Three thousand five hundred", Decimal("3500")),
        ("Ten thousand dollars and 5/100", Decimal("10000.05")),
    ],
)
def test_amount_in_words_reads_plain_english(text, amount):
    assert words_to_amount(text) == amount


@pytest.mark.parametrize("text", ["", "see attached", "ten thousand euros", "five dollars and lots"])
def test_amount_in_words_never_guesses(text):
    assert words_to_amount(text) is None


def test_page_amounts_are_read_from_the_loop_invoices():
    raw_2118 = (LOOP / "invoice_2118.txt").read_text(encoding="utf-8")
    raw_2120 = (LOOP / "invoice_2120.txt").read_text(encoding="utf-8")
    assert printed_totals(raw_2118) == [Decimal("10520.00")]
    assert amount_in_words(raw_2118) == Decimal("10250")
    assert printed_totals(raw_2120) == [Decimal("10500.00")]
    assert balances_due(raw_2120) == [Decimal("8000.00")]
    assert balances_due("Subtotal: $10.00\nTotal: $10.00\nDue Date: 2026-01-01\n") == []


def test_no_labeled_invoice_outside_the_loop_set_has_a_page_amount_hold():
    for folder in ("invoices", "extra_invoices"):
        for path in sorted((ROOT / "data" / folder).iterdir()):
            if path.suffix.lower() not in SUFFIXES:
                continue
            invoice = parse_file(path)
            codes = {f.code for f in assess(invoice, invoice.source.raw_text, STOCK).findings}
            assert not codes & {"AMOUNT_WORDS_MISMATCH", "BALANCE_DUE"}, path.name


def test_loop_set_parser_and_fake_model_match_the_key(db_path):
    files = sorted(path for path in LOOP.iterdir() if path.suffix.lower() in SUFFIXES)
    assert [path.name for path in files] == ["invoice_2117.txt", "invoice_2118.txt", "invoice_2120.txt"]
    assert len(KEY) == len(files)
    for path in files:
        expected = KEY[f"data/loop_invoices/{path.name}"]
        invoice = parse_file(path)
        assessed = assess(invoice, invoice.source.raw_text, STOCK)
        assert assessed.outcome.upper() == expected["outcome"], path.name
        assert assessed.reason_code == KEY_REASON_TO_CODE[expected["primary_reason"]], path.name
        result = _run(db_path, path, handler_for(path))
        row = grade(result, expected)
        assert row["outcome_ok"] and row["reason_ok"], (path.name, row)


def test_2117_goes_through_critique_and_audit_and_is_paid(db_path):
    path = LOOP / "invoice_2117.txt"
    handler, counts = scripted(_read("invoice_2117.txt"), critique={"concerns": [], "stricter": False})
    result = _run(db_path, path, handler)
    assert counts["critique"] == 1 and counts["audit"] == 1
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert result["payment"].amount == Decimal("10625.00")
    assert _row(result, "invoice_2117.txt")["reason_ok"]


def test_2118_faithful_read_is_an_unpaid_hold_not_a_reject(db_path):
    path = LOOP / "invoice_2118.txt"
    handler, counts = scripted(_read("invoice_2118.txt", total="10520.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "AMOUNT_WORDS_MISMATCH"
    # The line-math mismatch on the total stays recorded, as a warning.
    assert _codes(result)["TOTAL_MISMATCH"] == "warning"
    assert result["payment"].status == "not_paid"
    assert counts["critique"] == 0 and counts["audit"] == 0
    assert "approval" not in _stages(result)
    blocked = review_release_block(
        result["outcome"], result["findings"], result["reason_code"]
    )
    assert blocked == "AMOUNT_WORDS_MISMATCH is not a review hold and is never paid."
    row = _row(result, "invoice_2118.txt")
    assert row["outcome_ok"] and row["reason_ok"]


def test_2118_total_taken_from_the_words_is_still_held(db_path):
    """The misread that once got past validation. The printed digits still disagree."""

    path = LOOP / "invoice_2118.txt"
    handler, counts = scripted(_read("invoice_2118.txt", total="10250.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "AMOUNT_WORDS_MISMATCH"
    assert "TOTAL_MISMATCH" not in _codes(result)
    assert result["payment"].status == "not_paid"
    assert counts["audit"] == 0
    row = _row(result, "invoice_2118.txt")
    assert row["outcome_ok"] and row["reason_ok"]


def test_words_that_do_not_match_the_lines_leave_the_reject_in_place(tmp_path, db_path):
    text = (
        "Vendor: Widgets Inc.\nDue Date: 2026-02-01\nWidgetA qty 2 @ 250\n"
        "Total Amount: $600.00\nAmount in words: Five hundred fifty dollars\n"
    )
    path = write_invoice(tmp_path, "w.txt", text)
    handler, _ = scripted(_read("invoice_2118.txt", line_items=[_line(2, 250)], total="600.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "TOTAL_MISMATCH"
    assert _codes(result)["AMOUNT_WORDS_MISMATCH"] == "warning"
    assert result["payment"].status == "not_paid"


def test_words_that_agree_add_nothing(tmp_path, db_path):
    text = (
        "Vendor: Widgets Inc.\nDue Date: 2026-02-01\nWidgetA qty 2 @ 250\n"
        "Total Amount: $500.00\nAmount in words: Five hundred dollars and 00/100\n"
    )
    path = write_invoice(tmp_path, "w.txt", text)
    handler, _ = scripted(_read("invoice_2118.txt", line_items=[_line(2, 250)], total="500.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"


def test_2120_deposit_balance_is_held_and_nothing_is_paid(db_path):
    path = LOOP / "invoice_2120.txt"
    handler, counts = scripted(_read("invoice_2120.txt", total="10500.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "BALANCE_DUE"
    assert result["payment"].status == "not_paid"
    assert counts["critique"] == 0 and counts["audit"] == 0
    row = _row(result, "invoice_2120.txt")
    assert row["outcome_ok"] and row["reason_ok"]


_2120 = (LOOP / "invoice_2120.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "owed",
    [
        "Balance Due        $8,000.00",
        "Balance Due\t$8,000.00",
        "Balance Due: USD 8,000.00",
        "Balance Due (USD): $8,000.00",
        "Amount Due (USD): $8,000.00",
        "Remaining to pay: $8,000.00",
        "Please remit: $8,000.00",
        "Total Due: $8,000.00",
    ],
)
def test_an_amount_owed_below_the_total_holds(tmp_path, db_path, owed):
    text = _2120.replace("Balance Due: $8,000.00", owed)
    path = write_invoice(tmp_path, "owed.txt", text)
    handler, _ = scripted(_read("invoice_2120.txt", total="10500.00"))
    result = _run(db_path, path, handler)
    assert Decimal("8000.00") in balances_due(text)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "BALANCE_DUE"
    assert result["payment"].status == "not_paid"


def test_csv_balance_due_below_the_total_holds(tmp_path, db_path):
    text = "\n".join(
        [
            "field,value",
            "invoice_number,INV-2120",
            "vendor,Ironbridge Components Inc.",
            "date,2026-09-30",
            "due_date,2026-10-30",
            "item,WidgetA",
            "quantity,12",
            "unit_price,250.00",
            "item,WidgetB",
            "quantity,9",
            "unit_price,500.00",
            "item,GadgetX",
            "quantity,4",
            "unit_price,750.00",
            "subtotal,10500.00",
            "tax,0.00",
            "total,10500.00",
            "balance_due,8000.00",
            "",
        ]
    )
    path = write_invoice(tmp_path, "owed.csv", text)
    handler, _ = scripted(_read("invoice_2120.txt", total="10500.00"))
    result = _run(db_path, path, handler)
    assert balances_due(text) == [Decimal("8000.00")]
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "BALANCE_DUE"
    assert result["payment"].status == "not_paid"


def test_2120_balance_read_as_the_total_is_not_paid_either(db_path):
    path = LOOP / "invoice_2120.txt"
    handler, _ = scripted(_read("invoice_2120.txt", total="8000.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "reject"
    assert "TOTAL_MISMATCH" in _codes(result)
    assert "BALANCE_DUE" in _codes(result)
    assert result["payment"].status == "not_paid"


def _clean(extra: str) -> str:
    return (
        "Vendor: Widgets Inc.\nDue Date: 2026-02-01\nWidgetA qty 2 @ 250\n"
        f"Total Amount: $500.00\n{extra}\n"
    )


def test_account_balance_credit_on_2117_is_still_paid(tmp_path, db_path):
    text = (LOOP / "invoice_2117.txt").read_text(encoding="utf-8").rstrip()
    text += "\nAccount balance: $500.00 credit\n"
    path = write_invoice(tmp_path, "c.txt", text)
    handler, _ = scripted(_read("invoice_2117.txt"), critique={"concerns": [], "stricter": False})
    result = _run(db_path, path, handler)
    assert balances_due(text) == []
    assert "BALANCE_DUE" not in _codes(result)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert result["payment"].amount == Decimal("10625.00")


def test_previous_balance_on_2117_is_still_paid(tmp_path, db_path):
    text = (LOOP / "invoice_2117.txt").read_text(encoding="utf-8").rstrip() + "\nPrevious balance: $0.00\n"
    path = write_invoice(tmp_path, "p.txt", text)
    handler, _ = scripted(_read("invoice_2117.txt"), critique={"concerns": [], "stricter": False})
    result = _run(db_path, path, handler)
    assert "BALANCE_DUE" not in _codes(result)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"
    assert result["payment"].amount == Decimal("10625.00")


def test_balance_due_equal_to_the_2117_total_is_still_paid(tmp_path, db_path):
    text = (LOOP / "invoice_2117.txt").read_text(encoding="utf-8").rstrip() + "\nBalance Due: $10,625.00\n"
    path = write_invoice(tmp_path, "e.txt", text)
    handler, _ = scripted(_read("invoice_2117.txt"), critique={"concerns": [], "stricter": False})
    result = _run(db_path, path, handler)
    assert "BALANCE_DUE" not in _codes(result)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"


@pytest.mark.parametrize(
    "extra",
    [
        "Balance Due: $0.00",
        "Previous balance: $40.00",
        "Prior balance: $40.00",
        "Balance forward: $40.00",
        "Account balance: $40.00",
        "Balance Due: $40.00 credit",
        "Credit balance: $40.00",
    ],
)
def test_a_zero_or_carried_balance_on_a_clean_invoice_is_paid(tmp_path, db_path, extra):
    path = write_invoice(tmp_path, "c.txt", _clean(extra))
    handler, _ = scripted(_read("invoice_2118.txt", line_items=[_line(2, 250)], total="500.00"))
    result = _run(db_path, path, handler)
    assert "BALANCE_DUE" not in _codes(result)
    assert result["outcome"] == "approve"
    assert result["payment"].status == "paid"


def test_balance_due_equal_to_the_total_is_not_a_hold(tmp_path, db_path):
    text = "Vendor: Widgets Inc.\nWidgetA qty 2 @ 250\nTotal: $500.00\nBalance Due: $500.00\n"
    path = write_invoice(tmp_path, "b.txt", text)
    handler, _ = scripted(_read("invoice_2118.txt", line_items=[_line(2, 250)], total="500.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "approve"


def test_balance_due_in_a_json_field_is_held(tmp_path, db_path):
    text = json.dumps({"vendor": "Widgets Inc.", "items": ["WidgetA"], "total": 500.0, "amount_due": 300.0})
    path = write_invoice(tmp_path, "b.json", text)
    handler, _ = scripted(_read("invoice_2118.txt", line_items=[_line(2, 250)], total="500.00"))
    result = _run(db_path, path, handler)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "BALANCE_DUE"
    assert result["payment"].status == "not_paid"


@pytest.mark.parametrize("code", ["AMOUNT_WORDS_MISMATCH", "BALANCE_DUE"])
def test_page_amount_holds_cannot_be_released_for_payment(code):
    assert code not in PAYABLE_HOLD_CODES
    findings = [{"code": code, "message": "x", "severity": "warning"}]
    assert review_release_block("hold", findings, code) is not None


def test_critique_and_audit_holds_have_key_names(tmp_path, db_path):
    assert KEY_REASON_TO_CODE["Critique hold"] == "CRITIQUE_HOLD"
    assert KEY_REASON_TO_CODE["Audit escalation"] == "AUDIT_ESCALATED"
    path = LOOP / "invoice_2117.txt"
    concern = {
        "concerns": [
            {"category": "other", "text": "PO-7716 is not on file", "quote": "PO-7716. Quarterly restock"}
        ],
        "stricter": True,
        "innocent_reading": "A routine quarterly restock against a real PO.",
    }
    handler, _ = scripted(_read("invoice_2117.txt"), critique=concern)
    held = _run(db_path, path, handler)
    assert held["reason_code"] == "CRITIQUE_HOLD"
    row = grade(held, {"outcome": "HOLD", "primary_reason": "Critique hold"})
    assert row["outcome_ok"] and row["reason_ok"]
    assert held["payment"].status == "not_paid"

    handler, _ = scripted(_read("invoice_2117.txt"), audits=["SEND_BACK"])
    escalated = _run(db_path, path, handler)
    assert escalated["reason_code"] == "AUDIT_ESCALATED"
    row = grade(escalated, {"outcome": "HOLD", "primary_reason": "Audit escalation"})
    assert row["outcome_ok"] and row["reason_ok"]
    assert escalated["payment"].status == "not_paid"


def _line(quantity, price):
    return {
        "item": "WidgetA",
        "description": "WidgetA",
        "quantity": quantity,
        "unit_price": price,
        "amount": quantity * price,
        "note": None,
    }


def test_main_with_no_key_replays_a_loop_invoice(monkeypatch, capsys):
    from main import main

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_MODE", raising=False)
    assert main(["--invoice_path", str(LOOP / "invoice_2120.txt"), "--reset-ledger"]) == 0
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert last["outcome"] == "hold"
    assert last["reason_code"] == "BALANCE_DUE"
    assert last["payment_status"] == "not_paid"
