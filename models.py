"""Shared shapes for the invoice workflow.

Money is Decimal. Missing values are None. Dates stay as printed text.
The graph state holds these models. Nodes return updates to that state.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Outcome = Literal["approve", "reject", "hold"]
AuditVerdict = Literal["PASS", "SEND_BACK", "ESCALATE"]


class SourceInfo(BaseModel):
    """Where an invoice came from. raw_text is the document text before any repair."""

    model_config = ConfigDict(extra="forbid")

    path: str
    filename: str
    format: str
    parser: str = "grok"
    raw_text: str | None = None


class LineItem(BaseModel):
    """One priced row. item is the normalized code. description is the printed text."""

    model_config = ConfigDict(extra="forbid")

    item: str
    description: str
    quantity: Decimal
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    note: str | None = None


class ExtractedInvoice(BaseModel):
    """Ingestion output. Totals are the printed numbers, not recomputed ones."""

    model_config = ConfigDict(extra="forbid")

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
    # True only when the extraction model flags instructions aimed at the system.
    # A reject also needs the deterministic phrase check in rules.py.
    embedded_instructions: bool = False
    # Set by extraction when the vendor address looks like a home. Warning only.
    noncommercial_address: bool = False
    source: SourceInfo
    warnings: list[str] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        text = value.strip().upper()
        if len(text) != 3 or not text.isalpha():
            raise ValueError("currency must be a 3-letter code")
        return text


class Finding(BaseModel):
    """One problem a stage raised.

    code is a stable short name such as STOCK_SHORTFALL.
    severity is info, warning, or blocking.
    item is the line or invoice field this finding is about, or null.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    severity: Literal["info", "warning", "blocking"] = "blocking"
    item: str | None = None


class PaymentAuthorization(BaseModel):
    """Sign-off produced only by a PASS audit. Payment refuses to run without one.

    key is one-time for this process. The audit builds it from the invoice
    file path and a hash of the file text.
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str
    amount: Decimal
    currency: str
    key: str = Field(min_length=1)


class ValidationReport(BaseModel):
    """Validation output. passed is true only when no rule blocks payment."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    findings: list[Finding] = Field(default_factory=list)


class ApprovalReasoning(BaseModel):
    """Why a decision was made. Evidence is taken from the invoice and the findings."""

    model_config = ConfigDict(extra="forbid")

    rules_applied: list[str] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    risks_considered: list[str] = Field(min_length=1)
    conclusion: str = Field(min_length=1)


class ApprovalDecision(BaseModel):
    """Approval output. Pay only when decision is approve and the audit returns PASS."""

    model_config = ConfigDict(extra="forbid")

    decision: Outcome
    reasoning: ApprovalReasoning
    reason_code: str | None = None
    action: str | None = None
    revisions: list[ApprovalReasoning] = Field(default_factory=list)
    amount: Decimal | None = None
    currency: str | None = None


class PaymentRecord(BaseModel):
    """Payment output. mock_response is set only when status is paid."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["paid", "not_paid"]
    vendor: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    detail: str
    mock_response: dict | None = None


class StageRecord(BaseModel):
    """One printed and saved log record."""

    model_config = ConfigDict(extra="forbid")

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


class InvoiceLabel(BaseModel):
    """One row of data/labels.json. The outcome comes from that file's own content."""

    model_config = ConfigDict(extra="forbid")

    file: str
    outcome: Outcome
    reason: str = Field(min_length=1)


class Assessment(BaseModel):
    """Deterministic rule result for one invoice."""

    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    reason_code: str | None = None
    reason: str
    action: str | None = None
    findings: list[Finding]
    currency: str


class ExtractionLine(BaseModel):
    """One line in the extraction agent's JSON."""

    model_config = ConfigDict(extra="ignore")

    item: str
    description: str | None = None
    quantity: Decimal
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    note: str | None = None


class ExtractionSchema(BaseModel):
    """JSON the extraction agent must return."""

    model_config = ConfigDict(extra="ignore")

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


class ReasoningDraft(BaseModel):
    """The part of the approval reasoning the model writes."""

    model_config = ConfigDict(extra="ignore")

    risks_considered: list[str] = Field(min_length=1)
    conclusion: str = Field(min_length=1)


class CritiqueDraft(BaseModel):
    """Critique may add concerns and may only make the decision stricter."""

    model_config = ConfigDict(extra="ignore")

    concerns: list[str] = Field(default_factory=list)
    stricter: bool = False


class AuditDraft(BaseModel):
    """Audit verdict. Python may tighten it. It may not loosen it."""

    model_config = ConfigDict(extra="ignore")

    verdict: AuditVerdict
    feedback: list[str] = Field(default_factory=list)
    notes: str = ""


class PipelineState(BaseModel):
    """Graph state. One invoice, no memory of any other file."""

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
