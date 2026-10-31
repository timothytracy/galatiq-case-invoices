"""Plain-text and email invoices. The first matching line pattern wins."""

import re
from decimal import Decimal
from pathlib import Path

from models import ExtractedInvoice, LineItem, SourceInfo
from parsers.common import _build, _make_item, _money, _rate, _read, _source

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
    # Euro markers in the document. A missing currency with $ amounts stays USD.
    if re.search(r"(?i)€|\beuros?\b|\beur\b", text):
        return "EUR"
    return "USD"


def _search(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None
