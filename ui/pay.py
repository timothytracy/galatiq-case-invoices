"""One adapter for the pipeline's approve-and-pay guard.

The pipeline function is ``pipeline.approve_hold.approve_and_pay``:

    approve_and_pay(result, *, reviewer, note, amount, ledger_path=None, log_path=None, demo=False)

``result`` is the pipeline result dict, the same object the run loaded or
``run_one`` stored. It is not a display row. ``amount`` is the extracted
total, or the unpaid difference when the ledger shows one. ``demo=True``
still receives ``ledger_path``: that must be the ledger the pipeline run
wrote. Dropping it is refused. This module does not pay.

The shaped result is ``status`` "paid" or "refused", plus ``result``,
``reason_code``, ``reason``, and ``payment_status`` from the pipeline.
A missing pipeline function returns a refusal whose reason is "backend not available".
"""

import importlib
import sqlite3

from models import ExtractedInvoice

BACKEND_UNAVAILABLE = "backend not available"
_BACKEND_MODULE = "pipeline.approve_hold"
_BACKEND_NAME = "approve_and_pay"


def saved_result(entry: dict) -> dict:
    """The pipeline result on a run entry. Same object, not a rebuilt row.

    A later change can point this at the pipeline's saved output. Until that
    location is known, this is the state dict already stored on the entry.
    """
    if not isinstance(entry, dict):
        return entry
    state = entry.get("state")
    if isinstance(state, dict):
        return state
    return entry


def extracted_total(result: dict):
    """The invoice total field, or None. Not a formatted display amount."""
    if not isinstance(result, dict):
        return None
    invoice = result.get("invoice")
    if isinstance(invoice, dict) and "total" in invoice:
        return invoice["total"]
    return None


def payable_amount(result: dict, ledger_path=None):
    """What the review screen may pay.

    A higher revision of a confirmed paid invoice pays the unpaid difference
    from the ledger. Every other invoice pays the extracted total.
    """

    total = extracted_total(result)
    if ledger_path is None or not isinstance(result, dict):
        return total
    invoice = result.get("invoice")
    if not isinstance(invoice, dict):
        return total
    try:
        parsed = ExtractedInvoice.model_validate(invoice)
    except Exception:
        return total
    try:
        from ledger.payments import revision_difference

        owed = revision_difference(ledger_path, parsed, result.get("raw_text") or "")
    except sqlite3.Error:
        return total
    return total if owed is None else owed


def approve_and_pay(
    state: dict,
    *,
    reviewer: str,
    note: str,
    amount,
    ledger_path=None,
    log_path=None,
    demo: bool = False,
) -> dict:
    """Call the pipeline guard, or refuse when it is not installed yet."""
    backend = _find_backend()
    if backend is None:
        return _unavailable()
    raw = backend(
        state,
        reviewer=reviewer,
        note=note,
        amount=amount,
        ledger_path=ledger_path,
        log_path=log_path,
        demo=demo,
    )
    return shape_pay_result(raw)


def shape_pay_result(raw) -> dict:
    """Normalize a pipeline return into paid or refused, with the guard's reason."""
    body = raw if isinstance(raw, dict) else {}
    result = body.get("result")
    if result in {"paid", "held"}:
        status = "paid" if result == "paid" else "refused"
    elif body.get("status") == "paid" or body.get("paid") is True:
        status, result = "paid", "paid"
    else:
        status, result = "refused", "held"
    reason = body.get("reason")
    reason = reason.strip() if isinstance(reason, str) and reason.strip() else (
        "" if status == "paid" else "Payment refused."
    )
    payment_status = body.get("payment_status")
    if payment_status not in {"paid", "not_paid"}:
        payment_status = "paid" if status == "paid" else "not_paid"
    code = body.get("reason_code")
    if not isinstance(code, str) or not code.strip():
        code = None
    record = body.get("record") if isinstance(body.get("record"), dict) else {}
    shaped = {
        "status": status,
        "result": result,
        "reason_code": code,
        "reason": reason,
        "payment_status": payment_status,
        "payment": {"status": payment_status},
        "critique_concerns": _concerns(body.get("critique_concerns")),
    }
    if record.get("amount") not in (None, ""):
        shaped["amount"] = record["amount"]
    if record.get("currency"):
        shaped["currency"] = record["currency"]
    return shaped


def _unavailable() -> dict:
    return {
        "status": "refused",
        "result": "held",
        "reason_code": None,
        "reason": BACKEND_UNAVAILABLE,
        "payment_status": "not_paid",
        "payment": None,
        "critique_concerns": [],
    }


def _concerns(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def _find_backend():
    try:
        module = importlib.import_module(_BACKEND_MODULE)
    except ImportError:
        return None
    backend = getattr(module, _BACKEND_NAME, None)
    return backend if callable(backend) else None
