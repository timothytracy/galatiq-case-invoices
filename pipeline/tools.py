"""Read-only tools. A tool is a function plus the JSON schema the model sees.

A ToolInputError (unknown tool, bad arguments, unknown item or code) becomes
{"error": "..."} for the model. Any other exception leaves call() and the
loop turns it into a non-retryable LLMError. echo_note is for tests and
the probe. Critique gets critique_registry when XAI_TOOLS is on.
"""

from decimal import Decimal
from functools import partial

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


_ITEM = {
    "type": "object",
    "properties": {
        "item": {"type": "string", "description": "Item name as printed or extracted"},
    },
    "required": ["item"],
}

_CODE = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "A finding code, such as PRICE_SPIKE"},
    },
    "required": ["code"],
}


def critique_registry(invoice, stock: dict[str, int]) -> ToolRegistry:
    """check_stock, get_reference_price, and get_policy_rule for this invoice.

    stock is the snapshot critique already loaded read-only. The functions
    do not open inventory.db. An unknown item or code raises ToolInputError.
    """

    prices = POLICY.reference_prices
    return ToolRegistry(
        [
            Tool(
                name="check_stock",
                description=(
                    "Read-only. Available stock for one item, and the summed quantity "
                    "this invoice requests for it. Names match after ignoring case, "
                    "spaces, and hyphens only."
                ),
                parameters=_ITEM,
                function=partial(check_stock, invoice=invoice, stock=stock),
            ),
            Tool(
                name="get_reference_price",
                description="Read-only. USD reference unit price for one item.",
                parameters={
                    "type": "object",
                    "properties": {"item": {"type": "string"}},
                    "required": ["item"],
                },
                function=partial(get_reference_price, prices=prices),
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


def check_stock(arguments: dict, invoice, stock: dict[str, int]) -> dict:
    """Stock for one item against this invoice's summed quantity."""

    item = arguments["item"]
    matched = _matched_name(item, stock)
    if matched is None:
        raise ToolInputError(f"unknown item: {item}")
    requested = Decimal(0)
    if invoice is not None:
        requested = summed_quantities(invoice, stock).get(matched, Decimal(0))
    available = int(stock[matched])
    return {
        "item": item,
        "matched_item": matched,
        "available": available,
        "requested_on_invoice": show_qty(requested),
        "covered": requested <= Decimal(available),
    }


def get_reference_price(arguments: dict, prices: dict | None = None) -> dict:
    """USD reference price from policy.toml. Unknown items are an input error."""

    item = arguments["item"]
    table = POLICY.reference_prices if prices is None else prices
    matched = _matched_name(item, table)
    if matched is None:
        raise ToolInputError(f"unknown item: {item}")
    return {
        "item": item,
        "matched_item": matched,
        "reference_price": show_money(table[matched]),
        "currency": "USD",
    }


def get_policy_rule(arguments: dict) -> dict:
    """Class, rule text, and action for one finding code. Unknown codes error."""

    code = arguments["code"]
    name = code.strip().upper() if isinstance(code, str) else ""
    if name in REJECT_CODES:
        kind = "reject"
    elif name in HOLD_CODES:
        kind = "hold"
    elif name in WARNING_CODES:
        kind = "warning"
    else:
        raise ToolInputError(f"unknown code: {code}")
    return {
        "code": name,
        "class": kind,
        "rule": RULE_TEXT.get(name),
        "action": ACTION.get(name),
    }


def _matched_name(name: str, table: dict) -> str | None:
    """Inventory or price-table spelling. None when the name is not in the table."""

    if not isinstance(name, str) or name.strip() == "":
        return None
    found = canonical_item(name, table)
    if found in table:
        return found
    return None


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
    return None
