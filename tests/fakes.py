"""Model doubles and builders shared by the pipeline tests. No network."""

import json
import socket
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from models import (
    ApprovalDecision,
    ApprovalReasoning,
    ExtractedInvoice,
    LineItem,
    PipelineState,
    SourceInfo,
    ValidationReport,
)


class ZeroRandom:
    def uniform(self, _start, _end):
        return 0.0


class FakeClock:
    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


FRAUD_PASS = json.dumps(
    {
        "verdict": "pass",
        "innocent_reading": "An ordinary bill from the vendor.",
        "quote": "",
        "why": "Nothing on the page makes the innocent reading unlikely.",
    }
)


class FakeLLM:
    """handler answers every task. fraud, when given, answers TASK fraud instead.

    With no fraud handler, TASK fraud gets FRAUD_PASS. Fraud calls go to
    fraud_calls, not calls, so call counts for the other stages stay the same.
    """

    def __init__(self, handler, fraud=None):
        self.handler = handler
        self.fraud = fraud
        self.calls = []
        self.fraud_calls = []

    def request(self, messages, timeout=60):
        if task_of(messages) == "fraud":
            self.fraud_calls.append(messages)
            reply = FRAUD_PASS if self.fraud is None else self.fraud
            result = reply(messages) if callable(reply) else reply
            if isinstance(result, Exception):
                raise result
            return result
        self.calls.append(messages)
        result = self.handler(messages)
        if isinstance(result, Exception):
            raise result
        return result

    def request_tools(self, messages, tools, timeout=60):
        """The scripted text, and no tool call."""

        return {"content": self.request(messages, timeout), "tool_calls": []}


def task_of(messages) -> str:
    for message in messages:
        first = ((message.get("content") or "").splitlines() or [""])[0]
        if first.startswith("TASK "):
            return first.split()[1]
    return ""


class ScriptedToolModel:
    """A queue of replies. Each step is {"content", "tool_calls"} or an exception.

    request() is the plain path. request_tools() is the tools path.
    Existing tests keep using FakeLLM.
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.tool_requests = []
        self.fraud_requests = []

    def request(self, messages, timeout=60):
        if task_of(messages) == "fraud":
            self.fraud_requests.append(messages)
            return FRAUD_PASS
        self.requests.append({"messages": messages, "timeout": timeout})
        step = self._take()
        if isinstance(step, Exception):
            raise step
        if step.get("tool_calls"):
            raise AssertionError("plain request received tool_calls")
        return step.get("content") or ""

    def request_tools(self, messages, tools, timeout=60):
        self.tool_requests.append({"messages": messages, "tools": tools, "timeout": timeout})
        step = self._take()
        if isinstance(step, Exception):
            raise step
        return step

    def _take(self):
        if not self.steps:
            raise AssertionError("scripted model ran out of steps")
        return self.steps.pop(0)


def block_network(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("network is disabled in tests")

    monkeypatch.setattr(socket, "create_connection", explode)


def extraction(**overrides) -> str:
    payload = {
        "vendor": "Widgets Inc.",
        "vendor_address": None,
        "invoice_number": "INV-9",
        "invoice_date": "2026-01-01",
        "due_date": "2026-02-01",
        "currency": "USD",
        "line_items": [
            {
                "item": "WidgetA",
                "description": "WidgetA",
                "quantity": 2,
                "unit_price": 250,
                "amount": 500,
                "note": None,
            }
        ],
        "subtotal": 500,
        "tax_rate": 0,
        "tax_amount": 0,
        "shipping": 0,
        "total": 500,
        "payment_terms": "Net 30",
        "notes": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def write_invoice(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def scripted(extract_text, *, audits=None, critique=None, before_extract=None, reasons=None):
    """before_extract(call_number) may raise LLMError or return a stand-in string.

    reasons, when set, is the sequence of TASK reason replies. The last one repeats.
    """

    audit_replies = list(audits or ["PASS"])
    reason_replies = None if reasons is None else list(reasons)
    counts = {"extract": 0, "audit": 0, "critique": 0, "reason": 0}

    def handler(messages):
        name = task_of(messages)
        if name == "extract":
            counts["extract"] += 1
            if before_extract is not None:
                replacement = before_extract(counts["extract"], messages)
                if replacement is not None:
                    return replacement
            return extract_text
        if name == "reason":
            counts["reason"] += 1
            if reason_replies is not None:
                index = min(counts["reason"] - 1, len(reason_replies) - 1)
                return reason_replies[index]
            return json.dumps({"risks_considered": ["no extra risk"], "conclusion": "The checks passed."})
        if name == "critique":
            counts["critique"] += 1
            body = critique or {"concerns": ["amount is high"], "stricter": False}
            return json.dumps(body)
        if name == "audit":
            counts["audit"] += 1
            index = min(counts["audit"] - 1, len(audit_replies) - 1)
            verdict = audit_replies[index]
            feedback = ["due_date is wrong"] if verdict == "SEND_BACK" else []
            return json.dumps({"verdict": verdict, "feedback": feedback, "notes": verdict})
        if name == "fraud":
            return FRAUD_PASS
        raise AssertionError(f"unexpected task {name}")

    return handler, counts


ANSWER = '{"echoed": "phase-1"}'


class Echoed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    echoed: str


def tool_step(*calls, content=""):
    return {
        "content": content,
        "tool_calls": [
            {"id": call_id, "name": name, "arguments": arguments}
            for call_id, name, arguments in calls
        ],
    }


def text_step(content):
    return {"content": content, "tool_calls": []}


def payable_state() -> PipelineState:
    invoice = ExtractedInvoice(
        vendor="Widgets Inc.",
        currency="USD",
        invoice_date="2026-01-01",
        due_date="2026-02-01",
        total=Decimal("500"),
        subtotal=Decimal("500"),
        tax_amount=Decimal("0"),
        line_items=[
            LineItem(
                item="WidgetA",
                description="WidgetA",
                quantity=Decimal("2"),
                unit_price=Decimal("250"),
                amount=Decimal("500"),
            )
        ],
        source=SourceInfo(path="x", filename="x.txt", format="txt", raw_text="Total: 500.00"),
    )
    decision = ApprovalDecision(
        decision="approve",
        reasoning=ApprovalReasoning(
            rules_applied=["stock covered"],
            evidence=["total 500.00 USD"],
            risks_considered=["no extra risk"],
            conclusion="The checks passed.",
        ),
        amount=Decimal("500"),
        currency="USD",
    )
    return PipelineState(
        invoice_path="x",
        filename="x.txt",
        raw_text="WidgetA\nTotal: 500.00\n",
        invoice=invoice,
        validation=ValidationReport(passed=True, findings=[]),
        decision=decision,
    )


def audit_verdict(verdict: str) -> str:
    feedback = ["due_date is wrong"] if verdict == "SEND_BACK" else []
    return json.dumps(
        {"verdict": verdict, "feedback": feedback, "notes": verdict, "cited_codes": []}
    )
