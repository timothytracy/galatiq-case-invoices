"""Payments ledger. Offline. The model is the test fake. No network.

Synthetic split invoices are built in this file. They are not under data/,
so the answer keys do not change.
"""

import hashlib
import importlib
import json
import socket
import sqlite3
import threading
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from db import create_database
from graph import run_invoice
from ledger import arrival_findings, paid_rows, record_payment, vendor_key
from ledger.scenario import run_scenario
from main import main, payment_status
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    ExtractedInvoice,
    LineItem,
    PaymentAuthorization,
    PipelineState,
    SourceInfo,
    ValidationReport,
)
from pipeline.payment import payment
from pipeline.validation import validate
from decision.outcome import result_codes
from decision.policy import POLICY
from llm_client import MissingAPIKey
from tests.fake_llm_eval import AS_OF, handler_for
from tests.fakes import FakeClock, FakeLLM, ZeroRandom

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is disabled in tests")

    monkeypatch.setattr(socket, "create_connection", explode)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "inventory.db"
    create_database(path)
    return path


def _run(path, db_path, ledger_path=None):
    return run_invoice(
        path,
        db_path,
        FakeLLM(handler_for(path)),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=5,
        as_of=AS_OF,
        ledger_path=ledger_path,
    )


def _codes(result) -> list[str]:
    return result_codes(result)


def _payable(key: str, number: str = "INV-1") -> PipelineState:
    invoice = ExtractedInvoice(
        vendor="Widgets Inc.",
        invoice_number=number,
        invoice_date="2026-04-01",
        due_date="2026-05-01",
        currency="USD",
        total=Decimal("500.00"),
        subtotal=Decimal("500.00"),
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("2"),
                unit_price=Decimal("250"),
                amount=Decimal("500"),
            )
        ],
        source=SourceInfo(path="memo.txt", filename="memo.txt", format="txt", raw_text="WidgetA"),
    )
    return PipelineState(
        invoice_path="memo.txt",
        filename="memo.txt",
        raw_text="Invoice Number: INV-1\nWidgetA\nTotal: 500.00\n",
        invoice=invoice,
        validation=ValidationReport(passed=True, findings=[]),
        decision=ApprovalDecision(
            decision="approve",
            reasoning=ApprovalReasoning(
                rules_applied=["stock"],
                evidence=["total 500.00 USD"],
                risks_considered=["none"],
                conclusion="pay it",
            ),
            amount=invoice.total,
            currency=invoice.currency,
        ),
        outcome="approve",
        audit_verdict="PASS",
        authorization=PaymentAuthorization(
            vendor="Widgets Inc.",
            amount=Decimal("500.00"),
            currency="USD",
            key=key,
        ),
    )


def _seed(path, **overrides):
    fields = {
        "vendor": "Split Synthetic Co.",
        "invoice_number": "INV-9001",
        "revision": None,
        "total": Decimal("6000.00"),
        "currency": "USD",
        "invoice_date": "2026-04-01",
        "source_file": "synthetic_split_a.json",
        "raw_text": "synthetic split original",
    }
    fields.update(overrides)
    assert record_payment(path, **fields) is True


def _bill(vendor, number, total, when, raw, revision=None):
    """A synthetic invoice. Not a sample file."""

    amount = Decimal(total)
    return ExtractedInvoice(
        vendor=vendor,
        invoice_number=number,
        revision=revision,
        invoice_date=when,
        due_date="2026-05-01",
        currency="USD",
        subtotal=amount,
        tax_amount=Decimal("0"),
        shipping=Decimal("0"),
        total=amount,
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("1"),
                unit_price=amount,
                amount=amount,
            )
        ],
        source=SourceInfo(path="synthetic.txt", filename="synthetic.txt", format="txt", raw_text=raw),
    )


def _codes_of(findings) -> list[str]:
    return [finding.code for finding in findings]


def test_payment_writes_one_row_before_the_mock_and_refuses_a_repeat(tmp_path, monkeypatch):
    ledger = tmp_path / "payments.db"
    seen = {}
    calls = []

    def spy(vendor, amount):
        calls.append((vendor, str(amount)))
        rows = paid_rows(ledger)
        seen["rows"] = len(rows)
        seen["file"] = rows[0].source_file
        return {"status": "success"}

    payment_module = importlib.import_module("pipeline.payment")
    monkeypatch.setattr(payment_module, "mock_payment", spy)
    first = payment(_payable("key-a"), ledger)
    assert first["payment"].status == "paid"
    assert seen == {"rows": 1, "file": "memo.txt"}
    assert calls == [("Widgets Inc.", "500.00")]

    second = payment(_payable("key-b"), ledger)
    assert second["payment"].status == "not_paid"
    assert second["outcome"] == "hold"
    assert second["reason_code"] == "DUPLICATE_INVOICE"
    assert "PAYMENT_REFUSED" in _codes(second)
    assert calls == [("Widgets Inc.", "500.00")]
    assert len(paid_rows(ledger)) == 1

    connection = sqlite3.connect(f"file:{ledger.resolve().as_posix()}?mode=ro", uri=True)
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'payments_vendor_invoice'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM payments")
    finally:
        connection.close()
    assert "UNIQUE" in sql.upper()
    assert "vendor_key" in sql
    assert "invoice_number" in sql


def test_two_writers_cannot_insert_the_same_invoice(tmp_path):
    ledger = tmp_path / "payments.db"
    barrier = threading.Barrier(2)
    results = []

    def write():
        barrier.wait()
        results.append(
            record_payment(
                ledger,
                vendor="Widgets Inc.",
                invoice_number="INV-1",
                revision=None,
                total=Decimal("500.00"),
                currency="USD",
                invoice_date="2026-04-01",
                source_file="memo.txt",
                raw_text="same text",
            )
        )

    threads = [threading.Thread(target=write) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
    assert len(paid_rows(ledger)) == 1


def test_record_payment_is_only_called_from_the_payment_step():
    callers = []
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or "ledger" in path.parts:
            continue
        if "record_payment" in path.read_text(encoding="utf-8"):
            callers.append(path.relative_to(ROOT).as_posix())
    assert callers == ["pipeline/payment.py"]


def test_a_paid_run_does_not_change_inventory(tmp_path, db_path):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    ledger = tmp_path / "payments.db"
    result = _run(INVOICES / "invoice_1001.txt", db_path, ledger)
    assert result["outcome"] == "approve"
    assert payment_status(result) == "paid"
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert [row.source_file for row in paid_rows(ledger)] == ["invoice_1001.txt"]


def test_default_run_gives_each_invoice_a_fresh_ledger(db_path):
    """1011's pdf and txt are the same bill. A fresh ledger pays both.

    That is the scored eval. One shared ledger would hold the txt and the
    sample score would drop. The scored call passes no ledger path.
    """

    pdf = _run(INVOICES / "invoice_1011.pdf", db_path)
    text = _run(INVOICES / "invoice_1011.txt", db_path)
    assert pdf["outcome"] == text["outcome"] == "approve"
    assert payment_status(pdf) == payment_status(text) == "paid"
    assert "DUPLICATE_INVOICE" not in _codes(pdf)
    assert "DUPLICATE_INVOICE" not in _codes(text)

    source = (ROOT / "scripts" / "eval.py").read_text(encoding="utf-8")
    assert "run_invoice(path, db_path, GrokClient())" in source


def test_shared_ledger_holds_the_second_copy(db_path, tmp_path):
    ledger = tmp_path / "payments.db"
    first = _run(INVOICES / "invoice_1001.txt", db_path, ledger)
    second = _run(INVOICES / "invoice_1001.txt", db_path, ledger)
    assert payment_status(first) == "paid"
    assert second["outcome"] == "hold"
    assert second["reason_code"] == "DUPLICATE_INVOICE"
    assert payment_status(second) == "not_paid"
    reasons = " ".join(row["result"].get("reason") or "" for row in second["logs"])
    assert "already paid" in reasons
    assert len(paid_rows(ledger)) == 1


def test_1004_revision_is_not_a_duplicate_until_the_original_is_paid(db_path, tmp_path):
    """A fresh ledger leaves the revision as REVISED_INVOICE.

    After invoice_1004.json is paid, the revision is DUPLICATE_INVOICE and
    REVISED_INVOICE stays on the finding list. Nothing is paid the second time.
    """

    revised = INVOICES / "invoice_1004_revised.json"
    original = INVOICES / "invoice_1004.json"
    fresh = _run(revised, db_path)
    assert fresh["outcome"] == "hold"
    assert fresh["reason_code"] == "REVISED_INVOICE"
    assert "DUPLICATE_INVOICE" not in _codes(fresh)
    assert payment_status(fresh) == "not_paid"

    ledger = tmp_path / "payments.db"
    paid = _run(original, db_path, ledger)
    assert paid["outcome"] == "approve"
    assert payment_status(paid) == "paid"
    again = _run(revised, db_path, ledger)
    assert again["outcome"] == "hold"
    assert again["reason_code"] == "DUPLICATE_INVOICE"
    assert "REVISED_INVOICE" in _codes(again)
    assert payment_status(again) == "not_paid"
    assert "invoice_1004.json" in " ".join(
        finding["message"] if isinstance(finding, dict) else finding.message
        for finding in again["findings"]
    )
    assert [row.source_file for row in paid_rows(ledger)] == ["invoice_1004.json"]


def test_ordered_scenario_holds_duplicate_then_split_cases_in_order(db_path, tmp_path):
    ledger = tmp_path / "payments.db"
    report = run_scenario(
        ROOT / "data" / "ordered_scenario.json",
        db_path,
        ledger,
        lambda path: FakeLLM(handler_for(path)),
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=5,
        as_of=AS_OF,
    )
    missed = [step for step in report["steps"] if not step["ok"]]
    assert missed == [], missed
    assert report["ledger_ok"]
    assert report["ledger_files"] == [
        "invoice_1001.txt",
        "invoice_1011.pdf",
        "invoice_1004.json",
    ]
    assert len(paid_rows(ledger)) == 3


def test_reject_beats_a_duplicate_hold(db_path, tmp_path):
    ledger = tmp_path / "payments.db"
    _seed(
        ledger,
        vendor="Atlas Industrial Supply",
        invoice_number="INV-9",
        total=Decimal("100.00"),
        source_file="old.txt",
        raw_text="old bill",
    )
    amount = Decimal("6000")
    invoice = ExtractedInvoice(
        vendor="Atlas Industrial Supply",
        invoice_number="INV-9",
        invoice_date="2026-04-02",
        due_date="2026-05-01",
        currency="USD",
        subtotal=amount,
        tax_amount=Decimal("0"),
        shipping=Decimal("0"),
        total=amount,
        line_items=[
            LineItem(
                item="GadgetX",
                description="GadgetX",
                quantity=Decimal("8"),
                unit_price=Decimal("750"),
                amount=amount,
            )
        ],
        source=SourceInfo(path="short.txt", filename="short.txt", format="txt", raw_text="GadgetX"),
    )
    state = PipelineState(
        invoice_path="short.txt",
        filename="short.txt",
        raw_text="GadgetX quantity 8\nTotal 6000.00\n",
        invoice=invoice,
    )
    result = validate(state, db_path, ledger_path=ledger)
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "STOCK_SHORTFALL"
    assert "DUPLICATE_INVOICE" in _codes(result)
    assert len(paid_rows(ledger)) == 1


def test_synthetic_split_holds_only_when_the_window_sum_crosses(tmp_path):
    """Synthetic invoices. None of these files are in data/."""

    ledger = tmp_path / "payments.db"
    _seed(ledger)
    window = POLICY.split_window_days
    start = date(2026, 4, 1)

    def findings(vendor, number, total, when, raw="other synthetic text"):
        bill = _bill(vendor, number, total, when, raw)
        return arrival_findings(ledger, bill, raw)

    over = findings("Split Synthetic Co.", "INV-9002", "5000.00", "2026-04-15")
    assert _codes_of(over) == ["SPLIT_INVOICE"]
    assert over[0].severity == "warning"

    under = findings("Split Synthetic Co.", "INV-9003", "3000.00", "2026-04-15")
    assert under == []

    other = findings("Other Synthetic Co.", "INV-9004", "5000.00", "2026-04-15")
    assert other == []

    outside = (start + timedelta(days=window + 1)).isoformat()
    assert findings("Split Synthetic Co.", "INV-9005", "5000.00", outside) == []

    edge = (start + timedelta(days=window)).isoformat()
    assert _codes_of(findings("Split Synthetic Co.", "INV-9006", "5000.00", edge)) == [
        "SPLIT_INVOICE"
    ]

    own = findings("Split Synthetic Co.", "INV-9007", "11000.00", "2026-04-15")
    assert "SPLIT_INVOICE" not in _codes_of(own)

    folded = findings("split   synthetic co.", "9001", "100.00", "2026-04-02")
    assert _codes_of(folded) == ["DUPLICATE_INVOICE"]
    assert vendor_key("split   synthetic co.") == vendor_key("Split Synthetic Co.")

    same_total = findings(
        "Split Synthetic Co.",
        "INV-9001",
        "6000.00",
        "2026-04-01",
        raw="a different file body",
    )
    # 6000 + 6000 also crosses the threshold, so the split hold is recorded too.
    duplicate = next(item for item in same_total if item.code == "DUPLICATE_INVOICE")
    assert "file text differs" in duplicate.message
    assert "SPLIT_INVOICE" in _codes_of(same_total)

    exact = findings(
        "Split Synthetic Co.",
        "INV-9001",
        "6000.00",
        "2026-04-01",
        raw="synthetic split original",
    )
    assert "This document was already paid" in exact[0].message


def test_synthetic_split_invoice_is_held_by_the_graph_and_not_paid(db_path, tmp_path):
    """Two synthetic JSON invoices written for this test, not stored under data/."""

    def write(name, number, when, lines, total):
        payload = {
            "invoice_number": number,
            "vendor": "Split Synthetic Co.",
            "date": when,
            "due_date": "2026-05-01",
            "line_items": lines,
            "subtotal": total,
            "tax_rate": 0,
            "tax_amount": 0,
            "shipping": 0,
            "total": total,
            "currency": "USD",
            "payment_terms": "Net 30",
        }
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    first_path = write(
        "synthetic_split_a.json",
        "INV-9001",
        "2026-04-01",
        [
            {"item": "WidgetB", "quantity": 10, "unit_price": 500, "amount": 5000},
            {"item": "WidgetA", "quantity": 4, "unit_price": 250, "amount": 1000},
        ],
        6000,
    )
    second_path = write(
        "synthetic_split_b.json",
        "INV-9002",
        "2026-04-10",
        [
            {"item": "GadgetX", "quantity": 5, "unit_price": 750, "amount": 3750},
            {"item": "WidgetA", "quantity": 10, "unit_price": 250, "amount": 2500},
        ],
        6250,
    )
    ledger = tmp_path / "payments.db"
    first = _run(first_path, db_path, ledger)
    second = _run(second_path, db_path, ledger)
    assert first["outcome"] == "approve"
    assert payment_status(first) == "paid"
    assert second["outcome"] == "hold"
    assert second["reason_code"] == "SPLIT_INVOICE"
    assert "DUPLICATE_INVOICE" not in _codes(second)
    assert payment_status(second) == "not_paid"
    assert [row.source_file for row in paid_rows(ledger)] == ["synthetic_split_a.json"]


def test_reset_ledger_deletes_the_file_only_after_the_client_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invoice = tmp_path / "a.txt"
    invoice.write_text("Vendor: Widgets Inc.\n", encoding="utf-8")
    ledger = tmp_path / "payments.db"
    ledger.write_bytes(b"keep")
    monkeypatch.setattr("main.GrokClient", lambda: object())
    seen = {}

    def run(*_args, **kwargs):
        seen["existed"] = ledger.exists()
        seen["name"] = Path(kwargs["ledger_path"]).name
        return {
            "outcome": "approve",
            "reason_code": None,
            "action": None,
            "logs": [],
            "payment": None,
            "findings": [],
        }

    monkeypatch.setattr("main.run_invoice", run)
    assert main(["--invoice_path", str(invoice), "--reset-ledger"]) == 0
    assert seen == {"existed": False, "name": "payments.db"}

    ledger.write_bytes(b"keep")

    def boom():
        raise MissingAPIKey("no key")

    monkeypatch.setattr("main.GrokClient", boom)
    assert main(["--invoice_path", str(invoice), "--reset-ledger"]) == 1
    assert ledger.read_bytes() == b"keep"

    monkeypatch.setattr("main.GrokClient", lambda: object())

    def run_again(*_args, **kwargs):
        seen["kept"] = ledger.exists()
        return {
            "outcome": "approve",
            "reason_code": None,
            "action": None,
            "logs": [],
            "payment": None,
            "findings": [],
        }

    monkeypatch.setattr("main.run_invoice", run_again)
    assert main(["--invoice_path", str(invoice)]) == 0
    assert seen["kept"] is True
