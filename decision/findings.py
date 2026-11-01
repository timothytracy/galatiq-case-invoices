"""Findings for one invoice. Detection only. Precedence lives in outcome.py."""

import re
from datetime import datetime
from decimal import Decimal

from models import ExtractedInvoice, Finding

from decision.invoice_fields import (
    as_money,
    canonical_item,
    different,
    non_usd_message,
    resolve_currency,
    show_money,
    show_qty,
    summed_quantities,
)
from decision.outcome import REJECT_CODES
from decision.policy import APPROVAL_THRESHOLD_USD, POLICY

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%d-%B-%Y", "%B %d, %Y", "%b %d, %Y")
_MISSING_TEXT = {
    "vendor": "Vendor is missing.",
    "due_date": "Due date is missing.",
    "line_items": "No line items.",
    "total": "Total is missing.",
}

_FORMERLY = re.compile(r"\bformerly\b", re.IGNORECASE)
_URGENCY = re.compile(r"\burgent\b|\bimmediate(?:ly)?\b|pay\s+immediately", re.IGNORECASE)
_WIRE = re.compile(r"\bwire\s+(?:transfer|only)\b|\bwire\s+to\b", re.IGNORECASE)
_NEW_ACCOUNT = re.compile(
    r"new\s+(?:bank|remittance)\s+account"
    r"|remittance\s+(?:account|details)\s+(?:changed|updated)"
    r"|bank\s+account\s+(?:changed|updated|is new)"
    r"|updated\s+bank\s+details",
    re.IGNORECASE,
)
_REVISED_PHRASE = re.compile(r"revised\s+invoice", re.IGNORECASE)
_REVISED_FIELD = re.compile(
    r"""(?ix)(?:["']revision["']\s*:\s*["'](?!null)[^"']+["'])
        |\brevision\s*[:=]\s*(?!null\b)[A-Za-z0-9]+
        |\brev\.?\s*[:=#]\s*[A-Za-z0-9]+"""
)
_CREDIT = re.compile(r"\bcredit\s+(?:note|memo)\b", re.IGNORECASE)
# Imperative phrases aimed at the system. The bare word "approve" is not here.
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?previous\s+instructions"
    r"|approve\s+this\s+invoice"
    r"|skip\s+review"
    r"|\bsystem\s*:",
    re.IGNORECASE,
)
# Illustrative only: a unit or apartment number reads as a home.
# The extraction model sets noncommercial_address for anything else.
# This is not a list of streets or landmarks.
_RESIDENTIAL_UNIT = re.compile(r"\b(?:apt|apartment|unit)\b", re.IGNORECASE)


def injection_phrase(text: str) -> bool:
    """True for an imperative, system-directed phrase. Not the bare word approve."""

    return _INJECTION.search(text or "") is not None


def math_reconciles(invoice: ExtractedInvoice) -> bool:
    """True when the lines, tax, and shipping add up to the printed total."""

    if invoice.total is None or not invoice.line_items:
        return False
    return not _math_findings(invoice)


def collect_findings(
    invoice: ExtractedInvoice,
    raw_text: str,
    stock: dict[str, int],
) -> tuple[list[Finding], str]:
    """Every finding for this invoice, and the currency code that was resolved."""

    currency = resolve_currency(invoice.currency, raw_text)
    findings: list[Finding] = []

    for field in POLICY.required_fields:
        if _field_missing(invoice, field):
            findings.append(
                Finding(
                    code="MISSING_FIELD",
                    message=_MISSING_TEXT[field],
                    severity="blocking",
                    item=field,
                )
            )

    credit = (
        _is_credit_label(invoice, raw_text)
        and math_reconciles(invoice)
        and _has_negative_amount(invoice)
        and all(line.quantity != 0 for line in invoice.line_items)
    )
    if credit:
        findings.append(
            Finding(
                code="CREDIT_NOTE",
                message="Labeled credit note and the math reconciles.",
                severity="warning",
            )
        )
    else:
        for line in invoice.line_items:
            if line.quantity <= 0:
                findings.append(
                    Finding(
                        code="BAD_QUANTITY",
                        item=line.item,
                        message=f"{line.item} quantity {show_qty(line.quantity)} is negative or zero.",
                        severity="blocking",
                    )
                )
        if _has_negative_amount(invoice):
            findings.append(
                Finding(
                    code="NEGATIVE_AMOUNT",
                    message="Negative amount is not a labeled credit note.",
                    severity="blocking",
                    item="total",
                )
            )

    for item, qty in summed_quantities(invoice, stock).items():
        if item not in stock:
            findings.append(
                Finding(
                    code="UNKNOWN_ITEM",
                    item=item,
                    message=f"{item} is not in the inventory database.",
                    severity="blocking",
                )
            )
        elif qty > Decimal(stock[item]):
            findings.append(
                Finding(
                    code="STOCK_SHORTFALL",
                    item=item,
                    message=f"{item} quantity {show_qty(qty)} exceeds stock of {stock[item]}.",
                    severity="blocking",
                )
            )

    findings.extend(_math_findings(invoice))

    if currency not in POLICY.allowed_currencies:
        findings.append(
            Finding(
                code="NON_USD",
                message=non_usd_message(currency),
                severity="warning",
                item="currency",
            )
        )

    if _is_revised(invoice, raw_text):
        findings.append(
            Finding(
                code="REVISED_INVOICE",
                message="This invoice is marked as a revision.",
                severity="warning",
            )
        )

    blob = _blob(raw_text, invoice.vendor, invoice.notes, invoice.payment_terms)
    if _FORMERLY.search(blob):
        findings.append(
            Finding(
                code="VENDOR_RENAMED",
                message="The vendor name says formerly.",
                severity="warning",
                item="vendor",
            )
        )

    fraud: list[Finding] = []
    if _URGENCY.search(blob):
        fraud.append(
            Finding(code="FRAUD_SIGNAL", message="Urgency pressure is a fraud signal.", severity="warning")
        )
    if _WIRE.search(blob):
        fraud.append(
            Finding(code="FRAUD_SIGNAL", message="Wire transfer is a fraud signal.", severity="warning")
        )
    new_account = bool(_NEW_ACCOUNT.search(blob))
    if new_account:
        fraud.append(
            Finding(
                code="FRAUD_SIGNAL",
                message="A new remittance or bank account is a fraud signal.",
                severity="warning",
            )
        )
    findings.extend(fraud)

    if currency in POLICY.allowed_currencies:
        for line in invoice.line_items:
            ref = POLICY.reference_prices.get(canonical_item(line.item, stock))
            if ref is None or line.unit_price is None or not different(line.unit_price, ref):
                continue
            findings.append(
                Finding(
                    code="PRICE_VARIANCE",
                    item=line.item,
                    message=(
                        f"Unit price {show_money(line.unit_price)} for {line.item} "
                        f"differs from the reference price {show_money(ref)}."
                    ),
                    severity="warning",
                )
            )
            if line.unit_price > ref * (Decimal("1") + POLICY.price_fraud_ratio):
                high = Finding(
                    code="FRAUD_SIGNAL",
                    item=line.item,
                    message=(
                        f"Unit price {show_money(line.unit_price)} for {line.item} "
                        "is well above the reference price."
                    ),
                    severity="warning",
                )
                findings.append(high)
                fraud.append(high)

    for line in invoice.line_items:
        if line.description and re.search(r"\([^)]+\)", line.description) and line.note:
            findings.append(
                Finding(
                    code="DESCRIPTOR",
                    item=line.item,
                    message=(
                        f"Descriptor on {line.description}: matched as {line.item}, "
                        f"{line.note!r} kept as a note."
                    ),
                    severity="warning",
                )
            )

    if _near_threshold(invoice.total, currency):
        findings.append(
            Finding(
                code="NEAR_THRESHOLD",
                message=(
                    f"Total {show_money(invoice.total)} is within "
                    f"{POLICY.near_threshold_ratio * 100:.0f}% under the "
                    f"{show_money(APPROVAL_THRESHOLD_USD)} threshold."
                ),
                severity="warning",
                item="total",
            )
        )

    if _address_noted(invoice):
        findings.append(
            Finding(
                code="NONCOMMERCIAL_ADDRESS",
                message=(
            "Vendor address looks non-commercial and is noted: "
            f"{invoice.vendor_address or 'the vendor address'}."
        ),
                severity="warning",
                item="vendor_address",
            )
        )

    if invoice.due_date and not _dated(invoice.due_date):
        findings.append(
            Finding(
                code="UNPARSED_DATE",
                message=f"Due date {invoice.due_date!r} is not a date.",
                severity="warning",
                item="due_date",
            )
        )

    if _raw_has_ocr_o(raw_text) and math_reconciles(invoice):
        findings.append(
            Finding(
                code="OCR_FIX",
                message="OCR O-to-0 fix applied because the math reconciles.",
                severity="info",
            )
        )

    if invoice.embedded_instructions and injection_phrase(_blob(raw_text, invoice.notes)):
        findings.append(
            Finding(
                code="EMBEDDED_INSTRUCTIONS",
                message="Embedded instructions aimed at the system.",
                severity="blocking",
            )
        )

    rejected = any(
        finding.severity == "blocking" and finding.code in REJECT_CODES for finding in findings
    )
    over = (
        invoice.total is not None
        and currency in POLICY.allowed_currencies
        and invoice.total > APPROVAL_THRESHOLD_USD
    )
    if not rejected and new_account and over:
        findings.append(
            Finding(
                code="NEW_REMITTANCE",
                message="Clean invoice over the threshold with a new remittance or bank account.",
                severity="warning",
            )
        )
    elif not rejected and len(fraud) >= 2:
        findings.append(
            Finding(code="FRAUD_CLUSTER", message="Two or more fraud signals.", severity="warning")
        )
    return findings, currency


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
    for item, qty in summed_quantities(invoice, stock).items():
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


def _field_missing(invoice: ExtractedInvoice, field: str) -> bool:
    if field == "vendor":
        return not invoice.vendor
    if field == "due_date":
        return not invoice.due_date
    if field == "line_items":
        return not invoice.line_items
    if field == "total":
        return invoice.total is None
    return True


def _blob(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def _dated(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            continue
    return False


def _has_negative_amount(invoice: ExtractedInvoice) -> bool:
    if invoice.total is not None and invoice.total < 0:
        return True
    return any(
        line.quantity < 0 or (line.amount is not None and line.amount < 0) for line in invoice.line_items
    )


def _is_revised(invoice: ExtractedInvoice, raw_text: str) -> bool:
    if invoice.revision and invoice.revision.strip():
        return True
    blob = _blob(raw_text, invoice.notes)
    return bool(_REVISED_PHRASE.search(blob) or _REVISED_FIELD.search(blob))


def _is_credit_label(invoice: ExtractedInvoice, raw_text: str) -> bool:
    return bool(_CREDIT.search(_blob(raw_text, invoice.notes, invoice.payment_terms)))


def _address_noted(invoice: ExtractedInvoice) -> bool:
    """The model flags a non-commercial address. Apt/Unit is only an illustration."""

    if invoice.noncommercial_address:
        return True
    return bool(invoice.vendor_address and _RESIDENTIAL_UNIT.search(invoice.vendor_address))


def _near_threshold(total: Decimal | None, currency: str) -> bool:
    if total is None or currency not in POLICY.allowed_currencies:
        return False
    if total >= APPROVAL_THRESHOLD_USD:
        return False
    return (APPROVAL_THRESHOLD_USD - total) <= APPROVAL_THRESHOLD_USD * POLICY.near_threshold_ratio


def _raw_has_ocr_o(raw_text: str) -> bool:
    for match in re.finditer(r"[0-9O][0-9O,]*\.?[0-9O]*", raw_text or ""):
        token = match.group(0)
        if "O" in token and any(character.isdigit() for character in token):
            return True
    return False


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
                        code="TOTAL_MISMATCH",
                        item=line.item,
                        severity="blocking",
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
                    code="TOTAL_MISMATCH",
                    item=line.item,
                    severity="blocking",
                    message=f"{line.item} has no unit price or line amount.",
                )
            )

    line_sum = as_money(line_sum)
    if invoice.subtotal is not None and different(invoice.subtotal, line_sum):
        findings.append(
            Finding(
                code="TOTAL_MISMATCH",
                item="subtotal",
                severity="blocking",
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
                code="TOTAL_MISMATCH",
                item="total",
                severity="blocking",
                message=(
                    f"Printed total {show_money(invoice.total)} does not equal "
                    f"{show_money(expected_total)} (subtotal + tax + shipping)."
                ),
            )
        )
    return findings
