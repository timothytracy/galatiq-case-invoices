"""Stage functions. The graph in graph.py only wires these together.

Hard rules live in rules.py. Extraction, approval wording, critique, and audit
are model calls. A failed model call becomes manual_review and does not pay.
"""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from db import load_stock
from llm_client import AgentStepFailed, call_with_retries
from models import (
    ApprovalDecision,
    ApprovalReasoning,
    AuditDraft,
    CritiqueDraft,
    ExtractedInvoice,
    ExtractionSchema,
    LineItem,
    PaymentRecord,
    PipelineState,
    ReasoningDraft,
    SourceInfo,
    StageRecord,
    ValidationReport,
)
from parsers import read_raw
from rules import (
    HARD_REJECT,
    MAX_SEND_BACKS,
    PASSED_RULES,
    RULE_TEXT,
    assess,
    evidence_lines,
    normalize_invoice_number,
    normalize_item_name,
    resolve_currency,
    show_money,
)


def stage_log(filename: str, stage: str, result: dict) -> dict:
    record = StageRecord(
        stage=stage,
        invoice_file=filename,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        result=result,
    )
    payload = record.model_dump(mode="json")
    print(json.dumps(payload), flush=True)
    return payload


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


def validate(state: PipelineState, db_path) -> dict:
    invoice = state.invoice
    if invoice is None:
        return _stopped(state, "reject", "No invoice was extracted.", "validation", [], agent_error=state.agent_error)
    stock = dict(load_stock(db_path))
    assessment = assess(invoice, state.raw_text, stock)
    if assessment.currency != invoice.currency:
        invoice = invoice.model_copy(update={"currency": assessment.currency})
    report = ValidationReport(
        passed=assessment.outcome == "approve",
        findings=assessment.findings,
    )
    outcome = None if assessment.outcome == "approve" else assessment.outcome
    result = {
        "passed": report.passed,
        "outcome": assessment.outcome,
        "reason": assessment.reason,
        "currency": assessment.currency,
        "findings": [finding.model_dump(mode="json") for finding in report.findings],
    }
    return {
        "invoice": invoice,
        "validation": report,
        "outcome": outcome,
        "logs": state.logs + [stage_log(state.filename, "validation", result)],
    }


def approve(state: PipelineState, llm, db_path, clock, rng, max_attempts: int) -> dict:
    if state.agent_error:
        return {"outcome": "manual_review"}
    invoice = state.invoice
    report = state.validation
    if invoice is None or report is None:
        return _stopped(state, "reject", "Approval had no invoice.", "approval", [], agent_error=None)

    stock = dict(load_stock(db_path))
    step_attempts: list[dict] = []
    evidence = evidence_lines(invoice, stock, report.findings)
    try:
        draft = call_with_retries(
            llm.request,
            _reason_messages(state, invoice, report, evidence),
            ReasoningDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(state, "manual_review", str(exc), "approval", step_attempts, agent_error=str(exc))

    decision_name = "manual_review" if state.critique_stricter else "approve"
    risks = list(draft.risks_considered)
    for concern in state.critique_concerns:
        line = f"Critique concern: {concern}"
        if line not in risks:
            risks.append(line)
    conclusion = draft.conclusion
    if state.critique_stricter:
        conclusion = conclusion + " Critique made the decision stricter: manual review, no payment."
    reasoning = ApprovalReasoning(
        rules_applied=list(PASSED_RULES),
        evidence=evidence,
        risks_considered=risks,
        conclusion=conclusion,
    )
    revisions = []
    if state.decision is not None:
        revisions = list(state.decision.revisions) + [state.decision.reasoning]
    decision = ApprovalDecision(
        decision=decision_name,
        reasoning=reasoning,
        revisions=revisions,
        amount=invoice.total,
        currency=invoice.currency,
    )
    result = {
        "decision": decision.model_dump(mode="json"),
        "critique_rounds": state.critique_rounds,
        "attempts": step_attempts,
    }
    return {
        "decision": decision,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "approval", result)],
    }


def critique(state: PipelineState, llm, clock, rng, max_attempts: int) -> dict:
    decision = state.decision
    if decision is None:
        return _stopped(state, "reject", "Critique had no decision.", "critique", [], agent_error=None)
    step_attempts: list[dict] = []
    try:
        draft = call_with_retries(
            llm.request,
            _critique_messages(decision),
            CritiqueDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(state, "manual_review", str(exc), "critique", step_attempts, agent_error=str(exc))

    stricter = bool(draft.stricter) and decision.decision == "approve"
    result = {
        "concerns": list(draft.concerns),
        "stricter": stricter,
        "round": state.critique_rounds + 1,
        "attempts": step_attempts,
    }
    return {
        "critique_rounds": state.critique_rounds + 1,
        "critique_concerns": list(draft.concerns),
        "critique_stricter": stricter,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "critique", result)],
    }


def audit(state: PipelineState, llm, db_path, clock, rng, max_attempts: int) -> dict:
    invoice = state.invoice
    decision = state.decision
    if invoice is None or decision is None:
        return _stopped(state, "manual_review", "Audit had no decision.", "audit", [], agent_error=state.agent_error)

    step_attempts: list[dict] = []
    try:
        draft = call_with_retries(
            llm.request,
            _audit_messages(state, invoice, decision),
            AuditDraft,
            max_attempts=max_attempts,
            clock=clock,
            rng=rng,
            attempt_log=step_attempts,
        )
    except AgentStepFailed as exc:
        return _stopped(state, "manual_review", str(exc), "audit", step_attempts, agent_error=str(exc))

    stock = dict(load_stock(db_path))
    fresh = assess(invoice, state.raw_text, stock)
    guarded = _guard_audit(state, decision, fresh.findings, draft.verdict, list(draft.feedback))
    new_decision = decision
    if guarded["verdict"] != "SEND_BACK" and guarded["outcome"] not in (None, decision.decision):
        new_decision = _tighten(decision, guarded["outcome"], guarded["conclusion"], guarded["evidence"])

    result = {
        "model_verdict": draft.verdict,
        "verdict": guarded["verdict"],
        "outcome": guarded["outcome"],
        "feedback": guarded["feedback"],
        "notes": draft.notes,
        "attempts": step_attempts,
        "decision": new_decision.model_dump(mode="json"),
    }
    update = {
        "audit_verdict": guarded["verdict"],
        "decision": new_decision,
        "attempts": state.attempts + step_attempts,
        "agent_error": None,
        "logs": state.logs + [stage_log(state.filename, "audit", result)],
    }
    if guarded["verdict"] == "SEND_BACK":
        update["outcome"] = None
        update["feedback"] = guarded["feedback"]
        update["send_backs"] = state.send_backs + 1
        update["decision"] = None
        update["critique_rounds"] = 0
        update["critique_concerns"] = []
        update["critique_stricter"] = False
    else:
        update["outcome"] = guarded["outcome"]
    return update


def payment(state: PipelineState) -> dict:
    invoice = state.invoice
    decision = state.decision
    blocked = _payment_blocked(state)
    if blocked or invoice is None:
        record = PaymentRecord(
            status="not_paid",
            vendor=None if invoice is None else invoice.vendor,
            amount=None if invoice is None else invoice.total,
            currency=None if invoice is None else invoice.currency,
            detail=blocked or "payment blocked",
        )
    else:
        response = mock_payment(invoice.vendor, invoice.total)
        record = PaymentRecord(
            status="paid",
            vendor=invoice.vendor,
            amount=invoice.total,
            currency=invoice.currency,
            detail="mock payment accepted",
            mock_response=response,
        )
    result = {"payment": record.model_dump(mode="json"), "outcome": state.outcome}
    return {
        "payment": record,
        "logs": state.logs + [stage_log(state.filename, "payment", result)],
    }


def reject_log(state: PipelineState) -> dict:
    outcome = "manual_review" if state.agent_error else (state.outcome or "reject")
    if state.agent_error:
        reason = state.agent_error
        decision = _failure_decision(state, reason)
    elif state.decision is not None:
        decision = state.decision
        if decision.decision != outcome:
            decision = _tighten(decision, outcome, decision.reasoning.conclusion, [])
        reason = decision.reasoning.conclusion
    else:
        reason, decision = _decision_from_validation(state, outcome)

    invoice = state.invoice
    record = PaymentRecord(
        status="not_paid",
        vendor=None if invoice is None else invoice.vendor,
        amount=None if invoice is None else invoice.total,
        currency=None if invoice is None else invoice.currency,
        detail=reason,
    )
    result = {
        "outcome": outcome,
        "reason": reason,
        "decision": decision.model_dump(mode="json"),
        "payment": record.model_dump(mode="json"),
    }
    return {
        "outcome": outcome,
        "decision": decision,
        "payment": record,
        "logs": state.logs + [stage_log(state.filename, "reject_log", result)],
    }


def mock_payment(vendor, amount):
    shown = f"{amount:.2f}" if isinstance(amount, Decimal) else amount
    print(f"Paid {shown} to {vendor}", flush=True)
    return {"status": "success"}


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
        return re_amount(whole, raw)
    return False


def re_amount(whole: str, raw: str) -> bool:
    return re.search(rf"(?<![0-9]){re.escape(whole)}(?![0-9])", raw) is not None


def _guard_audit(state, decision, findings, model_verdict, feedback) -> dict:
    """Apply the audit checks. The result is never looser than the rules or the model."""

    hard = [finding for finding in findings if finding.code in HARD_REJECT]
    non_usd = [finding for finding in findings if finding.code == "non_usd"]
    fraud = [finding for finding in findings if finding.code == "fraud_signal"]
    text_notes = []
    if state.invoice is not None and state.invoice.total is not None:
        if not amount_in_text(state.invoice.total, state.raw_text or ""):
            text_notes.append(
                f"total {show_money(state.invoice.total)} was not found in the raw text"
            )

    if hard or decision.decision == "reject":
        # The model cannot loosen a hard failure. PASS means "do not send this
        # back"; the outcome stays reject, so the router logs it and does not pay.
        return {
            "verdict": "PASS",
            "outcome": "reject",
            "feedback": [],
            "conclusion": " ".join(finding.message for finding in hard) or decision.reasoning.conclusion,
            "evidence": [finding.message for finding in hard],
        }
    if non_usd:
        return {
            "verdict": "ESCALATE",
            "outcome": "manual_review",
            "feedback": [],
            "conclusion": non_usd[0].message,
            "evidence": [finding.message for finding in non_usd],
        }
    if fraud and decision.decision == "approve":
        return {
            "verdict": "ESCALATE",
            "outcome": "manual_review",
            "feedback": [],
            "conclusion": " ".join(finding.message for finding in fraud),
            "evidence": [finding.message for finding in fraud],
        }
    if not concerns_addressed(state.critique_concerns, decision):
        return {
            "verdict": "ESCALATE",
            "outcome": "manual_review",
            "feedback": [],
            "conclusion": "Critique concerns were not addressed.",
            "evidence": [f"Unaddressed critique concern: {item}" for item in state.critique_concerns],
        }

    verdict = model_verdict
    if text_notes and verdict == "PASS":
        verdict = "SEND_BACK"
        feedback = text_notes + feedback
    if verdict == "SEND_BACK":
        if state.send_backs >= MAX_SEND_BACKS:
            return {
                "verdict": "ESCALATE",
                "outcome": "manual_review",
                "feedback": feedback,
                "conclusion": "Audit send-back cap reached. Manual review, no payment.",
                "evidence": feedback or ["send-back cap reached"],
            }
        used = feedback or ["re-check the extracted fields against the raw text"]
        return {
            "verdict": "SEND_BACK",
            "outcome": None,
            "feedback": used,
            "conclusion": decision.reasoning.conclusion,
            "evidence": [],
        }
    if verdict == "ESCALATE" or decision.decision == "manual_review":
        outcome = "manual_review"
        return {
            "verdict": "ESCALATE",
            "outcome": outcome,
            "feedback": [],
            "conclusion": decision.reasoning.conclusion,
            "evidence": [],
        }
    # PASS. An approve stays approve. Anything else stays as strict as it was.
    if decision.decision == "approve":
        return {
            "verdict": "PASS",
            "outcome": "approve",
            "feedback": [],
            "conclusion": decision.reasoning.conclusion,
            "evidence": [],
        }
    return {
        "verdict": "PASS",
        "outcome": decision.decision,
        "feedback": [],
        "conclusion": decision.reasoning.conclusion,
        "evidence": [],
    }


def _payment_blocked(state: PipelineState) -> str:
    if state.agent_error:
        return state.agent_error
    if state.outcome != "approve":
        return state.outcome or "not approved"
    if state.decision is None or state.decision.decision != "approve":
        return "decision is not approve"
    invoice = state.invoice
    if invoice is None:
        return "no invoice"
    if invoice.currency != "USD":
        return f"non-USD currency ({invoice.currency}), manual review required"
    if state.validation is not None:
        for finding in state.validation.findings:
            if finding.code in HARD_REJECT or finding.code in {"non_usd", "fraud_signal"}:
                return finding.message
    return ""


def _tighten(decision: ApprovalDecision, outcome: str, conclusion: str, extra: list[str]) -> ApprovalDecision:
    reasoning = ApprovalReasoning(
        rules_applied=list(decision.reasoning.rules_applied) + ["Audit and critique can only make the decision stricter."],
        evidence=list(decision.reasoning.evidence) + (extra or ["The audit kept the stricter outcome."]),
        risks_considered=list(decision.reasoning.risks_considered),
        conclusion=conclusion,
    )
    return ApprovalDecision(
        decision=outcome,
        reasoning=reasoning,
        revisions=list(decision.revisions) + [decision.reasoning],
        amount=decision.amount,
        currency=decision.currency,
    )


def _failure_decision(state: PipelineState, reason: str) -> ApprovalDecision:
    invoice = state.invoice
    return ApprovalDecision(
        decision="manual_review",
        reasoning=ApprovalReasoning(
            rules_applied=["A failed agent step does not continue to payment."],
            evidence=[reason],
            risks_considered=[reason],
            conclusion=reason,
        ),
        amount=None if invoice is None else invoice.total,
        currency=None if invoice is None else invoice.currency,
    )


def _decision_from_validation(state: PipelineState, outcome: str) -> tuple[str, ApprovalDecision]:
    findings = [] if state.validation is None else list(state.validation.findings)
    invoice = state.invoice
    fired = []
    for finding in findings:
        text = RULE_TEXT.get(finding.code)
        if text and text not in fired:
            fired.append(text)
    if not fired:
        fired = ["The invoice did not pass the deterministic rules."]
    evidence = [finding.message for finding in findings] or ["No finding text was recorded."]
    if invoice is not None and invoice.total is not None:
        evidence = [f"total {show_money(invoice.total)} {invoice.currency}"] + evidence
    reason = findings[0].message if findings else "The invoice was not approved."
    if outcome == "manual_review" and findings:
        reason = " ".join(finding.message for finding in findings)
    elif findings:
        reason = " ".join(finding.message for finding in findings)
    decision = ApprovalDecision(
        decision=outcome,
        reasoning=ApprovalReasoning(
            rules_applied=fired,
            evidence=evidence,
            risks_considered=[finding.message for finding in findings] or ["No extra risk was recorded."],
            conclusion=reason,
        ),
        amount=None if invoice is None else invoice.total,
        currency=None if invoice is None else invoice.currency,
    )
    return reason, decision


def _stopped(state, outcome, reason, stage, step_attempts, agent_error) -> dict:
    result = {"outcome": outcome, "reason": reason, "attempts": step_attempts}
    return {
        "outcome": outcome,
        "agent_error": agent_error,
        "attempts": state.attempts + step_attempts,
        "logs": state.logs + [stage_log(state.filename, stage, result)],
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


def _reason_messages(state, invoice, report, evidence) -> list[dict]:
    concerns = ""
    if state.critique_concerns:
        concerns = "Critique concerns to address:\n" + "\n".join(
            f"- {item}" for item in state.critique_concerns
        )
    prior = ""
    if state.decision is not None:
        prior = "Previous conclusion: " + state.decision.reasoning.conclusion
    payload = {
        "vendor": invoice.vendor,
        "total": None if invoice.total is None else str(invoice.total),
        "currency": invoice.currency,
        "evidence": evidence,
        "findings": [finding.message for finding in report.findings],
    }
    return [
        {
            "role": "system",
            "content": (
                "TASK reason\n"
                "Write the approval reasoning as JSON with keys risks_considered (list of strings) "
                "and conclusion (string). The decision is already fixed by the rules. "
                "Cite only the evidence you are given. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                part
                for part in (json.dumps(payload), prior, concerns)
                if part
            ),
        },
    ]


def _critique_messages(decision: ApprovalDecision) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "TASK critique\n"
                "Review this approval. Return JSON with keys concerns (list of strings) and "
                "stricter (boolean). You may only make the decision stricter. "
                "You cannot approve a failure and you cannot clear a non-USD hold. Return JSON only."
            ),
        },
        {"role": "user", "content": decision.model_dump_json()},
    ]


def _audit_messages(state, invoice, decision) -> list[dict]:
    findings = []
    if state.validation is not None:
        findings = [finding.model_dump(mode="json") for finding in state.validation.findings]
    payload = {
        "extracted": invoice.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "findings": findings,
        "critique_concerns": list(state.critique_concerns),
        "raw_text": state.raw_text,
    }
    return [
        {
            "role": "system",
            "content": (
                "TASK audit\n"
                "Compare the extracted fields with the raw text. Check the decision against the "
                "findings. Re-check the line math and any fraud signals. Check that critique "
                "concerns were addressed. Return JSON with verdict (PASS, SEND_BACK, or ESCALATE), "
                "feedback (list of field notes, required for SEND_BACK), and notes (string). "
                "You can only make the outcome stricter. Never turn a hard failure or a non-USD "
                "invoice into a payment. Return JSON only."
            ),
        },
        {"role": "user", "content": json.dumps(payload)},
    ]


def _blank(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
