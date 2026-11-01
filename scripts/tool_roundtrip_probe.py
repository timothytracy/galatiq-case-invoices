"""One live tool round trip. Run it yourself:

    XAI_API_KEY=... python3 scripts/tool_roundtrip_probe.py

The script forces XAI_TOOLS=1 and calls echo_note through the real loop.
It prints the tool-call log and the final JSON. It never prints the key.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm_client import (  # noqa: E402
    GrokClient,
    LLMError,
    MissingAPIKey,
    load_dotenv,
    parse_json_object,
    tool_request,
)
from pipeline.tools import example_registry  # noqa: E402

PROMPT = (
    'Call the echo_note tool exactly once with note "phase-1". '
    "After the tool result comes back, reply with only a JSON object "
    '{"note":"phase-1","echoed":"<the echo field from the tool>"} '
    "and no other text."
)


def _show(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    key = os.environ.get("XAI_API_KEY", "").strip()
    if key:
        text = text.replace(key, "[redacted]")
    return text


def main() -> int:
    load_dotenv()
    os.environ["XAI_TOOLS"] = "1"
    if not os.environ.get("XAI_API_KEY", "").strip():
        print("XAI_API_KEY is not set", file=sys.stderr)
        return 2
    step = None
    try:
        step = tool_request(GrokClient(), example_registry())
        text = step([{"role": "user", "content": PROMPT}])
    except MissingAPIKey:
        print("XAI_API_KEY is not set", file=sys.stderr)
        return 2
    except LLMError as exc:
        print(_show(str(exc)), file=sys.stderr)
        print(_show({"tool_calls": [] if step is None else step.log}))
        return 1
    except Exception as exc:
        print(_show(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        print(_show({"tool_calls": [] if step is None else step.log}))
        return 1

    try:
        answer = parse_json_object(text)
    except (json.JSONDecodeError, ValueError):
        print(_show({"tool_calls": step.log, "final_text": text}))
        print("final message was not a JSON object", file=sys.stderr)
        return 1
    print(_show({"tool_calls": step.log, "answer": answer}))
    if not step.log:
        print("no tool call was made", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
