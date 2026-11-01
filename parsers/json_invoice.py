"""JSON invoices. A vendor may be a string or an object with name and address."""

import json
from decimal import Decimal
from pathlib import Path

from models import ExtractedInvoice
from parsers.common import _blank, _filled, _make_item, _read, _source


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
    return _filled(
        _source(path, "json", raw),
        items,
        {
            "vendor": vendor,
            "vendor_address": address,
            "invoice_number": data.get("invoice_number"),
            "revision": data.get("revision"),
            "invoice_date": data.get("date") or data.get("invoice_date"),
            "due_date": data.get("due_date"),
            "currency": data.get("currency"),
            "subtotal": data.get("subtotal"),
            "tax_rate": data.get("tax_rate"),
            "tax_amount": data.get("tax_amount"),
            "shipping": data.get("shipping"),
            "total": data.get("total"),
            "payment_terms": data.get("payment_terms"),
            "notes": data.get("notes"),
        },
    )


def _vendor_parts(value) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        return _blank(value.get("name")), _blank(value.get("address"))
    if isinstance(value, str):
        return _blank(value), None
    return None, None
