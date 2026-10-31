"""Plain orchestrator: ingestion -> validation -> approval -> payment.

Phase 1 validation and approval are stubs. They do not apply the rules in
rules.py and they do not read data/labels.json. Ingestion parsing is real.
The Grok client is carried on the run so later phases can call it; this
phase does not send a prompt.
"""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from llm_client import GrokClient
from models import (
    ApprovalDecision,
    BatchSummary,
    ExtractedInvoice,
    Finding,
    InvoiceOutcome,
    PaymentRecord,
    SourceInfo,
    StageRecord,
    ValidationReport,
)
from parsers import parse_file
from rules import to_usd


def invoice_files(folder: Path) -> list[Path]:
    """Filename order. Later duplicate checks depend on this order."""

    suffixes = {".txt", ".pdf", ".json", ".csv", ".xml"}
    files = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in suffixes]
    return sorted(files, key=lambda path: path.name)


def process_many(
    paths: list[Path],
    db_path: Path,
    log_dir: Path,
    client: GrokClient,
) -> BatchSummary:
    log_dir.mkdir(parents=True, exist_ok=True)
    outcomes = [process_invoice(path, db_path, log_dir, client) for path in paths]
    approved = sum(1 for row in outcomes if row.decision == "approve")
    summary = BatchSummary(
        processed=len(outcomes),
        approved=approved,
        rejected=len(outcomes) - approved,
        paid=sum(1 for row in outcomes if row.payment_status == "paid"),
        invoices=outcomes,
    )
    print(summary.model_dump_json())
    (log_dir / "summary.json").write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return summary


def process_invoice(path: Path, db_path: Path, log_dir: Path, client: GrokClient) -> InvoiceOutcome:
    invoice = ingest(path)
    records = [_emit(path, "ingestion", invoice, client, stub=False)]
    report = validate(invoice, db_path)
    records.append(_emit(path, "validation", report, client, stub=True))
    decision = approve(invoice, report)
    records.append(_emit(path, "approval", decision, client, stub=True))
    payment = pay(invoice, decision)
    records.append(_emit(path, "payment", payment, client, stub=False))
    log_dir.mkdir(parents=True, exist_ok=True)
    destination = log_dir / f"{path.name}.json"
    payload = [record.model_dump(mode="json") for record in records]
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return InvoiceOutcome(
        file=path.name,
        decision=decision.decision,
        needs_human=decision.needs_human,
        payment_status=payment.status,
    )


def ingest(path: Path) -> ExtractedInvoice:
    try:
        return parse_file(path)
    except Exception as exc:
        return ExtractedInvoice(
            source=_failed_source(path),
            warnings=[f"Could not parse file: {exc}"],
        )


def validate(invoice: ExtractedInvoice, db_path: Path) -> ValidationReport:
    """Read the inventory snapshot and stop. Stock rules land in a later phase."""

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT item, stock FROM inventory ORDER BY item").fetchall()
    finally:
        connection.close()
    snapshot = ", ".join(f"{item} ({stock})" for item, stock in rows)
    return ValidationReport(
        passed=None,
        stub=True,
        findings=[
            Finding(
                code="stub",
                severity="info",
                message=(
                    "Phase 1 does not check stock, duplicates, or totals. "
                    f"Inventory snapshot: {snapshot}."
                ),
            )
        ],
    )


def approve(invoice: ExtractedInvoice, report: ValidationReport) -> ApprovalDecision:
    """Approve only when a few required fields are present.

    A hard failure from validation is trusted. Phase 1 validation leaves
    `passed` empty, so this does not change the sample runs. amount_usd is
    filled in so the log shows the figure the $10,000 check will use.
    """

    reasons = []
    if report.passed is False:
        reasons.append("validation reported a failure")
    if not invoice.vendor:
        reasons.append("missing vendor")
    if not invoice.invoice_number:
        reasons.append("missing invoice number")
    if not invoice.line_items:
        reasons.append("no line items")
    if invoice.total is None:
        reasons.append("missing total")
    elif invoice.total <= 0:
        reasons.append("total is not positive")

    amount_usd = to_usd(invoice.total, invoice.currency) if invoice.total is not None else None
    if reasons:
        return ApprovalDecision(
            decision="reject",
            needs_human=False,
            reasons=reasons,
            amount=invoice.total,
            currency=invoice.currency,
            amount_usd=amount_usd,
        )
    return ApprovalDecision(
        decision="approve",
        needs_human=False,
        reasons=["Phase 1 stub: required fields are present. Business rules are not applied yet."],
        amount=invoice.total,
        currency=invoice.currency,
        amount_usd=amount_usd,
    )


def pay(invoice: ExtractedInvoice, decision: ApprovalDecision) -> PaymentRecord:
    if decision.decision != "approve" or decision.needs_human:
        return PaymentRecord(
            status="not_paid",
            vendor=invoice.vendor,
            amount=invoice.total,
            currency=invoice.currency,
            detail="; ".join(decision.reasons),
        )
    response = mock_payment(invoice.vendor, invoice.total)
    return PaymentRecord(
        status="paid",
        vendor=invoice.vendor,
        amount=invoice.total,
        currency=invoice.currency,
        detail="mock payment accepted",
        mock_response=response,
    )


def mock_payment(vendor, amount):
    shown = f"{amount:.2f}" if isinstance(amount, Decimal) else amount
    print(f"Paid {shown} to {vendor}", flush=True)
    return {"status": "success"}


def _emit(path: Path, stage: str, payload, client: GrokClient, stub: bool) -> StageRecord:
    record = StageRecord(
        stage=stage,
        invoice_file=path.name,
        offline=client.offline,
        stub=stub,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        result=payload.model_dump(mode="json"),
    )
    print(record.model_dump_json(), flush=True)
    return record


def _failed_source(path: Path) -> SourceInfo:
    return SourceInfo(
        path=str(path),
        filename=path.name,
        format=path.suffix.lstrip(".").lower() or "unknown",
        parser="deterministic",
    )
