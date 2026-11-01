"""Deterministic readers and the inventory seed. No model calls."""

import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from db import CREATE_INVENTORY, SEED, create_database, inventory_rows
from models import ApprovalDecision, ApprovalReasoning, ExtractedInvoice, LineItem, SourceInfo
from parsers import parse_file
from decision.invoice_fields import normalize_invoice_number, normalize_item_name

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"

PARSED = [
    ("invoice_1001.txt", "INV-1001", "5000.00", "Widgets Inc.", [("WidgetA", "10"), ("WidgetB", "5")]),
    ("invoice_1002.txt", "INV-1002", "15000.00", "Gadgets Co.", [("GadgetX", "20")]),
    ("invoice_1003.txt", "INV-1003", "100000.00", "Fraudster LLC", [("FakeItem", "100")]),
    ("invoice_1004.json", "INV-1004", "1890.00", "Precision Parts Ltd.", [("WidgetA", "3"), ("WidgetB", "2")]),
    ("invoice_1004_revised.json", "INV-1004", "5940.00", "Precision Parts Ltd.", [("WidgetA", "3"), ("WidgetB", "2"), ("GadgetX", "5")]),
    ("invoice_1005.json", "INV-1005", "15225.00", "Global Supply Chain Partners", [("WidgetA", "14"), ("GadgetX", "8"), ("WidgetB", "10")]),
    ("invoice_1006.csv", "INV-1006", "2750.00", "Acme Industrial Supplies", [("WidgetA", "5"), ("WidgetB", "3")]),
    ("invoice_1007.csv", "INV-1007", "15525.00", "MegaWidgets Corp", [("WidgetA", "20"), ("WidgetB", "15"), ("GadgetX", "3")]),
    ("invoice_1008.txt", "INV-1008", "9900.00", "NoProd Industries", [("SuperGizmo", "12"), ("MegaSprocket", "6")]),
    ("invoice_1009.json", "INV-1009", "-250.00", None, [("WidgetA", "-5"), ("WidgetB", "2")]),
    ("invoice_1010.txt", "INV-1010", "7185.00", "Consolidated Materials Group", [("WidgetA", "8"), ("WidgetB", "4"), ("GadgetX", "2"), ("WidgetA", "4")]),
    ("invoice_1011.pdf", "INV-1011", "3000.00", "Summit Manufacturing Co.", [("WidgetA", "6"), ("WidgetB", "3")]),
    ("invoice_1011.txt", "INV-1011", "3000.00", "Summit Manufacturing Co.", [("WidgetA", "6"), ("WidgetB", "3")]),
    ("invoice_1012.pdf", "INV-1012", "9975.00", "QuickShip Distributers (formerly FastShip Ltd.)", [("WidgetA", "12"), ("WidgetB", "7"), ("GadgetX", "4")]),
    ("invoice_1012.txt", "INV-1012", "9975.00", "QuickShip Distributers (formerly FastShip Ltd.)", [("WidgetA", "12"), ("WidgetB", "7"), ("GadgetX", "4")]),
    ("invoice_1013.json", "INV-1013", "22562.80", "Atlas Industrial Supply", [("WidgetA", "15"), ("WidgetB", "10"), ("GadgetX", "5"), ("WidgetA", "5"), ("WidgetB", "8"), ("GadgetX", "3"), ("WidgetA", "2"), ("GadgetX", "1")]),
    ("invoice_1013.pdf", "INV-1013", "22562.80", "Atlas Industrial Supply", [("WidgetA", "15"), ("WidgetB", "10"), ("GadgetX", "5"), ("WidgetA", "5"), ("WidgetB", "8"), ("GadgetX", "3"), ("WidgetA", "2"), ("GadgetX", "1")]),
    ("invoice_1014.xml", "INV-1014", "4125.00", "TechParts International", [("WidgetA", "4"), ("WidgetB", "6")]),
    ("invoice_1015.csv", "INV-1015", "6500.00", "Reliable Components Inc.", [("WidgetA", "10"), ("WidgetB", "5"), ("GadgetX", "2")]),
    ("invoice_1016.json", "INV-1016", "3233.00", "Widgets Inc.", [("WidgetA", "4"), ("WidgetB", "2"), ("WidgetC", "3")]),
]


def test_database_seed_matches_the_readme():
    assert CREATE_INVENTORY == (
        "CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)"
    )
    assert SEED == (("WidgetA", 15), ("WidgetB", 10), ("GadgetX", 5), ("FakeItem", 0))


def test_setup_script_is_idempotent(tmp_path):
    db_path = tmp_path / "inventory.db"
    command = [sys.executable, str(ROOT / "setup_db.py"), str(db_path)]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "4 rows" in first.stdout
    assert inventory_rows(db_path) == [
        ("FakeItem", 0),
        ("GadgetX", 5),
        ("WidgetA", 15),
        ("WidgetB", 10),
    ]
    connection = sqlite3.connect(db_path)
    try:
        info = connection.execute("PRAGMA table_info(inventory)").fetchall()
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    assert [(row[1], row[2], row[5]) for row in info] == [("item", "TEXT", 1), ("stock", "INTEGER", 0)]
    assert tables == [("inventory",)]


def test_models_round_trip_and_reject_a_bad_decision():
    invoice = ExtractedInvoice(
        vendor="Widgets Inc.",
        invoice_number="INV-1001",
        currency="usd",
        total=Decimal("5000"),
        line_items=[LineItem(item="WidgetA", description="WidgetA", quantity=Decimal("10"), unit_price=Decimal("250"))],
        source=SourceInfo(path="invoice_1001.txt", filename="invoice_1001.txt", format="txt"),
    )
    again = ExtractedInvoice.model_validate(invoice.model_dump(mode="json"))
    assert again == invoice
    assert again.currency == "USD"
    decision = ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(
            rules_applied=["stock"],
            evidence=["total 5000.00 USD"],
            risks_considered=["none"],
            conclusion="approve",
        ),
    )
    assert decision.revisions == []
    held = ApprovalDecision(
        decision="hold",
        reasoning=ApprovalReasoning(
            rules_applied=["stock"],
            evidence=["x"],
            risks_considered=["none"],
            conclusion="hold",
        ),
    )
    assert held.decision == "hold"
    with pytest.raises(ValidationError):
        ApprovalDecision(
            decision="manual_review",
            reasoning=ApprovalReasoning(
                rules_applied=["stock"],
                evidence=["x"],
                risks_considered=["none"],
                conclusion="no",
            ),
        )


def test_name_normalization():
    assert normalize_invoice_number("1002") == "INV-1002"
    assert normalize_invoice_number("INV 1012") == "INV-1012"
    assert normalize_item_name("Widget A") == ("WidgetA", None)
    assert normalize_item_name("widget-a") == ("widgeta", None)
    assert normalize_item_name("WidgetA (rush order)") == ("WidgetA", "rush order")


@pytest.mark.parametrize("filename, number, total, vendor, quantities", PARSED)
def test_parser_extracts_printed_fields(filename, number, total, vendor, quantities):
    invoice = parse_file(INVOICES / filename)
    assert invoice.invoice_number == number
    assert invoice.total == Decimal(total)
    assert invoice.vendor == vendor
    assert invoice.warnings == []
    assert [(item.item, item.quantity) for item in invoice.line_items] == [
        (name, Decimal(qty)) for name, qty in quantities
    ]
    expected_currency = "EUR" if filename == "invoice_1014.xml" else "USD"
    assert invoice.currency == expected_currency


def test_parser_keeps_the_traps_visible():
    short = parse_file(INVOICES / "invoice_1002.txt")
    assert short.invoice_number_raw == "1002"

    fraud = parse_file(INVOICES / "invoice_1003.txt")
    assert fraud.due_date == "yesterday"
    assert "Wire transfer" in fraud.notes

    broken = parse_file(INVOICES / "invoice_1007.csv")
    assert broken.total == Decimal("15525.00")
    assert broken.subtotal + broken.tax_amount != broken.total

    rush = parse_file(INVOICES / "invoice_1010.txt")
    assert rush.shipping == Decimal("150.00")
    assert rush.line_items[-1].note == "rush order"
    assert rush.invoice_date == "January 27, 2026"

    email = parse_file(INVOICES / "invoice_1008.txt")
    assert email.source.format == "email"

    ocr = parse_file(INVOICES / "invoice_1012.txt")
    assert "2O26" in ocr.source.raw_text
    assert ocr.invoice_date == "26-Jan-2026"
    assert ocr.line_items[1].amount == Decimal("3500.00")

    padded = parse_file(INVOICES / "invoice_1013.json")
    assert padded.subtotal + padded.tax_amount == Decimal("22512.80")
    assert padded.total - (padded.subtotal + padded.tax_amount) == Decimal("50.00")

    revised = parse_file(INVOICES / "invoice_1004_revised.json")
    assert revised.revision == "R1"

    from parsers.text_invoice import parse_text
    from models import SourceInfo

    broken = (
        "Vendor: Widgets Inc.\n"
        "Invoice Number: INV-1\n"
        "Due Date: 2026-02-01\n"
        "WidgetA qty: 1 unit price: $100.00\n"
        "Total: $2O0.00\n"
    )
    kept = parse_text(broken, SourceInfo(path="x.txt", filename="x.txt", format="txt", raw_text=broken))
    assert kept.total is None
    assert "2O0" in broken
