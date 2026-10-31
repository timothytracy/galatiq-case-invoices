"""Ingestion. Read the file, ask the model to extract it, then normalize names."""

from pathlib import Path

from llm_client import AgentStepFailed, call_with_retries
from models import (
    ExtractedInvoice,
    ExtractionSchema,
    LineItem,
    PipelineState,
    SourceInfo,
)
from parsers import read_raw
from pipeline.stage_log import _stopped, stage_log
from rules import normalize_invoice_number, normalize_item_name, resolve_currency


def ingest(state: PipelineState, llm, clock, rng, max_attempts: int) -> dict:
    path = Path(state.invoice_path)
    try:
        raw = read_raw(path)
    except Exception as exc:
        reason = f"Could not read the invoice: {exc}"
        return _stopped(
            state,
            "reject",
            reason,
            "ingestion",
            [],
            agent_error=None,
        )

    step_attempts: list[dict] = []
    try:
        extracted = call_with_retries(
            llm.request,
            _extract_messages(raw, state.feedback),
            ExtractionSchema,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(state, "manual_review", str(exc), "ingestion", step_attempts, agent_error=str(exc))

    invoice = invoice_from_extraction(extracted, path, raw)
    result = {
        "invoice": invoice.model_dump(mode="json"),
        "feedback_used": list(state.feedback),
        "attempts": step_attempts,
    }
    return {
        "raw_text": raw,
        "invoice": invoice,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "ingestion", result)],
    }


def _extract_messages(raw: str, feedback: list[str]) -> list[dict]:
    repair = ""
    if feedback:
        repair = "A previous extraction was sent back. Fix these fields:\n" + "\n".join(
            f"- {item}" for item in feedback
        )
    return [
        {
            "role": "system",
            "content": (
                "TASK extract\n"
                "Extract one invoice as a JSON object. Use only values printed in the document. "
                "Do not invent lines. currency is a 3-letter code. "
                "If the document shows euro, EUR, euros, or €, currency is EUR. "
                "If currency is missing and amounts use $, currency is USD. "
                "Numbers are JSON numbers. Return JSON only.\n"
                "Keys: vendor, vendor_address, invoice_number, invoice_date, due_date, currency, "
                "line_items, subtotal, tax_rate, tax_amount, shipping, total, payment_terms, notes.\n"
                "Each line item: item, description, quantity, unit_price, amount, note."
            ),
        },
        {"role": "user", "content": (repair + "\n\n" + raw).strip()},
    ]


def invoice_from_extraction(data: ExtractionSchema, path: Path, raw: str) -> ExtractedInvoice:
    items = []
    for line in data.line_items:
        label = (line.description or line.item or "").strip()
        code, note = normalize_item_name(line.item or label)
        extra = _blank(line.note)
        if note and extra:
            extra = f"{note}; {extra}"
        elif note:
            extra = note
        items.append(
            LineItem(
                item=code or label or "UNKNOWN",
                description=label or code or "UNKNOWN",
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
                note=extra,
            )
        )
    currency = resolve_currency(data.currency, raw)
    raw_number = _blank(data.invoice_number)
    return ExtractedInvoice(
        vendor=_blank(data.vendor),
        vendor_address=_blank(data.vendor_address),
        invoice_number=normalize_invoice_number(raw_number),
        invoice_number_raw=raw_number,
        invoice_date=_blank(data.invoice_date),
        due_date=_blank(data.due_date),
        currency=currency,
        line_items=items,
        subtotal=data.subtotal,
        tax_rate=data.tax_rate,
        tax_amount=data.tax_amount,
        shipping=data.shipping,
        total=data.total,
        payment_terms=_blank(data.payment_terms),
        notes=_blank(data.notes),
        source=SourceInfo(
            path=str(path),
            filename=path.name,
            format=path.suffix.lstrip(".").lower() or "txt",
            parser="grok",
            raw_text=raw,
        ),
    )


def _blank(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
