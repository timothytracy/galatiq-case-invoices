"""Release uses the pipeline record, then re-checks the live file and stock."""

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from db import inventory_db_path
from pipeline.approve_hold import approve_and_pay
from pipeline.release_record import ROOT

WHEN = __import__("datetime").datetime(2026, 11, 2, 5, 30, tzinfo=__import__("datetime").timezone.utc)


def _run(path: Path, tmp_path: Path, ledger: Path | None = None):
    from graph import run_invoice
    from tests.fake_llm_eval import AS_OF, handler_for
    from tests.fakes import FakeClock, FakeLLM, ZeroRandom
    return run_invoice(
        path, inventory_db_path(ROOT / "inventory.db"), FakeLLM(handler_for(path)),
        clock=FakeClock(), rng=ZeroRandom(), max_attempts=5, as_of=AS_OF,
        ledger_path=ledger or (tmp_path / "payments.db"),
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


def _edit(result, tmp_path, **changes):
    path = Path(os_release(result))
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    _stamp(tmp_path, result)
    return {"invoice_path": result["invoice"].source.path, "filename": result["filename"]}


def os_release(result):
    from pipeline.release_record import record_path, repo_path
    return record_path(repo_path(result["invoice"].source.path))


def _stamp(tmp_path, result):
    """Treat the file now on disk as the ledger's latest release."""

    from ledger.payments import save_release_sha
    from pipeline.release_record import repo_path
    path = Path(os_release(result))
    rel = repo_path(result["invoice"].source.path).as_posix()
    save_release_sha(tmp_path / "payments.db", rel, hashlib.sha256(path.read_bytes()).hexdigest())


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
    result = _run(ROOT / folder / name, tmp_path)
    assert code in [item.code for item in result["findings"]]
    paid = _pay(result, tmp_path)
    assert paid["result"] == "paid", paid["reason"]
    assert paid["pipeline_outcome"] == "hold"
    assert result["invoice"].vendor in capsys.readouterr().out
    assert not (ROOT / "logs" / f"{name}.release.json").exists()


def test_closed_or_hard_finding_blocks_a_payable_hold(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    key = _edit(result, tmp_path, findings=[
        {"code": "VENDOR_RENAMED", "message": "formerly", "severity": "warning"},
        {"code": "ITEM_NOT_IN_SOURCE", "message": "missing", "severity": "warning"},
    ], codes=["VENDOR_RENAMED", "ITEM_NOT_IN_SOURCE"])
    refused = _pay(key, tmp_path, amount=result["invoice"].total)
    assert refused["result"] == "held"
    assert "ITEM_NOT_IN_SOURCE" in refused["reason"]


def test_second_approval_does_not_pay_again(tmp_path, capsys):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    assert _pay(result, tmp_path)["result"] == "paid"
    again = _pay(result, tmp_path)
    assert again["result"] == "held"
    assert again["reason_code"] == "NOT_PAYABLE"
    assert again["reason"] == f"Invoice {result['invoice'].invoice_number} from this vendor is already paid."
    assert capsys.readouterr().out.count("Paid ") == 1


def test_reviewer_pays_only_the_difference_of_a_paid_revision(tmp_path, monkeypatch, capsys):
    """The original is paid first. The revision is held, then released for 4050.00."""

    from ledger.payments import paid_rows, supplement_rows

    ledger = tmp_path / "payments.db"
    original = _run(ROOT / "data/invoices/invoice_1004.json", tmp_path, ledger=ledger)
    assert original["outcome"] == "approve"
    assert str(original["invoice"].total) == "1890.00"
    revised = _run(ROOT / "data/invoices/invoice_1004_revised.json", tmp_path, ledger=ledger)
    assert revised["outcome"] == "hold"
    assert revised["reason_code"] == "REVISED_INVOICE"
    assert "DUPLICATE_INVOICE" not in [item.code for item in revised["findings"]]
    assert str(revised["invoice"].total) == "5940.00"

    full = _pay(revised, tmp_path)
    assert full["result"] == "held"
    assert full["reason_code"] == "AMOUNT_MISMATCH"
    assert full["reason"] == "Amount 5940.00 does not equal the unpaid difference 4050.00."
    assert supplement_rows(ledger) == []

    seen = {}
    payment_module = importlib.import_module("pipeline.payment")
    real_pay = payment_module.mock_payment

    def spy(vendor, amount):
        rows = supplement_rows(ledger)
        seen["during"] = (rows[0].status, rows[0].amount, str(amount))
        return real_pay(vendor, amount)

    monkeypatch.setattr(payment_module, "mock_payment", spy)
    paid = _pay(revised, tmp_path, amount=Decimal("4050.00"))
    assert paid["result"] == "paid", paid["reason"]
    assert paid["record"]["amount"] == "4050.00"
    assert seen["during"] == ("pending", "4050.00", "4050.00")
    output = capsys.readouterr().out
    assert "Paid 1890.00 to Precision Parts Ltd." in output
    assert "Paid 4050.00 to Precision Parts Ltd." in output
    assert "Paid 5940.00" not in output
    assert [row.source_file for row in paid_rows(ledger)] == ["invoice_1004.json"]
    assert [row.total for row in paid_rows(ledger)] == ["1890.00"]
    assert [(row.source_file, row.amount, row.status) for row in supplement_rows(ledger)] == [
        ("invoice_1004_revised.json", "4050.00", "paid")
    ]

    again = _pay(revised, tmp_path, amount=Decimal("4050.00"))
    assert again["result"] == "held"
    assert again["reason_code"] == "NOT_PAYABLE"
    assert again["reason"] == "Invoice INV-1004 from this vendor is already paid."
    assert len(supplement_rows(ledger)) == 1


def test_difference_is_required_when_the_original_is_paid_after_the_hold(tmp_path, capsys):
    ledger = tmp_path / "payments.db"
    revised = _run(ROOT / "data/invoices/invoice_1004_revised.json", tmp_path, ledger=ledger)
    assert revised["reason_code"] == "REVISED_INVOICE"
    assert "original was not paid" in (revised.get("action") or "")
    original = _run(ROOT / "data/invoices/invoice_1004.json", tmp_path, ledger=ledger)
    assert original["outcome"] == "approve"
    refused = _pay(revised, tmp_path)
    assert refused["result"] == "held"
    assert refused["reason_code"] == "AMOUNT_MISMATCH"
    assert "5940.00" in refused["reason"]
    assert "4050.00" in refused["reason"]
    paid = _pay(revised, tmp_path, amount="4050.00")
    assert paid["result"] == "paid", paid["reason"]
    assert paid["record"]["amount"] == "4050.00"
    assert "Paid 5940.00" not in capsys.readouterr().out


def test_a_failed_difference_payment_is_not_paid_again(tmp_path, monkeypatch):
    from ledger.payments import paid_rows, supplement_rows

    ledger = tmp_path / "payments.db"
    _run(ROOT / "data/invoices/invoice_1004.json", tmp_path, ledger=ledger)
    revised = _run(ROOT / "data/invoices/invoice_1004_revised.json", tmp_path, ledger=ledger)

    def broken(vendor, amount):
        raise ConnectionError("gateway timed out")

    payment_module = importlib.import_module("pipeline.payment")
    monkeypatch.setattr(payment_module, "mock_payment", broken)
    failed = _pay(revised, tmp_path, amount="4050.00")
    assert failed["result"] == "held"
    assert failed["reason_code"] == "SYSTEM_ERROR"
    assert [row.status for row in paid_rows(ledger)] == ["paid"]
    assert [row.total for row in paid_rows(ledger)] == ["1890.00"]
    assert [(row.amount, row.status) for row in supplement_rows(ledger)] == [("4050.00", "failed")]

    monkeypatch.setattr(payment_module, "mock_payment", lambda *_args: {"status": "success"})
    retry = _pay(revised, tmp_path, amount="4050.00")
    assert retry["result"] == "held"
    assert retry["reason_code"] == "NOT_PAYABLE"
    assert retry["reason"] == "Invoice INV-1004 from this vendor is already paid."
    assert [(row.amount, row.status) for row in supplement_rows(ledger)] == [("4050.00", "failed")]


def test_prior_payment_and_split_block_release(tmp_path):
    from ledger import record_payment
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
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
    _stamp(tmp_path, result)
    assert record_payment(
        ledger, vendor=result["invoice"].vendor, invoice_number=result["invoice"].invoice_number,
        revision=None, total=result["invoice"].total, currency="USD", invoice_date="2026-01-26",
        source_file="same.txt", raw_text="same text",
    ) is True
    prior = _pay(result, tmp_path)
    assert prior["reason_code"] == "NOT_PAYABLE"
    assert prior["reason"] == f"Invoice {result['invoice'].invoice_number} from this vendor is already paid."


@pytest.mark.parametrize("kind", ["corrupt", "locked"])
def test_unreadable_ledger_is_a_system_error(kind, tmp_path, monkeypatch):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    ledger = tmp_path / "payments.db"
    holder = None
    if kind == "corrupt":
        ledger.write_bytes(b"this is not a sqlite database")
    else:
        holder = sqlite3.connect(ledger, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr("ledger.payments._LOCK_TIMEOUT", 0.2)
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
    euro = _run(ROOT / "data/invoices/invoice_1014.xml", tmp_path)
    assert _pay(euro, tmp_path)["reason_code"] == "NOT_PAYABLE"
    hold = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    assert _pay(hold, tmp_path, note="n")["reason_code"] == "MISSING_NOTE"
    unknown = _pay(_edit(hold, tmp_path, outcome="HOLD"), tmp_path, amount=hold["invoice"].total)
    assert unknown["reason"] == "Only a held invoice can be approved for payment."
    for total in ("0", "-1"):
        key = _edit(hold, tmp_path, invoice={**json.loads(os_release(hold).read_text())["invoice"], "total": total})
        assert _pay(key, tmp_path, amount=total)["result"] == "held"


def test_one_fraud_signal_is_refused_and_a_plain_escalation_can_be_paid(tmp_path):
    clean = _run(ROOT / "data/invoices/invoice_1001.txt", tmp_path, ledger=tmp_path / "spent.db")
    signal = _edit(clean, tmp_path, outcome="hold", reason_code="FRAUD_SIGNAL", findings=[
        {"code": "FRAUD_SIGNAL", "message": "One signal.", "severity": "warning"},
    ], codes=["FRAUD_SIGNAL"])
    assert _pay(signal, tmp_path, amount=clean["invoice"].total)["result"] == "held"
    escalated = _edit(clean, tmp_path, outcome="hold", reason_code="AUDIT_ESCALATED", findings=[
        {"code": "AUDIT_ESCALATED", "message": "Audit escalated the invoice to a hold.", "severity": "warning"},
    ], codes=["AUDIT_ESCALATED"])
    assert _pay(escalated, tmp_path, amount=clean["invoice"].total)["result"] == "paid"


def test_the_log_has_an_attempt_before_the_payment(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    paid = _pay(result, tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "approve.jsonl").read_text().splitlines()]
    assert [row["decision"] for row in rows] == ["attempt", "paid"]
    assert rows[1] == paid["record"]
    assert rows[1]["vendor"] == result["invoice"].vendor
    assert rows[1]["pipeline_outcome"] == "hold"


def test_a_file_edited_after_the_run_is_refused(tmp_path):
    source = (ROOT / "data/invoices/invoice_1012.txt").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(dir=ROOT) as folder:
        probe = Path(folder) / "invoice_1012.txt"
        probe.write_text(source, encoding="utf-8")
        result = _run(probe, tmp_path)
        probe.write_text(source + "\nTAMPER\n", encoding="utf-8")
        refused = _pay(result, tmp_path)
    output = ROOT / "output"
    if output.is_dir() and not any(output.iterdir()):
        output.rmdir()
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
    result = _run(ROOT / filename, tmp_path)
    record = json.loads(os_release(result).read_text(encoding="utf-8"))
    for key in ("vendor", "total", "invoice_number"):
        if key in change:
            record["invoice"][key] = change[key]
    record.update({key: value for key, value in change.items() if key not in {"vendor", "total", "invoice_number"}})
    os_release(result).write_text(json.dumps(record) + "\n", encoding="utf-8")
    _stamp(tmp_path, result)
    refused = _pay(
        {"invoice_path": result["invoice"].source.path},
        tmp_path,
        amount=record["invoice"]["total"],
    )
    assert refused["result"] == "held"
    assert needle in refused["reason"]


def test_dropping_the_cap_flag_is_still_refused(tmp_path):
    from decision.outcome import SEND_BACK_CAP_MESSAGE
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    record = json.loads(os_release(result).read_text(encoding="utf-8"))
    record["outcome"] = "hold"
    record["reason_code"] = "AUDIT_ESCALATED"
    record["codes"] = ["AUDIT_ESCALATED"]
    record["findings"] = [{
        "code": "AUDIT_ESCALATED", "message": SEND_BACK_CAP_MESSAGE, "severity": "warning",
    }]
    os_release(result).write_text(json.dumps(record) + "\n", encoding="utf-8")
    _stamp(tmp_path, result)
    refused = _pay({"invoice_path": result["invoice"].source.path}, tmp_path, amount=result["invoice"].total)
    assert refused["result"] == "held"
    assert "AUDIT_ESCALATED" in refused["reason"]


def test_a_source_outside_the_repo_is_refused(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    record = json.loads(os_release(result).read_text(encoding="utf-8"))
    record["source"] = "/tmp/evil.txt"
    os_release(result).write_text(json.dumps(record) + "\n", encoding="utf-8")
    _stamp(tmp_path, result)
    refused = _pay(result, tmp_path)
    assert refused["result"] == "held"
    assert "outside the repository" in refused["reason"]


def test_record_keys_do_not_collide():
    from pipeline.release_record import record_path
    assert record_path(Path("a__b/x.txt")) != record_path(Path("a/b__x.txt"))


def test_basename_collision_does_not_pay_the_other_file(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    other = {"invoice_path": "data/extra_invoices/invoice_1012.txt", "filename": "invoice_1012.txt"}
    refused = _pay(other, tmp_path, amount=result["invoice"].total)
    assert refused["reason"] == "No pipeline record for this invoice."


def test_a_failed_record_write_does_not_pay_or_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("INVOICE_RELEASE_DIR", str(blocker / "releases"))
    result = _run(ROOT / "data/invoices/invoice_1001.txt", tmp_path)
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert result["payment"].status == "not_paid"


def test_stock_changed_after_the_run_is_refused(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    database = sqlite3.connect(inventory_db_path(ROOT / "inventory.db"))
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


def _approve_in_subprocess(cwd, release_dir, invoice_path, amount, ledger, log):
    code = (
        "import json, os, sys\n"
        "os.environ['INVOICE_RELEASE_DIR'] = sys.argv[1]\n"
        "from pipeline.approve_hold import approve_and_pay\n"
        "out = approve_and_pay(\n"
        "    {'invoice_path': sys.argv[2]},\n"
        "    reviewer='Avery Chen', note='Confirmed with the vendor.',\n"
        "    amount=sys.argv[3], ledger_path=sys.argv[4], log_path=sys.argv[5],\n"
        ")\n"
        "print(json.dumps({'result': out['result'], 'reason': out['reason'], 'reason_code': out['reason_code']}))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["INVOICE_RELEASE_DIR"] = str(release_dir)
    proc = subprocess.run(
        [sys.executable, "-c", code, str(release_dir), str(invoice_path), str(amount), str(ledger), str(log)],
        cwd=cwd, env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _plant_critique_hold(tmp_path, monkeypatch):
    """A 1001 record that would pay, stored under a nested release folder."""

    nest = tmp_path / "nest"
    releases = nest / "releases"
    releases.mkdir(parents=True)
    monkeypatch.setenv("INVOICE_RELEASE_DIR", str(releases))
    ledger = tmp_path / "fresh.db"
    result = _run(ROOT / "data/invoices/invoice_1001.txt", tmp_path, ledger=tmp_path / "spent.db")
    path = Path(os_release(result))
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(
        outcome="hold",
        reason_code="CRITIQUE_HOLD",
        codes=["CRITIQUE_HOLD"],
        findings=[{"code": "CRITIQUE_HOLD", "message": "Review the concern.", "severity": "warning"}],
    )
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    from ledger.payments import save_release_sha
    from pipeline.release_record import repo_path
    rel = repo_path(result["invoice"].source.path).as_posix()
    save_release_sha(ledger, rel, hashlib.sha256(path.read_bytes()).hexdigest())
    return result, path, releases, nest, ledger, record


@pytest.mark.parametrize("lock_parent", [False, True])
def test_read_only_stale_record_does_not_pay_in_a_new_process(lock_parent, tmp_path, monkeypatch):
    result, path, releases, nest, ledger, record = _plant_critique_hold(tmp_path, monkeypatch)
    path.chmod(0o444)
    releases.chmod(0o555)
    if lock_parent:
        nest.chmod(0o555)
    try:
        rerun = _run(ROOT / "data/invoices/invoice_1001.txt", tmp_path, ledger=ledger)
        paid = _approve_in_subprocess(
            ROOT, releases, result["invoice"].source.path, record["invoice"]["total"],
            ledger, tmp_path / "later.jsonl",
        )
    finally:
        if lock_parent:
            nest.chmod(0o755)
        releases.chmod(0o755)
        if path.exists():
            path.chmod(0o644)
    assert rerun["reason_code"] == "SYSTEM_ERROR"
    assert paid["result"] == "held"
    assert paid["reason"] == "release record is not the latest"


def test_an_older_release_file_is_not_the_latest(tmp_path):
    from pipeline.release_record import remember
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    path = Path(os_release(result))
    older = path.read_bytes()
    edited = json.loads(older)
    edited.update(
        outcome="hold",
        reason_code="CRITIQUE_HOLD",
        codes=["CRITIQUE_HOLD"],
        findings=[{"code": "CRITIQUE_HOLD", "message": "Review the concern.", "severity": "warning"}],
    )
    assert remember({
        "invoice": edited["invoice"],
        "invoice_path": result["invoice"].source.path,
        "raw_text": edited["raw_text"],
        "findings": edited["findings"],
        "outcome": edited["outcome"],
        "reason_code": edited["reason_code"],
        "critique_concerns": edited.get("critique_concerns") or [],
    }, tmp_path / "payments.db")
    path.write_bytes(older)
    refused = _pay(result, tmp_path)
    assert refused["result"] == "held"
    assert refused["reason"] == "release record is not the latest"


def _save_one_release_row(item):
    ledger, path, digest = item
    from ledger.payments import save_release_sha
    save_release_sha(ledger, path, digest)


def test_concurrent_release_rows_all_remain(tmp_path):
    ledger = tmp_path / "payments.db"
    rows = [(str(ledger), f"data/invoices/n{i}.txt", f"{i:064d}") for i in range(30)]
    with ProcessPoolExecutor(max_workers=30) as pool:
        list(pool.map(_save_one_release_row, rows))
    connection = sqlite3.connect(ledger)
    found = dict(connection.execute("SELECT path, record_sha FROM release_records"))
    connection.close()
    assert found == {path: digest for _ledger, path, digest in rows}


def test_approval_from_another_cwd_pays_the_same_hold(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    paid = _approve_in_subprocess(
        elsewhere, os.environ["INVOICE_RELEASE_DIR"], result["invoice"].source.path,
        result["invoice"].total, tmp_path / "payments.db", tmp_path / "cwd.jsonl",
    )
    assert paid["result"] == "paid"


def test_demo_with_the_run_ledger_pays_once(tmp_path, capsys):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    ledger = tmp_path / "payments.db"
    first = approve_and_pay(
        result, reviewer="Avery Chen", note="Confirmed with the vendor.",
        amount=result["invoice"].total, ledger_path=ledger, demo=True, now=lambda: WHEN,
    )
    assert first["result"] == "paid", first["reason"]
    again = approve_and_pay(
        result, reviewer="Avery Chen", note="Confirmed with the vendor.",
        amount=result["invoice"].total, ledger_path=ledger, demo=True, now=lambda: WHEN,
    )
    assert again["result"] == "held"
    assert capsys.readouterr().out.count("Paid ") == 1
    assert not (ROOT / "output" / "reviews" / "approve_pay.jsonl").exists()


def test_demo_without_a_ledger_is_refused(tmp_path):
    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    refused = approve_and_pay(
        result, reviewer="Avery Chen", note="Confirmed with the vendor.",
        amount=result["invoice"].total, demo=True, now=lambda: WHEN,
    )
    assert refused["result"] == "held"
    assert refused["reason"] == "A ledger is required."


def test_a_reject_is_not_payable(tmp_path):
    refused = _pay(_run(ROOT / "data/invoices/invoice_1002.txt", tmp_path), tmp_path)
    assert refused["reason_code"] == "NOT_PAYABLE"
    assert refused["reason"] == "A reject is never paid."


def test_eval_replay_leaves_the_repo_logs_alone(monkeypatch):
    monkeypatch.delenv("INVOICE_RELEASE_DIR", raising=False)
    logs = ROOT / "logs"
    before = {path.name: path.stat().st_mtime_ns for path in logs.glob("*.release.json")} if logs.is_dir() else {}
    from scripts.eval import main as eval_main
    assert eval_main(["--replay", str(ROOT / "recordings" / "samples.jsonl")]) == 0
    after = {path.name: path.stat().st_mtime_ns for path in logs.glob("*.release.json")} if logs.is_dir() else {}
    assert after == before


def test_demo_pays_one_hold_and_refuses_one_reject(capsys):
    from scripts.approve_hold_demo import main
    logs = ROOT / "logs"
    before = {path.name: path.stat().st_mtime_ns for path in logs.glob("*.release.json")} if logs.is_dir() else {}
    assert main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert "Paid 9975.00 to QuickShip Distributers (formerly FastShip Ltd.)" in out
    records = [json.loads(line) for line in out.splitlines() if '"reviewer"' in line]
    assert [row["decision"] for row in records] == ["paid", "held"]
    assert records[1]["reason_code"] == "NOT_PAYABLE"
    assert records[1]["reason"] == "A reject is never paid."
    after = {path.name: path.stat().st_mtime_ns for path in logs.glob("*.release.json")} if logs.is_dir() else {}
    assert after == before


def test_an_amount_that_cannot_be_read_is_refused_and_logged(tmp_path):
    class Unreadable:
        def __str__(self):
            raise RuntimeError("no text")

    result = _run(ROOT / "data/invoices/invoice_1012.txt", tmp_path)
    refused = _pay(result, tmp_path, amount=Unreadable())
    assert refused["result"] == "held"
    assert refused["reason_code"] == "AMOUNT_MISMATCH"
    rows = [json.loads(line) for line in (tmp_path / "approve.jsonl").read_text().splitlines()]
    assert rows[-1] == refused["record"]
    assert rows[-1]["reason_code"] == "AMOUNT_MISMATCH"
    assert "paid" not in [row["decision"] for row in rows]
