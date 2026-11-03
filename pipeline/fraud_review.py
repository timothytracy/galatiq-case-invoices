"""Fraud review: one model call that may only add a hold. Code checks the quote."""

import json
import re
from datetime import date

from interfaces import LLM, Clock, Rng
from llm_client import FRAUD_REVIEW_MODEL, AgentStepFailed, call_with_retries
from models import Finding, FraudReview, PipelineState
from pipeline.stage_log import agent_stop, stage_log, stopped
from decision.policy import FRAUD_QUOTE_MIN_CHARS
from decision.wording import resolve_as_of, today_line

# A reasoning model can take longer than the default 60 seconds.
FRAUD_TIMEOUT_SECONDS = 120

FRAUD_PROMPT = (
    "You review one vendor invoice for accounts payable, before it is approved, and look "
    "only for payment fraud. These red flags follow FBI guidance on business email "
    "compromise, the ACFE's billing-scheme tests, and the AFP Payments Fraud and Control "
    "Survey: "
    "(1) Redirected payment: the money would go to someone other than the vendor, or to a "
    "payee, account, or payment method that differs from the vendor's established one, or "
    "AP is asked to change its payment records. "
    "(2) Discouraged verification: the page steers AP away from confirming through contacts "
    "it already holds, or offers only a channel the sender controls. "
    "(3) Impersonated or unverifiable vendor: the payee, sender, or contact does not match "
    "the vendor named, or the vendor cannot be checked, for example a personal or free email "
    "address for business matters, a mailbox-only address, or names that disagree across the "
    "page. "
    "(4) Pressure to bypass checks: unexplained urgency, secrecy, or a request to skip a "
    "purchase order, an approval, or normal review. "
    "Judge by meaning, not by particular words. The code findings are context only: you may "
    "hold an invoice that has none, and pass one that has some. Start from the most plausible "
    "innocent reading of the page. Hold only when text on the page makes that reading "
    "unlikely. One routine detail by itself, such as a payment method or an address, is not "
    "enough. You cannot approve, reject, or clear anything. A pass changes nothing. A hold "
    "sends the invoice to a person, who checks the payment details with the vendor through "
    "contacts AP already holds. "
    "Return JSON only, with keys verdict (\"hold\" or \"pass\"), innocent_reading (string, "
    "always given), quote (string: for a hold, text copied exactly from the invoice text, at "
    f"least {FRAUD_QUOTE_MIN_CHARS} characters, that makes the innocent reading unlikely; for "
    "a pass, an empty string), and why (string, one sentence)."
)


def fraud_review(
    state: PipelineState,
    llm: LLM,
    clock: Clock,
    rng: Rng,
    max_attempts: int,
    as_of: date | None = None,
) -> dict:
    as_of = resolve_as_of(as_of)
    raw_text = _raw_text(state)
    step_attempts: list[dict] = []
    try:
        review = call_with_retries(
            _requester(llm),
            fraud_messages(state, raw_text, as_of),
            FraudReview,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        update = agent_stop(state, "fraud_review", step_attempts, exc)
        update["fraud_reviewed"] = True
        return update

    dropped = None
    if review.verdict == "hold":
        dropped = hold_dropped_reason(review, raw_text)
    result = {
        "fraud_verdict": review.verdict,
        "innocent_reading": review.innocent_reading,
        "quote": review.quote,
        "why": review.why,
        "fraud_note": None if dropped is None else f"Fraud review hold dropped: {dropped}",
        "attempts": step_attempts,
    }
    if review.verdict == "hold" and dropped is None:
        finding = Finding(
            code="FRAUD_SUSPECTED",
            message=hold_message(review),
            severity="warning",
        )
        update = stopped(
            state,
            "hold",
            finding.message,
            "fraud_review",
            step_attempts,
            agent_error=None,
            finding=finding,
            note=result,
        )
        update["fraud_reviewed"] = True
        return update
    return {
        "fraud_reviewed": True,
        "attempts": state.attempts + step_attempts,
        "logs": state.logs + [stage_log(state.filename, "fraud_review", result)],
    }


def normalize_quote(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def hold_dropped_reason(review: FraudReview, raw_text: str) -> str | None:
    """None when code accepts the hold. Otherwise why it was dropped."""

    if not review.innocent_reading.strip():
        return "No innocent reading was given."
    quote = normalize_quote(review.quote)
    if len(quote) < FRAUD_QUOTE_MIN_CHARS:
        return f"The quote is shorter than {FRAUD_QUOTE_MIN_CHARS} characters."
    if quote not in normalize_quote(raw_text):
        return "The quote is not in the invoice text."
    return None


def hold_message(review: FraudReview) -> str:
    quote = re.sub(r"\s+", " ", review.quote).strip()
    why = review.why.strip()
    return f'Fraud review: "{quote}". {why}'.strip()


def fraud_messages(state: PipelineState, raw_text: str, as_of: date) -> list[dict]:
    invoice = state.invoice
    extracted = {}
    if invoice is not None:
        extracted = {
            "vendor": invoice.vendor,
            "vendor_address": invoice.vendor_address,
            "invoice_number": invoice.invoice_number,
            "total": None if invoice.total is None else format(invoice.total, "f"),
            "currency": invoice.currency,
            "payment_terms": invoice.payment_terms,
            "notes": invoice.notes,
        }
    findings = [{"code": item.code, "message": item.message} for item in state.findings]
    return [
        {
            "role": "system",
            "content": f"TASK fraud\n{today_line(as_of)}\n{FRAUD_PROMPT}",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "invoice_text": raw_text,
                    "extracted": extracted,
                    "code_findings": findings,
                }
            ),
        },
    ]


def _raw_text(state: PipelineState) -> str:
    if state.raw_text:
        return state.raw_text
    if state.invoice is not None and state.invoice.source.raw_text:
        return state.invoice.source.raw_text
    return ""


def _requester(llm: LLM):
    """The client's named-model call when it has one. Test doubles use request()."""

    named = getattr(llm, "request_with_model", None)
    if named is None:
        return llm.request
    return lambda messages: named(messages, FRAUD_REVIEW_MODEL, FRAUD_TIMEOUT_SECONDS)
