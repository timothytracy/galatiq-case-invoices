"""Business rules later phases will apply. Phase 1 records them and does not enforce them.

The numbers here are the ones data/labels.json and docs/DECISIONS.md describe.
"""

import re
from decimal import Decimal, ROUND_HALF_UP

# The inventory table is a fixed snapshot. Approving an invoice does not reduce stock.
STOCK_IS_SNAPSHOT = True

# Sum quantities for the same item on one invoice, then compare.
# quantity > stock fails. quantity == stock passes.
EUR_USD_RATE = Decimal("1.10")
APPROVAL_THRESHOLD_USD = Decimal("10000")

# Batch order is filename order. The first file for an invoice number can be paid.
# A later file with the same number is a duplicate or revision and is not paid again.
_INVOICE_DIGITS = re.compile(r"(\d+)")
_ITEM_NOTE = re.compile(r"(.+?)\s*\(([^)]*)\)\s*\Z")


def as_money(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_usd(amount: Decimal, currency: str) -> Decimal | None:
    """Convert for the $10,000 check. Payment still uses the printed currency."""

    code = currency.strip().upper()
    if code == "USD":
        return as_money(amount)
    if code == "EUR":
        return as_money(amount * EUR_USD_RATE)
    return None


def normalize_invoice_number(raw: str | None) -> str | None:
    """Turn '1002' and 'INV 1012' into 'INV-1002' and 'INV-1012'."""

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    match = _INVOICE_DIGITS.search(text)
    if not match:
        return text
    return f"INV-{match.group(1)}"


def normalize_item_name(description: str) -> tuple[str, str | None]:
    """'Widget A' -> WidgetA. 'WidgetA (rush order)' -> WidgetA, note 'rush order'."""

    text = " ".join(description.strip().split())
    note = None
    match = _ITEM_NOTE.fullmatch(text)
    if match:
        text = match.group(1).strip()
        note = match.group(2).strip() or None
    code = re.sub(r"\s+", "", text)
    return code, note
