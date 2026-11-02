"""payments.db. One row per paid invoice. The unique index is the race backstop.

Match key: vendor_key plus the normalized invoice number.
vendor_key is the printed vendor, casefolded, with whitespace collapsed.
The invoice number goes through normalize_invoice_number, so INV 1004
and INV-1004 are the same number. Nothing else is fuzzy.

The same raw text (sha256) is also a duplicate, even when the number differs.
A second file with the same key is a duplicate too: same total means another
format, and a different total or a revision marker means a revision of a
bill that was already paid. A revision is not a duplicate until that key
has a paid row.

Split: this total is at or under the approval threshold, and this total
plus the same vendor's paid totals whose invoice dates fall within
split_window_days is over the threshold. The window is the absolute
difference of the printed invoice dates. A date that does not parse is
outside the window. Another vendor does not count.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from models import ExtractedInvoice, Finding, SourceInfo

from decision.findings import _is_revised, parse_printed_date
from decision.invoice_fields import different, normalize_invoice_number, show_money
from decision.policy import POLICY

_CREATE = """\
CREATE TABLE IF NOT EXISTS payments (
    vendor TEXT NOT NULL,
    vendor_key TEXT NOT NULL,
    invoice_number TEXT,
    revision TEXT,
    total TEXT NOT NULL,
    currency TEXT NOT NULL,
    invoice_date TEXT,
    source_file TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    paid_at TEXT NOT NULL
)
"""

# One paid row per vendor and invoice number. A second insert fails.
_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS payments_vendor_invoice "
    "ON payments (vendor_key, invoice_number)"
)

# Seconds a writer waits on a lock before the payment becomes a system-error hold.
_LOCK_TIMEOUT = 5

_COLUMNS = (
    "vendor",
    "vendor_key",
    "invoice_number",
    "revision",
    "total",
    "currency",
    "invoice_date",
    "source_file",
    "raw_sha256",
    "paid_at",
)


class PaymentRow(NamedTuple):
    vendor: str
    vendor_key: str
    invoice_number: str | None
    revision: str | None
    total: str
    currency: str
    invoice_date: str | None
    source_file: str
    raw_sha256: str
    paid_at: str


def vendor_key(name: str | None) -> str:
    """Casefolded, whitespace collapsed. Punctuation stays. Not fuzzy."""

    return " ".join((name or "").split()).casefold()


def raw_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def paid_rows(path: str | Path) -> list[PaymentRow]:
    """Read-only. A missing or empty file is an empty ledger.

    A sqlite error is not an empty ledger. The caller turns it into a hold.
    """

    db_path = Path(path)
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return []
    connection = _connect_readonly(db_path)
    try:
        cursor = connection.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM payments ORDER BY rowid"
        )
        found = cursor.fetchall()
    finally:
        connection.close()
    return [PaymentRow._make(row) for row in found]


def record_payment(
    path: str | Path,
    *,
    vendor: str,
    invoice_number: str | None,
    revision: str | None,
    total: Decimal,
    currency: str,
    invoice_date: str | None,
    source_file: str,
    raw_text: str,
) -> bool | str:
    """Insert one paid row.

    True means the row was inserted. False means this invoice was already
    paid. "SPLIT_INVOICE" means the split rule fired. Any other string means
    the file could not be written. Nothing was inserted in those cases.
    The duplicate and split checks run inside the write lock, so two callers
    cannot both pass the split rule. The caller writes this before the mock
    payment. Anything but True means the mock must not run.
    """

    number = normalize_invoice_number(invoice_number)
    marker = (revision or "").strip() or None
    row = (
        vendor,
        vendor_key(vendor),
        number,
        marker,
        show_money(total),
        currency,
        (invoice_date or "").strip() or None,
        source_file,
        raw_sha256(raw_text),
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    connection = None
    try:
        connection = sqlite3.connect(Path(path), timeout=_LOCK_TIMEOUT, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CREATE)
        connection.execute(_UNIQUE)
        rows = [
            PaymentRow._make(item)
            for item in connection.execute(
                "SELECT " + ", ".join(_COLUMNS) + " FROM payments ORDER BY rowid"
            )
        ]
        snapshot = ExtractedInvoice(
            vendor=vendor,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            currency=currency,
            total=total,
            source=SourceInfo(path=source_file, filename=source_file, format="txt"),
        )
        if _duplicate_finding(snapshot, raw_text, rows) is not None:
            connection.execute("ROLLBACK")
            return False
        _paid, over = split_window(snapshot, rows)
        if over:
            connection.execute("ROLLBACK")
            return "SPLIT_INVOICE"
        connection.execute(
            "INSERT INTO payments (" + ", ".join(_COLUMNS) + ") VALUES ("
            + ", ".join("?" for _ in _COLUMNS)
            + ")",
            row,
        )
        connection.execute("COMMIT")
        return True
    except sqlite3.IntegrityError:
        if connection is not None:
            connection.execute("ROLLBACK")
        return False
    except sqlite3.Error as exc:
        if connection is not None:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return f"payments ledger unreadable: {exc}"
    finally:
        if connection is not None:
            connection.close()


def arrival_findings(
    path: str | Path,
    invoice: ExtractedInvoice,
    raw_text: str,
) -> list[Finding]:
    """Hold findings from the ledger. Empty when nothing matches.

    Read-only. Does not create the file.
    """

    rows = paid_rows(path)
    if not rows:
        return []
    findings = []
    duplicate = _duplicate_finding(invoice, raw_text, rows)
    if duplicate is not None:
        findings.append(duplicate)
    split = _split_finding(invoice, rows)
    if split is not None:
        findings.append(split)
    return findings


def _duplicate_finding(
    invoice: ExtractedInvoice,
    raw_text: str,
    rows: list[PaymentRow],
) -> Finding | None:
    digest = raw_sha256(raw_text)
    key = vendor_key(invoice.vendor)
    number = normalize_invoice_number(invoice.invoice_number)
    sha_hit = next((row for row in rows if row.raw_sha256 == digest), None)
    key_hit = None
    if key and number:
        key_hit = next(
            (row for row in rows if row.vendor_key == key and row.invoice_number == number),
            None,
        )
    if sha_hit is not None:
        message = (
            f"This document was already paid as {sha_hit.source_file} "
            f"({sha_hit.invoice_number}, {sha_hit.total} {sha_hit.currency})."
        )
    elif key_hit is not None:
        message = _same_key_message(invoice, raw_text, key_hit)
    else:
        return None
    return Finding(
        code="DUPLICATE_INVOICE",
        message=message,
        severity="warning",
        item="invoice_number",
    )


def _same_key_message(invoice: ExtractedInvoice, raw_text: str, row: PaymentRow) -> str:
    """Same vendor and invoice number, different text. Say which case it is."""

    revised = _is_revised(invoice, raw_text or "")
    totals_differ = invoice.total is None or different(invoice.total, Decimal(row.total))
    if revised or totals_differ:
        if revised and totals_differ:
            why = "It is marked as a revision and the total differs."
        elif revised:
            why = "It is marked as a revision."
        else:
            why = "The total differs."
        return (
            f"Invoice {row.invoice_number} from {row.vendor} was already paid "
            f"as {row.source_file} for {row.total} {row.currency}. {why} "
            "It is not paid again."
        )
    return (
        f"The same vendor, invoice number, and total were already paid "
        f"as {row.source_file} ({row.total} {row.currency}). The file text differs."
    )


def split_window(invoice: ExtractedInvoice, rows: list[PaymentRow]) -> tuple[Decimal, bool]:
    """Same-vendor paid total inside the split window, and whether the rule fires.

    The total is this vendor and currency, with printed dates within
    split_window_days. A date that does not parse is outside the window.
    Another vendor does not count. The rule fires when this invoice is in an
    allowed currency, at or under the approval threshold, and this total plus
    the window is over the threshold.
    """

    paid = Decimal("0")
    key = vendor_key(invoice.vendor)
    this_date = parse_printed_date(invoice.invoice_date)
    if key and this_date is not None:
        for row in rows:
            if row.vendor_key != key or row.currency != invoice.currency:
                continue
            paid_on = parse_printed_date(row.invoice_date)
            if paid_on is None or abs((this_date - paid_on).days) > POLICY.split_window_days:
                continue
            paid += Decimal(row.total)
    total = invoice.total
    over = (
        total is not None
        and total <= POLICY.scrutiny_threshold
        and invoice.currency in POLICY.allowed_currencies
        and paid + total > POLICY.scrutiny_threshold
    )
    return paid, over


def _split_finding(invoice: ExtractedInvoice, rows: list[PaymentRow]) -> Finding | None:
    total = invoice.total
    window_sum, over = split_window(invoice, rows)
    if not over or total is None:
        return None
    combined = window_sum + total
    return Finding(
        code="SPLIT_INVOICE",
        message=(
            f"This {show_money(total)} {invoice.currency} invoice plus "
            f"{show_money(window_sum)} {invoice.currency} already paid to this vendor "
            f"within {POLICY.split_window_days} days is {show_money(combined)} "
            f"{invoice.currency}, over the {show_money(POLICY.scrutiny_threshold)} "
            "approval threshold."
        ),
        severity="warning",
        item="total",
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    name = path.resolve().as_posix()
    return sqlite3.connect(f"file:{name}?mode=ro", uri=True, timeout=5)


def save_release_sha(ledger, path: str, digest: str | None) -> None:
    """Upsert the latest release sha. SQLite serializes concurrent writers."""

    connection = sqlite3.connect(ledger, timeout=_LOCK_TIMEOUT, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CREATE)
        connection.execute(_UNIQUE)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS release_records (path TEXT PRIMARY KEY, record_sha TEXT)"
        )
        connection.execute(
            "INSERT INTO release_records (path, record_sha) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET record_sha = excluded.record_sha",
            (path, digest),
        )
        connection.execute("COMMIT")
    except sqlite3.Error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def stored_release_sha(ledger, path: str) -> str | None:
    """The stored sha, or None when the row is missing, NULL, or unreadable."""

    try:
        name = Path(ledger).resolve().as_posix()
        connection = sqlite3.connect(f"file:{name}?mode=ro", uri=True, timeout=_LOCK_TIMEOUT)
        try:
            row = connection.execute(
                "SELECT record_sha FROM release_records WHERE path = ?", (path,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return None if row is None else row[0]
