"""Findings for one invoice. Detection only. Precedence lives in outcome.py."""

import re
from datetime import date, datetime
from decimal import Decimal

from models import Assessment, ExtractedInvoice, Finding

from decision.invoice_fields import (
    as_money,
    canonical_item,
    different,
    item_key,
    non_usd_message,
    resolve_currency,
    show_money,
    show_qty,
    summed_quantities,
)
from decision.outcome import REJECT_CODES
from decision.policy import APPROVAL_THRESHOLD_USD, POLICY
from decision.wording import explain

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
# "Unit" is omitted: business parks use it. The model flag covers a residence or landmark.
_APARTMENT = re.compile(r"\b(?:apt|apartment)\b", re.IGNORECASE)


def _finding(
    code: str,
    message: str,
    severity: str = "warning",
    item: str | None = None,
) -> Finding:
    return Finding(code=code, message=message, severity=severity, item=item)


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
            findings.append(_finding("MISSING_FIELD", _MISSING_TEXT[field], "blocking", field))

    credit = (
        _is_credit_label(invoice, raw_text)
        and math_reconciles(invoice)
        and _has_negative_amount(invoice)
        and all(line.quantity != 0 for line in invoice.line_items)
    )
    if credit:
        findings.append(_finding("CREDIT_NOTE", "Labeled credit note and the math reconciles."))
    else:
        for line in invoice.line_items:
            if line.quantity <= 0:
                findings.append(
                    _finding(
                        "BAD_QUANTITY",
                        f"{line.item} quantity {show_qty(line.quantity)} is negative or zero.",
                        "blocking",
                        line.item,
                    )
                )
        if _has_negative_amount(invoice):
            findings.append(
                _finding(
                    "NEGATIVE_AMOUNT",
                    "Negative amount is not a labeled credit note.",
                    "blocking",
                    "total",
                )
            )

    for item, qty in summed_quantities(invoice, stock).items():
        if item not in stock:
            # Hint only. The stored item stays the extracted name.
            message = f"{item} is not in the inventory database."
            meant = _likely_name(item, stock)
            if meant is not None:
                message = f"{message} Likely meant {meant}."
            findings.append(_finding("UNKNOWN_ITEM", message, "blocking", item))
        elif qty > Decimal(stock[item]):
            findings.append(
                _finding(
                    "STOCK_SHORTFALL",
                    f"{item} quantity {show_qty(qty)} exceeds stock of {stock[item]}.",
                    "blocking",
                    item,
                )
            )

    # line.item is the base name: a trailing "(rush order)" was already split into the note.
    # item_key is the same case, space, and hyphen fold as inventory matching.
    # Empty source text fails closed. Every reader supplies text; a missing one holds.
    # The line is not rewritten.
    for line in invoice.line_items:
        if _name_in_source(line.item, raw_text):
            continue
        findings.append(
            _finding(
                "ITEM_NOT_IN_SOURCE",
                f"{line.item} is not in the invoice text.",
                item=line.item,
            )
        )

    findings.extend(_math_findings(invoice))
    if currency not in POLICY.allowed_currencies:
        findings.append(_finding("NON_USD", non_usd_message(currency), item="currency"))
    if _is_revised(invoice, raw_text):
        findings.append(_finding("REVISED_INVOICE", "This invoice is marked as a revision."))

    blob = _blob(raw_text, invoice.vendor, invoice.notes, invoice.payment_terms)
    if _FORMERLY.search(blob):
        findings.append(_finding("VENDOR_RENAMED", "The vendor name says formerly.", item="vendor"))

    fraud: list[Finding] = []
    if _URGENCY.search(blob):
        fraud.append(_finding("FRAUD_SIGNAL", "Urgency pressure is a fraud signal."))
    if _WIRE.search(blob):
        fraud.append(_finding("FRAUD_SIGNAL", "Wire transfer is a fraud signal."))
    new_account = bool(_NEW_ACCOUNT.search(blob))
    if new_account:
        fraud.append(
            _finding("FRAUD_SIGNAL", "A new remittance or bank account is a fraud signal.")
        )
    findings.extend(fraud)

    # USD only. abs(unit_price) so a -250 credit matches a 250 reference.
    if currency in POLICY.allowed_currencies:
        spike_at = Decimal("1") + POLICY.price_spike_ratio
        spike_pct = f"{POLICY.price_spike_ratio * 100:.0f}"
        for line in invoice.line_items:
            ref = POLICY.reference_prices.get(canonical_item(line.item, stock))
            if ref is None or line.unit_price is None:
                continue
            # Round to the money tolerance before comparing, same as the line math.
            price = as_money(abs(line.unit_price))
            if not different(price, ref):
                continue
            findings.append(
                _finding(
                    "PRICE_VARIANCE",
                    (
                        f"Unit price {show_money(line.unit_price)} for {line.item} "
                        f"differs from the reference price {show_money(ref)}."
                    ),
                    item=line.item,
                )
            )
            if price >= ref * spike_at:
                findings.append(
                    _finding(
                        "PRICE_SPIKE",
                        (
                            f"Unit price {show_money(line.unit_price)} for {line.item} "
                            f"is {spike_pct}% or more above the reference price {show_money(ref)}."
                        ),
                        item=line.item,
                    )
                )

    for line in invoice.line_items:
        if line.description and re.search(r"\([^)]+\)", line.description) and line.note:
            findings.append(
                _finding(
                    "DESCRIPTOR",
                    (
                        f"Descriptor on {line.description}: matched as {line.item}, "
                        f"{line.note!r} kept as a note."
                    ),
                    item=line.item,
                )
            )

    if _near_threshold(invoice.total, currency):
        findings.append(
            _finding(
                "NEAR_THRESHOLD",
                (
                    f"Total {show_money(invoice.total)} is within "
                    f"{POLICY.near_threshold_ratio * 100:.0f}% under the "
                    f"{show_money(APPROVAL_THRESHOLD_USD)} threshold."
                ),
                item="total",
            )
        )

    address_note = _address_note(invoice)
    if address_note is not None:
        findings.append(_finding("NONCOMMERCIAL_ADDRESS", address_note, item="vendor_address"))
    if invoice.due_date and not _dated(invoice.due_date):
        findings.append(
            _finding(
                "UNPARSED_DATE",
                f"Due date {invoice.due_date!r} is not a date.",
                item="due_date",
            )
        )
    if _raw_has_ocr_o(raw_text) and math_reconciles(invoice):
        findings.append(
            _finding(
                "OCR_FIX",
                "OCR O-to-0 fix applied because the math reconciles.",
                "info",
            )
        )
    if invoice.embedded_instructions and injection_phrase(_blob(raw_text, invoice.notes)):
        findings.append(
            _finding(
                "EMBEDDED_INSTRUCTIONS",
                "Embedded instructions aimed at the system.",
                "blocking",
            )
        )

    rejected = any(item.severity == "blocking" and item.code in REJECT_CODES for item in findings)
    over = (
        invoice.total is not None
        and currency in POLICY.allowed_currencies
        and invoice.total > APPROVAL_THRESHOLD_USD
    )
    if not rejected and new_account and over:
        findings.append(
            _finding(
                "NEW_REMITTANCE",
                "Clean invoice over the threshold with a new remittance or bank account.",
            )
        )
    elif not rejected and len(fraud) >= 2:
        findings.append(_finding("FRAUD_CLUSTER", "Two or more fraud signals."))
    return findings, currency


def assess(
    invoice: ExtractedInvoice,
    raw_text: str,
    stock: dict[str, int],
) -> Assessment:
    """Apply the hard rules to one invoice. The file is judged on its own."""

    found, currency = collect_findings(invoice, raw_text, stock)
    outcome, code, reason, action = explain(found, invoice, stock)
    return Assessment(
        outcome=outcome,
        reason_code=code,
        reason=reason,
        action=action,
        findings=found,
        currency=currency,
    )


def evidence_lines(
    invoice: ExtractedInvoice,
    stock: dict[str, int],
    findings: list[Finding],
) -> list[str]:
    """Facts the reasoning may cite. Built from the invoice, the stock, and the findings."""

    if invoice.total is None:
        lines = ["total is missing"]
    else:
        lines = [f"total {show_money(invoice.total)} {invoice.currency}"]
    for item, qty in summed_quantities(invoice, stock).items():
        if item in stock:
            lines.append(f"{item} quantity {show_qty(qty)} compared with stock {stock[item]}")
        else:
            lines.append(f"{item} quantity {show_qty(qty)} is not in inventory")
    for line in invoice.line_items:
        if line.quantity <= 0:
            lines.append(f"{line.item} quantity {show_qty(line.quantity)} is negative or zero")
    lines.extend(finding.message for finding in findings)
    if not findings:
        lines.append("No stock, math, currency, or fraud finding.")
    return lines


def _name_in_source(name: str, raw_text: str | None) -> bool:
    """True when the base item name appears in the source text.

    Both sides go through item_key, so case, spaces, and hyphens do not matter.
    An empty name is not a match. An empty or missing source is not a match either:
    txt, json, csv, and xml are the file text, and a pdf is the pypdf text layer.
    No supported format lacks that text. A missing one fails closed.
    """

    key = item_key(name)
    if not key:
        return False
    return key in item_key(raw_text or "")


def _one_edit(left: str, right: str) -> bool:
    """True when one insertion, deletion, or substitution turns left into right."""

    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    if len(left) > len(right):
        left, right = right, left
    for index in range(len(right)):
        if left == right[:index] + right[index + 1:]:
            return True
    return False


def _likely_name(name: str, stock: dict[str, int]) -> str | None:
    """The inventory spelling one edit away, only when exactly one name is."""

    key = item_key(name)
    if not key:
        return None
    found = [item for item in stock if _one_edit(key, item_key(item))]
    if len(found) == 1:
        return found[0]
    return None


def _field_missing(invoice: ExtractedInvoice, field: str) -> bool:
    checks = {
        "vendor": not invoice.vendor,
        "due_date": not invoice.due_date,
        "line_items": not invoice.line_items,
        "total": invoice.total is None,
    }
    return checks.get(field, True)


def _blob(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def parse_printed_date(value: str | None) -> date | None:
    """The printed date, or None when it is not a date this pipeline reads."""

    if not value:
        return None
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _dated(value: str | None) -> bool:
    return parse_printed_date(value) is not None


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


def _address_note(invoice: ExtractedInvoice) -> str | None:
    place = invoice.vendor_address or "the vendor address"
    if invoice.noncommercial_address:
        reason = invoice.address_reason or "the extraction model flagged the address"
        return f"Vendor address looks non-commercial and is noted: {place}. {reason}"
    if invoice.vendor_address and _APARTMENT.search(invoice.vendor_address):
        return f"Vendor address looks non-commercial and is noted: {place}."
    return None


def _near_threshold(total: Decimal | None, currency: str) -> bool:
    if (
        total is None
        or currency not in POLICY.allowed_currencies
        or total >= APPROVAL_THRESHOLD_USD
    ):
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
                    _finding(
                        "TOTAL_MISMATCH",
                        (
                            f"{line.item} line amount {show_money(line.amount)} does not equal "
                            f"quantity times unit price ({show_money(expected)})."
                        ),
                        "blocking",
                        line.item,
                    )
                )
        elif line.amount is not None:
            line_sum += line.amount
        else:
            findings.append(
                _finding(
                    "TOTAL_MISMATCH",
                    f"{line.item} has no unit price or line amount.",
                    "blocking",
                    line.item,
                )
            )
    line_sum = as_money(line_sum)
    if invoice.subtotal is not None and different(invoice.subtotal, line_sum):
        findings.append(
            _finding(
                "TOTAL_MISMATCH",
                (
                    f"Printed subtotal {show_money(invoice.subtotal)} does not equal "
                    f"the line sum {show_money(line_sum)}."
                ),
                "blocking",
                "subtotal",
            )
        )
    base = invoice.subtotal if invoice.subtotal is not None else line_sum
    tax = invoice.tax_amount if invoice.tax_amount is not None else Decimal("0")
    shipping = invoice.shipping if invoice.shipping is not None else Decimal("0")
    expected_total = as_money(base + tax + shipping)
    if different(expected_total, invoice.total):
        findings.append(
            _finding(
                "TOTAL_MISMATCH",
                (
                    f"Printed total {show_money(invoice.total)} does not equal "
                    f"{show_money(expected_total)} (subtotal + tax + shipping)."
                ),
                "blocking",
                "total",
            )
        )
    return findings
