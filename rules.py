"""Deterministic invoice rules.

Stock is a fixed snapshot. Quantities are summed per item on one invoice.
quantity greater than stock fails. An equal quantity passes.
Any currency other than USD is manual review. There is no conversion rate.
These rules decide the outcome. The model does not get to loosen them.
"""

import re
from decimal import Decimal, ROUND_HALF_UP

from models import Assessment, ExtractedInvoice, Finding

# A USD total above this goes to the critique node, then back to approval.
APPROVAL_THRESHOLD_USD = Decimal("10000")

# One critique pass, then approval revises once and the graph moves on.
CRITIQUE_CAP = 1

# The audit may send the invoice back to ingestion this many times.
MAX_SEND_BACKS = 3

MONEY = Decimal("0.01")
HARD_REJECT = (
    "negative_quantity",
    "missing_field",
    "unknown_item",
    "stock_mismatch",
    "math_mismatch",
)

_EURO = re.compile(r"€|\beuros?\b|\beur\b", re.IGNORECASE)
_FRAUD = (
    (
        re.compile(r"\bformerly\b", re.IGNORECASE),
        "Vendor identity change (formerly) requires manual review.",
    ),
    (
        re.compile(r"\bwire transfer\b", re.IGNORECASE),
        "Wire transfer language requires manual review.",
    ),
    (
        re.compile(r"pay immediately", re.IGNORECASE),
        "Payment-pressure language requires manual review.",
    ),
    (
        re.compile(r"\burgent\b", re.IGNORECASE),
        "Urgency language requires manual review.",
    ),
)

RULE_TEXT = {
    "negative_quantity": "A quantity of zero or below is a data integrity failure.",
    "missing_field": "Vendor, line items, due date, and total are required.",
    "unknown_item": "Every item must exist in the inventory snapshot.",
    "stock_mismatch": "Summed quantity for an item cannot exceed snapshot stock.",
    "math_mismatch": "Line math and subtotal plus tax plus shipping must match the printed total within 0.01.",
    "non_usd": "Any currency other than USD is manual review. There is no conversion.",
    "fraud_signal": "A fraud signal is manual review and is not auto-paid.",
}

PASSED_RULES = [
    "Quantities must be positive.",
    "Item quantities are summed and compared with the fixed inventory snapshot. An equal quantity passes.",
    "Printed total must match the lines, tax, and shipping within 0.01.",
    "Currency must be USD. Any other currency is manual review.",
    "Fraud signals are manual review.",
    "A USD total over 10000 goes to critique before audit.",
]


def as_money(amount: Decimal) -> Decimal:
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)


def show_qty(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def show_money(value: Decimal) -> str:
    return f"{as_money(value):.2f}"


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


def normalize_item_name(description: str) -> tuple[str, str | None]:
    """'Widget A' -> WidgetA. 'WidgetA (rush order)' -> WidgetA, note 'rush order'."""

    text = " ".join(description.strip().split())
    note = None
    match = re.fullmatch(r"(.+?)\s*\(([^)]*)\)\s*", text)
    if match:
        text = match.group(1).strip()
        note = match.group(2).strip() or None
    code = re.sub(r"\s+", "", text)
    return code, note


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


def non_usd_message(currency: str) -> str:
    return f"non-USD currency ({currency}), manual review required"


def summed_quantities(invoice: ExtractedInvoice) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for line in invoice.line_items:
        if line.quantity <= 0:
            continue
        totals[line.item] = totals.get(line.item, Decimal("0")) + line.quantity
    return totals


def different(left: Decimal, right: Decimal) -> bool:
    return abs(as_money(left) - as_money(right)) > MONEY


def assess(
    invoice: ExtractedInvoice,
    raw_text: str,
    stock: dict[str, int],
) -> Assessment:
    """Apply the hard rules to one invoice. The file is judged on its own."""

    currency = resolve_currency(invoice.currency, raw_text)
    findings: list[Finding] = []

    if not invoice.vendor:
        findings.append(Finding(code="missing_field", message="Vendor is missing."))
    if not invoice.due_date:
        findings.append(Finding(code="missing_field", message="Due date is missing."))
    if not invoice.line_items:
        findings.append(Finding(code="missing_field", message="No line items."))
    if invoice.total is None:
        findings.append(Finding(code="missing_field", message="Total is missing."))

    for line in invoice.line_items:
        if line.quantity <= 0:
            findings.append(
                Finding(
                    code="negative_quantity",
                    item=line.item,
                    message=f"{line.item} quantity {show_qty(line.quantity)} is negative or zero.",
                )
            )

    for item, qty in summed_quantities(invoice).items():
        if item not in stock:
            findings.append(
                Finding(
                    code="unknown_item",
                    item=item,
                    message=f"{item} is not in the inventory database.",
                )
            )
        elif qty > Decimal(stock[item]):
            findings.append(
                Finding(
                    code="stock_mismatch",
                    item=item,
                    message=f"{item} quantity {show_qty(qty)} exceeds stock of {stock[item]}.",
                )
            )

    findings.extend(_math_findings(invoice))

    if currency != "USD":
        findings.append(Finding(code="non_usd", message=non_usd_message(currency)))

    blob = "\n".join([raw_text or "", invoice.vendor or "", invoice.notes or ""])
    for pattern, message in _FRAUD:
        if pattern.search(blob):
            findings.append(Finding(code="fraud_signal", message=message))

    outcome, reason = _decide(invoice, findings, stock)
    return Assessment(outcome=outcome, reason=reason, findings=findings, currency=currency)


def evidence_lines(
    invoice: ExtractedInvoice,
    stock: dict[str, int],
    findings: list[Finding],
) -> list[str]:
    """Facts the reasoning may cite. Built from the invoice, the stock, and the findings."""

    lines = []
    if invoice.total is None:
        lines.append("total is missing")
    else:
        lines.append(f"total {show_money(invoice.total)} {invoice.currency}")
    for item, qty in summed_quantities(invoice).items():
        if item in stock:
            lines.append(f"{item} quantity {show_qty(qty)} compared with stock {stock[item]}")
        else:
            lines.append(f"{item} quantity {show_qty(qty)} is not in inventory")
    for line in invoice.line_items:
        if line.quantity <= 0:
            lines.append(f"{line.item} quantity {show_qty(line.quantity)} is negative or zero")
    for finding in findings:
        lines.append(finding.message)
    if not findings:
        lines.append("No stock, math, currency, or fraud finding.")
    return lines


def _decide(
    invoice: ExtractedInvoice,
    findings: list[Finding],
    stock: dict[str, int],
) -> tuple[str, str]:
    hard = [finding for finding in findings if finding.code in HARD_REJECT]
    if hard:
        ordered = sorted(hard, key=lambda finding: HARD_REJECT.index(finding.code))
        return "reject", " ".join(finding.message for finding in ordered)

    holds = [finding for finding in findings if finding.code in {"non_usd", "fraud_signal"}]
    if holds:
        holds.sort(key=lambda finding: 0 if finding.code == "non_usd" else 1)
        return "manual_review", " ".join(finding.message for finding in holds)

    bits = []
    for item, qty in summed_quantities(invoice).items():
        bits.append(f"{item} {show_qty(qty)} (stock {stock[item]})")
    detail = ", ".join(bits) if bits else "no positive quantities"
    return "approve", f"{detail} within stock and the printed total matches the lines."


def _math_findings(invoice: ExtractedInvoice) -> list[Finding]:
    if invoice.total is None or not invoice.line_items:
        return []

    findings = []
    line_sum = Decimal("0")
    for line in invoice.line_items:
        if line.unit_price is not None:
            expected = as_money(line.quantity * line.unit_price)
            line_sum += expected
            if line.amount is not None and different(line.amount, expected):
                findings.append(
                    Finding(
                        code="math_mismatch",
                        item=line.item,
                        message=(
                            f"{line.item} line amount {show_money(line.amount)} does not equal "
                            f"quantity times unit price ({show_money(expected)})."
                        ),
                    )
                )
        elif line.amount is not None:
            line_sum += line.amount
        else:
            findings.append(
                Finding(
                    code="math_mismatch",
                    item=line.item,
                    message=f"{line.item} has no unit price or line amount.",
                )
            )

    line_sum = as_money(line_sum)
    if invoice.subtotal is not None and different(invoice.subtotal, line_sum):
        findings.append(
            Finding(
                code="math_mismatch",
                message=(
                    f"Printed subtotal {show_money(invoice.subtotal)} does not equal "
                    f"the line sum {show_money(line_sum)}."
                ),
            )
        )

    base = invoice.subtotal if invoice.subtotal is not None else line_sum
    tax = invoice.tax_amount if invoice.tax_amount is not None else Decimal("0")
    shipping = invoice.shipping if invoice.shipping is not None else Decimal("0")
    expected_total = as_money(base + tax + shipping)
    if different(expected_total, invoice.total):
        findings.append(
            Finding(
                code="math_mismatch",
                message=(
                    f"Printed total {show_money(invoice.total)} does not equal "
                    f"{show_money(expected_total)} (subtotal + tax + shipping)."
                ),
            )
        )
    return findings
