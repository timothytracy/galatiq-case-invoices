"""Deterministic invoice readers. No network and no per-file special cases.

Understands JSON, the two CSV layouts in the sample set, XML, text, email text,
and text-layer PDFs. Printed totals are kept even when the arithmetic is wrong.
"""

import csv
import json
import re
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

from models import ExtractedInvoice, LineItem, SourceInfo
from rules import as_money, normalize_invoice_number, normalize_item_name

# A letter O inside a token that already contains a digit is treated as zero.
# "2O26" and "$3,500.O0" change. "NOTES" does not, because that O has no digit.
_OCR_TOKEN = re.compile(r"[$]?[0-9O][0-9O,]*\.?[0-9O]*")

_QTY_UNIT = re.compile(
    r"^(?P<item>.+?)\s+qty:\s*(?P<qty>-?\d+(?:\.\d+)?)\s+unit price:\s*\$?(?P<price>[0-9,]+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
_QTY_AT = re.compile(
    r"^(?P<item>\S+)\s+qty\s+(?P<qty>-?\d+(?:\.\d+)?)\s+@\s*\$?(?P<price>[0-9,]+(?:\.\d+)?)\s*ea\s*$",
    re.IGNORECASE,
)
_X_EACH = re.compile(
    r"^-?\s*(?P<item>.+?)\s+x(?P<qty>\d+)\s+\$?(?P<price>[0-9,]+\.\d{2})\s+each\s*$",
    re.IGNORECASE,
)
_TABLE_ROW = re.compile(
    r"^(?P<item>.+?)\s+(?P<qty>-?\d+)\s+\$(?P<price>[0-9,]+(?:\.\d{2})?)\s+\$(?P<amount>[0-9,]+\.\d{2})(?:\s+(?P<note>.+))?$"
)
_LINE_PATTERNS = (_QTY_UNIT, _QTY_AT, _X_EACH, _TABLE_ROW)

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


def parse_file(path: Path) -> ExtractedInvoice:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_json_file(path)
    if suffix == ".csv":
        return parse_csv_file(path)
    if suffix == ".xml":
        return parse_xml_file(path)
    if suffix == ".pdf":
        return parse_pdf_file(path)
    if suffix == ".txt":
        return parse_text_file(path)
    raise ValueError(f"unsupported invoice type: {suffix or path.name}")


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


def parse_csv_file(path: Path) -> ExtractedInvoice:
    raw = _read(path)
    rows = list(csv.reader(raw.splitlines()))
    source = _source(path, "csv", raw)
    if rows and [cell.strip().lower() for cell in rows[0][:2]] == ["field", "value"]:
        return _parse_kv_csv(rows, source)
    return _parse_column_csv(rows, source)


def parse_xml_file(path: Path) -> ExtractedInvoice:
    raw = _read(path)
    root = ElementTree.fromstring(raw)
    items = []
    for node in root.iter():
        if _xml_tag(node) != "item":
            continue
        name = _xml_child(node, "name") or _xml_child(node, "description") or ""
        quantity = _xml_child(node, "quantity")
        if not name or quantity is None:
            continue
        items.append(
            _make_item(
                name,
                quantity,
                _xml_child(node, "unit_price"),
                _xml_child(node, "amount") or _xml_child(node, "line_total"),
                _xml_child(node, "note"),
            )
        )
    return _build(
        vendor=_blank(_xml_first(root, "vendor")),
        invoice_number_raw=_blank(_xml_first(root, "invoice_number")),
        invoice_date=_blank(_xml_first(root, "date")),
        due_date=_blank(_xml_first(root, "due_date")),
        currency=_blank(_xml_first(root, "currency")) or "USD",
        line_items=items,
        subtotal=_money(_xml_first(root, "subtotal")),
        tax_rate=_rate(_xml_first(root, "tax_rate")),
        tax_amount=_money(_xml_first(root, "tax_amount")),
        shipping=_money(_xml_first(root, "shipping")),
        total=_money(_xml_first(root, "total")),
        payment_terms=_blank(_xml_first(root, "payment_terms")),
        notes=_blank(_xml_first(root, "notes")),
        source=_source(path, "xml", raw),
    )


def parse_pdf_file(path: Path) -> ExtractedInvoice:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parsing needs the pypdf package") from exc
    reader = PdfReader(str(path))
    raw = "\n".join((page.extract_text() or "") for page in reader.pages)
    return parse_text(raw, _source(path, "pdf", raw))


def parse_text_file(path: Path) -> ExtractedInvoice:
    raw = _read(path)
    kind = "email" if raw.lstrip().lower().startswith("from:") else "txt"
    return parse_text(raw, _source(path, kind, raw))


def parse_text(raw: str, source: SourceInfo) -> ExtractedInvoice:
    text = fix_ocr_digits(raw)
    items = _line_items(text)
    tax_rate, tax_amount = _find_tax(text)
    warnings = []
    if not items:
        warnings.append("no line items recognized")
    return _build(
        vendor=_find_vendor(text),
        invoice_number_raw=_find_invoice_number(text),
        invoice_date=_find_invoice_date(text),
        due_date=_find_due_date(text),
        currency=_find_currency(text),
        line_items=items,
        subtotal=_money(_search(r"(?im)Subtotal:\s*\$?\s*([0-9,]+\.\d{2})", text)),
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        shipping=_money(_search(r"(?im)Shipping:\s*\$?\s*([0-9,]+\.\d{2})", text)),
        total=_money(_find_total(text)),
        payment_terms=_search(r"(?im)^\s*(?:Payment\s+Terms|Pymnt\s+Terms|Terms):\s*(.+?)\s*$", text),
        notes=_search(r"(?im)^\s*Notes:\s*(.+?)\s*$", text),
        source=source,
        warnings=warnings,
    )


def fix_ocr_digits(text: str) -> str:
    def replace(match: re.Match) -> str:
        token = match.group(0)
        if not any(character.isdigit() for character in token):
            return token
        return token.replace("O", "0")

    return _OCR_TOKEN.sub(replace, text)


def _line_items(text: str) -> list[LineItem]:
    items = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pattern in _LINE_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            description = match.group("item").strip(" -")
            if _skip_item(description):
                break
            note = match.groupdict().get("note")
            items.append(
                _make_item(
                    description,
                    match.group("qty"),
                    match.group("price"),
                    match.groupdict().get("amount"),
                    note,
                )
            )
            break
    return items


def _skip_item(description: str) -> bool:
    lowered = description.lower()
    return lowered in {"item", "description", "qty", "quantity", "amount", "total"} or lowered.startswith(
        ("subtotal", "grand total", "sales tax")
    )


def _find_vendor(text: str) -> str | None:
    match = re.search(r"(?im)^\s*(?:Vendor|Vndr):\s*(.+?)\s*$", text)
    if match:
        return re.split(r"\s{2,}", match.group(1).strip())[0].strip() or None
    match = re.search(r"(?im)^\s*FROM:\s*(.+?)\s*$", text)
    if not match:
        return None
    name = match.group(1).strip()
    follow = re.match(r"\s*\n\s*(\([^)\n]+\))", text[match.end() :])
    if follow:
        name = f"{name} {follow.group(1).strip()}"
    return name or None


def _find_invoice_number(text: str) -> str | None:
    patterns = (
        r"(?i)Invoice\s*Number:\s*(INV[-\s]?\d+)",
        r"(?i)INV\s*NO:\s*(INV[-\s]?\d+)",
        r"(?i)Invoice:\s*(INV[-\s]?\d+)",
        r"(?i)INVOICE\s*#\s*(INV[-\s]?\d+)",
        r"(?i)Inv\s*#:\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def _find_invoice_date(text: str) -> str | None:
    match = re.search(r"(?im)^\s*(?:Date|Dt):\s*(.+?)\s*$", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"(?i)(?<!Due )Date:\s*(\S+)", text)
    if match:
        return match.group(1).strip()
    return None


def _find_due_date(text: str) -> str | None:
    match = re.search(r"(?im)^\s*Due\s*(?:Date|Dt)?:\s*(.+?)\s*$", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"(?i)\bDue(?:\s*(?:Date|Dt))?:\s*(\S+)", text)
    if match:
        return match.group(1).strip()
    return None


def _find_total(text: str) -> str | None:
    patterns = (
        r"(?im)Grand\s+Total:\s*\$?\s*([0-9,]+\.\d{2})",
        r"(?im)Total\s+Amount:\s*\$?\s*([0-9,]+\.\d{2})",
        r"(?im)^\s*Amt:\s*\$?\s*([0-9,]+\.\d{2})\s*$",
        r"(?im)^\s*Total:\s*\$?\s*([0-9,]+\.\d{2})\s*$",
    )
    for pattern in patterns:
        found = _search(pattern, text)
        if found:
            return found
    return None


def _find_tax(text: str) -> tuple[Decimal | None, Decimal | None]:
    match = re.search(
        r"(?i)(?:Sales\s+)?Tax(?:\s*\(([^)]*)\))?\s*:\s*\$?\s*([0-9,]+\.\d{2})",
        text,
    )
    if not match:
        return None, None
    rate = _rate(match.group(1)) if match.group(1) else None
    return rate, _money(match.group(2))


def _find_currency(text: str) -> str:
    if re.search(r"(?i)\bEUR\b|€", text):
        return "EUR"
    return "USD"


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


def _build(
    *,
    source: SourceInfo,
    line_items: list[LineItem],
    vendor: str | None = None,
    vendor_address: str | None = None,
    invoice_number_raw: str | None = None,
    revision: str | None = None,
    invoice_date: str | None = None,
    due_date: str | None = None,
    currency: str = "USD",
    subtotal: Decimal | None = None,
    tax_rate: Decimal | None = None,
    tax_amount: Decimal | None = None,
    shipping: Decimal | None = None,
    total: Decimal | None = None,
    payment_terms: str | None = None,
    notes: str | None = None,
    warnings: list[str] | None = None,
) -> ExtractedInvoice:
    raw_number = _blank(invoice_number_raw)
    return ExtractedInvoice(
        vendor=_blank(vendor),
        vendor_address=_blank(vendor_address),
        invoice_number=normalize_invoice_number(raw_number),
        invoice_number_raw=raw_number,
        revision=_blank(revision),
        invoice_date=_blank(invoice_date),
        due_date=_blank(due_date),
        currency=currency or "USD",
        line_items=line_items,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        shipping=shipping,
        total=total,
        payment_terms=_blank(payment_terms),
        notes=_blank(notes),
        source=source,
        warnings=list(warnings or []),
    )


def _make_item(description, quantity, unit_price, amount, note) -> LineItem:
    text = str(description).strip()
    code, name_note = normalize_item_name(text)
    extra = _blank(note)
    if name_note and extra:
        extra = f"{name_note}; {extra}"
    elif name_note:
        extra = name_note
    return LineItem(
        item=code,
        description=text,
        quantity=Decimal(str(quantity)),
        unit_price=_money(unit_price),
        amount=_money(amount),
        note=extra,
    )


def _vendor_parts(value) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        return _blank(value.get("name")), _blank(value.get("address"))
    if isinstance(value, str):
        return _blank(value), None
    return None, None


def _money(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "").replace("€", "")
        if value == "":
            return None
    return as_money(Decimal(str(value)))


def _rate(value) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("%"):
        return Decimal(text[:-1].strip()) / Decimal("100")
    return Decimal(text)


def _blank(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _search(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _header_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _source(path: Path, kind: str, raw: str) -> SourceInfo:
    return SourceInfo(
        path=str(path),
        filename=path.name,
        format=kind,
        parser="deterministic",
        raw_text=raw,
    )


def _xml_tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _xml_child(node, name: str) -> str | None:
    for child in list(node):
        if _xml_tag(child) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _xml_first(root, name: str) -> str | None:
    for node in root.iter():
        if _xml_tag(node) == name and node.text and node.text.strip():
            return node.text.strip()
    return None


if __name__ == "__main__":
    import sys

    invoice = parse_file(Path(sys.argv[1]))
    print(invoice.model_dump_json(indent=2))
