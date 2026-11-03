"""The pass list. An invoice is paid only when every check here is true.

The findings already decide the outcome; this is the same rule stated as an
allow-list, so a payment can be shown to meet six named conditions. Payment
refuses when any check is false, even if the outcome says approve.
"""

from decimal import Decimal

from decision.policy import POLICY
from decision.vendors import known_vendor

_TOTAL_CODES = {"PRINTED_TOTAL_MISMATCH", "TOTAL_MISMATCH", "AMOUNT_WORDS_MISMATCH", "BALANCE_DUE"}
_INJECTION_CODES = {"EMBEDDED_INSTRUCTIONS", "INJECTION_SUSPECTED"}
_BANK_CODES = {"NEW_REMITTANCE"}

CHECK_NAMES = (
    "Known vendor",
    "Bank details unchanged",
    "Printed total matches",
    "No embedded instructions",
    "Total above zero, in USD",
    "Audit passed",
)


def _get(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _codes(findings) -> set[str]:
    return {str(_get(item, "code")) for item in findings or []}


def _money(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def pass_checks(invoice, findings, audit_verdict: str | None) -> list[dict]:
    """Six named checks, each {"check", "ok", "detail"}. invoice may be a model or a dict."""

    codes = _codes(findings)
    vendor = _get(invoice, "vendor") if invoice is not None else None
    total = _money(_get(invoice, "total")) if invoice is not None else None
    currency = (_get(invoice, "currency") if invoice is not None else None) or ""
    vendor_ok = bool(vendor) and known_vendor(vendor) and "NEW_VENDOR" not in codes
    usd = currency in POLICY.allowed_currencies
    checks = [
        (
            vendor_ok,
            f"{vendor} is on the vendor master." if vendor_ok else f"{vendor or 'No vendor'} is not on the vendor master.",
        ),
        (
            not codes & _BANK_CODES,
            "No new or changed bank details." if not codes & _BANK_CODES else "The invoice changes the bank details.",
        ),
        (
            total is not None and not codes & _TOTAL_CODES,
            "The extracted total is the printed total and the lines add up."
            if total is not None and not codes & _TOTAL_CODES
            else "The total is missing, not printed, or does not add up.",
        ),
        (
            not codes & _INJECTION_CODES,
            "No wording aimed at the system." if not codes & _INJECTION_CODES else "Wording aimed at the system was flagged.",
        ),
        (
            total is not None and total > 0 and usd,
            f"{total} {currency}." if total is not None else "No total.",
        ),
        (
            audit_verdict == "PASS",
            "The audit passed." if audit_verdict == "PASS" else f"The audit verdict is {audit_verdict or 'missing'}.",
        ),
    ]
    return [{"check": name, "ok": bool(ok), "detail": detail} for name, (ok, detail) in zip(CHECK_NAMES, checks)]


def failed_checks(invoice, findings, audit_verdict: str | None) -> list[str]:
    return [row["check"] for row in pass_checks(invoice, findings, audit_verdict) if not row["ok"]]
