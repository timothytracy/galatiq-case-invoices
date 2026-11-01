"""Names, money, and currency. No decision."""

import re
from decimal import Decimal, ROUND_HALF_UP

from models import ExtractedInvoice

from decision.policy import MONEY

_EURO = re.compile(r"€|\beuros?\b|\beur\b", re.IGNORECASE)


def as_money(amount: Decimal) -> Decimal:
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)


def show_qty(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def show_money(value: Decimal) -> str:
    return f"{as_money(value):.2f}"


def different(left: Decimal, right: Decimal) -> bool:
    return abs(as_money(left) - as_money(right)) > MONEY


def normalize_invoice_number(raw: str | None) -> str | None:
    """Turn '1002' and 'INV 1012' into 'INV-1002' and 'INV-1012'."""

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return text
    return f"INV-{match.group(1)}"


def item_key(name: str) -> str:
    """Case, spaces, and hyphens do not change which item a name is."""

    return re.sub(r"[\s\-]+", "", name).casefold()


def canonical_item(name: str, stock: dict[str, int]) -> str:
    """Inventory spelling when the name matches. Otherwise the name with separators removed."""

    key = item_key(name)
    for item in stock:
        if item_key(item) == key:
            return item
    return re.sub(r"[\s\-]+", "", name)


def normalize_item_name(description: str) -> tuple[str, str | None]:
    """'Widget A' -> WidgetA. 'WidgetA (rush order)' -> WidgetA, note 'rush order'."""

    text = " ".join(description.strip().split())
    note = None
    match = re.fullmatch(r"(.+?)\s*\(([^)]*)\)\s*", text)
    if match:
        text = match.group(1).strip()
        note = match.group(2).strip() or None
    code = re.sub(r"[\s\-]+", "", text)
    return code, note


def non_usd_message(currency: str) -> str:
    return f"Non-USD currency ({currency})."


def resolve_currency(explicit: str | None, raw_text: str | None) -> str:
    """USD unless the document or the currency field says otherwise.

    Missing currency with dollar amounts is USD. Euro markers and a non-USD
    code are kept as that code. Nothing is converted.
    """

    code = (explicit or "").strip().upper()
    text = raw_text or ""
    if _EURO.search(text) or code in {"EUR", "EURO", "EUROS"}:
        return "EUR"
    if len(code) == 3 and code.isalpha():
        return code
    return "USD"


def summed_quantities(
    invoice: ExtractedInvoice,
    stock: dict[str, int] | None = None,
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for line in invoice.line_items:
        if line.quantity <= 0:
            continue
        name = canonical_item(line.item, stock) if stock is not None else line.item
        totals[name] = totals.get(name, Decimal("0")) + line.quantity
    return totals
