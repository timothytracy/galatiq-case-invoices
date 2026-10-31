"""Write docs/graph.md from the compiled LangGraph diagram.

    python3 scripts/export_graph.py

Drawing the graph does not call the model and does not need a key.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from graph import mermaid_text  # noqa: E402

DESTINATION = ROOT / "docs" / "graph.md"


def main() -> int:
    diagram = mermaid_text()
    body = (
        "# Invoice graph\n\n"
        "Generated from the compiled LangGraph `StateGraph`. "
        "Regenerate with `python3 scripts/export_graph.py`.\n\n"
        "```mermaid\n"
        f"{diagram}\n"
        "```\n"
    )
    DESTINATION.write_text(body, encoding="utf-8")
    print(DESTINATION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
