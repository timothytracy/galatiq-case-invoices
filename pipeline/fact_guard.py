"""Drop a narrow set of critique and audit claims the code has already checked.

Critique may drop a stock or totals_mismatch claim. Both steps may drop a
future_date claim. Every other concern is kept. Each drop is logged with the
rule that fired.
"""

from datetime import date
from decimal import Decimal

from models import FactConcern
from decision.findings import math_reconciles, parse_printed_date
from decision.invoice_fields import canonical_item, show_money, show_qty, summed_quantities
from decision.policy import POLICY

# The JSON key in both prompts is verified_by_code.
VERIFIED_FACTS = (
    "verified_by_code was verified by code from the extracted invoice: "
    "summed requested quantity against available stock, whether the totals reconcile, "
    "the reference prices, today, and whether the invoice date and the due date are after today. "
    "An equal quantity does not exceed stock."
)
CRITIQUE_CLAIMS = (
    "Each concern has category, item, field, and text. "
    "category stock, with item set, claims the requested quantity exceeds available stock. "
    "category totals_mismatch claims the line totals and the grand total do not reconcile. "
    "category future_date, with field invoice_date or due_date, claims that date is after today. "
    "Any other concern, including any other remark about a date, uses category other."
)
AUDIT_CLAIMS = (
    "feedback is a list of strings, or objects with category, item, field, and text. "
    "category future_date, with field invoice_date or due_date, claims that date is after today. "
    "Any other feedback is a string, including a disagreement between the raw text and the extraction."
)

_DATE_FIELDS = {"invoice_date", "due_date"}


def verified_payload(invoice, stock: dict[str, int], as_of: date) -> dict:
    """The facts the prompts show and the guard checks. Nothing the model already has."""

    stock = stock or {}
    summed = summed_quantities(invoice, stock)
    return {
        "today": as_of.isoformat(),
        "stock": [
            {"item": item, "requested": show_qty(qty), "available": stock.get(item)}
            for item, qty in summed.items()
        ],
        "totals_reconcile": math_reconciles(invoice),
        "reference_prices": {
            item: show_money(price) for item, price in POLICY.reference_prices.items()
        },
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "invoice_date_after_today": _after_today(invoice.invoice_date, as_of),
        "due_date_after_today": _after_today(invoice.due_date, as_of),
    }


def concern_text(concern: FactConcern) -> str:
    return (concern.text or "").strip()


def partition_concerns(concerns, invoice, stock, as_of, scope: str):
    """Return kept concerns and a log row for each one a rule removed.

    scope is critique or audit. Stock and totals rules run only for critique,
    because those facts describe the extraction and audit compares it to the raw text.
    """

    kept = []
    dropped = []
    for concern in concerns:
        hit = _rule(concern, invoice, stock or {}, as_of, scope)
        if hit is None:
            kept.append(concern)
            continue
        rule, detail = hit
        dropped.append(
            {
                "rule": rule,
                "category": _category(concern),
                "item": concern.item,
                "field": concern.field,
                "text": concern_text(concern),
                "detail": detail,
            }
        )
    return kept, dropped


def apply_concern_guard(verdict, kept, dropped, cited_codes):
    """PASS only when a stricter verdict has no feedback left and cites no code.

    kept is the feedback that remains. A cited code is left to the other audit guards.
    """

    notes = [concern_text(item) for item in kept if concern_text(item)]
    cited = [str(code).strip() for code in cited_codes if str(code).strip()]
    if dropped and not kept and not cited and verdict in {"SEND_BACK", "ESCALATE"}:
        return "PASS", []
    return verdict, notes


def _rule(concern, invoice, stock, as_of, scope):
    category = _category(concern)
    if category == "future_date":
        return _future_date(concern, invoice, as_of)
    if scope != "critique":
        return None
    if category == "stock":
        return _stock(concern, invoice, stock)
    if category == "totals_mismatch":
        return _totals(invoice)
    return None


def _stock(concern, invoice, stock):
    raw = (concern.item or "").strip()
    if not raw:
        return None
    name = canonical_item(raw, stock)
    qty = summed_quantities(invoice, stock).get(name)
    if qty is None or name not in stock or qty > Decimal(stock[name]):
        return None
    return "stock_sufficient", f"{name} requested {show_qty(qty)} and available stock is {stock[name]}."


def _totals(invoice):
    if not math_reconciles(invoice):
        return None
    return "totals_reconcile", "Line totals and the grand total reconcile."


def _future_date(concern, invoice, as_of):
    field = (concern.field or "").strip().casefold()
    if field not in _DATE_FIELDS:
        return None
    parsed = parse_printed_date(getattr(invoice, field, None))
    if parsed is None or parsed > as_of:
        return None
    return "not_after_today", f"{field} {parsed.isoformat()} is not after {as_of.isoformat()}."


def _category(concern: FactConcern) -> str:
    return (concern.category or "other").strip().casefold() or "other"


def _after_today(printed, as_of: date):
    parsed = parse_printed_date(printed)
    if parsed is None:
        return None
    return parsed > as_of
