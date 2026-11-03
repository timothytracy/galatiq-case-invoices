"""Which finding wins. Reject over hold over approve."""

from pathlib import Path

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
# AMOUNT_WORDS_MISMATCH and BALANCE_DUE are questions about which amount the
# vendor bills. They come after the do-not-pay holds and are never payable.
HOLD_CODES = (
    "SYSTEM_ERROR",
    "BAD_REASONING",
    "ITEM_NOT_IN_SOURCE",
    "NON_USD",
    "CREDIT_NOTE",
    "DUPLICATE_INVOICE",
    "SPLIT_INVOICE",
    "AMOUNT_WORDS_MISMATCH",
    "BALANCE_DUE",
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
# NON_USD is never converted, CREDIT_NOTE is a credit, DUPLICATE_INVOICE
# and SPLIT_INVOICE are already do-not-pay, and AMOUNT_WORDS_MISMATCH and
# BALANCE_DUE would pay an amount the page does not settle (a person pays the
# right amount by hand). A new hold code is unpaid until
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

# Warnings the audit may weigh into an AUDIT_ESCALATED hold, with a quote code checks.
# PRICE_VARIANCE counts only for a price above its reference.
# NONCOMMERCIAL_ADDRESS is left out: the extraction model sets it, so the audit
# would be escalating on a model's own flag.
ESCALATABLE_WARNING_CODES = (
    "FRAUD_SIGNAL",
    "PRICE_VARIANCE",
)

# Payment records this when it refuses. It does not change the invoice outcome.
PAYMENT_REFUSED = "PAYMENT_REFUSED"

# The audit uses this sentence when the send-back cap becomes a hold.
# That case is not a review hold. Other AUDIT_ESCALATED messages still are.
SEND_BACK_CAP_MESSAGE = "Audit send-back cap reached. Hold, no payment."

# An AUDIT_ESCALATED message that starts with this is a hold on a quoted warning.
WARNING_HOLD_PREFIX = "Audit held on "


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


def result_line(path: Path, result: dict) -> dict:
    line = {
        "record": "result",
        "file": path.name,
        "outcome": result.get("outcome"),
        "reason_code": result.get("reason_code"),
        "action": result.get("action"),
        "payment_status": payment_status(result),
        "codes": result_codes(result),
    }
    calls = tool_calls_of(result)
    if calls:
        line["tool_calls"] = calls
    for row in reversed(result.get("logs") or []):
        if not isinstance(row, dict) or row.get("stage") != "audit":
            continue
        body = row.get("result") or {}
        if body.get("tool_cap_hit"):
            line["tool_cap_hit"] = body["tool_cap_hit"]
        if body.get("forced_final_turn"):
            line["forced_final_turn"] = True
        break
    return line


def tool_calls_of(result: dict) -> list:
    """Tool calls recorded on stage results. Empty when a stage made none.

    Audit stores them on logs[].result.tool_calls where stage is audit. The
    result line copies that list. A missing key means the stage made no tool call.
    """

    calls = []
    for row in result.get("logs") or []:
        if not isinstance(row, dict):
            continue
        body = row.get("result")
        if isinstance(body, dict) and body.get("tool_calls"):
            calls.extend(body["tool_calls"])
    return calls


def payment_status(result: dict) -> str:
    payment = result.get("payment")
    if payment is None:
        return "not_paid"
    if isinstance(payment, dict):
        return payment.get("status", "not_paid")
    return payment.status


def _capped(finding) -> bool:
    """The flag or the recorded cap sentence. Dropping the flag is not enough."""

    if finding is None:
        return False
    if isinstance(finding, dict):
        message = finding.get("message") or ""
        marked = bool(finding.get("send_back_cap"))
    else:
        message = getattr(finding, "message", "") or ""
        marked = bool(getattr(finding, "send_back_cap", False))
    return marked or message == SEND_BACK_CAP_MESSAGE


def blocks_release(code: str, finding=None) -> bool:
    """True when this code keeps a reviewer from paying."""

    if _capped(finding):
        return True
    if code in REJECT_CODES or (code in HOLD_CODES and code not in PAYABLE_HOLD_CODES):
        return True
    if code in WARNING_CODES or code == PAYMENT_REFUSED or code in PAYABLE_HOLD_CODES:
        return False
    return True


def _severity(finding) -> str:
    if isinstance(finding, dict):
        return str(finding.get("severity") or "")
    return str(getattr(finding, "severity", "") or "")


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
        # A downgraded total-line warning is not the reason release is refused.
        if _severity(finding) == "warning" and code in REJECT_CODES:
            continue
        if blocks_release(code, finding):
            return f"{code} is not a review hold and is never paid."
    if reason_code and blocks_release(reason_code):
        return f"{reason_code} is not a review hold and is never paid."
    if any(code in PAYABLE_HOLD_CODES for code in codes) or codes.count("FRAUD_SIGNAL") >= 2:
        return None
    return "This hold is not a review hold and is never paid."
