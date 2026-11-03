"""payments.db. One row per paid invoice. The unique index is the race backstop.

Match key: vendor_key plus the normalized invoice number.
vendor_key is the printed vendor, casefolded, with whitespace collapsed.
The invoice number goes through normalize_invoice_number, so INV 1004
and INV-1004 are the same number. Nothing else is fuzzy.

The same raw text (sha256) is also a duplicate, even when the number differs.
A second file with the same key is a duplicate too. A revision whose total
is higher than the amount already recorded for that key is the exception:
it stays a revised-invoice hold, and a reviewer may pay only the difference.
That difference is a supplement row. It does not add a second payments row.
The same document, a revision that is not higher, or a different total that
is not marked as a revision stays a duplicate and is not paid.

Split: this total is at or under the approval threshold, and this total
plus the same vendor's paid totals whose invoice dates fall within
split_window_days is over the threshold. The window is the absolute
difference of the printed invoice dates. A date that does not parse is
outside the window. Another vendor does not count.
"""

import contextlib
import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from models import ExtractedInvoice, Finding, SourceInfo

from decision.findings import is_revised, parse_printed_date
from decision.invoice_fields import as_money, different, normalize_invoice_number, show_money
from decision.policy import POLICY
from timestamps import utc_stamp

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
    paid_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'paid',
    authorization_key TEXT
)
"""

# Columns added after the first ledgers were written. Old rows read as paid.
_ADDED_COLUMNS = (
    ("status", "TEXT NOT NULL DEFAULT 'paid'"),
    ("authorization_key", "TEXT"),
)

# One paid row per vendor and invoice number. A second insert fails.
_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS payments_vendor_invoice "
    "ON payments (vendor_key, invoice_number)"
)

# Authorization keys accepted against this file. Not shared with any other file.
# A missing table (a ledger written before this existed) means none are used.
_USED_AUTH = """\
CREATE TABLE IF NOT EXISTS used_authorizations (
    key TEXT PRIMARY KEY
)
"""

# Extra money paid on a revision after the original row. One payments row
# stays the unique key. A pending or failed supplement still counts as
# recorded: the gateway may have paid it.
_SUPPLEMENTS = """\
CREATE TABLE IF NOT EXISTS payment_supplements (
    vendor_key TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    source_file TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    status TEXT NOT NULL,
    authorization_key TEXT
)
"""

_SUPPLEMENT_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS payment_supplements_document "
    "ON payment_supplements (vendor_key, invoice_number, raw_sha256)"
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
    "status",
)


class PaymentRow(NamedTuple):
    """One ledger row. Pending and failed rows still block a second payment.

    paid_at is when the row was reserved, just before the gateway call.
    """

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
    status: str = "paid"


class SupplementRow(NamedTuple):
    """One difference payment for a revision. The original payments row stays."""

    vendor_key: str
    invoice_number: str
    amount: str
    currency: str
    source_file: str
    raw_sha256: str
    paid_at: str
    status: str


_SUPPLEMENT_COLUMNS = (
    "vendor_key",
    "invoice_number",
    "amount",
    "currency",
    "source_file",
    "raw_sha256",
    "paid_at",
    "status",
)


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
        have = _column_names(connection)
        picked = [name if name in have else f"'paid' AS {name}" for name in _COLUMNS]
        cursor = connection.execute(
            "SELECT " + ", ".join(picked) + " FROM payments ORDER BY rowid"
        )
        found = cursor.fetchall()
    finally:
        connection.close()
    return [PaymentRow._make(row) for row in found]


def supplement_rows(path: str | Path) -> list[SupplementRow]:
    """Read-only difference rows. A missing table is an empty list.

    A sqlite error is not an empty list. The caller turns it into a hold.
    """

    db_path = Path(path)
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return []
    connection = _connect_readonly(db_path)
    try:
        found = _supplements_on(connection)
    finally:
        connection.close()
    return found


def _supplements_on(connection: sqlite3.Connection) -> list[SupplementRow]:
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payment_supplements'"
    ).fetchone()
    if present is None:
        return []
    cursor = connection.execute(
        "SELECT " + ", ".join(_SUPPLEMENT_COLUMNS) + " FROM payment_supplements ORDER BY rowid"
    )
    return [SupplementRow._make(row) for row in cursor.fetchall()]


def _covered_amount(row: PaymentRow, supplements: list[SupplementRow]) -> Decimal:
    extra = Decimal("0")
    for item in supplements:
        if (
            item.vendor_key == row.vendor_key
            and item.invoice_number == row.invoice_number
            and item.currency == row.currency
        ):
            extra += Decimal(item.amount)
    return Decimal(row.total) + extra


def _rows_with_supplements(
    rows: list[PaymentRow], supplements: list[SupplementRow]
) -> list[PaymentRow]:
    """Payment rows whose totals include recorded difference payments."""

    if not supplements:
        return rows
    folded = []
    for row in rows:
        covered = _covered_amount(row, supplements)
        if covered == Decimal(row.total):
            folded.append(row)
        else:
            folded.append(row._replace(total=show_money(covered)))
    return folded


def _column_names(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(payments)")}


def _migrate(connection: sqlite3.Connection) -> None:
    """Add any column an older ledger file is missing. Runs inside the caller's write."""

    have = _column_names(connection)
    for name, kind in _ADDED_COLUMNS:
        if name not in have:
            connection.execute(f"ALTER TABLE payments ADD COLUMN {name} {kind}")


def authorization_used(path: str | Path, key: str) -> bool:
    """True when this ledger file has already accepted the authorization key.

    A missing file, an empty file, or a ledger with no used_authorizations
    table has not accepted it. Deleting the file (reset-ledger) drops the keys.
    """

    db_path = Path(path)
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return False
    try:
        connection = _connect_readonly(db_path)
    except sqlite3.Error:
        return False
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'used_authorizations'"
        ).fetchone()
        if table is None:
            return False
        return (
            connection.execute(
                "SELECT 1 FROM used_authorizations WHERE key = ?", (key,)
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False
    finally:
        connection.close()


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
    authorization_key: str | None = None,
    status: str = "paid",
) -> bool | str:
    """Insert one row, paid or pending.

    True means the row was inserted. False means this invoice was already
    paid. "SPLIT_INVOICE" means the split rule fired.
    "USED_AUTHORIZATION" means this ledger already accepted authorization_key.
    Any other string means the file could not be written. Nothing was inserted
    in those cases.
    The duplicate and split checks run inside the write lock, so two callers
    cannot both pass the split rule. The payment step writes a pending row
    before the mock payment and settles it after with settle_payment.
    Anything but True means the mock must not run.
    """

    if status not in ("pending", "paid"):
        raise ValueError(f"record_payment status must be pending or paid, not {status!r}")
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
        utc_stamp(),
        status,
        authorization_key,
    )
    connection = None
    try:
        connection = sqlite3.connect(Path(path), timeout=_LOCK_TIMEOUT, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CREATE)
        _migrate(connection)
        connection.execute(_UNIQUE)
        connection.execute(_USED_AUTH)
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
        supplements = _supplements_on(connection)
        if _duplicate_finding(snapshot, raw_text, rows, supplements) is not None:
            connection.execute("ROLLBACK")
            return False
        _paid, over = split_window(snapshot, _rows_with_supplements(rows, supplements))
        if over:
            connection.execute("ROLLBACK")
            return "SPLIT_INVOICE"
        inserted = _COLUMNS + ("authorization_key",)
        connection.execute(
            "INSERT INTO payments (" + ", ".join(inserted) + ") VALUES ("
            + ", ".join("?" for _ in inserted)
            + ")",
            row,
        )
        if authorization_key:
            try:
                connection.execute(
                    "INSERT INTO used_authorizations (key) VALUES (?)",
                    (authorization_key,),
                )
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return "USED_AUTHORIZATION"
        connection.execute("COMMIT")
        return True
    except sqlite3.IntegrityError:
        if connection is not None:
            connection.execute("ROLLBACK")
        return False
    except sqlite3.Error as exc:
        if connection is not None:
            _rollback_quietly(connection)
        return f"payments ledger unreadable: {exc}"
    finally:
        if connection is not None:
            connection.close()


def settle_payment(path: str | Path, authorization_key: str, status: str) -> bool:
    """Move this authorization's pending row to paid or failed.

    True when exactly one pending row changed. A failed row stays in the
    ledger and keeps blocking this invoice: the gateway may have paid it.
    """

    if status not in ("paid", "failed"):
        raise ValueError(f"settle_payment status must be paid or failed, not {status!r}")
    connection = None
    try:
        connection = sqlite3.connect(Path(path), timeout=_LOCK_TIMEOUT, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE payments SET status = ? WHERE authorization_key = ? AND status = 'pending'",
            (status, authorization_key),
        ).rowcount
        if changed == 0 and _supplements_on(connection):
            changed = connection.execute(
                "UPDATE payment_supplements SET status = ? "
                "WHERE authorization_key = ? AND status = 'pending'",
                (status, authorization_key),
            ).rowcount
        if changed != 1:
            connection.execute("ROLLBACK")
            return False
        connection.execute("COMMIT")
        return True
    except sqlite3.Error:
        if connection is not None:
            _rollback_quietly(connection)
        return False
    finally:
        if connection is not None:
            connection.close()


def revision_difference(
    path: str | Path,
    invoice: ExtractedInvoice,
    raw_text: str,
) -> Decimal | None:
    """Unpaid amount of a higher revision, or None when the full total is due.

    None also means this is not a payable difference: no prior row, same
    document, a total that is not higher, a currency mismatch, or non-USD.
    A sqlite error propagates. It is not treated as "nothing was paid".
    """

    balance = _revision_balance(invoice, raw_text or "", paid_rows(path), supplement_rows(path))
    if balance is None:
        return None
    return balance[1]


def record_revision_difference(
    path: str | Path,
    invoice: ExtractedInvoice,
    raw_text: str,
    amount: Decimal,
    authorization_key: str,
    source_file: str,
    status: str = "pending",
) -> bool | str:
    """Insert the unpaid difference. Does not add a second payments row.

    True means the supplement was inserted. False means this is not a
    positive difference or it was already recorded. "SPLIT_INVOICE" and
    "USED_AUTHORIZATION" match record_payment. Any other string means the
    file could not be written. The check runs inside the write lock.
    """

    if status not in ("pending", "paid"):
        raise ValueError(
            f"record_revision_difference status must be pending or paid, not {status!r}"
        )
    number = normalize_invoice_number(invoice.invoice_number)
    if invoice.total is None or not number:
        return False
    connection = None
    try:
        connection = sqlite3.connect(Path(path), timeout=_LOCK_TIMEOUT, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payments'"
        ).fetchone()
        if present is None:
            connection.execute("ROLLBACK")
            return False
        _migrate(connection)
        connection.execute(_SUPPLEMENTS)
        connection.execute(_SUPPLEMENT_UNIQUE)
        connection.execute(_USED_AUTH)
        rows = [
            PaymentRow._make(item)
            for item in connection.execute(
                "SELECT " + ", ".join(_COLUMNS) + " FROM payments ORDER BY rowid"
            )
        ]
        supplements = _supplements_on(connection)
        balance = _revision_balance(invoice, raw_text or "", rows, supplements)
        if balance is None or balance[1] != amount:
            connection.execute("ROLLBACK")
            return False
        _paid, over = split_window(invoice, _rows_with_supplements(rows, supplements))
        if over:
            connection.execute("ROLLBACK")
            return "SPLIT_INVOICE"
        inserted = _SUPPLEMENT_COLUMNS + ("authorization_key",)
        connection.execute(
            "INSERT INTO payment_supplements (" + ", ".join(inserted) + ") VALUES ("
            + ", ".join("?" for _ in inserted)
            + ")",
            (
                vendor_key(invoice.vendor),
                number,
                show_money(amount),
                invoice.currency,
                source_file,
                raw_sha256(raw_text or ""),
                utc_stamp(),
                status,
                authorization_key,
            ),
        )
        if authorization_key:
            try:
                connection.execute(
                    "INSERT INTO used_authorizations (key) VALUES (?)",
                    (authorization_key,),
                )
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return "USED_AUTHORIZATION"
        connection.execute("COMMIT")
        return True
    except sqlite3.IntegrityError:
        if connection is not None:
            connection.execute("ROLLBACK")
        return False
    except sqlite3.Error as exc:
        if connection is not None:
            _rollback_quietly(connection)
        return f"payments ledger unreadable: {exc}"
    finally:
        if connection is not None:
            connection.close()


def _revision_balance(
    invoice: ExtractedInvoice,
    raw_text: str,
    rows: list[PaymentRow],
    supplements: list[SupplementRow],
) -> tuple[PaymentRow, Decimal] | None:
    """The paid row and the unpaid difference, when a reviewer may pay it."""

    if not is_revised(invoice, raw_text or ""):
        return None
    if invoice.total is None or invoice.currency not in POLICY.allowed_currencies:
        return None
    digest = raw_sha256(raw_text or "")
    if any(row.raw_sha256 == digest for row in rows):
        return None
    if any(row.raw_sha256 == digest for row in supplements):
        return None
    key = vendor_key(invoice.vendor)
    number = normalize_invoice_number(invoice.invoice_number)
    if not key or not number:
        return None
    hit = next(
        (row for row in rows if row.vendor_key == key and row.invoice_number == number),
        None,
    )
    if hit is None or hit.currency != invoice.currency:
        return None
    owed = as_money(invoice.total - _covered_amount(hit, supplements))
    if owed <= 0:
        return None
    return hit, owed


def arrival_findings(
    path: str | Path,
    invoice: ExtractedInvoice,
    raw_text: str,
) -> list[Finding]:
    """Hold findings from the ledger. Empty when nothing matches.

    Read-only. Does not create the file. A higher revision of a paid invoice
    is a revised-invoice finding, not a duplicate, so the difference can be paid.
    """

    rows = paid_rows(path)
    supplements = supplement_rows(path)
    if not rows and not supplements:
        return []
    findings = []
    balance = _revision_balance(invoice, raw_text, rows, supplements)
    if balance is None:
        duplicate = _duplicate_finding(invoice, raw_text, rows, supplements)
        if duplicate is not None:
            findings.append(duplicate)
    else:
        hit, owed = balance
        findings.append(_balance_finding(invoice, hit, owed))
    split = _split_finding(invoice, _rows_with_supplements(rows, supplements))
    if split is not None:
        findings.append(split)
    return findings


def _balance_finding(invoice: ExtractedInvoice, row: PaymentRow, owed: Decimal) -> Finding:
    return Finding(
        code="REVISED_INVOICE",
        message=(
            f"Invoice {row.invoice_number} from {row.vendor} was already paid "
            f"as {row.source_file} for {row.total} {row.currency}. "
            f"The unpaid difference is {show_money(owed)} {invoice.currency}."
        ),
        severity="warning",
        item="total",
    )


def _duplicate_finding(
    invoice: ExtractedInvoice,
    raw_text: str,
    rows: list[PaymentRow],
    supplements: list[SupplementRow] | tuple = (),
) -> Finding | None:
    digest = raw_sha256(raw_text)
    key = vendor_key(invoice.vendor)
    number = normalize_invoice_number(invoice.invoice_number)
    sha_hit = next((row for row in rows if row.raw_sha256 == digest), None)
    if sha_hit is None:
        sha_hit = next((row for row in supplements if row.raw_sha256 == digest), None)
    key_hit = None
    if key and number:
        key_hit = next(
            (row for row in rows if row.vendor_key == key and row.invoice_number == number),
            None,
        )
    if sha_hit is not None:
        paid_amount = sha_hit.total if isinstance(sha_hit, PaymentRow) else sha_hit.amount
        message = (
            f"This document was already paid as {sha_hit.source_file} "
            f"({sha_hit.invoice_number}, {paid_amount} {sha_hit.currency})."
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

    revised = is_revised(invoice, raw_text or "")
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


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    """Roll back after an error. A failed rollback must not hide the first error."""

    with contextlib.suppress(sqlite3.Error):
        connection.execute("ROLLBACK")


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
        _rollback_quietly(connection)
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
