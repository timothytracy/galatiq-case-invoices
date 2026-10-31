"""Deterministic invoice rules.

Stock is a fixed snapshot. Quantities are summed per item on one invoice.
quantity greater than stock fails. An equal quantity passes.
Any currency other than the allowed list is manual review. There is no conversion.
The numbers live in policy.toml. outcome_from_findings turns findings into
approve, reject, or manual_review. The model does not get to loosen that.
"""

import re
import tomllib
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from models import Assessment, ExtractedInvoice, Finding, Outcome

# Blocking findings. Reason text keeps this order so the sample labels stay put.
REJECT_CODES = (
    "BAD_QUANTITY",
    "MISSING_FIELD",
    "UNKNOWN_ITEM",
    "STOCK_SHORTFALL",
    "TOTAL_MISMATCH",
    "UNREADABLE",
)

# A hold for a person. A reject-class finding wins when both are present.
REVIEW_CODES = (
    "NON_USD",
    "VENDOR_RENAMED",
    "FRAUD_SIGNAL",
    "BAD_REASONING",
    "AGENT_FAILED",
    "AUDIT_ESCALATED",
)

# Payment records this when it refuses. It does not change the invoice outcome.
PAYMENT_REFUSED = "PAYMENT_REFUSED"

_REQUIRED_FIELD_NAMES = ("vendor", "due_date", "line_items", "total")
_MISSING_TEXT = {
    "vendor": "Vendor is missing.",
    "due_date": "Due date is missing.",
    "line_items": "No line items.",
    "total": "Total is missing.",
}

_EURO = re.compile(r"€|\beuros?\b|\beur\b", re.IGNORECASE)
# pattern, message, code, field. "formerly" is a vendor rename. The rest are fraud language.
_FRAUD = (
    (
        re.compile(r"\bformerly\b", re.IGNORECASE),
        "Vendor identity change (formerly) requires manual review.",
        "VENDOR_RENAMED",
        "vendor",
    ),
    (
        re.compile(r"\bwire transfer\b", re.IGNORECASE),
        "Wire transfer language requires manual review.",
        "FRAUD_SIGNAL",
        None,
    ),
    (
        re.compile(r"pay immediately", re.IGNORECASE),
        "Payment-pressure language requires manual review.",
        "FRAUD_SIGNAL",
        None,
    ),
    (
        re.compile(r"\burgent\b", re.IGNORECASE),
        "Urgency language requires manual review.",
        "FRAUD_SIGNAL",
        None,
    ),
)

class PolicyError(RuntimeError):
    """policy.toml failed to load. The message names the key."""


class Policy(BaseModel):
    """The knobs in policy.toml. Checked when the file is loaded."""

    model_config = ConfigDict(extra="forbid")

    scrutiny_threshold: Decimal
    allowed_currencies: list[str] = Field(min_length=1)
    required_fields: list[str] = Field(min_length=1)
    money_tolerance: Decimal
    critique_cap: int = Field(ge=0)
    audit_send_back_cap: int = Field(ge=0)
    model_retry_attempts: int = Field(ge=1)

    @field_validator("scrutiny_threshold", "money_tolerance", mode="before")
    @classmethod
    def _as_decimal(cls, value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except Exception as exc:
            raise ValueError("must be a decimal amount") from exc
        if not amount.is_finite():
            raise ValueError("must be a finite decimal amount")
        return amount

    @field_validator("scrutiny_threshold")
    @classmethod
    def _threshold(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("money_tolerance")
    @classmethod
    def _tolerance(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("allowed_currencies")
    @classmethod
    def _currencies(cls, value: list[str]) -> list[str]:
        codes = []
        for item in value:
            code = str(item).strip().upper()
            if len(code) != 3 or not code.isalpha():
                raise ValueError("must be a list of 3-letter currency codes")
            codes.append(code)
        return codes

    @field_validator("required_fields")
    @classmethod
    def _fields(cls, value: list[str]) -> list[str]:
        names = []
        for item in value:
            name = str(item).strip()
            if name not in _REQUIRED_FIELD_NAMES:
                raise ValueError(f"unknown field {name}")
            if name not in names:
                names.append(name)
        return names


def load_policy(path: str | Path | None = None) -> Policy:
    """Read policy.toml. A bad file raises PolicyError and names the key."""

    policy_path = Path(__file__).resolve().parent / "policy.toml" if path is None else Path(path)
    if not policy_path.is_file():
        raise PolicyError(f"policy file not found: {policy_path}")
    try:
        raw = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"policy file: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy file: the document must be a table of keys")
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        key = ".".join(str(part) for part in err["loc"]) or "policy"
        raise PolicyError(f"{key}: {err['msg']}") from exc


POLICY = load_policy()

# Names the rest of the pipeline already uses. The values come from policy.toml.
APPROVAL_THRESHOLD_USD = POLICY.scrutiny_threshold
CRITIQUE_CAP = POLICY.critique_cap
MAX_SEND_BACKS = POLICY.audit_send_back_cap
MONEY = POLICY.money_tolerance

RULE_TEXT = {
    "BAD_QUANTITY": "A quantity of zero or below is a data integrity failure.",
    "MISSING_FIELD": "Vendor, line items, due date, and total are required.",
    "UNKNOWN_ITEM": "Every item must exist in the inventory snapshot.",
    "STOCK_SHORTFALL": "Summed quantity for an item cannot exceed snapshot stock.",
    "TOTAL_MISMATCH": (
        "Line math and subtotal plus tax plus shipping must match the printed total within "
        f"{MONEY}."
    ),
    "NON_USD": "Any currency other than USD is manual review. There is no conversion.",
    "VENDOR_RENAMED": "A vendor rename (formerly) is manual review and is not auto-paid.",
    "FRAUD_SIGNAL": "A fraud signal is manual review and is not auto-paid.",
    "BAD_REASONING": "Approval wording that refuses the input or contradicts approve is not paid.",
    "AGENT_FAILED": "A failed agent step does not continue to payment.",
    "AUDIT_ESCALATED": "The audit escalated the invoice to manual review.",
    "UNREADABLE": "The invoice file could not be read.",
}

PASSED_RULES = [
    "Quantities must be positive.",
    "Item quantities are summed and compared with the fixed inventory snapshot. An equal quantity passes.",
    f"Printed total must match the lines, tax, and shipping within {MONEY}.",
    f"Currency must be {', '.join(POLICY.allowed_currencies)}. Any other currency is manual review.",
    "Fraud signals are manual review.",
    f"A {POLICY.allowed_currencies[0]} total over {APPROVAL_THRESHOLD_USD} goes to critique before audit.",
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


def outcome_from_findings(findings: list[Finding]) -> Outcome:
    """Derive the invoice outcome from the findings.

    A blocking reject-class finding gives reject, and that wins over a hold.
    Otherwise a manual-review-class finding gives manual_review.
    No finding of either class gives approve. Other codes, including
    PAYMENT_REFUSED, do not change the outcome.
    """

    blocking_reject = any(
        finding.severity == "blocking" and finding.code in REJECT_CODES for finding in findings
    )
    if blocking_reject:
        return "reject"
    if any(finding.code in REVIEW_CODES for finding in findings):
        return "manual_review"
    return "approve"


def finding_codes(findings: list) -> list[str]:
    """Codes in the order the findings were raised."""

    codes = []
    for finding in findings:
        if isinstance(finding, dict):
            codes.append(finding["code"])
        else:
            codes.append(finding.code)
    return codes


def result_codes(result: dict) -> list[str]:
    """Codes stored on a graph result."""

    return finding_codes(result.get("findings") or [])


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


def assess(
    invoice: ExtractedInvoice,
    raw_text: str,
    stock: dict[str, int],
) -> Assessment:
    """Apply the hard rules to one invoice. The file is judged on its own."""

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

    for item, qty in summed_quantities(invoice).items():
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

    blob = "\n".join([raw_text or "", invoice.vendor or "", invoice.notes or ""])
    for pattern, message, code, field in _FRAUD:
        if pattern.search(blob):
            findings.append(Finding(code=code, message=message, severity="warning", item=field))

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
    outcome = outcome_from_findings(findings)
    if outcome == "reject":
        hard = [finding for finding in findings if finding.code in REJECT_CODES]
        ordered = sorted(hard, key=lambda finding: REJECT_CODES.index(finding.code))
        return outcome, " ".join(finding.message for finding in ordered)

    if outcome == "manual_review":
        holds = [finding for finding in findings if finding.code in REVIEW_CODES]
        holds.sort(key=lambda finding: 0 if finding.code == "NON_USD" else 1)
        return outcome, " ".join(finding.message for finding in holds)

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
