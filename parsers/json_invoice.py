"""JSON invoices. A vendor may be a string or an object with name and address."""

import json
from decimal import Decimal
from pathlib import Path

from models import ExtractedInvoice
from parsers.common import _blank, _build, _make_item, _money, _rate, _read, _source


def parse_json_file(path: Path) -> ExtractedInvoice:
    raw = _read(path)
    data = json.loads(raw, parse_float=Decimal)
    if not isinstance(data, dict):
        raise ValueError("invoice JSON must be an object")
    vendor, address = _vendor_parts(data.get("vendor"))
    items = [
        _make_item(
            str(row.get("item") or row.get("name") or row.get("description") or ""),
            row.get("quantity"),
            row.get("unit_price"),
            row.get("amount") if row.get("amount") is not None else row.get("line_total"),
            row.get("note"),
        )
        for row in (data.get("line_items") or data.get("items") or [])
        if str(row.get("item") or row.get("name") or row.get("description") or "").strip()
    ]
    return _build(
        vendor=vendor,
        vendor_address=address,
        invoice_number_raw=_blank(data.get("invoice_number")),
        revision=_blank(data.get("revision")),
        invoice_date=_blank(data.get("date") or data.get("invoice_date")),
        due_date=_blank(data.get("due_date")),
        currency=_blank(data.get("currency")) or "USD",
        line_items=items,
        subtotal=_money(data.get("subtotal")),
        tax_rate=_rate(data.get("tax_rate")),
        tax_amount=_money(data.get("tax_amount")),
        shipping=_money(data.get("shipping")),
        total=_money(data.get("total")),
        payment_terms=_blank(data.get("payment_terms")),
        notes=_blank(data.get("notes")),
        source=_source(path, "json", raw),
    )


def _vendor_parts(value) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        return _blank(value.get("name")), _blank(value.get("address"))
    if isinstance(value, str):
        return _blank(value), None
    return None, None
