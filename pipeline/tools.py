"""Read-only tools. A tool is a function plus the JSON schema the model sees.

call() always returns a dict. An unknown name or bad arguments come back as
{"error": "..."}. They do not raise. No agent is given a registry yet.
The only tool here is echo_note, for tests and the live probe.
"""

import json


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


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: str) -> dict:
        """Run one call. Always a JSON object. Never raises."""

        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"unknown tool: {name}"}
        parsed, error = _parse_arguments(arguments)
        if error is not None or parsed is None:
            return {"error": error or "arguments must be a JSON object"}
        error = _schema_error(tool.parameters, parsed)
        if error is not None:
            return {"error": error}
        try:
            result = tool.function(parsed)
        except Exception as exc:
            return {"error": f"tool failed: {exc}"}
        if not isinstance(result, dict):
            return {"error": "tool result must be an object"}
        return result


def echo_note(arguments: dict) -> dict:
    """Return the note. It reads nothing and writes nothing."""

    note = arguments.get("note")
    if not isinstance(note, str) or note.strip() == "":
        return {"error": "note must be a non-empty string"}
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


def _parse_arguments(raw: str) -> tuple[dict | None, str | None]:
    if raw.strip() == "":
        return {}, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, "arguments are not valid JSON"
    if not isinstance(value, dict):
        return None, "arguments must be a JSON object"
    return value, None


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
