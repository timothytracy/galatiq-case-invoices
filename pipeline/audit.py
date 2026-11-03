"""Audit. The model returns PASS, SEND_BACK, or ESCALATE. Python will not loosen it."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from db import load_stock
from interfaces import LLM, Clock, Rng
from llm_client import AgentStepFailed, call_with_retries
from tool_loop import tool_request
from models import ApprovalDecision, AuditDraft, Finding, PaymentAuthorization, PipelineState
from pipeline.decisions import tighten
from pipeline.fact_guard import (
    AUDIT_CLAIMS,
    VERIFIED_FACTS,
    apply_concern_guard,
    partition_concerns,
    verified_payload,
)
from pipeline.stage_log import agent_stop, stage_log, stop_with_finding
from pipeline.tools import audit_registry
from decision.invoice_fields import show_money
from decision.outcome import (
    ESCALATABLE_WARNING_CODES,
    HOLD_CODES,
    REJECT_CODES,
    SEND_BACK_CAP_MESSAGE,
    WARNING_CODES,
    WARNING_HOLD_PREFIX,
    finding_codes,
)
from decision.policy import AUDIT_QUOTE_MIN_CHARS, MAX_SEND_BACKS
from decision.findings import (
    above_reference_items,
    assess,
    normalize_quote,
    warning_trigger_lines,
)
from decision.wording import explain, resolve_as_of, today_line

TOOLS_LINE = (
    "The tools are read-only. Their results do not change during this step. "
    "Call each lookup at most once."
)
WARNING_HOLD_RULE = (
    "Warnings are recorded on many ordinary invoices. Most have an innocent reading: "
    "a rush or volume price, a vendor that prefers wire. A warning being present is not "
    "a reason to escalate. Only "
    + " and ".join(ESCALATABLE_WARNING_CODES)
    + " may be escalated on their own, and only when they appear in findings. "
    "PRICE_VARIANCE may be escalated only when the price is above the reference price. "
    "A lower price is never a reason to hold. Other warning codes never hold on their own. "
    "To escalate on one of them, fill warning_hold. The quote must not be the line that "
    "raised the warning, such as the wire-transfer terms or the price line. It must be "
    "other text on the invoice that makes the warning worse. First write innocent_reading: "
    "the most likely ordinary explanation, in one sentence. Escalate only if text printed "
    "on this invoice makes that reading unlikely. quote is that text, copied exactly from "
    f"raw_text, at least {AUDIT_QUOTE_MIN_CHARS} characters. why is one sentence. Put the "
    "code in cited_codes. Code checks the code and the quote. An invalid warning_hold is "
    "dropped, and the invoice is decided as if you passed. If the innocent reading stands, "
    "PASS and say so in notes. You can never approve, reject, or clear a finding."
)
LEDGER_LINE = (
    "check_payment_ledger reports prior payments for this vendor and invoice "
    "number. A prior payment means you must not pass."
)


@dataclass(frozen=True)
class AuditResult:
    """The guarded audit: never looser than the rules or the model."""

    verdict: str
    outcome: str | None
    conclusion: str
    feedback: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    warning_hold: bool = False


def audit(
    state: PipelineState,
    llm: LLM,
    db_path: str | Path,
    clock: Clock,
    rng: Rng,
    max_attempts: int,
    as_of: date | None = None,
    ledger_path: str | Path | None = None,
) -> dict:
    as_of = resolve_as_of(as_of)
    invoice = state.invoice
    decision = state.decision
    if invoice is None or decision is None:
        reason = "Audit had no decision."
        return stop_with_finding(
            state,
            "audit",
            reason,
            "SYSTEM_ERROR",
            outcome="hold",
            severity="warning",
            agent_error=state.agent_error,
        )

    stock = dict(load_stock(db_path))
    facts = verified_payload(invoice, stock, as_of)
    step_attempts: list[dict] = []
    step = tool_request(
        llm,
        audit_registry(invoice, stock, state.raw_text or "", ledger_path=ledger_path),
        agent="audit",
        final_turn_on_cap=True,
    )
    try:
        draft = call_with_retries(
            step,
            _audit_messages(state, invoice, decision, as_of, facts),
            AuditDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return agent_stop(state, "audit", step_attempts, exc, note=_cap_note(step))

    fresh = assess(invoice, state.raw_text, stock)
    # Stock and totals are not checked here: a note may be about the raw text.
    kept_feedback, dropped = partition_concerns(draft.feedback, invoice, stock, as_of, "audit")
    verdict, feedback = apply_concern_guard(
        draft.verdict, kept_feedback, dropped, list(draft.cited_codes)
    )
    hold_message, hold_dropped = _warning_hold(
        verdict, draft, invoice, state.raw_text or "", stock, fresh
    )
    ignored = None
    if hold_message is None:
        ignored = _ignored_audit_note(
            verdict, list(draft.cited_codes), feedback, draft.notes, hold_dropped
        )
    if ignored is not None:
        verdict = "PASS"
        feedback = []
    # A tool hit can only add a hold. A miss adds nothing.
    ledger_holds = _ledger_holds(step)
    guarded = _guard_audit(
        state, decision, list(fresh.findings) + ledger_holds, verdict, feedback, hold_message
    )
    new_decision = decision
    if guarded.verdict != "SEND_BACK" and guarded.outcome not in (None, decision.decision):
        new_decision = tighten(decision, guarded.outcome, guarded.conclusion, guarded.evidence)

    findings, note_text = _merge_findings(state, fresh.findings, ledger_holds, ignored, guarded)
    if guarded.verdict == "SEND_BACK":
        kept = []
    else:
        kept = findings
    authorization = None
    if guarded.verdict == "PASS" and guarded.outcome == "approve":
        authorization = issue_authorization(state)

    result = {
        "model_verdict": draft.verdict,
        "verdict": guarded.verdict,
        "outcome": guarded.outcome,
        "feedback": guarded.feedback,
        "notes": draft.notes,
        "cited_codes": [str(code).strip().upper() for code in draft.cited_codes if str(code).strip()],
        "audit_note": note_text,
        "dropped_concerns": dropped,
        "warning_hold": draft.warning_hold.model_dump() if draft.warning_hold else None,
        "warning_hold_kept": guarded.warning_hold,
        "warning_hold_dropped": hold_dropped,
        "attempts": step_attempts,
        "decision": new_decision.model_dump(mode="json"),
        "codes": finding_codes(findings),
    }
    note = _cap_note(step)
    if note:
        result.update(note)
    if step.log:
        result["tool_calls"] = list(step.log)
    logged = stage_log(state.filename, "audit", result)
    return _audit_update(state, guarded, new_decision, kept, authorization, step_attempts, logged)


def _merge_findings(
    state: PipelineState,
    fresh: list[Finding],
    ledger_holds: list[Finding],
    ignored: str | None,
    guarded: AuditResult,
) -> tuple[list[Finding], str | None]:
    """Findings after audit, and the audit note text when a note was not an escalation."""

    findings = list(state.findings) or list(fresh)
    for extra in ledger_holds:
        if not any(item.code == extra.code for item in findings):
            findings.append(extra)
    note_text = None
    if ignored is not None:
        note_text = f"Audit note, not an escalation: {ignored}"
        findings = findings + [
            Finding(code="AUDIT_NOTE", message=note_text, severity="info")
        ]
    extra = _audit_finding(guarded, findings)
    if extra is not None:
        findings = findings + [extra]
    return findings, note_text


def _audit_update(
    state: PipelineState,
    guarded: AuditResult,
    new_decision: ApprovalDecision,
    kept: list[Finding],
    authorization: PaymentAuthorization | None,
    step_attempts: list[dict],
    logged: dict,
) -> dict:
    """The graph update. SEND_BACK clears the decision for another round."""

    update = {
        "audit_verdict": guarded.verdict,
        "decision": new_decision,
        "findings": kept,
        "authorization": authorization,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [logged],
    }
    if guarded.verdict == "SEND_BACK":
        update["outcome"] = None
        update["reason_code"] = None
        update["action"] = None
        update["feedback"] = guarded.feedback
        update["send_backs"] = state.send_backs + 1
        update["decision"] = None
        update["critique_rounds"] = 0
        update["critique_concerns"] = []
        update["critique_stricter"] = False
    else:
        _outcome, code, _reason, action = explain(kept, state.invoice)
        if guarded.outcome == "approve":
            code, action = None, None
        update["outcome"] = guarded.outcome
        update["reason_code"] = code
        update["action"] = action
        update["decision"] = new_decision.model_copy(
            update={"decision": guarded.outcome, "reason_code": code, "action": action}
        )
    return update


def authorization_key(invoice_path: str, content: str) -> str:
    """One-time key: the invoice file plus a hash of its text."""

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"{invoice_path}:{digest}"


def issue_authorization(state: PipelineState) -> PaymentAuthorization | None:
    """One-time sign-off for this invoice's vendor, total, and currency.

    A PASS audit calls this. A reviewer release calls it only after the hold
    guards have passed. The payment step checks the three fields again.
    """

    invoice = state.invoice
    if invoice is None or not invoice.vendor or invoice.total is None:
        return None
    return PaymentAuthorization(
        vendor=invoice.vendor,
        amount=invoice.total,
        currency=invoice.currency,
        key=authorization_key(state.invoice_path, state.raw_text),
    )


def _audit_finding(guarded: AuditResult, findings: list[Finding]) -> Finding | None:
    """A hold the audit itself added, when the rules did not already record one."""

    if guarded.verdict != "ESCALATE" or guarded.outcome != "hold":
        return None
    if any(finding.code in HOLD_CODES for finding in findings):
        return None
    authored = {
        SEND_BACK_CAP_MESSAGE,
        "Critique concerns were not addressed.",
    }
    message = guarded.conclusion
    if message not in authored and not guarded.warning_hold:
        message = "Audit escalated the invoice to a hold."
    return Finding(
        code="AUDIT_ESCALATED",
        message=message,
        severity="warning",
        send_back_cap=message == SEND_BACK_CAP_MESSAGE,
    )


def _audit_messages(state, invoice, decision, as_of, facts) -> list[dict]:
    findings = []
    if state.validation is not None:
        findings = [finding.model_dump(mode="json") for finding in state.validation.findings]
    payload = {
        "extracted": invoice.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "findings": findings,
        "critique_concerns": list(state.critique_concerns),
        "raw_text": state.raw_text,
        "verified_by_code": facts,
    }
    return [
        {
            "role": "system",
            "content": (
                "TASK audit\n"
                f"{today_line(as_of)}\n"
                "Compare the extracted fields with the raw text. Check the decision against the "
                "findings. Re-check the line math and any fraud signals. Check that critique "
                "concerns were addressed. Return JSON with verdict (PASS, SEND_BACK, or ESCALATE), "
                "feedback (list of strings or objects, required for SEND_BACK), "
                "notes (string), cited_codes (list of finding codes this verdict relies on), "
                "and warning_hold (object with code, innocent_reading, quote, and why, or null). "
                f"{VERIFIED_FACTS} {AUDIT_CLAIMS} "
                "You can only make the outcome stricter. Never turn a hard failure or a non-USD "
                "invoice into a payment.\n"
                "Warning codes: " + ", ".join(WARNING_CODES) + ". "
                f"{WARNING_HOLD_RULE} "
                "Put every code you rely on in cited_codes. Leave cited_codes empty when the "
                "concern is not one of those codes. "
                "A printed date on or before today is not in the future. "
                + f"{TOOLS_LINE} {LEDGER_LINE} Return JSON only."
            ),
        },
        {"role": "user", "content": json.dumps(payload)},
    ]


def _cap_note(step) -> dict | None:
    """Cap fields for the audit stage. Absent when this step did not hit the cap."""

    if not step.cap_hit:
        return None
    note = {"tool_cap_hit": step.cap_hit}
    if step.forced_final_turn:
        note["forced_final_turn"] = True
    return note


def concerns_addressed(concerns: list[str], decision: ApprovalDecision | None) -> bool:
    if not concerns:
        return True
    if decision is None:
        return False
    blob = " ".join(
        [
            decision.reasoning.conclusion,
            " ".join(decision.reasoning.risks_considered),
            " ".join(decision.reasoning.evidence),
            " ".join(item.conclusion for item in decision.revisions),
        ]
    ).lower()
    return all(concern.lower() in blob for concern in concerns if concern)


def amount_in_text(amount: Decimal, raw: str) -> bool:
    quantized = show_money(amount)
    if quantized in raw:
        return True
    with_commas = f"{Decimal(quantized):,.2f}"
    if with_commas in raw:
        return True
    whole = quantized.split(".")[0]
    if Decimal(quantized) == Decimal(whole):
        return _whole_number_in_text(whole, raw)
    return False


def _whole_number_in_text(whole: str, raw: str) -> bool:
    """A whole-dollar total can be printed without the trailing .00."""

    return re.search(rf"(?<![0-9]){re.escape(whole)}(?![0-9])", raw) is not None


def warning_only_citation(cited_codes: list[str]) -> bool:
    """True only when every cited code is a warning. Empty means the concern stands."""

    codes = [str(code).strip().upper() for code in cited_codes if str(code).strip()]
    if not codes:
        return False
    return all(code in WARNING_CODES for code in codes)


def _ignored_audit_note(
    verdict: str,
    cited_codes: list[str],
    feedback: list[str],
    notes: str,
    dropped_reason: str | None = None,
) -> str | None:
    """Drop a SEND_BACK or ESCALATE only when it cites warnings and nothing else.

    dropped_reason is why a warning hold failed its checks, when the audit made one.
    """

    if verdict not in {"SEND_BACK", "ESCALATE"}:
        return None
    if not warning_only_citation(cited_codes):
        return None
    codes = ", ".join(str(code).strip().upper() for code in cited_codes if str(code).strip())
    text = " ".join(
        part for part in [dropped_reason or "", *(feedback or []), notes or ""] if part
    ).strip()
    return f"{codes}. {text}".strip() if text else codes


def _warning_hold(verdict, draft, invoice, raw_text, stock, fresh) -> tuple[str | None, str | None]:
    """The hold message when a warning-only ESCALATE proves its case, else why it did not.

    Both are None when this is not a warning-only ESCALATE. Every check is code:
    the code is escalatable, cited, and on this invoice; a price is above its
    reference; there is an innocent reading; the quote is long enough, is on the
    page, and is more than the line that raised the warning.
    """

    cited = [str(code).strip().upper() for code in draft.cited_codes if str(code).strip()]
    if verdict != "ESCALATE" or not warning_only_citation(cited):
        return None, None
    case = draft.warning_hold
    if case is None:
        return None, "No warning_hold was given."
    code = case.code.strip().upper()
    if code not in ESCALATABLE_WARNING_CODES:
        return None, f"{code or 'No code'} is not a warning the audit may hold on."
    if code not in cited:
        return None, f"{code} is not in cited_codes."
    on_invoice = [finding for finding in fresh.findings if finding.code == code]
    if not on_invoice:
        return None, f"{code} is not a finding on this invoice."
    if code == "PRICE_VARIANCE":
        above = above_reference_items(invoice, stock, fresh.currency)
        if not any(finding.item in above for finding in on_invoice):
            return None, "No price on this invoice is above its reference."
    if not case.innocent_reading.strip():
        return None, "No innocent reading was given."
    quote = normalize_quote(case.quote)
    if len(quote) < AUDIT_QUOTE_MIN_CHARS:
        return None, f"The quote is shorter than {AUDIT_QUOTE_MIN_CHARS} characters."
    if quote not in normalize_quote(raw_text):
        return None, "The quote is not in the invoice text."
    triggers = warning_trigger_lines(code, invoice, raw_text, stock, fresh.currency)
    if any(quote in line for line in triggers):
        return None, "The quote is only the text that raised the warning."
    shown = " ".join(case.quote.split())
    return f'{WARNING_HOLD_PREFIX}{code}: "{shown}". {case.why.strip()}'.strip(), None


def _ledger_holds(step) -> list[Finding]:
    """Hold findings from a ledger-tool hit. One code, one finding.

    A miss adds nothing, so the result cannot loosen the decision. Validation
    already holds a duplicate or a split before audit in this process.
    """

    found: list[Finding] = []
    seen: set[str] = set()
    for finding in _findings_from_ledger_tool(step):
        if finding.code in seen:
            continue
        seen.add(finding.code)
        found.append(finding)
    return found


def _findings_from_ledger_tool(step) -> list[Finding]:
    """Turn one ledger-tool hit into hold findings. Repeats are the same hit."""

    result = None
    for row in step.log:
        if row.get("name") != "check_payment_ledger" or row.get("repeat"):
            continue
        body = row.get("result")
        if isinstance(body, dict):
            result = body
    if not result:
        return []
    found = []
    if result.get("already_paid") is True:
        found.append(
            Finding(
                code="DUPLICATE_INVOICE",
                message=(
                    "This vendor and invoice number were already paid "
                    f"on {result.get('match_date')} for {result.get('match_total')}."
                ),
                severity="warning",
                item="invoice_number",
            )
        )
    if result.get("over_split_threshold") is True:
        found.append(
            Finding(
                code="SPLIT_INVOICE",
                message=(
                    f"This {result.get('this_total')} plus "
                    f"{result.get('same_vendor_paid_in_window')} already paid to this vendor "
                    f"within {result.get('split_window_days')} days is {result.get('combined')}, "
                    f"over the {result.get('scrutiny_threshold')} threshold."
                ),
                severity="warning",
                item="total",
            )
        )
    return found


def _guard_audit(
    state, decision, findings, model_verdict, feedback, hold_message=None
) -> AuditResult:
    """Never looser than the rules or the model. PASS on a reject means do not send it back.

    hold_message is a warning hold that passed its checks. It applies only to an approve
    the rules and the critique left alone.
    """

    hard = [finding for finding in findings if finding.severity == "blocking" and finding.code in REJECT_CODES]
    holds = [finding for finding in findings if finding.code in HOLD_CODES]
    text_notes = []
    if state.invoice is not None and state.invoice.total is not None:
        if not amount_in_text(state.invoice.total, state.raw_text or ""):
            text_notes.append(
                f"total {show_money(state.invoice.total)} was not found in the raw text"
            )
    conclusion = decision.reasoning.conclusion
    if hard or decision.decision == "reject":
        messages = [finding.message for finding in hard]
        return AuditResult("PASS", "reject", " ".join(messages) or conclusion, evidence=messages)
    if holds and decision.decision == "approve":
        messages = [finding.message for finding in holds]
        return AuditResult("ESCALATE", "hold", " ".join(messages), evidence=messages)
    if not concerns_addressed(state.critique_concerns, decision):
        return AuditResult(
            "ESCALATE",
            "hold",
            "Critique concerns were not addressed.",
            evidence=[f"Unaddressed critique concern: {item}" for item in state.critique_concerns],
        )

    verdict = model_verdict
    if text_notes and verdict == "PASS":
        verdict = "SEND_BACK"
        feedback = text_notes + feedback
    if verdict == "SEND_BACK":
        if state.send_backs >= MAX_SEND_BACKS:
            return AuditResult(
                "ESCALATE",
                "hold",
                SEND_BACK_CAP_MESSAGE,
                feedback or [],
                feedback or ["send-back cap reached"],
            )
        return AuditResult(
            "SEND_BACK",
            None,
            conclusion,
            feedback or ["re-check the extracted fields against the raw text"],
        )
    if hold_message and verdict == "ESCALATE" and decision.decision == "approve":
        return AuditResult("ESCALATE", "hold", hold_message, evidence=[hold_message], warning_hold=True)
    if verdict == "ESCALATE" or decision.decision == "hold":
        return AuditResult("ESCALATE", "hold", conclusion)
    if decision.decision == "approve":
        return AuditResult("PASS", "approve", conclusion)
    return AuditResult("PASS", decision.decision, conclusion)
