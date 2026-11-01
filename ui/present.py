"""Read a serialized graph result. Names here are the pipeline's field names."""

from datetime import datetime
from decimal import Decimal

from main import payment_status

OUTCOMES = ("approve", "reject", "hold")
TRAIL_STAGES = ("ingestion", "validation", "approval", "critique", "audit", "payment")

INVOICE_FIELDS = (
    "vendor",
    "vendor_address",
    "invoice_number",
    "invoice_number_raw",
    "revision",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_rate",
    "tax_amount",
    "shipping",
    "total",
    "payment_terms",
    "notes",
    "embedded_instructions",
    "noncommercial_address",
    "address_reason",
)


def invoice_view(entry: dict) -> dict:
    """One invoice, using the graph state and, for a main.py log, its result line."""

    state = entry.get("state") or {}
    invoice = state.get("invoice") if isinstance(state.get("invoice"), dict) else {}
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    payment = state.get("payment") if isinstance(state.get("payment"), dict) else None
    amount = invoice.get("total")
    currency = invoice.get("currency")
    if amount is None:
        amount = decision.get("amount")
        currency = currency or decision.get("currency")
    if amount is None and payment:
        amount = payment.get("amount")
        currency = currency or payment.get("currency")
    filename = state.get("filename") or (entry.get("result_line") or {}).get("file") or ""
    return {
        "file": filename,
        "invoice_number": invoice.get("invoice_number"),
        "outcome": state.get("outcome"),
        "reason_code": state.get("reason_code"),
        "amount": amount,
        "currency": currency,
        "reason": _reason(state),
        "action": state.get("action"),
        "payment_status": _payment_status(entry),
        "payment": payment,
        "findings": _findings(state),
        "invoice": invoice or None,
        "logs": list(state.get("logs") or []),
        "elapsed_seconds": _elapsed(entry),
        "audit_verdict": state.get("audit_verdict"),
        "raw_text": state.get("raw_text") or "",
    }


def table_rows(entries: list[dict], outcome: str | None = None) -> list[dict]:
    """Rows for the results table. outcome filters on the real lowercase value."""

    selected = None if outcome in (None, "", "all") else outcome
    rows = []
    for entry in entries:
        view = invoice_view(entry)
        if selected and view["outcome"] != selected:
            continue
        rows.append(view)
    return rows


def holds(entries: list[dict]) -> list[dict]:
    return [invoice_view(entry) for entry in entries if (entry.get("state") or {}).get("outcome") == "hold"]


def summarize(entries: list[dict]) -> dict:
    """Counts, USD totals by outcome, and the mean per-invoice time."""

    counts = {name: 0 for name in OUTCOMES}
    dollars = {name: Decimal("0") for name in OUTCOMES}
    other: dict[str, dict[str, Decimal]] = {}
    timed = []
    sources = set()
    for entry in entries:
        view = invoice_view(entry)
        outcome = view["outcome"]
        if outcome in counts:
            counts[outcome] += 1
        amount = _decimal(view["amount"])
        currency = view["currency"] or ""
        if amount is not None and outcome in dollars and currency:
            if currency == "USD":
                dollars[outcome] += amount
            else:
                bucket = other.setdefault(currency, {name: Decimal("0") for name in OUTCOMES})
                bucket[outcome] += amount
        if view["elapsed_seconds"] is not None:
            timed.append(view["elapsed_seconds"])
            sources.add("elapsed_seconds" if entry.get("elapsed_seconds") is not None else "timestamps")
    average = sum(timed) / len(timed) if timed else None
    if not sources:
        timing = "none"
    elif sources == {"elapsed_seconds"}:
        timing = "elapsed_seconds"
    elif sources == {"timestamps"}:
        timing = "timestamps"
    else:
        timing = "mixed"
    return {
        "counts": counts,
        "dollars": dollars,
        "other": other,
        "average_seconds": average,
        "timed_invoices": len(timed),
        "invoices": len(entries),
        "timing": timing,
    }


def agent_trail(logs: list[dict]) -> dict:
    """Stage records in log order, plus which of the six agents never wrote a record."""

    rows = []
    seen = []
    for row in logs:
        stage = row.get("stage") or ""
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        rows.append(
            {
                "stage": stage,
                "timestamp": row.get("timestamp") or "",
                "summary": stage_summary(stage, result),
                "result": result,
            }
        )
        seen.append(stage)
    missing = [stage for stage in TRAIL_STAGES if stage not in seen]
    return {"rows": rows, "missing": missing}


def stage_summary(stage: str, result: dict) -> str:
    if stage == "ingestion":
        invoice = result.get("invoice") or {}
        return (
            f"vendor {_cell(invoice.get('vendor'))}, total {_cell(invoice.get('total'))} "
            f"{_cell(invoice.get('currency'))}"
        )
    if stage == "validation":
        return (
            f"passed {_cell(result.get('passed'))}, outcome {_cell(result.get('outcome'))}, "
            f"reason_code {_cell(result.get('reason_code'))}"
        )
    if stage == "approval":
        decision = result.get("decision") or {}
        return f"decision {_cell(decision.get('decision'))}"
    if stage == "critique":
        return f"stricter {_cell(result.get('stricter'))}, round {_cell(result.get('round'))}"
    if stage == "audit":
        return f"verdict {_cell(result.get('verdict'))}, outcome {_cell(result.get('outcome'))}"
    if stage == "payment":
        payment = result.get("payment") or {}
        return f"status {_cell(payment.get('status'))}"
    if stage in {"reject_log", "hold"}:
        return f"outcome {_cell(result.get('outcome'))}"
    return ""


def _cell(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def detail_index(views: list[dict]) -> int:
    """Prefer a paid invoice that also has a warning, so the first detail is not empty."""

    def score(view: dict) -> tuple:
        warnings = sum(1 for item in view["findings"] if item.get("severity") == "warning")
        paid = view["payment_status"] == "paid"
        return (warnings > 0 and paid, warnings, paid)

    if not views:
        return 0
    return max(range(len(views)), key=lambda index: score(views[index]))


def format_amount(amount, currency: str | None) -> str:
    number = _decimal(amount)
    if number is None:
        return ""
    text = f"{number:,.2f}"
    if currency:
        return f"{text} {currency}"
    return text


def format_money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _reason(state: dict) -> str:
    """Plain-English line. The decision stores it as reasoning.conclusion."""

    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    reasoning = decision.get("reasoning") if isinstance(decision.get("reasoning"), dict) else {}
    text = reasoning.get("conclusion")
    if text:
        return text
    for row in reversed(state.get("logs") or []):
        reason = (row.get("result") or {}).get("reason")
        if reason:
            return reason
    return ""


def _findings(state: dict) -> list:
    found = state.get("findings")
    if found:
        return list(found)
    for row in reversed(state.get("logs") or []):
        body = row.get("result") or {}
        if body.get("findings"):
            return list(body["findings"])
    return []


def _payment_status(entry: dict) -> str:
    state = entry.get("state") or {}
    if state.get("payment") is not None:
        return payment_status(state)
    line = entry.get("result_line") or {}
    if line.get("payment_status"):
        return line["payment_status"]
    return "not_paid"


def _elapsed(entry: dict) -> float | None:
    if entry.get("elapsed_seconds") is not None:
        return float(entry["elapsed_seconds"])
    return _span(entry.get("state", {}).get("logs") or [])


def _span(logs: list) -> float | None:
    stamps = []
    for row in logs:
        text = row.get("timestamp")
        if not text:
            continue
        stamps.append(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    if len(stamps) < 2:
        return None
    return (max(stamps) - min(stamps)).total_seconds()


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))
