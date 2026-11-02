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
# ITEM_NOT_IN_SOURCE is an extraction miss. It is named before a business hold.
# DUPLICATE_INVOICE and SPLIT_INVOICE come before REVISED_INVOICE, so a paid
# revision is named as a duplicate and the revision finding stays recorded.
# PRICE_SPIKE follows FRAUD_CLUSTER, so a cluster stays the named reason
# when a spiked price is present too.
HOLD_CODES = (
    "SYSTEM_ERROR",
    "BAD_REASONING",
    "ITEM_NOT_IN_SOURCE",
    "NON_USD",
    "CREDIT_NOTE",
    "DUPLICATE_INVOICE",
    "SPLIT_INVOICE",
    "REVISED_INVOICE",
    "VENDOR_RENAMED",
    "NEW_REMITTANCE",
    "FRAUD_CLUSTER",
    "PRICE_SPIKE",
    "CRITIQUE_HOLD",
    "AUDIT_ESCALATED",
)

# A reviewer may pay a hold only when every hold finding is in this list.
# Warnings may sit beside them. Anything else stays unpaid. These are the
# holds whose next step is a person confirming a fact, after which the
# invoice itself can be paid:
#   REVISED_INVOICE — confirm the revision replaces an unpaid original.
#   VENDOR_RENAMED — confirm it is the same company.
#   NEW_REMITTANCE — confirm the bank change with the vendor.
#   FRAUD_CLUSTER — confirm the signals with the vendor, using contacts on file.
#   PRICE_SPIKE — confirm the price with the vendor.
#   CRITIQUE_HOLD — review the critique concern. No hard finding is attached.
#   AUDIT_ESCALATED — the audit sent the invoice to a person, except the
#     send-back cap (see SEND_BACK_CAP_MESSAGE), which is rerun-by-hand.
# Reject codes are not here. The other holds stay unpaid: SYSTEM_ERROR and
# BAD_REASONING are a failed run, ITEM_NOT_IN_SOURCE is an extraction miss,
# NON_USD is never converted, CREDIT_NOTE is a credit, and DUPLICATE_INVOICE
# and SPLIT_INVOICE are already do-not-pay. A new hold code is unpaid until
# it is added here.
PAYABLE_HOLD_CODES = (
    "REVISED_INVOICE",
    "VENDOR_RENAMED",
    "NEW_REMITTANCE",
    "FRAUD_CLUSTER",
    "PRICE_SPIKE",
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

# The audit uses this sentence when the send-back cap becomes a hold.
# That case is not a review hold. Other AUDIT_ESCALATED messages still are.
SEND_BACK_CAP_MESSAGE = "Audit send-back cap reached. Hold, no payment."


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


def _capped(finding) -> bool:
    if finding is None:
        return False
    if isinstance(finding, dict):
        return bool(finding.get("send_back_cap"))
    return bool(getattr(finding, "send_back_cap", False))


def _blocks_release(code: str, finding=None) -> bool:
    """True when this code keeps a reviewer from paying."""

    if _capped(finding):
        return True
    if code in REJECT_CODES or (code in HOLD_CODES and code not in PAYABLE_HOLD_CODES):
        return True
    if code in WARNING_CODES or code == PAYMENT_REFUSED or code in PAYABLE_HOLD_CODES:
        return False
    return True


def review_release_block(
    outcome: str | None,
    findings: list,
    reason_code: str | None = None,
) -> str | None:
    """Why this result cannot be paid. None when every hold is a review hold.

    Two FRAUD_SIGNAL findings count as FRAUD_CLUSTER, same as the outcome rule.
    A reject-class code blocks even when the stored outcome is hold.
    """

    if outcome == "reject":
        return "A reject is never paid."
    if outcome != "hold":
        return "Only a held invoice can be approved for payment."
    codes = finding_codes(findings)
    for finding, code in zip(findings, codes):
        if _blocks_release(code, finding):
            return f"{code} is not a review hold and is never paid."
    if reason_code and _blocks_release(reason_code):
        return f"{reason_code} is not a review hold and is never paid."
    if any(code in PAYABLE_HOLD_CODES for code in codes) or codes.count("FRAUD_SIGNAL") >= 2:
        return None
    return "This hold is not a review hold and is never paid."
