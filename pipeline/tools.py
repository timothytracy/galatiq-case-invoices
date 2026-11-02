"""Read-only tools. A tool is a function plus the JSON schema the model sees.

A ToolInputError (unknown tool, bad arguments, unknown item or code) becomes
{"error": "..."} for the model. Any other exception leaves call() and the
loop turns it into a non-retryable LLMError. echo_note is for tests and
the probe. Audit gets audit_registry when XAI_TOOLS is on. Critique does not.
"""

from decimal import Decimal
from functools import partial

from decision.findings import collect_findings, math_reconciles
from decision.invoice_fields import canonical_item, show_money, show_qty, summed_quantities
from decision.outcome import HOLD_CODES, REJECT_CODES, WARNING_CODES
from decision.policy import POLICY
from decision.wording import ACTION, RULE_TEXT
from llm_client import parse_tool_arguments


class Tool:
    def __init__(self, name: str, description: str, parameters: dict, function) -> None:
        if parameters.get("type") != "object":
            raise ValueError(f"{name}: parameters.type must be object")
        self.name = name
        self.description = description
        self.parameters = parameters
        self.function = function

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolInputError(Exception):
    """The model can fix this: a bad argument, an unknown tool, or a missed lookup."""


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: str) -> dict:
        """Run one call. ToolInputError becomes {"error": "..."}.

        Any other exception propagates. That is an infrastructure failure.
        """

        try:
            return self._call(name, arguments)
        except ToolInputError as exc:
            return {"error": str(exc)}

    def _call(self, name: str, arguments: str) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolInputError(f"unknown tool: {name}")
        parsed, error, _raw = parse_tool_arguments(arguments)
        if error is not None or parsed is None:
            raise ToolInputError(error or "arguments must be a JSON object")
        schema_error = _schema_error(tool.parameters, parsed)
        if schema_error is not None:
            raise ToolInputError(schema_error)
        result = tool.function(parsed)
        if not isinstance(result, dict):
            raise TypeError("tool result must be an object")
        return result


def echo_note(arguments: dict) -> dict:
    """Return the note. It reads nothing and writes nothing."""

    note = arguments.get("note")
    if not isinstance(note, str) or note.strip() == "":
        raise ToolInputError("note must be a non-empty string")
    return {"note": note, "echo": note}


def example_registry() -> ToolRegistry:
    """echo_note only. Tests and the probe use this. Agents do not."""

    return ToolRegistry(
        [
            Tool(
                name="echo_note",
                description="Read-only. Echo a note. It does not read or change anything.",
                parameters={
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "description": "Text to echo"},
                    },
                    "required": ["note"],
                },
                function=echo_note,
            )
        ]
    )


_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _schema_error(parameters: dict, arguments: dict) -> str | None:
    required = parameters.get("required") or []
    properties = parameters.get("properties") or {}
    for name in required:
        if name not in arguments:
            return f"missing argument: {name}"
    for name, value in arguments.items():
        expected = (properties.get(name) or {}).get("type")
        kind = _JSON_TYPES.get(expected)
        if kind is None:
            continue
        # True is an int in Python. A JSON integer still rejects it.
        if isinstance(value, bool) and expected in {"integer", "number"}:
            return f"argument {name} must be a {expected}"
        if not isinstance(value, kind):
            return f"argument {name} must be a {expected}"
        allowed = (properties.get(name) or {}).get("enum")
        if isinstance(allowed, list) and value not in allowed:
            return f"argument {name} must be one of the allowed values"
    return None


_ITEM = {
    "type": "object",
    "properties": {
        "item": {"type": "string", "description": "Item name as printed or extracted"},
    },
    "required": ["item"],
}

# Not a business rule. The model does not look these up.
_PROCESS_CODES = {
    "CRITIQUE_HOLD",
    "AUDIT_ESCALATED",
    "SYSTEM_ERROR",
    "BAD_REASONING",
    "AUDIT_NOTE",
}

# Reject, then hold, then warning. Skip process codes and codes with no rule text.
_POLICY_CODES = [
    code
    for code in (*REJECT_CODES, *HOLD_CODES, *WARNING_CODES)
    if code not in _PROCESS_CODES and code in RULE_TEXT
]

_CODE = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "One finding code.",
            "enum": _POLICY_CODES,
        },
    },
    "required": ["code"],
}


def audit_registry(invoice, stock: dict[str, int], raw_text: str) -> ToolRegistry:
    """lookup_item, recompute_totals, and get_policy_rule for this invoice.

    stock is the snapshot audit already loaded. The functions do not open
    inventory.db. An unknown item raises ToolInputError and is not corrected.
    """

    return ToolRegistry(
        [
            Tool(
                name="lookup_item",
                description=(
                    "Read-only. Stock, the quantity this invoice requests, and the "
                    "USD reference price for one item. Names match after ignoring "
                    "case, spaces, and hyphens only. The item is not corrected."
                ),
                parameters=_ITEM,
                function=partial(lookup_item, invoice=invoice, stock=stock),
            ),
            Tool(
                name="recompute_totals",
                description=(
                    "Read-only. Whether the line totals and the printed total reconcile, "
                    "and the mismatch messages. No arguments."
                ),
                parameters={"type": "object", "properties": {}},
                function=partial(
                    recompute_totals, invoice=invoice, raw_text=raw_text, stock=stock
                ),
            ),
            Tool(
                name="get_policy_rule",
                description=(
                    "Read-only. Class (reject, hold, or warning), rule text, and next "
                    "action for one finding code."
                ),
                parameters=_CODE,
                function=get_policy_rule,
            ),
        ]
    )


def lookup_item(arguments: dict, invoice, stock: dict[str, int]) -> dict:
    """Stock and reference price for one item on this invoice."""

    item = arguments["item"]
    matched = _matched_name(item, stock)
    if matched is None:
        raise ToolInputError(f"unknown item: {item}")
    requested = summed_quantities(invoice, stock).get(matched, Decimal(0))
    available = int(stock[matched])
    price = POLICY.reference_prices.get(matched)
    return {
        "item": item,
        "matched_item": matched,
        "available": available,
        "requested_on_invoice": show_qty(requested),
        "covered": requested <= Decimal(available),
        "reference_price": None if price is None else show_money(price),
        "currency": None if price is None else "USD",
    }


def recompute_totals(_arguments: dict, invoice, raw_text: str, stock: dict[str, int]) -> dict:
    """Printed total against the line math. The model passes no arguments."""

    findings, _currency = collect_findings(invoice, raw_text or "", stock or {})
    total = invoice.total
    return {
        "reconciles": math_reconciles(invoice),
        "printed_total": None if total is None else show_money(total),
        "mismatches": [item.message for item in findings if item.code == "TOTAL_MISMATCH"],
    }


def get_policy_rule(arguments: dict) -> dict:
    """Class, rule text, and action for one finding code. Unknown codes error."""

    code = arguments["code"]
    if code not in _POLICY_CODES:
        raise ToolInputError(f"unknown code: {code}")
    if code in REJECT_CODES:
        kind = "reject"
    elif code in HOLD_CODES:
        kind = "hold"
    else:
        kind = "warning"
    return {"code": code, "class": kind, "rule": RULE_TEXT[code], "action": ACTION.get(code)}


def _matched_name(name: str, table: dict) -> str | None:
    """Inventory spelling. None when the name is not in the table. The name is not rewritten."""

    if not isinstance(name, str) or name.strip() == "":
        return None
    found = canonical_item(name, table)
    if found in table:
        return found
    return None
