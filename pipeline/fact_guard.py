"""Drop critique and audit claims the code has already checked, or cannot find on the page.

partition_concerns: critique may drop a stock or totals_mismatch claim. Both
steps may drop a future_date claim.

quote_gate: a claim code does not confirm stands only when the draft gives an
innocent reading and the claim quotes prose found on the page. Each drop is
logged with the rule that fired.
"""

import re
from datetime import date
from decimal import Decimal

from models import FactConcern
from decision.findings import math_reconciles, normalize_quote, parse_printed_date
from decision.invoice_fields import canonical_item, show_money, show_qty, summed_quantities
from decision.policy import FRAUD_QUOTE_MIN_CHARS, POLICY

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
QUOTE_RULE = (
    "A concern the code cannot confirm stands only with a quote: text copied exactly from the "
    f"invoice text, at least {FRAUD_QUOTE_MIN_CHARS} characters, from the page's wording "
    "(notes, terms, remarks), not only a date, vendor, item, or total line. Its text must say, in your "
    "own words, what the quoted wording contradicts in the bill; a concern that only repeats "
    "the page is ignored. Also give innocent_reading (string): the most plausible innocent "
    "reading of the page, which the quote makes unlikely."
)
AUDIT_CLAIMS = (
    "feedback is a list of strings, or objects with category, item, field, text, and quote. "
    "category future_date, with field invoice_date or due_date, claims that date is after today. "
    "Any other feedback is a string, including a disagreement between the raw text and the extraction. "
    "An ESCALATE that cites no finding code needs a feedback object with a quote, and "
    "innocent_reading."
)

_DATE_FIELDS = {"invoice_date", "due_date"}
# Lines whose facts code has already checked: dates, item lines, subtotal and total.
_FIELD_LINE = re.compile(
    r"^\s*(?:items?\s*:|vendor\s*:|invoice\s*(?:number|no\.?|#)|(?:invoice\s+)?date\b|due\s*date|"
    r"sub\s*-?\s*total|total(?:\s+amount)?\s*(?:due)?\s*:|amount\s+due)",
    re.IGNORECASE,
)
_PROSE_LINE = re.compile(r"^\s*(?:notes?|memo|remarks?|comments?|payment\s+terms|terms)\b", re.IGNORECASE)
_MONEY_OR_QTY = re.compile(r"\$|\bqty\b|\bunit\s+price\b|\d+\.\d{2}\b", re.IGNORECASE)


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


def quote_gate(concerns, invoice, stock, as_of, raw_text: str, innocent_reading: str):
    """Return kept concerns and a log row for each one the quote check removed.

    A claim code confirms (a real shortage, totals that do not reconcile, a date
    after today) is kept without a quote. Every other claim needs a quote.
    """

    kept = []
    dropped = []
    for concern in concerns:
        if invoice is not None and _confirmed(concern, invoice, stock or {}, as_of):
            kept.append(concern)
            continue
        reason = quote_dropped_reason(concern.quote, invoice, raw_text, innocent_reading)
        if reason is None and _restates_quote(concern):
            reason = (
                "concern_restates_quote",
                "The concern only repeats the page and does not say what the wording contradicts.",
            )
        if reason is None:
            kept.append(concern)
            continue
        rule, detail = reason
        dropped.append(
            {
                "rule": rule,
                "category": _category(concern),
                "item": concern.item,
                "field": concern.field,
                "text": concern_text(concern),
                "quote": concern.quote,
                "detail": detail,
            }
        )
    return kept, dropped


def quote_dropped_reason(quote: str, invoice, raw_text: str, innocent_reading: str):
    """None when the quote stands. Otherwise (rule, detail)."""

    if not (innocent_reading or "").strip():
        return "no_innocent_reading", "No innocent reading was given."
    squashed = normalize_quote(quote)
    if len(squashed) < FRAUD_QUOTE_MIN_CHARS:
        return "quote_too_short", f"The quote is shorter than {FRAUD_QUOTE_MIN_CHARS} characters."
    if squashed not in normalize_quote(raw_text):
        return "quote_not_on_page", "The quote is not in the invoice text."
    if not _quote_has_prose(squashed, invoice, raw_text):
        return (
            "quote_structured_only",
            "The quote is only a date, vendor, item, or total line, which code has already checked.",
        )
    return None


def _restates_quote(concern) -> bool:
    text = re.sub(r"[^\w]+", " ", normalize_quote(concern_text(concern))).strip()
    quote = re.sub(r"[^\w]+", " ", normalize_quote(concern.quote)).strip()
    return not text or text in quote


def _confirmed(concern, invoice, stock, as_of) -> bool:
    category = _category(concern)
    if category == "future_date":
        field = (concern.field or "").strip().casefold()
        parsed = parse_printed_date(getattr(invoice, field, None)) if field in _DATE_FIELDS else None
        return parsed is not None and parsed > as_of
    if category == "totals_mismatch":
        return not math_reconciles(invoice)
    if category == "stock":
        raw = (concern.item or "").strip()
        if not raw:
            return False
        name = canonical_item(raw, stock)
        qty = summed_quantities(invoice, stock).get(name)
        return name in stock and qty is not None and qty > Decimal(stock[name])
    return False


def _quote_has_prose(squashed: str, invoice, raw_text: str) -> bool:
    """True when some occurrence of the quote touches a line that is not a checked field."""

    fmt = ""
    if invoice is not None and invoice.source is not None:
        fmt = (invoice.source.format or "").casefold()
    if fmt and fmt != "txt":
        return _overlaps_notes(squashed, invoice)
    items = []
    if invoice is not None:
        items = [line.item.casefold() for line in invoice.line_items if line.item]
    text = ""
    prose_mask: list[bool] = []
    for line in (raw_text or "").splitlines():
        part = normalize_quote(line)
        if not part:
            continue
        if text:
            text += " "
            prose_mask.append(False)
        text += part
        prose_mask.extend([not _field_line(line, items)] * len(part))
    start = text.find(squashed)
    while start != -1:
        span = range(start, start + len(squashed))
        if any(prose_mask[i] and not text[i].isspace() for i in span):
            return True
        start = text.find(squashed, start + 1)
    return _overlaps_notes(squashed, invoice)


def _field_line(line: str, items: list[str]) -> bool:
    if _PROSE_LINE.match(line):
        return False
    if _FIELD_LINE.match(line):
        return True
    folded = line.casefold()
    return any(item in folded for item in items) and _MONEY_OR_QTY.search(line) is not None


def _overlaps_notes(squashed: str, invoice) -> bool:
    if invoice is None:
        return False
    prose = normalize_quote(" ".join(part for part in [invoice.notes, invoice.payment_terms] if part))
    if not prose:
        return False
    if squashed in prose:
        return True
    size = FRAUD_QUOTE_MIN_CHARS
    return any(squashed[i : i + size] in prose for i in range(0, len(squashed) - size + 1))


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
