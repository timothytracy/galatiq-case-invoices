"""Plain-English reasons, the next action, and prompt wording."""

import re
from datetime import date

from models import Assessment, ExtractedInvoice, Finding, Outcome

from decision.findings import collect_findings
from decision.invoice_fields import show_qty, summed_quantities
from decision.outcome import (
    PAYMENT_REFUSED,
    REJECT_CODES,
    outcome_from_findings,
    primary_code,
)
from decision.policy import APPROVAL_THRESHOLD_USD, MONEY, POLICY

# The answer key names findings in the policy's words. This is the only map.
KEY_REASON_TO_CODE = {
    "None (all checks pass)": None,
    "Missing required field": "MISSING_FIELD",
    "Quantity that is zero, negative, or not a number": "BAD_QUANTITY",
    "Unknown item": "UNKNOWN_ITEM",
    "Stock shortfall": "STOCK_SHORTFALL",
    "Total or line-math mismatch": "TOTAL_MISMATCH",
    "Unreadable file": "UNREADABLE",
    "Embedded instructions aimed at the system": "EMBEDDED_INSTRUCTIONS",
    "Negative amount that is not a labeled credit note": "NEGATIVE_AMOUNT",
    "Non-USD currency": "NON_USD",
    "Revised invoice": "REVISED_INVOICE",
    "Labeled credit note whose math reconciles": "CREDIT_NOTE",
    "Renamed vendor": "VENDOR_RENAMED",
    "Two or more fraud signals": "FRAUD_CLUSTER",
    "New remittance account": "NEW_REMITTANCE",
    "Grok or system failure after retries": "SYSTEM_ERROR",
}

# What a person does on a hold, or what the vendor must send on a reject.
ACTION = {
    "BAD_QUANTITY": "Resend with corrected quantities.",
    "MISSING_FIELD": "Resend the invoice with the missing field filled in.",
    "UNKNOWN_ITEM": "Resend with the correct item name.",
    "STOCK_SHORTFALL": "Resend for the quantity in stock, or amend the PO.",
    "TOTAL_MISMATCH": "Resend with corrected totals.",
    "UNREADABLE": "Resend in a readable format.",
    "EMBEDDED_INSTRUCTIONS": "Resend a clean invoice.",
    "NEGATIVE_AMOUNT": "Resend with corrected amounts, or send a labeled credit note.",
    "NON_USD": "Finance confirms the currency and amount. Nothing is converted and nothing is paid.",
    "REVISED_INVOICE": "Confirm it replaces the original and that the original was not paid.",
    "CREDIT_NOTE": "Accounting applies the credit. Nothing is paid.",
    "VENDOR_RENAMED": "Verify it is the same company.",
    "FRAUD_CLUSTER": "Verify with the vendor using contact details already on file.",
    "NEW_REMITTANCE": "Verify the change with the vendor.",
    "SYSTEM_ERROR": "Rerun the invoice or process it by hand.",
    "BAD_REASONING": "Rerun the invoice or process it by hand.",
    "CRITIQUE_HOLD": "Review the critique concern. Nothing is paid.",
    "AUDIT_ESCALATED": "Rerun the invoice or process it by hand.",
}

RULE_TEXT = {
    "BAD_QUANTITY": "A quantity of zero or below, or not a number, is a data integrity failure.",
    "MISSING_FIELD": "Vendor, line items, due date, and total are required.",
    "UNKNOWN_ITEM": "Every item must exist in the inventory snapshot.",
    "STOCK_SHORTFALL": "Summed quantity for an item cannot exceed snapshot stock.",
    "TOTAL_MISMATCH": (
        "Line math and subtotal plus tax plus shipping must match the printed total within "
        f"{MONEY}."
    ),
    "NEGATIVE_AMOUNT": "A negative amount that is not a labeled credit note is rejected.",
    "EMBEDDED_INSTRUCTIONS": "Embedded instructions aimed at the system are rejected.",
    "NON_USD": "Any currency other than USD is a hold. There is no conversion.",
    "VENDOR_RENAMED": "A vendor rename (formerly) is a hold and is not auto-paid.",
    "REVISED_INVOICE": "A revised invoice is a hold until someone confirms it replaces an unpaid original.",
    "CREDIT_NOTE": "A labeled credit note whose math reconciles is a hold. Nothing is paid.",
    "FRAUD_CLUSTER": "Two or more fraud signals are a hold.",
    "FRAUD_SIGNAL": "A single fraud signal is a warning.",
    "NEW_REMITTANCE": "A new bank account on a clean invoice over the threshold is a hold.",
    "BAD_REASONING": "Approval wording that refuses the input or contradicts approve is not paid.",
    "SYSTEM_ERROR": "A failed agent step is a hold tracked as a system error and is not paid.",
    "AUDIT_ESCALATED": "The audit escalated the invoice to a hold.",
    "UNREADABLE": "The invoice file could not be read.",
    "CRITIQUE_HOLD": "A critique concern other than the dollar threshold is a hold.",
}

PASSED_RULES = [
    "Quantities must be positive, unless this is a labeled credit note whose math reconciles.",
    "Item quantities are summed and compared with the fixed inventory snapshot. An equal quantity passes.",
    f"Printed total must match the lines, tax, and shipping within {MONEY}.",
    f"Currency must be {', '.join(POLICY.allowed_currencies)}. Any other currency is a hold.",
    "One fraud signal is a warning. Two or more, with no reject, are a hold.",
    (
        f"A {POLICY.allowed_currencies[0]} total over {APPROVAL_THRESHOLD_USD} goes to critique. "
        "Being over that threshold is not a risk."
    ),
]

# Glue left behind once the threshold phrase is removed.
_FILLER = set(
    "a an the is are was were be being been this that it its of to for and or but only just because so very too".split()
)
_AMOUNT = r"(?:\$\s*)?10[,.]?000|\$?\s*10k"
_THRESHOLD_PHRASE = re.compile(
    rf"""(?ix)
    \b(?:total|amount|invoice|being)?\s*
    (?:is\s+|being\s+|was\s+)?
    (?:over|above|exceeds?|exceeding|greater\s+than)\s+
    (?:the\s+)?(?:{_AMOUNT}|scrutiny\s+threshold|threshold)
    (?:\s+threshold)?
    |
    \b(?:over|above)\s+(?:the\s+)?(?:scrutiny\s+)?threshold\b
    |
    \b(?:the\s+)?(?:dollar\s+|scrutiny\s+)?threshold\b
    |
    \b{_AMOUNT}\b
    """
)


def today_line(as_of: date) -> str:

    return (
        f"Today's date is {as_of.isoformat()}. "
        "A printed date is in the future only when it is after today. "
        "Do not decide that from a model's training cutoff."
    )


def resolve_as_of(as_of: date | None) -> date:
    return as_of or date.today()


def threshold_only_concern(text: str) -> bool:
    """True when nothing substantive is left after the threshold phrase is removed."""

    if not _THRESHOLD_PHRASE.search(text or ""):
        return False
    leftover = _THRESHOLD_PHRASE.sub(" ", text)
    words = re.findall(r"[a-z0-9%]+", leftover.lower())
    return not [word for word in words if word not in _FILLER]


def assess(
    invoice: ExtractedInvoice,
    raw_text: str,
    stock: dict[str, int],
) -> Assessment:
    """Apply the hard rules to one invoice. The file is judged on its own."""

    findings, currency = collect_findings(invoice, raw_text, stock)
    outcome, code, reason, action = explain(findings, invoice, stock)
    return Assessment(
        outcome=outcome,
        reason_code=code,
        reason=reason,
        action=action,
        findings=findings,
        currency=currency,
    )


def explain(
    findings: list[Finding],
    invoice: ExtractedInvoice | None = None,
    stock: dict[str, int] | None = None,
) -> tuple[Outcome, str | None, str, str | None]:
    """Outcome, primary reason code, plain-English reason, and the next action."""

    outcome = outcome_from_findings(findings)
    code = primary_code(findings, outcome)
    action = _action_text(findings, outcome, code)
    if outcome == "approve":
        base = _approve_sentence(invoice, stock)
        extra = [finding.message for finding in findings if finding.code != PAYMENT_REFUSED]
    else:
        primary = [finding.message for finding in findings if finding.code == code]
        base = " ".join(primary) or "The invoice was not approved."
        extra = [
            finding.message
            for finding in findings
            if finding.code != code and finding.code != PAYMENT_REFUSED
        ]
    reason = base
    if extra:
        reason = f"{reason} Also recorded: " + " ".join(extra)
    if action:
        reason = f"{reason} {action}"
    return outcome, code, reason, action


def _approve_sentence(invoice: ExtractedInvoice | None, stock: dict[str, int] | None) -> str:
    if invoice is None or stock is None:
        return "All checks passed."
    bits = []
    for item, qty in summed_quantities(invoice, stock).items():
        if item in stock:
            bits.append(f"{item} {show_qty(qty)} (stock {stock[item]})")
    detail = ", ".join(bits) if bits else "no positive quantities"
    return f"{detail} within stock and the printed total matches the lines."


def _action_text(findings: list[Finding], outcome: Outcome, code: str | None) -> str | None:
    if outcome == "approve" or code is None:
        return None
    if outcome == "reject":
        steps = []
        present = {
            finding.code
            for finding in findings
            if finding.severity == "blocking" and finding.code in REJECT_CODES
        }
        for name in REJECT_CODES:
            step = ACTION.get(name)
            if name in present and step and step not in steps:
                steps.append(step)
        return " ".join(steps) or ACTION.get(code)
    return ACTION.get(code)
