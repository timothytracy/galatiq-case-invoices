"""Read a serialized graph result and turn it into screen text."""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from decision.invoice_fields import normalize_invoice_number
from decision.outcome import PAYABLE_HOLD_CODES, payment_status, review_release_block
from decision.pass_list import pass_checks
from decision.policy import APPROVAL_THRESHOLD_USD
from ledger.payments import vendor_key
from parsers import SUFFIXES, parse_file
from timestamps import STAMP_FORMAT
from tool_loop import TOOL_CALL_CAP, TOOL_ROUND_CAP

OUTCOMES = ("approve", "reject", "hold")
_ROOT = Path(__file__).resolve().parents[1]
_ANSWER_KEY = _ROOT / "data" / "answer_key.json"
_INVOICES = _ROOT / "data" / "invoices"


def _count_phrase(counts: dict, released: int = 0) -> str:
    parts = [f"{counts[name]} {name}" for name in OUTCOMES]
    if released:
        parts.append(f"released after review {released}")
    if len(parts) < 2:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


_COUNT_CACHE: tuple | None = None


def _labeled_folder_counts() -> tuple[dict, dict, list[tuple[str, str]]] | None:
    global _COUNT_CACHE
    if _COUNT_CACHE is not None:
        return _COUNT_CACHE
    counted = _folder_counts()
    if counted is not None:
        _COUNT_CACHE = counted
    return counted


def _folder_counts() -> tuple[dict, dict, list[tuple[str, str]]] | None:
    """Sample-score counts, and the same files on one ledger, from the answer key.

    An approve pays. A later approve of that same vendor and invoice number
    is a duplicate hold. A row counts as its labeled outcome, which is what the
    samples recording gives: 1004 is a fraud review hold. This reads the
    labeled file only. It does not run the pipeline.
    """
    try:
        labeled = json.loads(_ANSWER_KEY.read_text(encoding="utf-8"))["invoices"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    files = sorted(
        path for path in _INVOICES.iterdir()
        if path.is_file() and path.suffix.lower() in SUFFIXES
    ) if _INVOICES.is_dir() else []
    alone = dict.fromkeys(OUTCOMES, 0)
    shared = dict.fromkeys(OUTCOMES, 0)
    paid: dict[tuple[str, str], str] = {}
    flips: list[tuple[str, str]] = []
    for path in files:
        row = labeled.get(f"data/invoices/{path.name}")
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").lower()
        if outcome not in alone:
            continue
        alone[outcome] += 1
        number = normalize_invoice_number(row.get("invoice_number"))
        vendor = _vendor_of(path)
        identity = (vendor, number or "")
        if outcome == "approve" and vendor and number and identity in paid:
            shared["hold"] += 1
            flips.append((paid[identity], path.name))
            continue
        shared[outcome] += 1
        if outcome == "approve" and vendor and number:
            paid[identity] = path.name
    return alone, shared, flips


def _vendor_of(path: Path) -> str:
    try:
        return vendor_key(parse_file(path).vendor)
    except (OSError, ValueError, TypeError):
        return ""


def shared_ledger_note(batch_counts: dict | None = None, released: int = 0) -> str:
    """Why a shared-ledger folder tally differs from the per-invoice sample score.

    With no batch counts, the sentence says what a batch run is expected to give.
    With the session counts, it repeats those counts, including releases.
    """
    lead = "Process data/invoices shares one ledger, so an earlier payment changes a later invoice. "
    counted = _labeled_folder_counts()
    if counted is None:
        return lead + "The folder tally can differ from the README sample score, which runs each invoice on its own ledger."
    alone, shared, flips = counted
    change = ""
    if flips:
        change = " ".join(
            f"{earlier} is paid first, and {later} is then held as a duplicate instead of approved."
            for earlier, later in flips
        ) + " "
    if batch_counts is None:
        tally = f"A batch run is expected to give {_count_phrase(shared)}. "
    else:
        tally = f"The counts are {_count_phrase(batch_counts, released)}. "
    return (
        lead
        + change
        + tally
        + f"The README sample score runs each invoice on its own ledger: {_count_phrase(alone)}."
    )


TRAIL_STAGES = ("ingestion", "validation", "fraud_review", "approval", "critique", "audit", "payment")
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
    amount = APPROVAL_THRESHOLD_USD
    return f"${amount:,.0f}" if amount == amount.to_integral_value() else f"${amount:,.2f}"
def not_required() -> str:
    return f"Not required (total under the {threshold_text()} threshold)"

def invoice_view(entry: dict) -> dict:
    state = entry.get("state") if isinstance(entry.get("state"), dict) else {}
    invoice = state.get("invoice") if isinstance(state.get("invoice"), dict) else {}
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    payment = state.get("payment") if isinstance(state.get("payment"), dict) else None
    amount, currency = _first_amount(invoice, decision, payment)
    logs = list(state.get("logs") or [])
    filename = entry.get("display_name") or state.get("filename") or (entry.get("result_line") or {}).get("file") or ""
    return {
        "file": filename, "invoice_number": invoice.get("invoice_number"),
        "outcome": state.get("outcome"), "reason_code": state.get("reason_code"),
        "amount": amount, "currency": currency, "reason": _reason(state),
        "action": state.get("action"), "payment_status": _payment_status(entry),
        "payment": payment, "findings": _findings(state), "invoice": invoice or None,
        "logs": logs, "elapsed_seconds": _elapsed(entry),
        "raw_text": state.get("raw_text") or "", "llm": entry.get("llm"),
        "critique_concerns": concern_list(state.get("critique_concerns")),
        "tool_calls": tool_call_view(logs, entry.get("result_line"), state.get("attempts")),
        "authorization": _authorization_key(state),
    }
def pass_list_rows(view: dict) -> list[dict]:
    """The six pass-list checks for the detail screen.

    The run's own record is used when it has one. An older saved run is
    checked again from its invoice, findings, and audit verdict.
    """
    checks = None
    verdict = None
    for row in view.get("logs") or []:
        result = row.get("result") if isinstance(row, dict) and isinstance(row.get("result"), dict) else {}
        if isinstance(result.get("pass_list"), list):
            checks = result["pass_list"]
        if row.get("stage") == "audit" and result.get("verdict"):
            verdict = result["verdict"]
    if checks is None:
        if not view.get("invoice"):
            return []
        checks = pass_checks(view["invoice"], view.get("findings") or [], verdict)
    return [
        {"Check": item.get("check"), "Result": "Pass" if item.get("ok") else "Fail", "Detail": item.get("detail")}
        for item in checks if isinstance(item, dict)
    ]
def _place(view: dict, key: str) -> str:
    if view.get("outcome") == "approve" and blank(view.get(key)):
        return APPROVED
    return "" if blank(view.get(key)) else str(view[key])
def reason_code_text(view: dict) -> str:
    return _place(view, "reason_code")
def action_text(view: dict) -> str:
    return _place(view, "action")
def payment_detail_text(view: dict) -> str:
    """Amount, reviewer, time, and authorization for a payment that happened.

    Empty when nothing was paid. The hold reason is never used. On a hold,
    payment.detail is that reason, so this does not read it.
    """
    if view.get("payment_status") != "paid":
        return ""
    payment = view.get("payment") if isinstance(view.get("payment"), dict) else {}
    parts = []
    amount = payment.get("amount")
    if blank(amount):
        amount = view.get("amount")
    currency = payment.get("currency") or view.get("currency")
    shown = format_amount(amount, currency)
    if shown:
        parts.append(f"Amount {shown}")
    if not blank(payment.get("reviewer")):
        parts.append(f"Reviewer {payment['reviewer']}")
    when = payment.get("time") or payment.get("paid_at") or _payment_logged_at(view)
    if not blank(when):
        parts.append(f"Time {when}")
    auth = _repo_authorization(view.get("authorization") or payment.get("authorization"))
    if not blank(auth):
        parts.append(f"Authorization {auth}")
    if not parts:
        return ""
    return ". ".join(parts) + "."


def already_paid_notice(view: dict, row: dict) -> str:
    """Why the pay form is hidden: the ledger already has this invoice paid."""
    number = view.get("invoice_number")
    if blank(number):
        number = "this invoice"
    source = "" if not isinstance(row, dict) else row.get("source_file")
    if blank(source):
        return f"Invoice {number} from this vendor is already paid."
    return f"Invoice {number} from this vendor is already paid as {source}."


def _repo_authorization(value) -> str:
    """Show a checkout-relative path when the authorization key starts with one."""
    if blank(value):
        return ""
    text = str(value)
    path, sep, digest = text.rpartition(":")
    if not sep or not path:
        return text
    candidate = Path(path)
    if not candidate.is_absolute():
        return text
    try:
        relative = candidate.resolve().relative_to(_ROOT.resolve())
    except (OSError, ValueError):
        return text
    return f"{relative.as_posix()}:{digest}"


def _authorization_key(state: dict) -> str:
    payment = state.get("payment") if isinstance(state.get("payment"), dict) else {}
    if not blank(payment.get("authorization")):
        return str(payment["authorization"])
    auth = state.get("authorization")
    if isinstance(auth, dict) and not blank(auth.get("key")):
        return str(auth["key"])
    return ""


def _payment_logged_at(view: dict) -> str:
    for row in reversed(view.get("logs") or []):
        if isinstance(row, dict) and row.get("stage") == "payment" and not blank(row.get("timestamp")):
            return str(row["timestamp"])
    return ""


def payment_text(status: str | None) -> str:
    names = {"paid": "Paid", "not_paid": "Not paid"}
    if not status:
        return ""
    return names.get(status, str(status))
def table_rows(entries: list[dict], outcome: str | None = None) -> list[dict]:
    selected = None if outcome in (None, "", "all") else outcome
    return [view for view in map(invoice_view, entries) if not selected or view["outcome"] == selected]
def pay_block(view: dict) -> str | None:
    """Why this hold cannot be paid. None when every hold code is payable.

    Payable codes are PAYABLE_HOLD_CODES. Warnings may sit beside them.
    """
    if view.get("outcome") != "hold":
        return "Only a held invoice can be paid."
    if released(view):
        return "Already paid after review."
    blocked = review_release_block(
        view.get("outcome"), view.get("findings") or [], view.get("reason_code"),
    )
    if blocked:
        return blocked
    codes = [
        item.get("code") for item in view.get("findings") or []
        if isinstance(item, dict) and item.get("code")
    ]
    if any(code in PAYABLE_HOLD_CODES for code in codes) or codes.count("FRAUD_SIGNAL") >= 2:
        return None
    return "This hold is not a review hold and is never paid."
def can_pay(view: dict) -> bool:
    """True only for an unpaid hold whose codes are all review holds. Rejects are not payable."""
    return view.get("outcome") == "hold" and pay_block(view) is None
def released(view: dict) -> bool:
    """A hold a reviewer released and paid. Its pipeline outcome stays hold."""
    return view.get("outcome") == "hold" and view.get("payment_status") == "paid"
def holds(entries: list[dict]) -> list[dict]:
    return [view for view in map(invoice_view, entries) if view.get("outcome") == "hold"]
def confirm_label(view: dict) -> str:
    amount = format_amount(view.get("amount"), view.get("currency"))
    name = view.get("file") or "this invoice"
    if amount:
        return f"Confirm payment of {amount} for {name}"
    return f"Confirm payment for {name}"
def concern_list(value) -> list[str]:
    """Critique concerns as plain text. A missing or empty value is no concerns."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if not blank(item) and str(item).strip()]
def concern_text(value) -> str:
    items = concern_list(value)
    if not items:
        return ""
    return "Critique concerns: " + "; ".join(items)
def pay_message(view: dict, result: dict) -> tuple[str, str]:
    """("success", text) or ("error", text).

    Paid names the payment status. A refusal shows the reason code and the reason.
    Critique concerns are added when the result includes them.
    """
    concerns = concern_text(None if not isinstance(result, dict) else result.get("critique_concerns"))
    if result.get("result") == "paid" or result.get("status") == "paid":
        status = payment_text(result.get("payment_status") or "paid") or "Paid"
        text = f"Paid {view.get('file') or 'invoice'}. Payment status: {status}."
        paid_amount = result.get("amount")
        currency = result.get("currency") or view.get("currency")
        amount = format_amount(
            view.get("amount") if paid_amount in (None, "") else paid_amount,
            currency,
        )
        if amount:
            text = f"{text} {amount}."
        if concerns:
            text = f"{text} {concerns}"
        return "success", text
    reason = result.get("reason")
    reason = reason.strip() if isinstance(reason, str) and reason.strip() else "Payment refused."
    code = result.get("reason_code")
    text = f"{code.strip()}. {reason}" if isinstance(code, str) and code.strip() else reason
    if concerns:
        text = f"{text} {concerns}"
    return "error", text

def summarize(entries: list[dict]) -> dict:
    counts = dict.fromkeys(OUTCOMES, 0)
    dollars = {name: Decimal("0") for name in OUTCOMES}
    other: dict[str, dict[str, Decimal]] = {}
    paid_holds = {"count": 0, "dollars": Decimal("0"), "other": {}}
    timed, sources = [], set()
    for entry in entries:
        view = invoice_view(entry)
        outcome = view["outcome"]
        amount, currency = _decimal(view["amount"]), view["currency"]
        if released(view):
            paid_holds["count"] += 1
            paid_amount = _decimal((view.get("payment") or {}).get("amount"))
            if paid_amount is None:
                paid_amount = amount
            if paid_amount is not None and currency == "USD":
                paid_holds["dollars"] += paid_amount
            elif paid_amount is not None and currency:
                paid_holds["other"][currency] = paid_holds["other"].get(currency, Decimal("0")) + paid_amount
        else:
            if outcome in counts:
                counts[outcome] += 1
            if amount is not None and outcome in dollars and currency:
                bucket = dollars if currency == "USD" else other.setdefault(currency, {name: Decimal("0") for name in OUTCOMES})
                bucket[outcome] += amount
        if view["elapsed_seconds"] is not None:
            timed.append(view["elapsed_seconds"])
            sources.add("elapsed_seconds" if entry.get("elapsed_seconds") is not None else "timestamps")
    timing = "none" if not sources else (next(iter(sources)) if len(sources) == 1 else "mixed")
    return {
        "counts": counts, "dollars": dollars, "other": other, "released": paid_holds,
        "average_seconds": sum(timed) / len(timed) if timed else None,
        "timed_invoices": len(timed), "invoices": len(entries), "timing": timing,
    }

def agent_trail(logs: list[dict], total=None) -> dict:
    rows, seen = [], []
    for row in logs:
        stage = row.get("stage") or ""
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        rows.append({
            "stage": stage, "timestamp": row.get("timestamp") or "",
            "summary": stage_summary(stage, result),
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
    invoice = result.get("invoice") or {}
    decision = result.get("decision") or {}
    payment = result.get("payment") or {}
    if stage == "critique":
        concerns = [item for item in result.get("concerns") or [] if not blank(item)]
        bits = [("Round", result.get("round"))]
        if isinstance(result.get("stricter"), bool):
            bits.append(("", "Stricter" if result["stricter"] else "Not stricter"))
        if concerns:
            bits.append(("Concerns", "; ".join(concerns)))
        return _join(bits)
    pairs = {
        "ingestion": [("Vendor", invoice.get("vendor")), ("total", format_amount(invoice.get("total"), invoice.get("currency")))],
        "validation": [("Passed", _yes(result.get("passed"))), ("outcome", result.get("outcome")), ("reason code", result.get("reason_code"))],
        "fraud_review": [("Verdict", result.get("fraud_verdict")), ("outcome", result.get("outcome"))],
        "approval": [("Decision", decision.get("decision"))],
        "audit": [("Verdict", result.get("verdict")), ("outcome", result.get("outcome"))],
        "payment": [("Status", payment_text(payment.get("status")))],
        "reject_log": [("Outcome", result.get("outcome"))],
        "hold": [("Outcome", result.get("outcome"))],
    }
    return _join(pairs.get(stage, []))
def detail_index(views: list[dict]) -> int:
    def score(view: dict) -> tuple:
        warnings = sum(1 for item in view["findings"] if item.get("severity") == "warning")
        paid = view["payment_status"] == "paid"
        return (warnings > 0 and paid, warnings, paid)
    return max(range(len(views)), key=lambda i: score(views[i])) if views else 0
def format_amount(amount, currency: str | None) -> str:
    number = _decimal(amount)
    if number is None:
        return ""
    return f"{number:,.2f} {currency}" if currency else f"{number:,.2f}"
def format_money(amount: Decimal) -> str:
    return f"{amount:,.2f}"
def _first_amount(invoice, decision, payment):
    for source, key in ((invoice, "total"), (decision, "amount"), (payment or {}, "amount")):
        if source and source.get(key) is not None:
            currency = source.get("currency") or invoice.get("currency") or decision.get("currency")
            return source.get(key), currency
    return None, invoice.get("currency") or decision.get("currency")

def _reason(state: dict) -> str:
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    reasoning = decision.get("reasoning") if isinstance(decision.get("reasoning"), dict) else {}
    if reasoning.get("conclusion"):
        return reasoning["conclusion"]
    return next((
        (row.get("result") or {}).get("reason") for row in reversed(state.get("logs") or [])
        if (row.get("result") or {}).get("reason")
    ), "")

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
        datetime.strptime(row["timestamp"], STAMP_FORMAT)
        for row in (entry.get("state") or {}).get("logs") or [] if row.get("timestamp")
    ]
    if len(stamps) < 2:
        return None
    return (max(stamps) - min(stamps)).total_seconds()
def _decimal(value) -> Decimal | None:
    return None if blank(value) else Decimal(str(value))
def _join(pairs) -> str:
    parts = [str(value) if not name else f"{name} {value}" for name, value in pairs if not blank(value)]
    return ", ".join(parts)
def _yes(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    return None if blank(value) else value

# Fields the tool-call record is known to carry. Anything else is shown as text,
# so a new marker lands on screen without a name being chosen here.
_CALL_KEYS = frozenset({
    "agent", "round", "attempt", "call_id", "name", "arguments", "arguments_raw",
    "result", "error", "latency_ms", "repeat",
})
_ATTEMPT_KEYS = frozenset({
    "attempt", "error_type", "backoff_seconds", "retryable", "matched",
    "conclusion", "risks_considered", "tool_calls", "cap",
})
_RESULT_KEYS = frozenset({
    "invoice", "feedback_used", "attempts", "payment", "outcome", "codes", "reason",
    "decision", "passed", "reason_code", "action", "currency", "as_of", "date_context",
    "findings", "model_verdict", "verdict", "feedback", "notes", "cited_codes",
    "audit_note", "dropped_concerns", "concerns", "stricter", "round", "tool_calls",
    "record", "file", "payment_status", "critique_rounds",
    "tool_cap_hit", "forced_final_turn",
    "fraud_verdict", "innocent_reading", "quote", "why", "fraud_note",
})
_BLOCKED_ERROR = "blocked: tool_cap"

def tool_call_view(logs, result_line=None, attempts=None) -> dict:
    """Tool calls for one invoice, in log order. Empty lists and missing keys yield no rows."""
    calls, seen, notes, noted = [], {}, [], set()
    holders = []
    for log in _list(logs):
        if isinstance(log, dict):
            holders.append((str(log.get("stage") or ""), log.get("result")))
    if isinstance(result_line, dict):
        holders.append(("", result_line))
    for stage, result in holders:
        if not isinstance(result, dict):
            continue
        _add_cap(notes, noted, result)
        if "tool_calls" in result:
            _add_note(notes, noted, stage, _unknown(result, _RESULT_KEYS))
            _take_calls(calls, seen, result.get("tool_calls"), stage, False, None)
        for attempt in _list(result.get("attempts")):
            _read_attempt(calls, seen, notes, noted, stage, attempt)
    for attempt in _list(attempts):
        _read_attempt(calls, seen, notes, noted, "", attempt)
    return {"calls": calls, "extras": notes}

def _read_attempt(calls, seen, notes, noted, stage, attempt) -> None:
    if not isinstance(attempt, dict):
        return
    number = attempt.get("attempt")
    where = stage if blank(number) else f"{stage} attempt {number}".strip()
    _add_note(notes, noted, where, _unknown(attempt, _ATTEMPT_KEYS))
    _take_calls(calls, seen, attempt.get("tool_calls"), stage, True, number)

def _take_calls(calls, seen, raw, stage, failed, attempt_number) -> None:
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, dict):
            continue
        row = _call_row(item, stage, failed, attempt_number)
        key = _call_key(item, attempt_number)
        prior = None if key is None else seen.get(key)
        if prior is None:
            if key is not None:
                seen[key] = len(calls)
            calls.append(row)
            continue
        if failed:
            calls[prior]["failed_attempt"] = True
        if row["extra"] and not calls[prior]["extra"]:
            calls[prior]["extra"] = row["extra"]

def _call_row(call, stage, failed, attempt_number) -> dict:
    attempt = call.get("attempt") if "attempt" in call else attempt_number
    error_text = "" if blank(call.get("error")) else str(call.get("error"))
    blocked = error_text == _BLOCKED_ERROR
    plain_error = bool(error_text) and not blocked
    if blocked:
        detail = "Stopped by the tool cap"
    elif plain_error:
        detail = error_text
    elif "result" in call:
        detail = _brief(call.get("result"))
    else:
        detail = ""
    who = call.get("agent")
    if blank(who):
        who = stage
    return {
        "who": "" if blank(who) else str(who),
        "round": _num(call.get("round")) if "round" in call else "",
        "attempt": _num(attempt),
        "name": "" if blank(call.get("name")) else str(call.get("name")),
        "arguments": _brief(call.get("arguments")) if "arguments" in call else "",
        "arguments_raw": "" if blank(call.get("arguments_raw")) else str(call.get("arguments_raw")),
        "detail": detail,
        "latency": _latency(call.get("latency_ms")) if "latency_ms" in call else "",
        "repeat": call.get("repeat") is True,
        "blocked": blocked,
        "error": plain_error,
        "failed_attempt": bool(failed),
        "extra": _brief(_unknown(call, _CALL_KEYS)),
    }

def _call_key(call, attempt_number):
    attempt = call.get("attempt") if "attempt" in call else attempt_number
    attempt_text = "" if blank(attempt) else str(attempt)
    call_id = call.get("call_id")
    if not blank(call_id):
        agent = "" if blank(call.get("agent")) else str(call.get("agent"))
        return ("id", agent, str(call_id), attempt_text)
    try:
        body = json.dumps(call, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return ("body", body, attempt_text)

def _unknown(body, known) -> dict:
    return {key: value for key, value in body.items() if key not in known and not blank(value)}

def _add_cap(notes, noted, body) -> None:
    """Say when this stage hit the tool cap, and when a no-tools answer was forced."""
    hit = body.get("tool_cap_hit")
    forced = body.get("forced_final_turn") is True
    parts = []
    if hit == "rounds":
        parts.append(f"The tool cap was hit on rounds (limit {TOOL_ROUND_CAP}).")
    elif hit == "calls":
        parts.append(f"The tool cap was hit on calls (limit {TOOL_CALL_CAP}).")
    elif not blank(hit):
        parts.append(f"The tool cap was hit on {hit}.")
    if forced:
        parts.append("A final no-tools answer was forced.")
    text = " ".join(parts)
    if not text or text in noted:
        return
    noted.add(text)
    notes.append(text)

def _add_note(notes, noted, where, fields) -> None:
    text = _brief(fields)
    if not text or text in noted:
        return
    noted.add(text)
    prefix = label(where) if where else "Result"
    notes.append(f"{prefix}: {text}")

def _brief(value) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if blank(value):
        return ""
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _brief(item)
            if text:
                parts.append(f"{label(str(key))} {text}")
        return ", ".join(parts)
    if isinstance(value, list):
        return ", ".join(text for text in (_brief(item) for item in value) if text)
    return str(value)

def _latency(value) -> str:
    if isinstance(value, bool) or blank(value):
        return ""
    if isinstance(value, (int, float)):
        number = int(value) if float(value).is_integer() else value
        return f"{number} ms"
    return f"{value} ms"

def _num(value) -> str:
    if isinstance(value, bool) or blank(value):
        return ""
    return str(value)

def _list(value):
    return value if isinstance(value, list) else []
