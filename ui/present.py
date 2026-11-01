"""Read a serialized graph result and turn it into screen text."""

from datetime import datetime
from decimal import Decimal

from decision.policy import APPROVAL_THRESHOLD_USD
from main import payment_status

OUTCOMES = ("approve", "reject", "hold")
TRAIL_STAGES = ("ingestion", "validation", "approval", "critique", "audit", "payment")
APPROVED = "Approved, no action needed"
INVOICE_FIELDS = (
    "vendor", "vendor_address", "invoice_number", "invoice_number_raw", "revision",
    "invoice_date", "due_date", "currency", "subtotal", "tax_rate", "tax_amount",
    "shipping", "total", "payment_terms", "notes", "embedded_instructions",
    "noncommercial_address", "address_reason",
)
LINE_FIELDS = ("item", "description", "quantity", "unit_price", "amount", "note")


def blank(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def label(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else text


def show_text(value) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(show_text(item) for item in value if not blank(item))
    return str(value)


def threshold_text() -> str:
    """Critique bar from policy.toml, not a hardcoded amount."""

    amount = APPROVAL_THRESHOLD_USD
    return f"${amount:,.0f}" if amount == amount.to_integral_value() else f"${amount:,.2f}"


def not_required() -> str:
    return f"Not required (total under the {threshold_text()} threshold)"


def invoice_view(entry: dict) -> dict:
    state = entry.get("state") or {}
    invoice = state.get("invoice") if isinstance(state.get("invoice"), dict) else {}
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    payment = state.get("payment") if isinstance(state.get("payment"), dict) else None
    amount, currency = _first_amount(invoice, decision, payment)
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
        "raw_text": state.get("raw_text") or "",
        "llm": entry.get("llm"),
    }


def reason_code_text(view: dict) -> str:
    if view.get("outcome") == "approve" and blank(view.get("reason_code")):
        return APPROVED
    return "" if blank(view.get("reason_code")) else str(view["reason_code"])


def action_text(view: dict) -> str:
    if view.get("outcome") == "approve" and blank(view.get("action")):
        return APPROVED
    return "" if blank(view.get("action")) else str(view["action"])


def payment_text(status: str | None) -> str:
    return {"paid": "Paid", "not_paid": "Not paid"}.get(status or "", "" if blank(status) else str(status))


def table_rows(entries: list[dict], outcome: str | None = None) -> list[dict]:
    selected = None if outcome in (None, "", "all") else outcome
    rows = []
    for entry in entries:
        view = invoice_view(entry)
        if not selected or view["outcome"] == selected:
            rows.append(view)
    return rows


def holds(entries: list[dict]) -> list[dict]:
    return [view for view in (invoice_view(entry) for entry in entries) if view["outcome"] == "hold"]


def summarize(entries: list[dict]) -> dict:
    counts = {name: 0 for name in OUTCOMES}
    dollars = {name: Decimal("0") for name in OUTCOMES}
    other: dict[str, dict[str, Decimal]] = {}
    timed, sources = [], set()
    for entry in entries:
        view = invoice_view(entry)
        outcome = view["outcome"]
        if outcome in counts:
            counts[outcome] += 1
        amount = _decimal(view["amount"])
        if amount is not None and outcome in dollars and view["currency"]:
            bucket = dollars if view["currency"] == "USD" else other.setdefault(
                view["currency"], {name: Decimal("0") for name in OUTCOMES}
            )
            bucket[outcome] += amount
        if view["elapsed_seconds"] is not None:
            timed.append(view["elapsed_seconds"])
            sources.add("elapsed_seconds" if entry.get("elapsed_seconds") is not None else "timestamps")
    timing = "none" if not sources else sources.pop() if len(sources) == 1 else "mixed"
    return {
        "counts": counts, "dollars": dollars, "other": other,
        "average_seconds": (sum(timed) / len(timed)) if timed else None,
        "timed_invoices": len(timed), "invoices": len(entries), "timing": timing,
    }


def agent_trail(logs: list[dict], total=None) -> dict:
    rows, seen = [], []
    for row in logs:
        stage, result = row.get("stage") or "", row.get("result") if isinstance(row.get("result"), dict) else {}
        rows.append({
            "stage": stage,
            "timestamp": row.get("timestamp") or "",
            "summary": stage_summary(stage, result),
            "result": clean(result),
        })
        seen.append(stage)
    amount = _decimal(total)
    if "critique" in seen:
        critique = next(row["summary"] for row in reversed(rows) if row["stage"] == "critique")
    elif amount is None or amount <= APPROVAL_THRESHOLD_USD:
        critique = not_required()
    else:
        critique = "Did not run"
    missing = [stage for stage in TRAIL_STAGES if stage not in seen and stage != "critique"]
    return {"rows": rows, "missing": missing, "critique": critique}


def stage_summary(stage: str, result: dict) -> str:
    if stage == "ingestion":
        invoice = result.get("invoice") or {}
        return _join([
            ("Vendor", invoice.get("vendor")),
            ("total", format_amount(invoice.get("total"), invoice.get("currency"))),
        ])
    if stage == "validation":
        return _join([
            ("Passed", _yes(result.get("passed"))),
            ("outcome", result.get("outcome")),
            ("reason code", result.get("reason_code")),
        ])
    if stage == "approval":
        return _join([("Decision", (result.get("decision") or {}).get("decision"))])
    if stage == "critique":
        concerns = [item for item in result.get("concerns") or [] if not blank(item)]
        bits = [("Round", result.get("round"))]
        if isinstance(result.get("stricter"), bool):
            bits.append(("", "Stricter" if result["stricter"] else "Not stricter"))
        if concerns:
            bits.append(("Concerns", "; ".join(concerns)))
        return _join(bits)
    if stage == "audit":
        return _join([("Verdict", result.get("verdict")), ("outcome", result.get("outcome"))])
    if stage == "payment":
        return _join([("Status", payment_text((result.get("payment") or {}).get("status")))])
    if stage in {"reject_log", "hold"}:
        return _join([("Outcome", result.get("outcome"))])
    return ""


def clean(value):
    """Drop empty values so a stage result does not render null or []."""

    if isinstance(value, dict):
        return {key: item for key, item in ((key, clean(item)) for key, item in value.items()) if not blank(item)}
    if isinstance(value, list):
        return [item for item in (clean(item) for item in value) if not blank(item)]
    return value


def detail_index(views: list[dict]) -> int:
    def score(view: dict) -> tuple:
        warnings = sum(1 for item in view["findings"] if item.get("severity") == "warning")
        paid = view["payment_status"] == "paid"
        return (warnings > 0 and paid, warnings, paid)

    return max(range(len(views)), key=lambda index: score(views[index])) if views else 0


def format_amount(amount, currency: str | None) -> str:
    number = _decimal(amount)
    if number is None:
        return ""
    return f"{number:,.2f} {currency}" if currency else f"{number:,.2f}"


def format_money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _first_amount(invoice, decision, payment):
    for source, amount_key in ((invoice, "total"), (decision, "amount"), (payment or {}, "amount")):
        if source and source.get(amount_key) is not None:
            currency = source.get("currency") or invoice.get("currency") or decision.get("currency")
            return source.get(amount_key), currency
    return None, invoice.get("currency") or decision.get("currency")


def _reason(state: dict) -> str:
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    reasoning = decision.get("reasoning") if isinstance(decision.get("reasoning"), dict) else {}
    if reasoning.get("conclusion"):
        return reasoning["conclusion"]
    for row in reversed(state.get("logs") or []):
        reason = (row.get("result") or {}).get("reason")
        if reason:
            return reason
    return ""


def _findings(state: dict) -> list:
    if state.get("findings"):
        return list(state["findings"])
    for row in reversed(state.get("logs") or []):
        found = (row.get("result") or {}).get("findings")
        if found:
            return list(found)
    return []


def _payment_status(entry: dict) -> str:
    state = entry.get("state") or {}
    if state.get("payment") is not None:
        return payment_status(state)
    return (entry.get("result_line") or {}).get("payment_status") or "not_paid"


def _elapsed(entry: dict) -> float | None:
    if entry.get("elapsed_seconds") is not None:
        return float(entry["elapsed_seconds"])
    stamps = [
        datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
        for row in (entry.get("state") or {}).get("logs") or []
        if row.get("timestamp")
    ]
    if len(stamps) < 2:
        return None
    return (max(stamps) - min(stamps)).total_seconds()


def _decimal(value) -> Decimal | None:
    return None if blank(value) else Decimal(str(value))


def _join(pairs: list[tuple[str, object]]) -> str:
    parts = []
    for name, value in pairs:
        if not blank(value):
            parts.append(str(value) if not name else f"{name} {value}")
    return ", ".join(parts)


def _yes(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    return None if blank(value) else value
