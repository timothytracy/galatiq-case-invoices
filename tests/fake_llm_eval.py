"""Run the sample invoices through the graph with the test fake LLM.

This is a regression probe, not a product mode. The model double is the same
shape the pipeline tests inject: request(messages, timeout=60), passed to
run_invoice with a fake clock, a zero jitter rng, and max_attempts=5.

Extraction text is the deterministic parse of that file, serialized as the
JSON the extraction prompt asks for. It is not a table of outcomes. Reasoning,
critique, and audit replies match the defaults in tests/test_pipeline.py.
"""

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from db import create_database
from graph import run_invoice
from main import payment_status
from parsers import parse_file

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
# Fixed so the sample run does not depend on the wall clock or the time zone.
AS_OF = date(2026, 10, 31)


class ZeroRandom:
    def uniform(self, _start, _end):
        return 0.0


class FakeClock:
    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


class FakeLLM:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, messages, timeout=60):
        self.calls.append(messages)
        result = self.handler(messages)
        if isinstance(result, Exception):
            raise result
        return result


def task_of(messages) -> str:
    for message in messages:
        first = (message.get("content") or "").splitlines()[0]
        if first.startswith("TASK "):
            return first.split()[1]
    return ""


def _num(value):
    if value is None:
        return None
    return format(value, "f")


def extraction_json(path: Path) -> str:
    """Stand-in model output: the fields the deterministic reader already found."""

    invoice = parse_file(path)
    payload = {
        "vendor": invoice.vendor,
        "vendor_address": invoice.vendor_address,
        "invoice_number": invoice.invoice_number_raw or invoice.invoice_number,
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "currency": invoice.currency,
        "line_items": [
            {
                "item": line.item,
                "description": line.description,
                "quantity": _num(line.quantity),
                "unit_price": _num(line.unit_price),
                "amount": _num(line.amount),
                "note": line.note,
            }
            for line in invoice.line_items
        ],
        "subtotal": _num(invoice.subtotal),
        "tax_rate": _num(invoice.tax_rate),
        "tax_amount": _num(invoice.tax_amount),
        "shipping": _num(invoice.shipping),
        "total": _num(invoice.total),
        "payment_terms": invoice.payment_terms,
        "notes": invoice.notes,
    }
    return json.dumps(payload)


def handler_for(path: Path):
    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            return extraction_json(path)
        if name == "reason":
            return json.dumps(
                {"risks_considered": ["no extra risk"], "conclusion": "The checks passed."}
            )
        if name == "critique":
            return json.dumps({"concerns": ["amount is high"], "stricter": False})
        if name == "audit":
            return json.dumps({"verdict": "PASS", "feedback": [], "notes": "PASS"})
        raise AssertionError(f"unexpected task {name}")

    return handler


def _reason(result: dict) -> str | None:
    decision = result.get("decision")
    if decision is None:
        return None
    if isinstance(decision, BaseModel):
        return decision.reasoning.conclusion
    return decision["reasoning"]["conclusion"]


def run_samples(db_path: Path) -> dict:
    """One row per sample file: outcome, reason code, and the reason text.

    as_of is fixed. The row does not include stage logs or today's date.
    """

    files = sorted(
        path for path in INVOICES.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES
    )
    rows = []
    for path in files:
        result = run_invoice(
            path,
            db_path,
            FakeLLM(handler_for(path)),
            clock=FakeClock(),
            rng=ZeroRandom(),
            max_attempts=5,
            as_of=AS_OF,
        )
        rows.append(
            {
                "file": path.name,
                "outcome": result.get("outcome"),
                "reason_code": result.get("reason_code"),
                "reason": _reason(result),
                "payment": payment_status(result),
            }
        )
    return {"as_of": AS_OF.isoformat(), "invoices": rows}


def main() -> int:
    db_path = ROOT / "inventory.db"
    created = not db_path.exists()
    if created:
        create_database(db_path)
    report = run_samples(db_path)
    for row in report["invoices"]:
        print(
            f"{row['file']}\t{row['outcome']}\t{row['reason_code']}\t{row['payment']}",
            flush=True,
        )
    print(f"{len(report['invoices'])} files", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
