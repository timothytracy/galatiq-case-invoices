"""Shared data shapes for the four invoice stages.

Later phases should keep these fields stable and add behavior behind them.
Money is Decimal. Missing values are None. Dates stay as printed text.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceInfo(BaseModel):
    """Where an invoice came from. raw_text is the text the parser saw, before OCR repair."""

    model_config = ConfigDict(extra="forbid")

    path: str
    filename: str
    format: str
    parser: str = "deterministic"
    raw_text: str | None = None


class LineItem(BaseModel):
    """One priced row. `item` is the normalized code; `description` is the printed text."""

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
    """One validation observation. `code` stays an open string so later phases can add codes."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    severity: Literal["error", "warning", "info"] = "error"
    item: str | None = None


class ValidationReport(BaseModel):
    """Validation output. passed is None when this stage did not judge the invoice."""

    model_config = ConfigDict(extra="forbid")

    passed: bool | None = None
    findings: list[Finding] = Field(default_factory=list)
    stub: bool = False


class ApprovalDecision(BaseModel):
    """Approval output. Pay only when decision is approve and needs_human is false."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    needs_human: bool = False
    reasons: list[str] = Field(min_length=1)
    amount: Decimal | None = None
    currency: str | None = None
    amount_usd: Decimal | None = None


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
    """One saved log record. The pipeline writes four of these per invoice."""

    model_config = ConfigDict(extra="forbid")

    record: Literal["stage"] = "stage"
    stage: Literal["ingestion", "validation", "approval", "payment"]
    invoice_file: str
    offline: bool
    stub: bool
    timestamp: str
    result: dict


class InvoiceOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    decision: Literal["approve", "reject"]
    needs_human: bool
    payment_status: Literal["paid", "not_paid"]


class BatchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: Literal["summary"] = "summary"
    processed: int
    approved: int
    rejected: int
    paid: int
    invoices: list[InvoiceOutcome]


class InvoiceLabel(BaseModel):
    """One row of data/labels.json. This is the answer key, not the Phase 1 stub."""

    model_config = ConfigDict(extra="forbid")

    file: str
    invoice_number: str
    decision: Literal["approve", "reject"]
    needs_human: bool
    scenario: str
    reason: str = Field(min_length=1)
