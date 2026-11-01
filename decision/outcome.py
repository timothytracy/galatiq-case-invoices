"""Which finding wins. Reject over hold over approve."""

from models import Finding, Outcome

# Most fundamental data problem first: a bad quantity, then a missing field, then identity, stock, and math. Embedded instructions are a security reject and come first.
REJECT_CODES = (
    "EMBEDDED_INSTRUCTIONS",
    "BAD_QUANTITY",
    "NEGATIVE_AMOUNT",
    "MISSING_FIELD",
    "UNKNOWN_ITEM",
    "STOCK_SHORTFALL",
    "TOTAL_MISMATCH",
    "UNREADABLE",
)

# A hold for a person. A reject-class finding wins when both are present.
# SYSTEM_ERROR is a Grok failure after retries, not a business finding.
HOLD_CODES = (
    "SYSTEM_ERROR",
    "BAD_REASONING",
    "NON_USD",
    "CREDIT_NOTE",
    "REVISED_INVOICE",
    "VENDOR_RENAMED",
    "NEW_REMITTANCE",
    "FRAUD_CLUSTER",
    "CRITIQUE_HOLD",
    "AUDIT_ESCALATED",
)

# Recorded. Not enough on their own to change the outcome.
WARNING_CODES = (
    "FRAUD_SIGNAL",
    "NEAR_THRESHOLD",
    "DESCRIPTOR",
    "PRICE_VARIANCE",
    "NONCOMMERCIAL_ADDRESS",
    "UNPARSED_DATE",
    "OCR_FIX",
    "AUDIT_NOTE",
)

# Payment records this when it refuses. It does not change the invoice outcome.
PAYMENT_REFUSED = "PAYMENT_REFUSED"


def outcome_from_findings(findings: list[Finding]) -> Outcome:
    """A blocking reject wins. Otherwise a hold. Otherwise approve.

    Two FRAUD_SIGNAL findings are a hold even without a FRAUD_CLUSTER row.
    Warnings and PAYMENT_REFUSED do not decide.
    """

    blocking_reject = any(
        finding.severity == "blocking" and finding.code in REJECT_CODES for finding in findings
    )
    if blocking_reject:
        return "reject"
    if any(finding.code in HOLD_CODES for finding in findings):
        return "hold"
    if sum(1 for finding in findings if finding.code == "FRAUD_SIGNAL") >= 2:
        return "hold"
    return "approve"


def primary_code(findings: list[Finding], outcome: Outcome) -> str | None:
    """The one reason code for this outcome. Approve has none."""

    if outcome == "reject":
        present = {
            finding.code
            for finding in findings
            if finding.severity == "blocking" and finding.code in REJECT_CODES
        }
        order = REJECT_CODES
    elif outcome == "hold":
        present = {finding.code for finding in findings if finding.code in HOLD_CODES}
        if sum(1 for finding in findings if finding.code == "FRAUD_SIGNAL") >= 2:
            present.add("FRAUD_CLUSTER")
        order = HOLD_CODES
    else:
        return None
    for code in order:
        if code in present:
            return code
    return None


def finding_codes(findings: list) -> list[str]:
    """Codes in the order the findings were raised."""

    return [finding["code"] if isinstance(finding, dict) else finding.code for finding in findings]


def result_codes(result: dict) -> list[str]:
    """Codes stored on a graph result."""

    return finding_codes(result.get("findings") or [])
