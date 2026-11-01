"""Public names for the invoice rules.

Detection is decision.findings. Precedence is decision.outcome.
Reasons and actions are decision.wording. The knobs are decision.policy.
"""

from models import Assessment, ExtractedInvoice

from decision.findings import collect_findings, evidence_lines, injection_phrase, math_reconciles
from decision.invoice_fields import (
    as_money,
    canonical_item,
    different,
    non_usd_message,
    normalize_invoice_number,
    normalize_item_name,
    resolve_currency,
    show_money,
    show_qty,
    summed_quantities,
)
from decision.outcome import (
    HOLD_CODES,
    PAYMENT_REFUSED,
    REJECT_CODES,
    WARNING_CODES,
    finding_codes,
    outcome_from_findings,
    primary_code,
    result_codes,
)
from decision.policy import (
    APPROVAL_THRESHOLD_USD,
    CRITIQUE_CAP,
    MAX_SEND_BACKS,
    MONEY,
    POLICY,
    Policy,
    PolicyError,
    load_policy,
)
from decision.wording import (
    ACTION,
    KEY_REASON_TO_CODE,
    PASSED_RULES,
    RULE_TEXT,
    explain,
    resolve_as_of,
    threshold_only_concern,
    today_line,
)


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


__all__ = [
    "ACTION",
    "APPROVAL_THRESHOLD_USD",
    "CRITIQUE_CAP",
    "HOLD_CODES",
    "KEY_REASON_TO_CODE",
    "MAX_SEND_BACKS",
    "MONEY",
    "PASSED_RULES",
    "PAYMENT_REFUSED",
    "POLICY",
    "REJECT_CODES",
    "RULE_TEXT",
    "WARNING_CODES",
    "Policy",
    "PolicyError",
    "as_money",
    "assess",
    "canonical_item",
    "different",
    "evidence_lines",
    "explain",
    "finding_codes",
    "injection_phrase",
    "load_policy",
    "math_reconciles",
    "non_usd_message",
    "normalize_invoice_number",
    "normalize_item_name",
    "outcome_from_findings",
    "primary_code",
    "resolve_as_of",
    "resolve_currency",
    "result_codes",
    "show_money",
    "show_qty",
    "summed_quantities",
    "threshold_only_concern",
    "today_line",
]
