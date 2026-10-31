"""Phase 1 tests: CLI, seed database, shared shapes, offline client, and the answer key."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from db import CREATE_INVENTORY, SEED, create_database, inventory_rows
from llm_client import BASE_URL, DEFAULT_MODEL, GrokClient
from models import (
    ApprovalDecision,
    BatchSummary,
    ExtractedInvoice,
    Finding,
    InvoiceLabel,
    LineItem,
    SourceInfo,
    StageRecord,
    ValidationReport,
)
from parsers import parse_file
from pipeline import approve, validate
from rules import APPROVAL_THRESHOLD_USD, EUR_USD_RATE, normalize_invoice_number, normalize_item_name, to_usd

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"
LABELS = ROOT / "data" / "labels.json"

README_CASES = {
    "invoice_1001.txt": ("approve", "passes_validation", None),
    "invoice_1004.json": ("approve", "passes_validation", None),
    "invoice_1006.csv": ("approve", "passes_validation", None),
    "invoice_1002.txt": ("reject", "stock_mismatch", "stock"),
    "invoice_1003.txt": ("reject", "fake_item", "FakeItem"),
    "invoice_1008.txt": ("reject", "unknown_item", "SuperGizmo"),
    "invoice_1016.json": ("reject", "unknown_item", "WidgetC"),
    "invoice_1009.json": ("reject", "negative_quantity", "negative"),
}

APPROVED_FILES = [
    "invoice_1001.txt",
    "invoice_1004.json",
    "invoice_1006.csv",
    "invoice_1010.txt",
    "invoice_1011.pdf",
    "invoice_1014.xml",
    "invoice_1015.csv",
]

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


def load_labels():
    data = json.loads(LABELS.read_text(encoding="utf-8"))
    rows = [InvoiceLabel.model_validate(row) for row in data["invoices"]]
    return data, rows


def run_cli(tmp_path, *args, extra_env=None):
    env = os.environ.copy()
    env.pop("XAI_API_KEY", None)
    env["INVOICE_OFFLINE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def json_records(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def test_labels_cover_every_sample_and_the_readme_table():
    data, rows = load_labels()
    sample = sorted(path.name for path in INVOICES.iterdir() if path.is_file())
    assert len(sample) == 20
    assert [row.file for row in rows] == sample
    by_file = {row.file: row for row in rows}

    for filename, (decision, scenario, word) in README_CASES.items():
        row = by_file[filename]
        assert row.decision == decision
        assert row.scenario == scenario
        assert row.needs_human is False
        if word:
            assert word.lower() in row.reason.lower()

    assert "MegaSprocket" in by_file["invoice_1008.txt"].reason
    assert [row.file for row in rows if row.decision == "approve"] == APPROVED_FILES
    assert sorted(row.file for row in rows if row.needs_human) == [
        "invoice_1004_revised.json",
        "invoice_1012.pdf",
    ]
    assert all(row.invoice_number.startswith("INV-") for row in rows)


def test_labels_record_the_policy_defaults():
    data, rows = load_labels()
    policies = data["policies"]
    assert "snapshot" in policies["stock"].lower()
    assert "does not decrease" in policies["stock"].lower()
    assert "never paid twice" in policies["duplicates"].lower()
    assert "1.10" in policies["fx"]
    assert policies["eur_usd_rate"] == str(EUR_USD_RATE)
    assert Decimal(policies["approval_threshold_usd"]) == APPROVAL_THRESHOLD_USD
    assert "pipeline.py" in policies["orchestrator"]
    by_file = {row.file: row for row in rows}
    assert by_file["invoice_1004_revised.json"].scenario == "duplicate_or_revision"
    assert by_file["invoice_1011.pdf"].decision == "approve"
    assert by_file["invoice_1011.txt"].scenario == "duplicate"
    assert by_file["invoice_1012.txt"].scenario == "duplicate"
    assert by_file["invoice_1012.pdf"].scenario == "vendor_rename"
    assert by_file["invoice_1014.xml"].decision == "approve"
    assert by_file["invoice_1013.json"].decision == "reject"
    assert by_file["invoice_1013.pdf"].decision == "reject"


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
    decision = ApprovalDecision(decision="approve", reasons=["ok"])
    assert decision.needs_human is False
    with pytest.raises(ValidationError):
        ApprovalDecision(decision="hold", reasons=["no"])
    with pytest.raises(ValidationError):
        ApprovalDecision(decision="reject", reasons=[])


def test_money_rules_and_name_normalization():
    assert to_usd(Decimal("4125"), "EUR") == Decimal("4537.50")
    assert to_usd(Decimal("5000"), "USD") == Decimal("5000.00")
    assert to_usd(Decimal("10"), "GBP") is None
    assert normalize_invoice_number("1002") == "INV-1002"
    assert normalize_invoice_number("INV 1012") == "INV-1012"
    assert normalize_item_name("Widget A") == ("WidgetA", None)
    assert normalize_item_name("WidgetA (rush order)") == ("WidgetA", "rush order")


def test_client_is_offline_without_a_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("INVOICE_OFFLINE", raising=False)
    client = GrokClient()
    result = client.complete([{"role": "user", "content": "ping"}])
    assert client.offline is True
    assert client.model == DEFAULT_MODEL
    assert result.offline is True
    assert result.text == ""
    assert result.latency_ms == 0
    assert BASE_URL == "https://api.x.ai/v1"
    assert DEFAULT_MODEL == "grok-4.7"


def test_offline_flag_does_not_use_the_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("network call")

    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setenv("XAI_MODEL", "grok-3")
    client = GrokClient(api_key="not-a-real-key", offline=True)
    result = client.complete([{"role": "user", "content": "ping"}])
    assert client.model == "grok-3"
    assert result.offline is True
    assert "not-a-real-key" not in repr(client)


def test_env_var_forces_offline_when_a_key_is_present(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "present-but-unused")
    monkeypatch.setenv("INVOICE_OFFLINE", "1")
    client = GrokClient()
    assert client.offline is True
    assert "present-but-unused" not in repr(client)
    forced_off = GrokClient(api_key="", offline=False)
    assert forced_off.offline is True


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


def test_stub_does_not_apply_the_answer_key(tmp_path):
    db_path = create_database(tmp_path / "inventory.db")
    mismatch = parse_file(INVOICES / "invoice_1002.txt")
    decision = approve(mismatch, validate(mismatch, db_path))
    assert decision.decision == "approve"
    assert decision.needs_human is False

    euros = parse_file(INVOICES / "invoice_1014.xml")
    euro_decision = approve(euros, validate(euros, db_path))
    assert euro_decision.amount_usd == Decimal("4537.50")
    assert euro_decision.amount == Decimal("4125.00")

    broken = parse_file(INVOICES / "invoice_1009.json")
    rejected = approve(broken, validate(broken, db_path))
    assert rejected.decision == "reject"
    assert "missing vendor" in rejected.reasons
    assert "total is not positive" in rejected.reasons

    clean = parse_file(INVOICES / "invoice_1001.txt")
    failed = ValidationReport(
        passed=False,
        findings=[Finding(code="stock_mismatch", message="short")],
    )
    held = approve(clean, failed)
    assert held.decision == "reject"
    assert "validation reported a failure" in held.reasons


def test_cli_single_file_offline(tmp_path):
    invoice = INVOICES / "invoice_1001.txt"
    proc = run_cli(tmp_path, f"--invoice_path={invoice}", "--offline")
    assert proc.returncode == 0, proc.stderr
    assert "Paid 5000.00 to Widgets Inc." in proc.stdout
    records = [row for row in json_records(proc.stdout) if row.get("record") == "stage"]
    assert [row["stage"] for row in records] == ["ingestion", "validation", "approval", "payment"]
    for row in records:
        StageRecord.model_validate(row)
        assert row["offline"] is True
        assert row["invoice_file"] == "invoice_1001.txt"
    assert records[0]["stub"] is False
    assert records[1]["stub"] is True
    assert records[1]["result"]["passed"] is None
    assert records[2]["stub"] is True
    assert records[2]["result"]["decision"] == "approve"
    assert records[2]["result"]["amount_usd"] == "5000.00"
    assert records[3]["result"]["status"] == "paid"
    assert "WidgetA (15)" in records[1]["result"]["findings"][0]["message"]

    saved = json.loads((tmp_path / "logs" / "invoice_1001.txt.json").read_text())
    assert [row["stage"] for row in saved] == ["ingestion", "validation", "approval", "payment"]
    summary = BatchSummary.model_validate_json((tmp_path / "logs" / "summary.json").read_text())
    assert summary.processed == 1
    assert summary.paid == 1
    assert inventory_rows(tmp_path / "inventory.db")[2] == ("WidgetA", 15)
    assert len(inventory_rows(tmp_path / "inventory.db")) == 4


def test_cli_folder_offline(tmp_path):
    proc = run_cli(tmp_path, f"--invoice_path={INVOICES}", "--offline")
    assert proc.returncode == 0, proc.stderr
    summary = BatchSummary.model_validate_json((tmp_path / "logs" / "summary.json").read_text())
    assert summary.processed == 20
    assert summary.approved + summary.rejected == 20
    stage_files = [path for path in (tmp_path / "logs").glob("*.json") if path.name != "summary.json"]
    assert len(stage_files) == 20
    for name in ("invoice_1004.json.json", "invoice_1006.csv.json", "invoice_1011.pdf.json", "invoice_1014.xml.json"):
        saved = json.loads((tmp_path / "logs" / name).read_text())
        assert [row["stage"] for row in saved] == ["ingestion", "validation", "approval", "payment"]


def test_cli_batch_flag_offline(tmp_path):
    proc = run_cli(tmp_path, f"--batch={INVOICES}", "--offline")
    assert proc.returncode == 0, proc.stderr
    summary = BatchSummary.model_validate_json((tmp_path / "logs" / "summary.json").read_text())
    assert summary.processed == 20
    assert [row.file for row in summary.invoices] == sorted(path.name for path in INVOICES.iterdir())


def test_cli_offline_does_not_print_a_key(tmp_path):
    invoice = INVOICES / "invoice_1001.txt"
    proc = run_cli(
        tmp_path,
        f"--invoice_path={invoice}",
        "--offline",
        extra_env={"XAI_API_KEY": "should-not-appear", "INVOICE_OFFLINE": "0"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "should-not-appear" not in proc.stdout
    assert "should-not-appear" not in proc.stderr
    assert json_records(proc.stdout)[0]["offline"] is True


def test_cli_rejects_a_missing_path_and_mixed_flags(tmp_path):
    missing = run_cli(tmp_path, "--invoice_path=does-not-exist.txt")
    assert missing.returncode == 2
    mixed = run_cli(tmp_path, "--invoice_path=data", "--batch=data")
    assert mixed.returncode == 2
    not_a_folder = run_cli(tmp_path, f"--batch={INVOICES / 'invoice_1001.txt'}")
    assert not_a_folder.returncode == 2
