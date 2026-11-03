"""Plain-English reasons, the next action, and prompt wording."""

import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from models import ExtractedInvoice, Finding, Outcome

from decision.invoice_fields import show_money, show_qty, summed_quantities
from decision.outcome import (
    PAYMENT_REFUSED,
    REJECT_CODES,
    outcome_from_findings,
    primary_code,
)
from decision.policy import APPROVAL_THRESHOLD_USD, MONEY, POLICY, Policy

def price_spike_reason() -> str:
    # The reason text depends on price_spike_ratio. 0.50 stays 50; 0.333 stays 33.3.
    percent = format(POLICY.price_spike_ratio * 100, "f").rstrip("0").rstrip(".")
    return f"Unit price {percent}% or more above reference"


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
    "Duplicate invoice": "DUPLICATE_INVOICE",
    "Split invoice": "SPLIT_INVOICE",
    "Revised invoice": "REVISED_INVOICE",
    "Labeled credit note whose math reconciles": "CREDIT_NOTE",
    "Renamed vendor": "VENDOR_RENAMED",
    "Two or more fraud signals": "FRAUD_CLUSTER",
    "Fraud suspected": "FRAUD_SUSPECTED",
    "New remittance account": "NEW_REMITTANCE",
    price_spike_reason(): "PRICE_SPIKE",
    "Grok or system failure after retries": "SYSTEM_ERROR",
    "Item name not in source text": "ITEM_NOT_IN_SOURCE",
    "Printed total and amount in words disagree": "AMOUNT_WORDS_MISMATCH",
    "Balance due lower than the total": "BALANCE_DUE",
    "Critique hold": "CRITIQUE_HOLD",
    "Audit escalation": "AUDIT_ESCALATED",
    "Vendor not on the vendor master": "NEW_VENDOR",
    "Zero total": "ZERO_TOTAL",
    "Extracted total does not match the printed total": "PRINTED_TOTAL_MISMATCH",
    "Possible embedded instructions": "INJECTION_SUSPECTED",
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
    "DUPLICATE_INVOICE": "Do not pay it again. This vendor and invoice number are already paid.",
    "SPLIT_INVOICE": (
        "Confirm with the vendor that these are separate orders, not one bill split under "
        "the approval threshold, before releasing it."
    ),
    "REVISED_INVOICE": "Confirm it replaces the original and that the original was not paid.",
    "CREDIT_NOTE": "Accounting applies the credit. Nothing is paid.",
    "VENDOR_RENAMED": "Verify it is the same company.",
    "FRAUD_CLUSTER": "Verify with the vendor using contact details already on file.",
    "FRAUD_SUSPECTED": "Verify payment details with the vendor using contact details already on file.",
    "NEW_REMITTANCE": "Verify the change with the vendor.",
    "PRICE_SPIKE": "Verify the price with the vendor.",
    "SYSTEM_ERROR": "Rerun the invoice or process it by hand.",
    "BAD_REASONING": "Rerun the invoice or process it by hand.",
    "ITEM_NOT_IN_SOURCE": "Check the item name against the invoice.",
    "CRITIQUE_HOLD": "Review the critique concern. Nothing is paid.",
    "AUDIT_ESCALATED": "Rerun the invoice or process it by hand.",
    "AMOUNT_WORDS_MISMATCH": "Confirm the amount with the vendor. Nothing is paid.",
    "BALANCE_DUE": (
        "Confirm the deposit or credit, then pay the balance by hand. Nothing is paid here."
    ),
    "NEW_VENDOR": "Set the vendor up in the vendor master, then release the hold.",
    "ZERO_TOTAL": "Confirm with the vendor why the total is zero. Nothing is paid.",
    "PRINTED_TOTAL_MISMATCH": "Rerun the invoice or read the total by hand. Nothing is paid.",
    "INJECTION_SUSPECTED": "Read the flagged wording and confirm it is not aimed at the payment system.",
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
    "EMBEDDED_INSTRUCTIONS": (
        "Embedded instructions aimed at the system, flagged by both the model and the text check, are rejected."
    ),
    "NON_USD": "Any currency other than USD is a hold. There is no conversion.",
    "VENDOR_RENAMED": "A vendor rename (formerly) is a hold and is not auto-paid.",
    "DUPLICATE_INVOICE": (
        "The same vendor and invoice number already in the payments ledger is a hold. "
        "The same document text is the same hold."
    ),
    "SPLIT_INVOICE": (
        "An invoice at or under the approval threshold is a hold when it, plus the same "
        "vendor's paid invoices inside the split window that were each at or under it, "
        "is over that threshold."
    ),
    "REVISED_INVOICE": "A revised invoice is a hold until someone confirms it replaces an unpaid original.",
    "CREDIT_NOTE": "A labeled credit note whose math reconciles is a hold. Nothing is paid.",
    "FRAUD_CLUSTER": "Two or more fraud signals are a hold.",
    "FRAUD_SIGNAL": "A single fraud signal is a warning.",
    "FRAUD_SUSPECTED": (
        "The fraud review holds an invoice when it quotes page text that makes the "
        "innocent reading of a payment red flag unlikely."
    ),
    "NEW_REMITTANCE": "A new or changed bank or remittance account is a hold at any amount.",
    "NEW_VENDOR": "A vendor that is not on the vendor master is a hold.",
    "ZERO_TOTAL": "A total of zero that is not a labeled credit note is a hold. Nothing is paid.",
    "PRINTED_TOTAL_MISMATCH": (
        "The extracted total must equal a total printed on the page. Otherwise it is a hold."
    ),
    "INJECTION_SUSPECTED": (
        "Wording aimed at the system, flagged by the model or the text check but not both, is a hold."
    ),
    "PRICE_SPIKE": (
        f"A unit price {POLICY.price_spike_ratio * 100:.0f}% or more above its "
        "reference price is a hold."
    ),
    "BAD_REASONING": "Approval wording that refuses the input or contradicts approve is not paid.",
    "ITEM_NOT_IN_SOURCE": (
        "An extracted item name that is not in the invoice text is a hold. The name is not corrected."
    ),
    "SYSTEM_ERROR": "A failed agent step is a hold tracked as a system error and is not paid.",
    "AUDIT_ESCALATED": "The audit escalated the invoice to a hold.",
    "UNREADABLE": "The invoice file could not be read.",
    "CRITIQUE_HOLD": "A critique concern other than the dollar threshold is a hold.",
    "AMOUNT_WORDS_MISMATCH": (
        "A printed total that disagrees with the amount in words is a hold. Neither is paid."
    ),
    "BALANCE_DUE": (
        "A stated balance or amount due below the invoice total is a hold. "
        "Neither the total nor the balance is paid automatically."
    ),
}

PASSED_RULES = [
    "Quantities must be positive, unless this is a labeled credit note whose math reconciles.",
    "Item quantities are summed and compared with the fixed inventory snapshot. An equal quantity passes.",
    f"Printed total must match the lines, tax, and shipping within {MONEY}.",
    f"Currency must be {', '.join(POLICY.allowed_currencies)}. Any other currency is a hold.",
    "One fraud signal is a warning. Two or more, with no reject, are a hold.",
    (
        f"A unit price {POLICY.price_spike_ratio * 100:.0f}% or more above its "
        "USD reference is a hold. A smaller difference is a price-variance warning."
    ),
    (
        f"Every {POLICY.allowed_currencies[0]} invoice goes to critique. A total over "
        f"{APPROVAL_THRESHOLD_USD} is marked VP scrutiny. Being over that threshold is not a risk."
    ),
]

# Glue left behind once the threshold phrase is removed.
_FILLER = set(
    (
        "a an the is are was were be being been "
        "this that it its of to for and or but "
        "only just because so very too"
    ).split()
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


def difference_action(amount: Decimal, currency: str) -> str:
    """What a reviewer does when the original invoice was already paid."""

    return (
        f"Pay only the difference of {show_money(amount)} {currency}. "
        "Do not pay the revised total again."
    )


def difference_rule(amount: Decimal, currency: str) -> str:
    """Rule text for a revision of an invoice that is already paid."""

    return (
        "A revised invoice whose original was already paid is a hold. "
        f"Pay only the unpaid difference of {show_money(amount)} {currency}."
    )


def _owed_amount(finding):
    value = finding.get("owed") if isinstance(finding, dict) else getattr(finding, "owed", None)
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def rule_for(finding, currency: str | None) -> str | None:
    """Rule text for one finding. A difference uses its own sentence."""

    owed = _owed_amount(finding)
    if owed is not None and currency:
        return difference_rule(owed, currency)
    code = finding.get("code") if isinstance(finding, dict) else finding.code
    return RULE_TEXT.get(code)


def rules_for(findings, currency: str | None) -> list[str]:
    """Rule sentences for a decision. A difference replaces the unpaid-original rule."""

    fired = []
    has_difference = any(_owed_amount(item) is not None for item in findings)
    for finding in findings:
        code = finding.get("code") if isinstance(finding, dict) else finding.code
        if has_difference and code == "REVISED_INVOICE" and _owed_amount(finding) is None:
            continue
        text = rule_for(finding, currency)
        if text and text not in fired:
            fired.append(text)
    return fired


def explain(
    findings: list[Finding],
    invoice: ExtractedInvoice | None = None,
    stock: dict[str, int] | None = None,
) -> tuple[Outcome, str | None, str, str | None]:
    """Outcome, primary reason code, plain-English reason, and the next action."""

    outcome = outcome_from_findings(findings)
    code = primary_code(findings, outcome)
    action = _action_text(findings, outcome, code, invoice)
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


def _review_money(amount: Decimal, tolerance: Decimal, currency: str) -> str:
    """Two decimals, then the currency code. The same shape for every policy currency."""

    quantized = amount.quantize(tolerance, rounding=ROUND_HALF_UP)
    return f"{quantized:,.2f} {currency}"


def approval_conclusion(
    invoice: ExtractedInvoice,
    findings: list[Finding],
    policy: Policy,
    *,
    passed_critique: bool = False,
) -> str:
    """One sentence from the checks validation already ran and the finding text.

    passed_critique is true only after critique has run and left the approval in place.
    Threshold and currency come from policy. Warning text is the finding messages.
    """

    codes = {finding.code for finding in findings}
    parts: list[str] = []
    items: list[str] = []
    for line in invoice.line_items:
        if line.quantity > 0 and line.item not in items:
            items.append(line.item)
    known = bool(items) and "UNKNOWN_ITEM" not in codes
    if known:
        if len(items) == 1:
            parts.append("1 item found in inventory")
        else:
            parts.append(f"All {len(items)} items found in inventory")
    if known and "STOCK_SHORTFALL" not in codes:
        parts.append("stock covers every line")
    if invoice.total is not None and invoice.line_items and "TOTAL_MISMATCH" not in codes:
        parts.append("line and grand totals reconcile")
    if invoice.total is not None and invoice.currency in policy.allowed_currencies:
        code = invoice.currency
        amount = _review_money(invoice.total, policy.money_tolerance, code)
        limit = _review_money(policy.scrutiny_threshold, policy.money_tolerance, code)
        if invoice.total > policy.scrutiny_threshold:
            clause = f"{amount} is over the {limit} review threshold"
            if passed_critique:
                clause += " and passed critique"
        elif invoice.total < policy.scrutiny_threshold:
            clause = f"{amount} is under the {limit} review threshold"
        else:
            clause = f"{amount} is at the {limit} review threshold"
        parts.append(clause)
        parts.append(f"currency {code}")
    warnings = [
        finding.message.rstrip(".")
        for finding in findings
        if finding.severity == "warning"
    ]
    if warnings:
        parts.append("warnings: " + "; ".join(warnings))
    else:
        parts.append("no warnings")
    sentence = "; ".join(parts)
    if not sentence.endswith("."):
        sentence += "."
    return sentence


def _approve_sentence(invoice: ExtractedInvoice | None, stock: dict[str, int] | None) -> str:
    if invoice is None or stock is None:
        return "All checks passed."
    bits = []
    for item, qty in summed_quantities(invoice, stock).items():
        if item in stock:
            bits.append(f"{item} {show_qty(qty)} (stock {stock[item]})")
    detail = ", ".join(bits) if bits else "no positive quantities"
    return f"{detail} within stock and the printed total matches the lines."


def _action_text(
    findings: list[Finding], outcome: Outcome, code: str | None, invoice: ExtractedInvoice | None = None,
) -> str | None:
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
    if code == "REVISED_INVOICE":
        currency = None if invoice is None else invoice.currency
        for finding in findings:
            owed = _owed_amount(finding)
            if owed is not None and currency:
                return difference_action(owed, currency)
    if code == "DUPLICATE_INVOICE":
        for finding in findings:
            text = finding.get("message") if isinstance(finding, dict) else getattr(finding, "message", "")
            if "unconfirmed payment" in (text or "") or "pending payment" in (text or ""):
                return "Do not pay it. The earlier payment is not confirmed."
    return ACTION.get(code)
