"""Release uses the pipeline record, then re-checks the live file and stock."""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from models import Finding
from pipeline.approve_hold import ROOT, approve_and_pay, remember

WHEN = __import__("datetime").datetime(2026, 11, 2, 5, 30, tzinfo=__import__("datetime").timezone.utc)


def _run(path: Path):
    from graph import run_invoice
    from tests.fake_llm_eval import AS_OF, handler_for
    from tests.fakes import FakeClock, FakeLLM, ZeroRandom
    return run_invoice(
        path, ROOT / "inventory.db", FakeLLM(handler_for(path)),
        clock=FakeClock(), rng=ZeroRandom(), max_attempts=5, as_of=AS_OF,
    )


def _pay(result, tmp_path, **overrides):
    invoice = result.get("invoice") if isinstance(result, dict) else None
    total = None
    if invoice is not None and not isinstance(invoice, dict):
        total = invoice.total
    return approve_and_pay(
        result,
        reviewer=overrides.pop("reviewer", "Avery Chen"),
        note=overrides.pop("note", "Confirmed with the vendor."),
        amount=overrides.pop("amount", total if total is not None else overrides.pop("fallback", "0")),
        ledger_path=tmp_path / "payments.db",
        log_path=tmp_path / "approve.jsonl",
        now=lambda: WHEN,
    )


def _edit(result, **changes):
    path = Path(os_release(result))
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return {"invoice_path": result["invoice"].source.path, "filename": result["filename"]}


def os_release(result):
    from pipeline.approve_hold import _record_path, _repo_path
    return _record_path(_repo_path(result["invoice"].source.path))


@pytest.mark.parametrize(
    "folder, name, code",
    [
        ("data/invoices", "invoice_1012.txt", "VENDOR_RENAMED"),
        ("data/invoices", "invoice_1004_revised.json", "REVISED_INVOICE"),
        ("data/extra_invoices", "invoice_2112.txt", "FRAUD_CLUSTER"),
        ("data/extra_invoices", "invoice_2114.txt", "NEW_REMITTANCE"),
        ("data/extra_invoices", "invoice_2001.pdf", "PRICE_SPIKE"),
    ],
)
def test_genuine_review_holds_can_be_paid(folder, name, code, tmp_path, capsys):
    result = _run(ROOT / folder / name)
    assert code in [item.code for item in result["findings"]]
    paid = _pay(result, tmp_path)
    assert paid["result"] == "paid", paid["reason"]
    assert paid["pipeline_outcome"] == "hold"
    assert result["invoice"].vendor in capsys.readouterr().out
    assert not (ROOT / "logs" / f"{name}.release.json").exists()


def test_closed_or_hard_finding_blocks_a_payable_hold(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    key = _edit(result, findings=[
        {"code": "VENDOR_RENAMED", "message": "formerly", "severity": "warning"},
        {"code": "ITEM_NOT_IN_SOURCE", "message": "missing", "severity": "warning"},
    ], codes=["VENDOR_RENAMED", "ITEM_NOT_IN_SOURCE"])
    refused = _pay(key, tmp_path, amount=result["invoice"].total)
    assert refused["result"] == "held"
    assert "ITEM_NOT_IN_SOURCE" in refused["reason"]


def test_second_approval_does_not_pay_again(tmp_path, capsys):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    assert _pay(result, tmp_path)["result"] == "paid"
    again = _pay(result, tmp_path)
    assert again["result"] == "held"
    assert "DUPLICATE_INVOICE" in again["reason"]
    assert capsys.readouterr().out.count("Paid ") == 1


def test_prior_payment_and_split_block_release(tmp_path):
    from ledger import record_payment
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    ledger = tmp_path / "payments.db"
    assert record_payment(
        ledger, vendor=result["invoice"].vendor, invoice_number="INV-1", revision=None,
        total=Decimal("6000"), currency="USD", invoice_date="2026-01-20",
        source_file="earlier.txt", raw_text="earlier",
    ) is True
    split = _pay(result, tmp_path)
    assert split["result"] == "held"
    assert "SPLIT_INVOICE" in split["reason"]
    ledger.unlink()
    assert record_payment(
        ledger, vendor=result["invoice"].vendor, invoice_number=result["invoice"].invoice_number,
        revision=None, total=result["invoice"].total, currency="USD", invoice_date="2026-01-26",
        source_file="same.txt", raw_text="same text",
    ) is True
    prior = _pay(result, tmp_path)
    assert "DUPLICATE_INVOICE" in prior["reason"]


@pytest.mark.parametrize("kind", ["corrupt", "locked"])
def test_unreadable_ledger_is_a_system_error(kind, tmp_path, monkeypatch):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    ledger = tmp_path / "payments.db"
    holder = None
    if kind == "corrupt":
        ledger.write_bytes(b"this is not a sqlite database")
    else:
        holder = sqlite3.connect(ledger, isolation_level=None)
        holder.execute("CREATE TABLE payments (id INTEGER)")
        holder.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(
            "ledger.payments._connect_readonly",
            lambda path: sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=0.2),
        )
    try:
        refused = _pay(result, tmp_path)
    finally:
        if holder is not None:
            holder.close()
    assert refused["result"] == "held"
    assert refused["reason_code"] == "SYSTEM_ERROR"


def test_non_usd_missing_note_unknown_outcome_and_bad_total(tmp_path):
    euro = _run(ROOT / "data/invoices/invoice_1014.xml")
    assert _pay(euro, tmp_path)["reason_code"] == "NOT_PAYABLE"
    hold = _run(ROOT / "data/invoices/invoice_1012.txt")
    assert _pay(hold, tmp_path, note="n")["reason_code"] == "MISSING_NOTE"
    unknown = _pay(_edit(hold, outcome="HOLD"), tmp_path, amount=hold["invoice"].total)
    assert unknown["reason"] == "Only a held invoice can be approved for payment."
    for total in ("0", "-1"):
        key = _edit(hold, invoice={**json.loads(os_release(hold).read_text())["invoice"], "total": total})
        assert _pay(key, tmp_path, amount=total)["result"] == "held"


def test_one_fraud_signal_is_refused_and_a_plain_escalation_can_be_paid(tmp_path):
    clean = _run(ROOT / "data/invoices/invoice_1001.txt")
    signal = _edit(clean, outcome="hold", reason_code="FRAUD_SIGNAL", findings=[
        {"code": "FRAUD_SIGNAL", "message": "One signal.", "severity": "warning"},
    ], codes=["FRAUD_SIGNAL"])
    assert _pay(signal, tmp_path, amount=clean["invoice"].total)["result"] == "held"
    escalated = _edit(clean, outcome="hold", reason_code="AUDIT_ESCALATED", findings=[
        {"code": "AUDIT_ESCALATED", "message": "Audit escalated the invoice to a hold.", "severity": "warning"},
    ], codes=["AUDIT_ESCALATED"])
    assert _pay(escalated, tmp_path, amount=clean["invoice"].total)["result"] == "paid"


def test_the_log_has_an_attempt_before_the_payment(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    paid = _pay(result, tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "approve.jsonl").read_text().splitlines()]
    assert [row["decision"] for row in rows] == ["attempt", "paid"]
    assert rows[1] == paid["record"]
    assert rows[1]["vendor"] == result["invoice"].vendor
    assert rows[1]["pipeline_outcome"] == "hold"


def test_a_file_edited_after_the_run_is_refused(tmp_path):
    probe = ROOT / "data" / "_probe_release.txt"
    source = (ROOT / "data/invoices/invoice_1012.txt").read_text(encoding="utf-8")
    probe.write_text(source, encoding="utf-8")
    try:
        result = _run(probe)
        probe.write_text(source + "\nTAMPER\n", encoding="utf-8")
        refused = _pay(result, tmp_path)
    finally:
        probe.unlink(missing_ok=True)
    assert refused["result"] == "held"
    assert "does not match" in refused["reason"]


@pytest.mark.parametrize(
    "change, needle, filename",
    [
        ({"outcome": "hold", "reason_code": "VENDOR_RENAMED", "findings": [
            {"code": "VENDOR_RENAMED", "message": "formerly", "severity": "warning"}],
          "codes": ["VENDOR_RENAMED"]}, "STOCK_SHORTFALL", "data/invoices/invoice_1002.txt"),
        ({"vendor": "Evil Payee LLC"}, "source file", "data/extra_invoices/invoice_2112.txt"),
        ({"total": "475"}, "TOTAL_MISMATCH", "data/invoices/invoice_1012.txt"),
        ({"total": "9500"}, "TOTAL_MISMATCH", "data/invoices/invoice_1012.txt"),
        ({"invoice_number": "INV-4421"}, "source file", "data/invoices/invoice_1012.txt"),
        ({"invoice_number": "20260115"}, "source file", "data/invoices/invoice_1012.txt"),
        ({"invoice_number": "INV-1012-B"}, "source file", "data/invoices/invoice_1012.txt"),
    ],
)
def test_release_record_edits_are_refused(change, needle, filename, tmp_path):
    result = _run(ROOT / filename)
    record = json.loads(os_release(result).read_text(encoding="utf-8"))
    for key in ("vendor", "total", "invoice_number"):
        if key in change:
            record["invoice"][key] = change[key]
    record.update({key: value for key, value in change.items() if key not in {"vendor", "total", "invoice_number"}})
    os_release(result).write_text(json.dumps(record) + "\n", encoding="utf-8")
    refused = _pay(
        {"invoice_path": result["invoice"].source.path},
        tmp_path,
        amount=record["invoice"]["total"],
    )
    assert refused["result"] == "held"
    assert needle in refused["reason"]


def test_dropping_the_cap_flag_is_still_refused(tmp_path):
    from decision.outcome import SEND_BACK_CAP_MESSAGE
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    record = json.loads(os_release(result).read_text(encoding="utf-8"))
    record["outcome"] = "hold"
    record["reason_code"] = "AUDIT_ESCALATED"
    record["codes"] = ["AUDIT_ESCALATED"]
    record["findings"] = [{
        "code": "AUDIT_ESCALATED", "message": SEND_BACK_CAP_MESSAGE, "severity": "warning",
    }]
    os_release(result).write_text(json.dumps(record) + "\n", encoding="utf-8")
    refused = _pay({"invoice_path": result["invoice"].source.path}, tmp_path, amount=result["invoice"].total)
    assert refused["result"] == "held"
    assert "AUDIT_ESCALATED" in refused["reason"]


def test_a_source_outside_the_repo_is_refused(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    record = json.loads(os_release(result).read_text(encoding="utf-8"))
    record["source"] = "/tmp/evil.txt"
    os_release(result).write_text(json.dumps(record) + "\n", encoding="utf-8")
    refused = _pay(result, tmp_path)
    assert refused["result"] == "held"
    assert "outside the repository" in refused["reason"]


def test_basename_collision_does_not_pay_the_other_file(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    other = {"invoice_path": "data/extra_invoices/invoice_1012.txt", "filename": "invoice_1012.txt"}
    refused = _pay(other, tmp_path, amount=result["invoice"].total)
    assert refused["reason"] == "No pipeline record for this invoice."


def test_a_failed_record_write_does_not_pay_or_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("INVOICE_RELEASE_DIR", str(blocker / "releases"))
    result = _run(ROOT / "data/invoices/invoice_1001.txt")
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert result["payment"].status == "not_paid"


def test_stock_changed_after_the_run_is_refused(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt")
    database = sqlite3.connect(ROOT / "inventory.db")
    before = database.execute("SELECT item, stock FROM inventory").fetchall()
    database.execute("UPDATE inventory SET stock = 0")
    database.commit()
    try:
        refused = _pay(result, tmp_path)
    finally:
        database.executemany("UPDATE inventory SET stock = ? WHERE item = ?", [(stock, item) for item, stock in before])
        database.commit()
        database.close()
    assert refused["result"] == "held"
    assert "STOCK_SHORTFALL" in refused["reason"]


def test_demo_pays_one_hold_and_refuses_one_reject(capsys):
    from pipeline.approve_hold import main
    assert main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert "Paid 9975.00 to QuickShip Distributers (formerly FastShip Ltd.)" in out
    records = [json.loads(line) for line in out.splitlines() if '"reviewer"' in line]
    assert [row["decision"] for row in records] == ["paid", "held"]
    assert records[1]["reason_code"] == "NOT_PAYABLE"
    assert not list((ROOT / "logs").glob("*.release.json"))
