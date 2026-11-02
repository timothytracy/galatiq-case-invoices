"""Count how often each loop fired in a recorded run. Replay only: no network, no API key.

A recording is the exact model replies from one live run, so the counts are
that run's counts. Stage records are swallowed; stdout is one JSON object.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import create_database  # noqa: E402
from graph import run_invoice  # noqa: E402
from llm_client import begin_client  # noqa: E402
from scripts.eval import SUFFIXES, _release_dir  # noqa: E402

REPAIR_PREFIX = "The JSON failed validation"


class CountingClient:
    """Wraps the replay client and counts schema-repair turns."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.repairs = 0

    def _note(self, messages) -> None:
        last = messages[-1] if messages else {}
        if last.get("role") == "user" and str(last.get("content") or "").startswith(REPAIR_PREFIX):
            self.repairs += 1

    def request(self, messages, timeout=60):
        self._note(messages)
        return self.inner.request(messages, timeout)

    def request_tools(self, messages, tools, timeout=60):
        self._note(messages)
        return self.inner.request_tools(messages, tools, timeout)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def stages(result: dict, name: str) -> list[dict]:
    return [log["result"] for log in result.get("logs") or [] if log.get("stage") == name]


def disagrees(call: dict) -> bool:
    """A tool result that contradicts what code already checked, or a tool error."""

    body = call.get("result") or {}
    return bool(
        call.get("error")
        or body.get("covered") is False
        or body.get("reconciles") is False
        or body.get("already_paid") is True
        or body.get("over_split_threshold") is True
        or (call.get("name") == "lookup_item" and body.get("matched_item") is None)
    )


KEYS = (
    "audit_runs", "model_send_backs", "send_backs", "guard_changed_verdict",
    "critique_runs", "critique_holds", "set_asides_critique", "set_asides_audit",
    "audits_with_tools", "audits_with_tools_not_pass", "tool_calls", "tool_repeats",
    "tool_results_disagreeing", "tool_cap_hits", "retries",
)


def tally(results: list[dict], repairs: int) -> dict:
    counts = Counter({key: 0 for key in KEYS})
    tools = Counter()
    for result in results:
        critiques = stages(result, "critique")
        audits = stages(result, "audit")
        counts["critique_runs"] += len(critiques)
        counts["critique_holds"] += sum(1 for row in critiques if row.get("stricter"))
        counts["set_asides_critique"] += sum(len(row.get("dropped_concerns") or []) for row in critiques)
        counts["audit_runs"] += len(audits)
        counts["model_send_backs"] += sum(1 for row in audits if row.get("model_verdict") == "SEND_BACK")
        counts["send_backs"] += sum(1 for row in audits if row.get("verdict") == "SEND_BACK")
        counts["set_asides_audit"] += sum(len(row.get("dropped_concerns") or []) for row in audits)
        counts["guard_changed_verdict"] += sum(
            1 for row in audits if row.get("model_verdict") != row.get("verdict")
        )
        counts["retries"] += sum(1 for row in result.get("attempts") or [] if row.get("error_type"))
        for row in audits:
            calls = row.get("tool_calls") or []
            if not calls:
                continue
            counts["audits_with_tools"] += 1
            counts["tool_calls"] += len(calls)
            counts["tool_repeats"] += sum(1 for call in calls if call.get("repeat"))
            counts["tool_results_disagreeing"] += sum(1 for call in calls if disagrees(call))
            counts["tool_cap_hits"] += int(bool(row.get("tool_cap_hit")))
            tools.update(call.get("name") for call in calls)
            if row.get("verdict") != "PASS":
                counts["audits_with_tools_not_pass"] += 1
    counts["schema_repairs"] = repairs
    return {"invoices": len(results), **dict(counts), "tools_by_name": dict(tools)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay", required=True)
    parser.add_argument("--invoices", default=str(ROOT / "data" / "invoices"))
    parser.add_argument("--ordered", nargs="?", const=str(ROOT / "data" / "ordered_scenario.json"))
    args = parser.parse_args(argv)

    inner, as_of = begin_client("replay", args.replay, None)
    client = CountingClient(inner)
    db_path = ROOT / "inventory.db"
    if not db_path.exists():
        create_database(db_path)

    results = []
    with _release_dir(), contextlib.redirect_stdout(io.StringIO()):
        if args.ordered:
            scenario = json.loads(Path(args.ordered).read_text(encoding="utf-8"))
            handle, ledger = tempfile.mkstemp(prefix="payments-ordered-", suffix=".db")
            os.close(handle)
            try:
                for step in scenario["steps"]:
                    results.append(
                        run_invoice(ROOT / step["file"], db_path, client, as_of=as_of, ledger_path=ledger)
                    )
            finally:
                Path(ledger).unlink(missing_ok=True)
        else:
            files = sorted(
                path
                for path in Path(args.invoices).iterdir()
                if path.is_file() and path.suffix.lower() in SUFFIXES
            )
            for path in files:
                results.append(run_invoice(path, db_path, client, as_of=as_of))
    print(json.dumps(tally(results, client.repairs), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
