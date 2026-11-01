"""Plain-text and email invoices. The first matching line pattern wins."""

import re
from pathlib import Path

from models import ExtractedInvoice, LineItem, SourceInfo
from decision.findings import math_reconciles
from decision.invoice_fields import resolve_currency
from parsers.common import _filled, _make_item, _read, _source

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
    """Read one text invoice. An O-to-0 fix is kept only when the math then adds up."""

    fixed = fix_ocr_digits(raw)
    if fixed != raw:
        candidate = _parse_fields(fixed, source)
        if math_reconciles(candidate):
            return candidate
    return _parse_fields(raw, source)


def _parse_fields(text: str, source: SourceInfo) -> ExtractedInvoice:
    items = _line_items(text)
    tax_rate, tax_amount = _find_tax(text)
    warnings = [] if items else ["no line items recognized"]
    return _filled(
        source,
        items,
        {
            "vendor": _find_vendor(text),
            "invoice_number": _first(
                text,
                r"(?i)Invoice\s*Number:\s*(INV[-\s]?\d+)",
                r"(?i)INV\s*NO:\s*(INV[-\s]?\d+)",
                r"(?i)Invoice:\s*(INV[-\s]?\d+)",
                r"(?i)INVOICE\s*#\s*(INV[-\s]?\d+)",
                r"(?i)Inv\s*#:\s*(\d+)",
            ),
            "invoice_date": _first(
                text,
                r"(?im)^\s*(?:Date|Dt):\s*(.+?)\s*$",
                r"(?i)(?<!Due )Date:\s*(\S+)",
            ),
            "due_date": _first(
                text,
                r"(?im)^\s*Due\s*(?:Date|Dt)?:\s*(.+?)\s*$",
                r"(?i)\bDue(?:\s*(?:Date|Dt))?:\s*(\S+)",
            ),
            "currency": resolve_currency(None, text),
            "subtotal": _first(text, r"(?im)Subtotal:\s*\$?\s*([0-9,]+\.\d{2})"),
            "tax_rate": tax_rate,
            "tax_amount": tax_amount,
            "shipping": _first(text, r"(?im)Shipping:\s*\$?\s*([0-9,]+\.\d{2})"),
            "total": _first(
                text,
                r"(?im)Grand\s+Total:\s*\$?\s*([0-9,]+\.\d{2})",
                r"(?im)Total\s+Amount:\s*\$?\s*([0-9,]+\.\d{2})",
                r"(?im)^\s*Amt:\s*\$?\s*([0-9,]+\.\d{2})\s*$",
                r"(?im)^\s*Total:\s*\$?\s*([0-9,]+\.\d{2})\s*$",
            ),
            "payment_terms": _first(
                text,
                r"(?im)^\s*(?:Payment\s+Terms|Pymnt\s+Terms|Terms):\s*(.+?)\s*$",
            ),
            "notes": _first(text, r"(?im)^\s*Notes:\s*(.+?)\s*$"),
        },
        warnings,
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


def _find_tax(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"(?i)(?:Sales\s+)?Tax(?:\s*\(([^)]*)\))?\s*:\s*\$?\s*([0-9,]+\.\d{2})",
        text,
    )
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _first(text: str, *patterns: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None
