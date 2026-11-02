"""Inventory auto-setup. These tests use a temporary file, not the repo database."""

import sqlite3
import subprocess
import sys
from pathlib import Path

from db import SEED, create_database, ensure_inventory_db, load_stock
from llm_client import MissingAPIKey

ROOT = Path(__file__).resolve().parents[1]
SEEDED = sorted(SEED)


def test_ensure_creates_a_missing_database_and_leaves_an_existing_one(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    assert not path.exists()
    assert ensure_inventory_db(path) == path
    assert load_stock(path) == SEEDED
    created = path.read_bytes()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE inventory SET stock = 1")
    connection.commit()
    connection.close()
    edited = path.read_bytes()
    assert edited != created

    def explode(*_args, **_kwargs):
        raise AssertionError("an existing inventory file must not be opened")

    monkeypatch.setattr(sqlite3, "connect", explode)
    assert ensure_inventory_db(path) == path
    assert path.read_bytes() == edited


def test_setup_script_still_creates_the_seed(tmp_path):
    path = tmp_path / "via-setup.db"
    command = [sys.executable, str(ROOT / "setup_db.py"), str(path)]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert "4 rows" in first.stdout
    assert load_stock(path) == SEEDED
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode == 0, second.stderr
    assert load_stock(path) == SEEDED


def test_setup_script_rebuilds_an_empty_database(tmp_path):
    path = tmp_path / "empty.db"
    path.write_bytes(b"")
    command = [sys.executable, str(ROOT / "setup_db.py"), str(path)]
    rebuilt = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert "Traceback" not in rebuilt.stderr
    assert load_stock(path) == SEEDED

    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM inventory")
    connection.commit()
    connection.close()
    again = subprocess.run(command, capture_output=True, text=True, check=False)
    assert again.returncode == 0, again.stderr
    assert load_stock(path) == SEEDED


def _fake_result(*_args, **_kwargs):
    return {
        "outcome": "hold",
        "reason_code": None,
        "action": None,
        "logs": [],
        "payment": None,
        "findings": [],
        "attempts": [],
    }


def test_reset_db_rebuilds_inventory_and_keeps_the_ledger(tmp_path, monkeypatch, inventory_db):
    from main import main

    monkeypatch.chdir(tmp_path)
    connection = sqlite3.connect(inventory_db)
    connection.execute("UPDATE inventory SET stock = 0")
    connection.commit()
    connection.close()
    edited = inventory_db.read_bytes()
    ledger = tmp_path / "payments.db"
    ledger.write_bytes(b"keep")
    invoice = tmp_path / "a.txt"
    invoice.write_text("Vendor: Widgets Inc.\n", encoding="utf-8")
    monkeypatch.setattr("llm_client.GrokClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("main.run_invoice", _fake_result)

    assert main(["--invoice_path", str(tmp_path / "missing.txt"), "--reset"]) == 2
    assert inventory_db.read_bytes() == edited
    assert ledger.read_bytes() == b"keep"

    def boom(*_args, **_kwargs):
        raise MissingAPIKey("no key")

    monkeypatch.setattr("llm_client.GrokClient", boom)
    assert main(["--invoice_path", str(invoice), "--reset-db", "--reset-ledger"]) == 1
    assert inventory_db.read_bytes() == edited
    assert ledger.read_bytes() == b"keep"

    monkeypatch.setattr("llm_client.GrokClient", lambda *_args, **_kwargs: object())
    assert main(["--invoice_path", str(invoice), "--reset-db"]) == 0
    assert load_stock(inventory_db) == SEEDED
    assert ledger.read_bytes() == b"keep"

    connection = sqlite3.connect(inventory_db)
    connection.execute("UPDATE inventory SET stock = 4")
    connection.commit()
    connection.close()
    ledger.write_bytes(b"keep")
    assert main(["--invoice_path", str(invoice), "--reset-db", "--reset-ledger"]) == 0
    assert load_stock(inventory_db) == SEEDED
    assert not ledger.exists()

    ledger.write_bytes(b"keep")
    connection = sqlite3.connect(inventory_db)
    connection.execute("UPDATE inventory SET stock = 4")
    connection.commit()
    connection.close()
    assert main(["--invoice_path", str(invoice), "--reset"]) == 0
    assert load_stock(inventory_db) == SEEDED
    assert not ledger.exists()


def test_main_creates_a_missing_inventory_file(tmp_path, monkeypatch):
    from main import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INVENTORY_DB", raising=False)
    (tmp_path / "inventory.db").unlink()
    invoice = tmp_path / "a.txt"
    invoice.write_text("Vendor: Widgets Inc.\n", encoding="utf-8")
    monkeypatch.setattr("llm_client.GrokClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("main.run_invoice", _fake_result)
    root_db = ROOT / "inventory.db"
    before = root_db.read_bytes() if root_db.is_file() else None
    assert main(["--invoice_path", str(invoice)]) == 0
    assert load_stock(tmp_path / "inventory.db") == SEEDED
    after = root_db.read_bytes() if root_db.is_file() else None
    assert after == before


def test_eval_reset_db_rebuilds_without_a_setup_step(tmp_path, monkeypatch, inventory_db, capsys):
    from scripts.eval import main as eval_main

    monkeypatch.chdir(tmp_path)
    connection = sqlite3.connect(inventory_db)
    connection.execute("DELETE FROM inventory")
    connection.commit()
    connection.close()
    ledger = tmp_path / "payments.db"
    ledger.write_bytes(b"keep")
    monkeypatch.setattr("scripts.eval.run_invoice", _fake_result)
    empty = tmp_path / "invoices"
    empty.mkdir()
    recording = str(ROOT / "recordings" / "samples_tools.jsonl")

    assert eval_main(["--replay", str(tmp_path / "missing.jsonl"), "--reset-db"]) == 1
    assert load_stock(inventory_db) == []
    assert ledger.read_bytes() == b"keep"

    assert eval_main(["--invoices", str(empty), "--replay", recording, "--reset-db"]) == 1
    assert load_stock(inventory_db) == SEEDED
    assert ledger.read_bytes() == b"keep"

    connection = sqlite3.connect(inventory_db)
    connection.execute("UPDATE inventory SET stock = 4")
    connection.commit()
    connection.close()
    assert eval_main(["--invoices", str(empty), "--replay", recording, "--reset"]) == 1
    assert load_stock(inventory_db) == SEEDED
    assert ledger.read_bytes() == b"keep"

    edited = inventory_db.read_bytes()
    missing = eval_main([
        "--invoices", str(tmp_path / "no-such-folder"),
        "--replay", recording,
        "--reset",
    ])
    captured = capsys.readouterr()
    assert missing == 2
    assert "Traceback" not in captured.err
    assert "Invoice directory not found" in captured.err
    assert inventory_db.read_bytes() == edited
    assert ledger.read_bytes() == b"keep"


def test_create_database_still_seeds_an_empty_table(tmp_path):
    path = tmp_path / "empty.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
    connection.commit()
    connection.close()
    body = path.read_bytes()
    assert ensure_inventory_db(path) == path
    assert path.read_bytes() == body
    assert load_stock(path) == []
    assert load_stock(create_database(path)) == SEEDED
