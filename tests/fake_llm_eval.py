"""Run the sample invoices through the graph with the test fake LLM.

This is a regression probe, not a product mode. The model double is the same
shape the pipeline tests inject: request(messages, timeout=60), passed to
run_invoice with a fake clock, a zero jitter rng, and max_attempts=5.

Extraction text is the deterministic parse of that file, serialized as the
JSON the extraction prompt asks for. It is not a table of outcomes. Reasoning,
critique, and audit replies match the defaults in tests/test_pipeline.py.
"""

import json
import re
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path

from pydantic import BaseModel

from db import create_database
from graph import run_invoice
from main import payment_status
from parsers import parse_file

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "data" / "invoices"
LABELS = ROOT / "data" / "labels.json"
SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


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


def _jsonable(value):
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    return value


def _stabilize(value, invoices_dir: Path):
    """Drop wall-clock timestamps and make invoice paths repo-relative.

    The pipeline prints a UTC timestamp on every stage log. That is the only
    value that changes between two identical runs.
    """

    prefix = str(invoices_dir.resolve())
    prefix_plain = str(invoices_dir)

    def walk(item):
        if isinstance(item, dict):
            return {key: walk(inner) for key, inner in item.items()}
        if isinstance(item, list):
            return [walk(inner) for inner in item]
        if isinstance(item, str):
            text = item.replace(prefix, "data/invoices").replace(prefix_plain, "data/invoices")
            return TIMESTAMP.sub("TIMESTAMP", text)
        return item

    return walk(value)


def run_samples(db_path: Path) -> dict:
    """Same per-file loop as scripts/eval.py, with the test fake injected."""

    labels = {
        row["file"]: row["outcome"]
        for row in json.loads(LABELS.read_text(encoding="utf-8"))["invoices"]
    }
    files = sorted(
        path for path in INVOICES.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES
    )
    rows = []
    correct = 0
    for path in files:
        stdout = StringIO()
        with redirect_stdout(stdout):
            result = run_invoice(
                path,
                db_path,
                FakeLLM(handler_for(path)),
                clock=FakeClock(),
                rng=ZeroRandom(),
                max_attempts=5,
            )
        actual = result.get("outcome")
        expected = labels.get(path.name)
        ok = actual == expected
        correct += int(ok)
        summary = (
            f"{path.name}\toutcome={actual}\texpected={expected}\t"
            f"payment={payment_status(result)}\t{'ok' if ok else 'MISS'}"
        )
        rows.append(
            {
                "summary": summary,
                "stdout": _stabilize(stdout.getvalue(), INVOICES),
                "state": _stabilize(_jsonable(dict(result)), INVOICES),
            }
        )
    return {
        "score": f"score {correct}/{len(files)}",
        "invoices": rows,
    }


def main() -> int:
    db_path = ROOT / "inventory.db"
    created = not db_path.exists()
    if created:
        create_database(db_path)
    report = run_samples(db_path)
    for row in report["invoices"]:
        print(row["summary"], flush=True)
    print(report["score"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
