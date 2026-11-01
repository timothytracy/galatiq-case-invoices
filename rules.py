"""Deterministic invoice rules.

Stock is a fixed snapshot. Quantities are summed per item on one invoice.
quantity greater than stock fails. An equal quantity passes.
Any currency other than the allowed list is a hold. There is no conversion.
The numbers live in policy.toml. outcome_from_findings turns findings into
approve, reject, or hold. Reject wins over hold. The model does not loosen that.

Warnings are recorded and do not change the outcome on their own.
"""

import re
import tomllib
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from models import Assessment, ExtractedInvoice, Finding, Outcome

# Blocking findings, in the order used to pick the primary reason.
# Quantity comes before a missing field so a negative line is the primary
# reason when both are present, which is how the answer key reads invoice 1009.
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
# SYSTEM_ERROR is the Grok failure after retries. It is not a business finding.
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

# Recorded, and not enough on their own to change the outcome.
WARNING_CODES = (
    "FRAUD_SIGNAL",
    "NEAR_THRESHOLD",
    "DESCRIPTOR",
    "PRICE_VARIANCE",
    "NONCOMMERCIAL_ADDRESS",
    "UNPARSED_DATE",
    "OCR_FIX",
)

# Payment records this when it refuses. It does not change the invoice outcome.
PAYMENT_REFUSED = "PAYMENT_REFUSED"

# The answer key names findings in the policy's words. This is the only map
# from those names to the codes above. None means the invoice passed.
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

_REQUIRED_FIELD_NAMES = ("vendor", "due_date", "line_items", "total")
_MISSING_TEXT = {
    "vendor": "Vendor is missing.",
    "due_date": "Due date is missing.",
    "line_items": "No line items.",
    "total": "Total is missing.",
}
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%d-%B-%Y", "%B %d, %Y", "%b %d, %Y")

_EURO = re.compile(r"€|\beuros?\b|\beur\b", re.IGNORECASE)
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
    near_threshold_ratio: Decimal
    price_fraud_ratio: Decimal
    reference_prices: dict[str, Decimal]
    residential_address_markers: list[str]
    civic_address_markers: list[str]

    @field_validator(
        "scrutiny_threshold",
        "money_tolerance",
        "near_threshold_ratio",
        "price_fraud_ratio",
        mode="before",
    )
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

    @field_validator("money_tolerance", "price_fraud_ratio")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("near_threshold_ratio")
    @classmethod
    def _ratio(cls, value: Decimal) -> Decimal:
        if value < 0 or value >= 1:
            raise ValueError("must be at least 0 and less than 1")
        return value

    @field_validator("reference_prices", mode="before")
    @classmethod
    def _prices(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, dict) or not value:
            raise ValueError("must be a table of item prices")
        prices = {}
        for item, amount in value.items():
            try:
                price = Decimal(str(amount))
            except Exception as exc:
                raise ValueError("must be a table of item prices") from exc
            if not price.is_finite() or price <= 0:
                raise ValueError("must be a table of positive prices")
            prices[str(item)] = price
        return prices

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

    @field_validator("residential_address_markers", "civic_address_markers")
    @classmethod
    def _markers(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


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


def as_money(amount: Decimal) -> Decimal:
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)


def show_qty(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def show_money(value: Decimal) -> str:
    return f"{as_money(value):.2f}"


def today_line(as_of: date) -> str:
    """One sentence for every agent prompt. Tests inject as_of."""

    return (
        f"Today's date is {as_of.isoformat()}. "
        "A printed date is in the future only when it is after today. "
        "Do not decide that from a model's training cutoff."
    )


def resolve_as_of(as_of: date | None) -> date:
    return as_of or date.today()


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


def _item_key(name: str) -> str:
    """Case, spaces, and hyphens do not change which item a name is."""

    return re.sub(r"[\s\-]+", "", name).casefold()


def canonical_item(name: str, stock: dict[str, int]) -> str:
    """Inventory spelling when the name matches. Otherwise the name with separators removed."""

    key = _item_key(name)
    for item in stock:
        if _item_key(item) == key:
            return item
    return re.sub(r"[\s\-]+", "", name)


def normalize_item_name(description: str) -> tuple[str, str | None]:
    """'Widget A' -> WidgetA. 'WidgetA (rush order)' -> WidgetA, note 'rush order'."""

    text = " ".join(description.strip().split())
    note = None
    match = re.fullmatch(r"(.+?)\s*\(([^)]*)\)\s*", text)
    if match:
        text = match.group(1).strip()
        note = match.group(2).strip() or None
    code = re.sub(r"[\s\-]+", "", text)
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
    return f"Non-USD currency ({currency})."


def summed_quantities(
    invoice: ExtractedInvoice,
    stock: dict[str, int] | None = None,
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for line in invoice.line_items:
        if line.quantity <= 0:
            continue
        name = canonical_item(line.item, stock) if stock is not None else line.item
        totals[name] = totals.get(name, Decimal("0")) + line.quantity
    return totals


def different(left: Decimal, right: Decimal) -> bool:
    return abs(as_money(left) - as_money(right)) > MONEY


def math_reconciles(invoice: ExtractedInvoice) -> bool:
    """True when the lines, tax, and shipping add up to the printed total."""

    if invoice.total is None or not invoice.line_items:
        return False
    return not _math_findings(invoice)


def injection_phrase(text: str) -> bool:
    """True for an imperative, system-directed phrase. Not the bare word approve."""

    return _INJECTION.search(text or "") is not None


def threshold_only_concern(text: str) -> bool:
    """True when a critique concern is only that the total is over the threshold."""

    lowered = text.lower()
    mentions = any(token in lowered for token in ("10,000", "10000", "10k", "threshold", "scrutiny"))
    if not mentions:
        return False
    other = (
        "fraud",
        "stock",
        "mismatch",
        "missing",
        "currency",
        "vendor",
        "quantity",
        "inject",
        "remit",
        "wire",
        "revision",
        "credit",
    )
    return not any(token in lowered for token in other)


def outcome_from_findings(findings: list[Finding]) -> Outcome:
    """Derive the invoice outcome from the findings.

    A blocking reject-class finding gives reject, and that wins over a hold.
    Otherwise a hold-class finding gives hold. Two or more FRAUD_SIGNAL
    findings are a hold even without a FRAUD_CLUSTER row. No finding of
    either class gives approve. Warnings and PAYMENT_REFUSED do not decide.
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
    """The one reason code the answer key calls primary. Approve has none."""

    if outcome == "reject":
        present = {
            finding.code
            for finding in findings
            if finding.severity == "blocking" and finding.code in REJECT_CODES
        }
        for code in REJECT_CODES:
            if code in present:
                return code
        return None
    if outcome == "hold":
        present = {finding.code for finding in findings if finding.code in HOLD_CODES}
        if sum(1 for finding in findings if finding.code == "FRAUD_SIGNAL") >= 2:
            present.add("FRAUD_CLUSTER")
        for code in HOLD_CODES:
            if code in present:
                return code
        return None
    return None


def explain(
    findings: list[Finding],
    invoice: ExtractedInvoice | None = None,
    stock: dict[str, int] | None = None,
) -> tuple[Outcome, str | None, str, str | None]:
    """Outcome, primary reason code, plain-English reason, and the next action.

    Lower-precedence findings stay in the reason after the primary ones.
    """

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
        line.quantity < 0 or (line.amount is not None and line.amount < 0)
        for line in invoice.line_items
    )


def _is_revised(invoice: ExtractedInvoice, raw_text: str) -> bool:
    if invoice.revision and invoice.revision.strip():
        return True
    blob = _blob(raw_text, invoice.notes)
    return bool(_REVISED_PHRASE.search(blob) or _REVISED_FIELD.search(blob))


def _is_credit_label(invoice: ExtractedInvoice, raw_text: str) -> bool:
    return bool(_CREDIT.search(_blob(raw_text, invoice.notes, invoice.payment_terms)))


def _address_is_noncommercial(address: str | None) -> bool:
    if not address:
        return False
    for marker in POLICY.residential_address_markers:
        if re.search(rf"\b{re.escape(marker)}\b", address, re.IGNORECASE):
            return True
    for marker in POLICY.civic_address_markers:
        if marker.lower() in address.lower():
            return True
    return False


def _raw_has_ocr_o(raw_text: str) -> bool:
    for match in re.finditer(r"[0-9O][0-9O,]*\.?[0-9O]*", raw_text or ""):
        token = match.group(0)
        if "O" in token and any(character.isdigit() for character in token):
            return True
    return False


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
            Finding(
                code="FRAUD_SIGNAL",
                message="Urgency pressure is a fraud signal.",
                severity="warning",
            )
        )
    if _WIRE.search(blob):
        fraud.append(
            Finding(
                code="FRAUD_SIGNAL",
                message="Wire transfer is a fraud signal.",
                severity="warning",
            )
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
            if ref is None or line.unit_price is None:
                continue
            if not different(line.unit_price, ref):
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
                findings.append(
                    Finding(
                        code="FRAUD_SIGNAL",
                        item=line.item,
                        message=(
                            f"Unit price {show_money(line.unit_price)} for {line.item} "
                            "is well above the reference price."
                        ),
                        severity="warning",
                    )
                )
                fraud.append(findings[-1])

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

    if (
        invoice.total is not None
        and currency in POLICY.allowed_currencies
        and invoice.total < APPROVAL_THRESHOLD_USD
        and (APPROVAL_THRESHOLD_USD - invoice.total)
        <= APPROVAL_THRESHOLD_USD * POLICY.near_threshold_ratio
    ):
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

    if _address_is_noncommercial(invoice.vendor_address):
        findings.append(
            Finding(
                code="NONCOMMERCIAL_ADDRESS",
                message=f"Vendor address looks non-commercial and is noted: {invoice.vendor_address}.",
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
            Finding(
                code="FRAUD_CLUSTER",
                message="Two or more fraud signals.",
                severity="warning",
            )
        )

    outcome, code, reason, action = explain(findings, invoice, stock)
    return Assessment(
        outcome=outcome,
        reason_code=code,
        reason=reason,
        action=action,
        findings=findings,
        currency=currency,
    )


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
