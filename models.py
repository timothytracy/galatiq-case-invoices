"""Shared shapes. Money is Decimal. Missing values are None. Dates stay as printed text."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Outcome = Literal["approve", "reject", "hold"]
AuditVerdict = Literal["PASS", "SEND_BACK", "ESCALATE"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SourceInfo(_Strict):
    path: str
    filename: str
    format: str
    parser: str = "grok"
    raw_text: str | None = None


class LineItem(_Strict):
    item: str
    description: str
    quantity: Decimal
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    note: str | None = None


class ExtractedInvoice(_Strict):
    vendor: str | None = None
    vendor_address: str | None = None
    invoice_number: str | None = None
    invoice_number_raw: str | None = None
    revision: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str = "USD"
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax_rate: Decimal | None = None
    tax_amount: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    payment_terms: str | None = None
    notes: str | None = None
    # Reject also needs the phrase check in decision.findings.
    embedded_instructions: bool = False
    # Warning only. Set when the address looks non-commercial.
    noncommercial_address: bool = False
    address_reason: str | None = None
    source: SourceInfo
    warnings: list[str] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        text = value.strip().upper()
        if len(text) != 3 or not text.isalpha():
            raise ValueError("currency must be a 3-letter code")
        return text


class Finding(_Strict):
    code: str
    message: str
    severity: Literal["info", "warning", "blocking"] = "blocking"
    item: str | None = None


class PaymentAuthorization(_Strict):
    vendor: str
    amount: Decimal
    currency: str
    key: str = Field(min_length=1)


class ValidationReport(_Strict):
    passed: bool
    findings: list[Finding] = Field(default_factory=list)


class ApprovalReasoning(_Strict):
    rules_applied: list[str] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    risks_considered: list[str] = Field(min_length=1)
    conclusion: str = Field(min_length=1)


class ApprovalDecision(_Strict):
    decision: Outcome
    reasoning: ApprovalReasoning
    reason_code: str | None = None
    action: str | None = None
    revisions: list[ApprovalReasoning] = Field(default_factory=list)
    amount: Decimal | None = None
    currency: str | None = None


class PaymentRecord(_Strict):
    status: Literal["paid", "not_paid"]
    vendor: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    detail: str
    mock_response: dict | None = None


class StageRecord(_Strict):
    record: Literal["stage"] = "stage"
    stage: Literal[
        "ingestion",
        "validation",
        "approval",
        "critique",
        "audit",
        "payment",
        "reject_log",
        "hold",
    ]
    invoice_file: str
    timestamp: str
    result: dict


class Assessment(_Strict):
    outcome: Outcome
    reason_code: str | None = None
    reason: str
    action: str | None = None
    findings: list[Finding]
    currency: str


class ExtractionLine(_Loose):
    item: str
    description: str | None = None
    quantity: Decimal
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    note: str | None = None


class ExtractionSchema(_Loose):
    vendor: str | None = None
    vendor_address: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    line_items: list[ExtractionLine] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax_rate: Decimal | None = None
    tax_amount: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    payment_terms: str | None = None
    notes: str | None = None
    revision: str | None = None
    embedded_instructions: bool = False
    noncommercial_address: bool = False
    address_reason: str | None = None


class ReasoningDraft(_Loose):
    risks_considered: list[str] = Field(min_length=1)
    # Optional. The approval sentence is written in code, not from this field.
    conclusion: str = ""


class FactConcern(_Loose):
    """One critique or audit concern the code can check against a verified fact.

    category is stock, totals, date, or other. item is the inventory name for a
    stock concern. field is invoice_date or due_date for a date, or total or the
    line item for totals. A plain string has no category, so it is other.
    """

    category: str = "other"
    item: str = ""
    field: str = ""
    text: str = ""

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value):
        if isinstance(value, str):
            return {"category": "other", "text": value}
        return value


class CritiqueDraft(_Loose):
    concerns: list[FactConcern] = Field(default_factory=list)
    stricter: bool = False


class AuditDraft(_Loose):
    verdict: AuditVerdict
    feedback: list[str] = Field(default_factory=list)
    notes: str = ""
    # Empty means the concern is free text, not a finding code.
    cited_codes: list[str] = Field(default_factory=list)
    concerns: list[FactConcern] = Field(default_factory=list)


class PipelineState(BaseModel):
    """One invoice. Nothing in here is remembered for the next file."""

    invoice_path: str
    filename: str
    raw_text: str = ""
    feedback: list[str] = Field(default_factory=list)
    invoice: ExtractedInvoice | None = None
    validation: ValidationReport | None = None
    decision: ApprovalDecision | None = None
    critique_rounds: int = 0
    critique_concerns: list[str] = Field(default_factory=list)
    critique_stricter: bool = False
    send_backs: int = 0
    audit_verdict: str | None = None
    outcome: Outcome | None = None
    payment: PaymentRecord | None = None
    logs: list[dict] = Field(default_factory=list)
    attempts: list[dict] = Field(default_factory=list)
    agent_error: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    authorization: PaymentAuthorization | None = None
    reason_code: str | None = None
    action: str | None = None
