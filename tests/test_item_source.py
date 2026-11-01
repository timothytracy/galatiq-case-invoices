"""Item names must be in the source text. An unknown item may carry a hint.

The hint never changes the line. A name missing from the source is a hold, and a
reject still wins when both apply.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from db import SEED, create_database
from decision.findings import assess
from decision.invoice_fields import item_key, normalize_item_name
from decision.wording import ACTION, KEY_REASON_TO_CODE
from graph import run_invoice
from models import ExtractedInvoice, LineItem, SourceInfo
from parsers import parse_file
from scripts.eval import grade, load_answer_key
from tests.fake_llm_eval import handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom, task_of

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
STOCK = dict(SEED)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "inventory.db"
    create_database(path)
    return path


def _invoice(item, source, *, note=None, description=None, items=None):
    """One clean USD invoice. `items` overrides the single line when set."""

    if items is None:
        items = [(item, note, description or item)]
    lines = []
    total = Decimal("0")
    for name, line_note, label in items:
        amount = Decimal("250")
        total += amount
        lines.append(
            LineItem(
                item=name,
                description=label,
                quantity=Decimal("1"),
                unit_price=amount,
                amount=amount,
                note=line_note,
            )
        )
    return ExtractedInvoice(
        vendor="Widgets Inc.",
        due_date="2026-04-30",
        currency="USD",
        subtotal=total,
        tax_amount=Decimal("0"),
        shipping=Decimal("0"),
        total=total,
        line_items=lines,
        source=SourceInfo(path="x.txt", filename="x.txt", format="txt", raw_text=source),
    )


def _names(invoice):
    return [(line.item, line.note) for line in invoice.line_items]


def _codes(assessment):
    return [finding.code for finding in assessment.findings]


@pytest.mark.parametrize(
    "source",
    ["WidgetA", "WIDGETA", "widget a", "Widget-A", "widget  -  A"],
)
def test_a_printed_name_matches_after_case_spaces_and_hyphens(source):
    invoice = _invoice("WidgetA", source)
    before = _names(invoice)
    assessed = assess(invoice, source, STOCK)
    assert item_key(source) == "widgeta"
    assert item_key("WidgetA") == "widgeta"
    assert assessed.outcome == "approve"
    assert assessed.reason_code is None
    assert "ITEM_NOT_IN_SOURCE" not in _codes(assessed)
    assert _names(invoice) == before


def test_a_name_missing_from_the_source_is_an_unpaid_hold(tmp_path, db_path):
    source = "Vendor: Acme Supply\nDue Date: 2026-04-30\nTotal: 250.00\n"
    path = tmp_path / "missing.txt"
    path.write_text(source, encoding="utf-8")
    body = json.dumps(
        {
            "vendor": "Widgets Inc.",
            "invoice_number": "INV-9",
            "due_date": "2026-04-30",
            "currency": "USD",
            "line_items": [
                {
                    "item": "WidgetA",
                    "description": "WidgetA",
                    "quantity": 1,
                    "unit_price": 250,
                    "amount": 250,
                }
            ],
            "subtotal": 250,
            "tax_amount": 0,
            "shipping": 0,
            "total": 250,
        }
    )

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            return body
        if name == "reason":
            return json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
        if name == "audit":
            return json.dumps({"verdict": "PASS", "feedback": [], "notes": "PASS"})
        raise AssertionError(name)

    result = run_invoice(
        path,
        db_path,
        FakeLLM(handler),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=3,
    )
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "ITEM_NOT_IN_SOURCE"
    source_miss = next(finding for finding in result["findings"] if finding.code == "ITEM_NOT_IN_SOURCE")
    assert source_miss.severity == "warning"
    assert result["action"] == "Check the item name against the invoice."
    assert result["action"] == ACTION["ITEM_NOT_IN_SOURCE"]
    assert "WidgetA is not in the invoice text." in result["decision"].reasoning.conclusion
    assert result["payment"].status == "not_paid"
    assert result["invoice"].line_items[0].item == "WidgetA"


def test_a_split_descriptor_checks_the_base_name():
    """'WidgetA (rush order)' is stored as WidgetA. The base name must be in the source."""

    code, note = normalize_item_name("WidgetA (rush order)")
    assert (code, note) == ("WidgetA", "rush order")

    printed = _invoice(code, "WidgetA (rush order)", note=note, description="WidgetA (rush order)")
    found = assess(printed, printed.source.raw_text, STOCK)
    assert "ITEM_NOT_IN_SOURCE" not in _codes(found)
    assert found.outcome == "approve"
    assert printed.line_items[0].item == "WidgetA"
    assert printed.line_items[0].note == "rush order"

    base_only = _invoice(code, "WidgetA", note=note)
    bare = assess(base_only, base_only.source.raw_text, STOCK)
    assert "ITEM_NOT_IN_SOURCE" not in _codes(bare)

    other = _invoice(code, "GadgetX (rush order)", note=note, description="WidgetA (rush order)")
    missed = assess(other, other.source.raw_text, STOCK)
    assert missed.outcome == "hold"
    assert missed.reason_code == "ITEM_NOT_IN_SOURCE"
    assert other.line_items[0].item == "WidgetA"
    assert "WidgetA is not in the invoice text." in missed.reason


def test_missing_source_text_fails_closed():
    """Every supported format has source text. An empty source is a hold, not a skip."""

    invoice = _invoice("WidgetA", "")
    assessed = assess(invoice, "", STOCK)
    assert assessed.outcome == "hold"
    assert assessed.reason_code == "ITEM_NOT_IN_SOURCE"
    assert assessed.action == "Check the item name against the invoice."
    source_miss = next(finding for finding in assessed.findings if finding.code == "ITEM_NOT_IN_SOURCE")
    revised = assess(_invoice("WidgetA", "WidgetA\nRevised invoice"), "WidgetA\nRevised invoice", STOCK)
    other_hold = next(finding for finding in revised.findings if finding.code == "REVISED_INVOICE")
    assert source_miss.severity == other_hold.severity == "warning"
    assert invoice.line_items[0].item == "WidgetA"


def test_unknown_and_missing_from_the_source_stays_a_reject(tmp_path, db_path):
    """Reject wins. UNKNOWN_ITEM is a blocking reject, so it beats the hold.

    The vendor still has to send a real item name. The source miss is recorded,
    nothing is paid, and the extracted name is left as it is.
    """

    source = "Vendor: Acme Supply\nDue Date: 2026-04-30\nTotal: 250.00\n"
    path = tmp_path / "both.txt"
    path.write_text(source, encoding="utf-8")
    body = json.dumps(
        {
            "vendor": "Widgets Inc.",
            "invoice_number": "INV-9",
            "due_date": "2026-04-30",
            "currency": "USD",
            "line_items": [
                {
                    "item": "NopeItem",
                    "description": "NopeItem",
                    "quantity": 1,
                    "unit_price": 250,
                    "amount": 250,
                }
            ],
            "subtotal": 250,
            "tax_amount": 0,
            "shipping": 0,
            "total": 250,
        }
    )

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            return body
        raise AssertionError(name)

    result = run_invoice(
        path,
        db_path,
        FakeLLM(handler),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=3,
    )
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "UNKNOWN_ITEM"
    assert "ITEM_NOT_IN_SOURCE" in [finding.code for finding in result["findings"]]
    assert "NopeItem is not in the invoice text." in result["decision"].reasoning.conclusion
    assert result["action"] == "Resend with the correct item name."
    assert result["payment"].status == "not_paid"
    assert result["invoice"].line_items[0].item == "NopeItem"


def test_likely_meant_hint_needs_exactly_one_inventory_name():
    """One edit away, and only one such name. The line item is never replaced."""

    one = _invoice("WidgitA", "WidgitA")
    hinted = assess(one, "WidgitA", STOCK)
    assert hinted.outcome == "reject"
    assert hinted.reason_code == "UNKNOWN_ITEM"
    assert hinted.reason == (
        "WidgitA is not in the inventory database. Likely meant WidgetA. "
        "Resend with the correct item name."
    )
    assert one.line_items[0].item == "WidgitA"

    folded = _invoice("Widgit-A", "Widgit-A")
    folded_hit = assess(folded, "Widgit-A", STOCK)
    assert "Likely meant WidgetA." in folded_hit.reason
    assert folded.line_items[0].item == "Widgit-A"

    spaced = assess(_invoice("WIDGIT A", "WIDGIT A"), "WIDGIT A", {"Widget A": 15})
    assert "Likely meant Widget A." in spaced.reason

    tie = assess(_invoice("WidgetC", "WidgetC"), "WidgetC", STOCK)
    assert tie.outcome == "reject"
    assert tie.reason_code == "UNKNOWN_ITEM"
    assert tie.reason == "WidgetC is not in the inventory database. Resend with the correct item name."
    assert "Likely meant" not in tie.reason

    none = _invoice(
        "SuperGizmo",
        "SuperGizmo MegaSprocket",
        items=[
            ("SuperGizmo", None, "SuperGizmo"),
            ("MegaSprocket", None, "MegaSprocket"),
        ],
    )
    blank = assess(none, none.source.raw_text, STOCK)
    assert blank.reason_code == "UNKNOWN_ITEM"
    assert "Likely meant" not in blank.reason
    assert _names(none) == [("SuperGizmo", None), ("MegaSprocket", None)]

    made_up = assess(_invoice("Sprockets", "Sprockets"), "Sprockets", {"Sprocket": 4})
    assert "Likely meant Sprocket." in made_up.reason
    assert made_up.reason_code == "UNKNOWN_ITEM"


def test_extraction_prompt_copies_item_names_as_printed():
    from pipeline.ingestion import _extract_messages

    text = _extract_messages("Vendor: Widgets Inc.", [], date(2026, 10, 31))[0]["content"]
    assert (
        "Copy each item name exactly as printed; don't correct spelling or match it to a catalog."
        in text
    )


def test_offline_runs_keep_the_41_answer_key_outcomes(db_path):
    """Deterministic extraction of every sample and extra file keeps its key outcome."""

    folders = (
        (ROOT / "data" / "invoices", ROOT / "data" / "answer_key.json"),
        (ROOT / "data" / "extra_invoices", ROOT / "data" / "extra_answer_key.json"),
    )
    seen = 0
    for folder, key_path in folders:
        key = load_answer_key(key_path)
        files = sorted(
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in SUFFIXES
        )
        for path in files:
            seen += 1
            expected = key[f"data/{folder.name}/{path.name}"]
            invoice = parse_file(path)
            assert invoice.source.raw_text, path.name
            before = _names(invoice)
            assessed = assess(invoice, invoice.source.raw_text, STOCK)
            assert _names(invoice) == before
            assert "ITEM_NOT_IN_SOURCE" not in _codes(assessed), path.name
            # 2107's live reject is a separate strict xfail. This loop still checks the name.
            if path.name == "invoice_2107.pdf":
                continue
            assert assessed.outcome.upper() == expected["outcome"], path.name
            assert assessed.reason_code == KEY_REASON_TO_CODE[expected["primary_reason"]], path.name
            result = run_invoice(
                path,
                db_path,
                FakeLLM(handler_for(path)),
                clock=FakeClock(),
                rng=ZeroRandom(),
                max_attempts=5,
            )
            row = grade(result, expected)
            assert row["outcome_ok"] and row["reason_ok"], (path.name, row)
            if result["outcome"] == "approve":
                assert result["payment"].status == "paid", path.name
            else:
                assert result["payment"].status == "not_paid", path.name
            if path.name == "invoice_2108.txt":
                assert "Likely meant WidgetA." in assessed.reason
                assert result["invoice"].line_items[0].item == "WidgitA"
                assert "Likely meant WidgetA." in result["decision"].reasoning.conclusion
            if path.name in {"invoice_1008.txt", "invoice_1016.json"}:
                assert "Likely meant" not in assessed.reason
                assert "Likely meant" not in result["decision"].reasoning.conclusion
    assert seen == 41


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The live system rejects 2107 as EMBEDDED_INSTRUCTIONS, "
        "and the offline reader never sets that flag."
    ),
)
def test_offline_2107_matches_the_live_reject(db_path):
    """Offline extraction approves and pays 2107. The answer key rejects it."""

    path = ROOT / "data" / "extra_invoices" / "invoice_2107.pdf"
    expected = load_answer_key(ROOT / "data" / "extra_answer_key.json")[
        "data/extra_invoices/invoice_2107.pdf"
    ]
    invoice = parse_file(path)
    assessed = assess(invoice, invoice.source.raw_text or "", STOCK)
    assert assessed.outcome.upper() == expected["outcome"]
    assert assessed.reason_code == KEY_REASON_TO_CODE[expected["primary_reason"]]
    result = run_invoice(
        path,
        db_path,
        FakeLLM(handler_for(path)),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=5,
    )
    row = grade(result, expected)
    assert row["outcome_ok"] and row["reason_ok"], row
    assert result["payment"].status == "not_paid"
