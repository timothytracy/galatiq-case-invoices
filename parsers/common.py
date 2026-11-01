"""Fields every format reader fills the same way."""

from decimal import Decimal
from pathlib import Path

from models import ExtractedInvoice, LineItem, SourceInfo
from decision.invoice_fields import as_money, normalize_invoice_number, normalize_item_name


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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _filled(source: SourceInfo, items: list[LineItem], fields: dict, warnings=None) -> ExtractedInvoice:
    """Build an invoice from a header dict. Money and blank strings are normalized here."""

    return _build(
        source=source,
        line_items=items,
        vendor=fields.get("vendor"),
        vendor_address=fields.get("vendor_address"),
        invoice_number_raw=fields.get("invoice_number"),
        revision=fields.get("revision"),
        invoice_date=fields.get("invoice_date"),
        due_date=fields.get("due_date"),
        currency=_blank(fields.get("currency")) or "USD",
        subtotal=_money(fields.get("subtotal")),
        tax_rate=_rate(fields.get("tax_rate")),
        tax_amount=_money(fields.get("tax_amount")),
        shipping=_money(fields.get("shipping")),
        total=_money(fields.get("total")),
        payment_terms=fields.get("payment_terms"),
        notes=fields.get("notes"),
        warnings=warnings,
    )


def _source(path: Path, kind: str, raw: str) -> SourceInfo:
    return SourceInfo(
        path=str(path),
        filename=path.name,
        format=kind,
        parser="deterministic",
        raw_text=raw,
    )
