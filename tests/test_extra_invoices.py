"""Extra invoices. Parser and rules only, plus one fake-LLM pass.

The fake extracts whatever the deterministic reader already found. It does
not change tests/fixtures/fake_llm_eval.json, and it does not call a model.
"""

import json
from decimal import Decimal
from pathlib import Path

from db import SEED, create_database
from graph import run_invoice
from models import InvoiceLabel
from parsers import parse_file
from pipeline.audit import amount_in_text
from rules import APPROVAL_THRESHOLD_USD, assess
from scripts.eval import SETS, resolve_sets
from tests.fake_llm_eval import FakeClock, FakeLLM, ZeroRandom, handler_for

ROOT = Path(__file__).resolve().parents[1]
EXTRA = ROOT / "data" / "extra_invoices"
EXTRA_LABELS = ROOT / "data" / "extra_labels.json"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}


def _rows():
    payload = json.loads(EXTRA_LABELS.read_text(encoding="utf-8"))
    return [InvoiceLabel.model_validate(row) for row in payload["invoices"]]


def _files():
    return sorted(
        path for path in EXTRA.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES
    )


def test_eval_set_defaults_to_the_original_samples():
    assert resolve_sets([]) == ["samples"]
    assert resolve_sets(["--set", "samples"]) == ["samples"]
    assert resolve_sets(["--set", "extra"]) == ["extra"]
    assert resolve_sets(["--set", "all"]) == ["samples", "extra"]
    assert SETS["samples"][0] == ROOT / "data" / "invoices"
    assert SETS["extra"] == (EXTRA, EXTRA_LABELS)
    assert SETS["samples"][0] != SETS["extra"][0]


def test_every_extra_file_parses_and_matches_its_label():
    rows = _rows()
    files = _files()
    assert [row.file for row in rows] == [path.name for path in files]
    assert len(files) == 8
    stock = dict(SEED)
    for row, path in zip(rows, files):
        invoice = parse_file(path)
        assert invoice.warnings == []
        raw = invoice.source.raw_text or ""
        fresh = assess(invoice, raw, stock)
        assert fresh.outcome == row.outcome
        assert fresh.reason == row.reason
        if row.outcome == "approve":
            assert invoice.total is not None
            assert amount_in_text(invoice.total, raw)
    ocr_raw = (EXTRA / "invoice_2008.txt").read_text(encoding="utf-8")
    assert "2OO8" in ocr_raw
    assert "lB" in ocr_raw
    assert "6,150.00" in ocr_raw


def test_stock_sum_math_and_missing_field_match_their_labels():
    stock = dict(SEED)
    by_file = {row.file: row for row in _rows()}

    summed = parse_file(EXTRA / "invoice_2004.csv")
    assert [line.item for line in summed.line_items] == ["WidgetA", "WidgetA"]
    assert all(line.quantity <= stock["WidgetA"] for line in summed.line_items)
    assert sum(line.quantity for line in summed.line_items) == Decimal("17")
    summed_result = assess(summed, summed.source.raw_text or "", stock)
    assert [finding.code for finding in summed_result.findings] == ["stock_mismatch"]
    assert summed_result.outcome == by_file["invoice_2004.csv"].outcome == "reject"
    assert summed_result.reason == by_file["invoice_2004.csv"].reason

    mismatch = parse_file(EXTRA / "invoice_2005.xml")
    assert mismatch.shipping == Decimal("75.00")
    assert mismatch.subtotal + mismatch.tax_amount + mismatch.shipping == Decimal("2550.00")
    assert mismatch.total == Decimal("2475.00")
    mismatch_result = assess(mismatch, mismatch.source.raw_text or "", stock)
    assert [finding.code for finding in mismatch_result.findings] == ["math_mismatch"]
    assert mismatch_result.outcome == by_file["invoice_2005.xml"].outcome == "reject"
    assert mismatch_result.reason == by_file["invoice_2005.xml"].reason

    missing = parse_file(EXTRA / "invoice_2006.json")
    assert missing.vendor
    assert missing.line_items
    assert missing.total is not None
    assert missing.due_date is None
    missing_result = assess(missing, missing.source.raw_text or "", stock)
    assert [finding.code for finding in missing_result.findings] == ["missing_field"]
    assert missing_result.outcome == by_file["invoice_2006.json"].outcome == "reject"
    assert missing_result.reason == by_file["invoice_2006.json"].reason


def test_fake_llm_follows_the_rule_labels_and_the_over_10k_critique_loop(tmp_path):
    """Correct extraction keeps the rule label. Totals over 10000 enter critique once."""

    db_path = tmp_path / "inventory.db"
    create_database(db_path)
    stock = dict(SEED)
    for row, path in zip(_rows(), _files()):
        invoice = parse_file(path)
        result = run_invoice(
            path,
            db_path,
            FakeLLM(handler_for(path)),
            clock=FakeClock(),
            rng=ZeroRandom(),
            max_attempts=5,
        )
        assert result["outcome"] == row.outcome
        over = (
            invoice.currency == "USD"
            and invoice.total is not None
            and invoice.total > APPROVAL_THRESHOLD_USD
            and assess(invoice, invoice.source.raw_text or "", stock).outcome == "approve"
        )
        if over:
            assert result["critique_rounds"] == 1
            assert "critique" in [log["stage"] for log in result["logs"]]
        else:
            assert result["critique_rounds"] == 0
