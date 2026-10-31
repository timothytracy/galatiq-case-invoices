"""CSV invoices. The sample set has a field/value layout and a column layout.

The header row picks which one. Summary rows (subtotal, tax, total) sit under
the line items in the column layout.
"""

import csv
import re
from pathlib import Path

from models import ExtractedInvoice, LineItem, SourceInfo
from parsers.common import _blank, _build, _make_item, _money, _rate, _read, _source

_COLUMN_FIELDS = {
    "invoicenumber": "invoice_number",
    "vendor": "vendor",
    "date": "invoice_date",
    "duedate": "due_date",
    "item": "item",
    "description": "item",
    "qty": "quantity",
    "quantity": "quantity",
    "unitprice": "unit_price",
    "rate": "unit_price",
    "linetotal": "amount",
    "amount": "amount",
}
_KV_HEADER = {
    "invoice_number": "invoice_number",
    "vendor": "vendor",
    "date": "invoice_date",
    "due_date": "due_date",
    "subtotal": "subtotal",
    "tax": "tax_amount",
    "tax_amount": "tax_amount",
    "shipping": "shipping",
    "total": "total",
    "payment_terms": "payment_terms",
    "currency": "currency",
    "notes": "notes",
    "revision": "revision",
}


def parse_csv_file(path: Path) -> ExtractedInvoice:
    raw = _read(path)
    rows = list(csv.reader(raw.splitlines()))
    source = _source(path, "csv", raw)
    if rows and [cell.strip().lower() for cell in rows[0][:2]] == ["field", "value"]:
        return _parse_kv_csv(rows, source)
    return _parse_column_csv(rows, source)


def _parse_kv_csv(rows: list[list[str]], source: SourceInfo) -> ExtractedInvoice:
    header: dict[str, str] = {}
    pending: dict[str, str] | None = None
    items: list[LineItem] = []

    def flush() -> None:
        nonlocal pending
        if pending and pending.get("item"):
            items.append(
                _make_item(
                    pending["item"],
                    pending.get("quantity"),
                    pending.get("unit_price"),
                    pending.get("amount"),
                    pending.get("note"),
                )
            )
        pending = None

    for row in rows[1:]:
        if len(row) < 2 or not row[0].strip():
            continue
        key = row[0].strip().lower()
        value = row[1].strip()
        if key == "item":
            flush()
            pending = {"item": value}
        elif key in {"quantity", "unit_price", "amount", "note"} and pending is not None:
            pending[key] = value
        else:
            flush()
            field = _KV_HEADER.get(key)
            if field:
                header[field] = value
    flush()
    return _from_header(header, items, source)


def _parse_column_csv(rows: list[list[str]], source: SourceInfo) -> ExtractedInvoice:
    if not rows:
        return _build(line_items=[], source=source, warnings=["empty csv"])
    headers = [_header_key(cell) for cell in rows[0]]
    header: dict[str, str] = {}
    items: list[LineItem] = []
    for row in rows[1:]:
        if not row or not any(cell.strip() for cell in row):
            continue
        if not row[0].strip():
            filled = [cell.strip() for cell in row if cell.strip()]
            if len(filled) >= 2:
                _apply_summary(filled[-2], filled[-1], header)
            continue
        record: dict[str, str] = {}
        for index, cell in enumerate(row):
            if index >= len(headers):
                break
            field = _COLUMN_FIELDS.get(headers[index])
            if field and cell.strip():
                record[field] = cell.strip()
        for key in ("invoice_number", "vendor", "invoice_date", "due_date"):
            if record.get(key) and key not in header:
                header[key] = record[key]
        if record.get("item"):
            items.append(
                _make_item(
                    record["item"],
                    record.get("quantity"),
                    record.get("unit_price"),
                    record.get("amount"),
                    record.get("note"),
                )
            )
    return _from_header(header, items, source)


def _apply_summary(label: str, value: str, header: dict[str, str]) -> None:
    key = label.strip().rstrip(":").strip().lower()
    if key.startswith("subtotal"):
        header["subtotal"] = value
    elif "shipping" in key:
        header["shipping"] = value
    elif key.startswith("tax") or key.startswith("sales tax"):
        header["tax_amount"] = value
        found = re.search(r"\(([^)]*)\)", label)
        if found:
            header["tax_rate"] = found.group(1)
    elif "total" in key:
        header["total"] = value


def _from_header(header: dict[str, str], items: list[LineItem], source: SourceInfo) -> ExtractedInvoice:
    warnings = []
    if not items:
        warnings.append("no line items recognized")
    return _build(
        vendor=_blank(header.get("vendor")),
        invoice_number_raw=_blank(header.get("invoice_number")),
        revision=_blank(header.get("revision")),
        invoice_date=_blank(header.get("invoice_date")),
        due_date=_blank(header.get("due_date")),
        currency=_blank(header.get("currency")) or "USD",
        line_items=items,
        subtotal=_money(header.get("subtotal")),
        tax_rate=_rate(header.get("tax_rate")),
        tax_amount=_money(header.get("tax_amount")),
        shipping=_money(header.get("shipping")),
        total=_money(header.get("total")),
        payment_terms=_blank(header.get("payment_terms")),
        notes=_blank(header.get("notes")),
        source=source,
        warnings=warnings,
    )


def _header_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())
