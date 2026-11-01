"""Read-only tools. A tool is a function plus the JSON schema the model sees.

A ToolInputError (unknown tool, bad arguments, nothing found) becomes
{"error": "..."} for the model. Any other exception leaves call() and the
loop turns it into a non-retryable LLMError. echo_note is for tests and
the probe. No agent is given a registry yet.
"""

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
    return None
