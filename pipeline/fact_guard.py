"""Drop critique and audit concerns that contradict facts the code already checked.

A concern is dropped only when its category and item or field disagree with
those facts. Any other category is kept. The caller logs every dropped concern.
"""

from datetime import date
from decimal import Decimal

from models import FactConcern
from decision.findings import math_reconciles, parse_printed_date
from decision.invoice_fields import canonical_item, show_money, show_qty, summed_quantities
from decision.policy import POLICY

# Shared by the critique and audit prompts. The JSON key is verified_by_code.
VERIFIED_SENTENCE = (
    "verified_by_code was verified by code. "
    "stock_sufficient is the summed requested quantity compared with available stock; "
    "an equal quantity is sufficient. "
    "totals_reconcile is true when the line totals and the grand total match. "
    "A printed date is after today only when it is later than today. "
    "Do not contradict those facts. "
    "Return concerns as objects with category, item, field, and text. "
    "category is stock, totals, date, or other. "
    "For stock, item is the inventory name. "
    "For date, field is invoice_date or due_date. "
    "For totals, field is total or the line item. "
    "A concern with category other can still make the decision stricter."
)

_DATE_FIELDS = {"invoice_date", "due_date"}
_CHECKED = {"stock", "totals", "date"}


def verified_payload(invoice, stock: dict[str, int], as_of: date) -> dict:
    """Facts the decision code has already checked, for the critique and audit prompts."""

    stock = stock or {}
    summed = summed_quantities(invoice, stock)
    lines = []
    for line in invoice.line_items:
        name = canonical_item(line.item, stock)
        requested = summed.get(name)
        available = stock.get(name)
        expected = None
        if line.unit_price is not None:
            expected = show_money(line.quantity * line.unit_price)
        ref = POLICY.reference_prices.get(name)
        lines.append(
            {
                "item": name,
                "requested_quantity": show_qty(line.quantity),
                "summed_requested_quantity": None if requested is None else show_qty(requested),
                "available_stock": available,
                "stock_sufficient": _covered(name, requested, stock),
                "unit_price": None if line.unit_price is None else show_money(line.unit_price),
                "line_total": None if line.amount is None else show_money(line.amount),
                "expected_line_total": expected,
                "reference_price": None if ref is None else show_money(ref),
            }
        )
    return {
        "today": as_of.isoformat(),
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "invoice_date_after_today": _after_today(invoice.invoice_date, as_of),
        "due_date_after_today": _after_today(invoice.due_date, as_of),
        "lines": lines,
        "subtotal": _shown(invoice.subtotal),
        "tax": _shown(invoice.tax_amount),
        "shipping": _shown(invoice.shipping),
        "grand_total": _shown(invoice.total),
        "totals_reconcile": math_reconciles(invoice),
        "reference_prices": {
            item: show_money(price) for item, price in POLICY.reference_prices.items()
        },
    }


def concern_text(concern: FactConcern) -> str:
    text = (concern.text or "").strip()
    if text:
        return text
    parts = [(concern.category or "").strip(), (concern.item or "").strip(), (concern.field or "").strip()]
    return " ".join(part for part in parts if part)


def partition_concerns(
    concerns: list[FactConcern],
    invoice,
    stock: dict[str, int],
    as_of: date,
) -> tuple[list[FactConcern], list[dict]]:
    """Split concerns into those that stand and those that contradict a verified fact."""

    kept: list[FactConcern] = []
    dropped: list[dict] = []
    for concern in concerns:
        reason = _contradiction(concern, invoice, stock, as_of)
        if reason is None:
            kept.append(concern)
            continue
        dropped.append(
            {
                "category": _category(concern),
                "item": concern.item,
                "field": concern.field,
                "text": concern_text(concern),
                "reason": reason,
            }
        )
    return kept, dropped


def apply_concern_guard(
    verdict: str,
    feedback: list[str],
    cited_codes: list,
    kept: list,
    dropped: list,
) -> tuple[str, list[str]]:
    """A send-back or escalation with only contradicted concerns does not stand.

    A cited finding code is a separate claim and is left to the audit's other guards.
    One kept concern leaves the model's verdict unchanged.
    """

    cited = [str(code).strip() for code in cited_codes if str(code).strip()]
    if dropped and not kept and verdict in {"SEND_BACK", "ESCALATE"} and not cited:
        return "PASS", []
    return verdict, list(feedback)


def _contradiction(concern: FactConcern, invoice, stock: dict[str, int], as_of: date) -> str | None:
    category = _category(concern)
    if category not in _CHECKED:
        return None
    if category == "stock":
        return _stock_contradiction(concern, invoice, stock or {})
    if category == "totals":
        if math_reconciles(invoice):
            return "Line totals and the grand total reconcile."
        return None
    return _date_contradiction(concern, invoice, as_of)


def _stock_contradiction(concern: FactConcern, invoice, stock: dict[str, int]) -> str | None:
    summed = summed_quantities(invoice, stock)
    name = _concern_item(concern, stock)
    if name is None:
        if summed and all(_covered(item, qty, stock) for item, qty in summed.items()):
            return "Every item's requested quantity is within available stock."
        return None
    qty = summed.get(name)
    if qty is None or name not in stock:
        return None
    if qty <= Decimal(stock[name]):
        return f"{name} requested {show_qty(qty)} and available stock is {stock[name]}."
    return None


def _date_contradiction(concern: FactConcern, invoice, as_of: date) -> str | None:
    field, printed = _concern_date(concern, invoice)
    parsed = parse_printed_date(printed)
    if parsed is None or parsed > as_of:
        return None
    shown = parsed.isoformat()
    if field in _DATE_FIELDS:
        return f"{field} {shown} is not after {as_of.isoformat()}."
    return f"{shown} is not after {as_of.isoformat()}."


def _concern_date(concern: FactConcern, invoice) -> tuple[str, str | None]:
    field = (concern.field or "").strip()
    token = field.casefold()
    if token in _DATE_FIELDS:
        return token, getattr(invoice, token, None)
    if parse_printed_date(field) is not None:
        return token, field
    item = (concern.item or "").strip()
    if parse_printed_date(item) is not None:
        return "", item
    return token, None


def _concern_item(concern: FactConcern, stock: dict[str, int]) -> str | None:
    raw = (concern.item or "").strip()
    if not raw:
        return None
    return canonical_item(raw, stock)


def _category(concern: FactConcern) -> str:
    return (concern.category or "other").strip().casefold() or "other"


def _covered(name: str, qty: Decimal | None, stock: dict[str, int]) -> bool:
    return qty is not None and name in stock and qty <= Decimal(stock[name])


def _after_today(printed: str | None, as_of: date) -> bool | None:
    parsed = parse_printed_date(printed)
    if parsed is None:
        return None
    return parsed > as_of


def _shown(amount: Decimal | None) -> str | None:
    if amount is None:
        return None
    return show_money(amount)
