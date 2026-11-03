"""Record and replay. No network and no API key on the replay path."""

import json
import re
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from graph import run_invoice
from llm_client import (
    DEFAULT_MODEL,
    LLMError,
    call_with_retries,
    tool_request,
)
from pipeline.tools import example_registry
from recording import estimate_cost, request_fingerprint
from scripts.eval import main as eval_main
from tests.fakes import ANSWER, FRAUD_PASS, Echoed, FakeClock, ZeroRandom, extraction, task_of, write_invoice

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]


def _fp(messages, **overrides):
    payload = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "tools": None,
        "tool_choice": None,
        "response_format": None,
        "temperature": 0,
    }
    payload.update(overrides)
    return request_fingerprint(**payload)


def _usage(prompt=10, completion=2):
    return type(
        "Usage",
        (),
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    )()


class _Function:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = _Function(name, arguments)


class _Message:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls


def _response(content, tool_calls=None, usage=None):
    message = _Message(content, tool_calls)
    choice = type("Choice", (), {"message": message})()
    return type("Resp", (), {"choices": [choice], "usage": usage or _usage()})()


def install_openai(monkeypatch, replies):
    """Replies is a list of response objects, consumed in order."""

    import openai

    seen = []
    queue = list(replies)

    class Completions:
        def create(self, **kwargs):
            seen.append(kwargs)
            if not queue:
                raise AssertionError("scripted model ran out of replies")
            return queue.pop(0)

    class Client:
        def __init__(self, **_kwargs):
            self.chat = type("Chat", (), {"completions": Completions()})()

    monkeypatch.setattr(openai, "OpenAI", Client)
    return seen


def install_openai_raise(monkeypatch):
    import openai

    def boom(**_kwargs):
        raise AssertionError("network client constructed")

    monkeypatch.setattr(openai, "OpenAI", boom)


def _freeze_today(monkeypatch, module, day):
    class Frozen(date):
        @classmethod
        def today(cls):
            return day

    monkeypatch.setattr(module, "date", Frozen)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_fingerprint_is_stable_and_sees_a_tool_result():
    conversation = [
        {"role": "user", "content": "audit this"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "lookup_item", "arguments": '{"item":"WidgetA"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"available": 15}'},
    ]
    reordered = [
        {"content": "audit this", "role": "user"},
        {
            "tool_calls": [
                {
                    "function": {"arguments": '{"item":"WidgetA"}', "name": "lookup_item"},
                    "type": "function",
                    "id": "c1",
                }
            ],
            "content": None,
            "role": "assistant",
        },
        {"content": '{"available": 15}', "role": "tool", "tool_call_id": "c1"},
    ]
    changed = [
        conversation[0],
        conversation[1],
        {"role": "tool", "tool_call_id": "c1", "content": '{"available": 14}'},
    ]
    first = _fp(conversation)
    assert first == _fp(conversation)
    assert first == _fp(reordered)
    assert len(first) == 64
    assert first != _fp(changed)
    assert first != _fp(list(reversed(conversation)))
    assert first != _fp(conversation, temperature=1)
    assert first != _fp(conversation, tools=[{"type": "function"}])
    nested = [{"role": "tool", "content": {"b": 1, "a": 2}}]
    nested_reordered = [{"content": {"a": 2, "b": 1}, "role": "tool"}]
    nested_changed = [{"role": "tool", "content": {"b": 3, "a": 2}}]
    assert _fp(nested) == _fp(nested_reordered)
    assert _fp(nested) != _fp(nested_changed)


def test_cost_uses_short_rates_under_200k_and_long_rates_at_the_boundary():
    short = estimate_cost([{"prompt_tokens": 100_000, "completion_tokens": 0}])
    assert short == Decimal("0.125000")
    at_boundary = estimate_cost([{"prompt_tokens": 200_000, "completion_tokens": 0}])
    assert at_boundary == Decimal("0.500000")
    over = estimate_cost([{"prompt_tokens": 1_000_000, "completion_tokens": 0}])
    assert over == Decimal("2.500000")
    under = estimate_cost([{"prompt_tokens": 199_999, "completion_tokens": 0}])
    assert under == Decimal("0.249999")


def test_live_payload_matches_the_baseline_and_writes_nothing(tmp_path, monkeypatch):
    from llm_client import GrokClient

    messages = [{"role": "user", "content": "Return a JSON object."}]
    baseline = {"model": DEFAULT_MODEL, "messages": messages, "temperature": 0}
    stray = tmp_path / "not-opened.jsonl"
    monkeypatch.setenv("XAI_RECORDING", str(stray))
    monkeypatch.delenv("XAI_MODE", raising=False)
    seen = install_openai(monkeypatch, [_response('{"echoed": "x"}')])
    client = GrokClient(api_key="test-key")
    assert client.request(messages) == '{"echoed": "x"}'
    assert seen[0] == baseline
    assert "tools" not in seen[0]
    assert "tool_choice" not in seen[0]
    assert "response_format" not in seen[0]
    assert not stray.exists()

    recorded = tmp_path / "one.jsonl"
    seen_record = install_openai(monkeypatch, [_response('{"echoed": "x"}')])
    recorder = GrokClient(api_key="test-key", mode="record", recording=recorded, as_of=date(2026, 11, 1))
    assert recorder.request(list(messages)) == '{"echoed": "x"}'
    assert seen_record[0] == baseline


def test_a_named_model_call_is_sent_recorded_and_replayed_on_that_model(tmp_path, monkeypatch):
    from llm_client import FRAUD_REVIEW_MODEL, GrokClient

    messages = [{"role": "user", "content": "Return a JSON object."}]
    path = tmp_path / "named.jsonl"
    usage = type(
        "Usage",
        (),
        {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 112,
            "completion_tokens_details": type("Details", (), {"reasoning_tokens": 100})(),
        },
    )()
    seen = install_openai(monkeypatch, [_response('{"n": 1}', usage=usage)])
    recorder = GrokClient(api_key="test-key", mode="record", recording=path, as_of=date(2026, 11, 1))
    assert recorder.request_with_model(messages, FRAUD_REVIEW_MODEL) == '{"n": 1}'
    assert seen[0]["model"] == FRAUD_REVIEW_MODEL
    rows = _rows(path)
    assert rows[0]["model"] == DEFAULT_MODEL
    assert rows[1]["model"] == FRAUD_REVIEW_MODEL
    assert rows[1]["usage"]["reasoning_tokens"] == 100
    # 10 input at $1.25 and 102 output at $2.50 per million.
    assert recorder.usage_summary()["cost_usd"] == Decimal("0.000268")

    install_openai_raise(monkeypatch)
    replayer = GrokClient(mode="replay", recording=path)
    with pytest.raises(LLMError, match="replay miss"):
        replayer.request(list(messages))
    assert replayer.request_with_model(list(messages), FRAUD_REVIEW_MODEL) == '{"n": 1}'


def test_record_then_replay_round_trip_and_a_miss(tmp_path, monkeypatch):
    from llm_client import GrokClient

    messages = [{"role": "user", "content": "Return a JSON object."}]
    path = tmp_path / "round.jsonl"
    install_openai(
        monkeypatch,
        [_response('{"n": 1}'), _response('{"n": 2}')],
    )
    recorder = GrokClient(api_key="test-key", mode="record", recording=path, as_of=date(2026, 11, 1))
    assert recorder.request(messages) == '{"n": 1}'
    assert recorder.request(list(messages)) == '{"n": 2}'
    rows = _rows(path)
    assert rows[0] == {
        "record": "header",
        "as_of": "2026-11-01",
        "model": DEFAULT_MODEL,
    }
    assert [row["answer"]["content"] for row in rows[1:]] == ['{"n": 1}', '{"n": 2}']
    assert rows[1]["answer"]["tool_calls"] is None
    assert set(rows[1]) == {"answer", "fingerprint", "model", "usage"}
    assert set(rows[1]["usage"]) == {"completion_tokens", "prompt_tokens", "total_tokens"}
    assert "test-key" not in path.read_text(encoding="utf-8")
    assert rows[1]["fingerprint"] == rows[2]["fingerprint"]

    install_openai_raise(monkeypatch)
    monkeypatch.setenv("XAI_MODEL", "some-other-model")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    player = GrokClient(mode="replay", recording=path)
    assert player.model == DEFAULT_MODEL
    assert player.as_of == date(2026, 11, 1)
    assert player.request(list(messages)) == '{"n": 1}'
    assert player.request(list(messages)) == '{"n": 2}'
    fingerprint = rows[1]["fingerprint"]
    with pytest.raises(LLMError) as caught:
        player.request(list(messages))
    assert caught.value.retryable is False
    assert caught.value.error_type == f"replay miss: no recording for fingerprint {fingerprint[:12]}"
    summary = player.usage_summary()
    assert summary["total_tokens"] == 24
    assert summary["cost_usd"] == estimate_cost([rows[1]["usage"], rows[2]["usage"]])
    assert player.usage_line() == "tokens total=24 cost_usd=0.000035"


def test_replay_miss_is_a_system_error_hold_and_is_not_paid(tmp_path, db_path, monkeypatch):
    from llm_client import GrokClient

    recording = tmp_path / "empty.jsonl"
    recording.write_text(
        json.dumps(
            {"record": "header", "as_of": "2026-11-01", "model": DEFAULT_MODEL},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    install_openai_raise(monkeypatch)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    path = write_invoice(tmp_path, "clean.txt", "Vendor: Widgets Inc.\nWidgetA\nTotal: 500.00\n")
    clock = FakeClock()
    client = GrokClient(mode="replay", recording=recording)
    result = run_invoice(
        path,
        db_path,
        client,
        clock=clock,
        rng=ZeroRandom(),
        max_attempts=3,
    )
    assert result["outcome"] == "hold"
    assert result["reason_code"] == "SYSTEM_ERROR"
    assert result["payment"].status == "not_paid"
    assert result["attempts"][0]["error_type"].startswith("replay miss: no recording for fingerprint ")
    assert len(result["attempts"][0]["error_type"].rsplit(" ", 1)[-1]) == 12
    assert len(result["attempts"]) == 1
    assert clock.slept == []
    assert "replay miss: no recording for fingerprint " in result["agent_error"]


def test_tool_loop_forced_final_and_repair_replay_without_a_network_client(tmp_path, monkeypatch):
    from llm_client import GrokClient

    path = tmp_path / "tools.jsonl"

    def call(call_id, note):
        return _ToolCall(call_id, "echo_note", json.dumps({"note": note}))

    install_openai(
        monkeypatch,
        [
            _response("", [call("c1", "one")]),
            _response("", [call("c2", "two")]),
            _response("", [call("c3", "three")]),
            _response("", [call("c4", "four")]),
            _response("not-json"),
            _response(ANSWER),
        ],
    )
    messages = [{"role": "user", "content": "Answer with JSON."}]
    recorder = GrokClient(api_key="test-key", mode="record", recording=path, as_of=date(2026, 11, 1))
    step = tool_request(
        recorder, example_registry(), agent="audit", final_turn_on_cap=True
    )
    parsed = call_with_retries(
        step,
        messages,
        Echoed,
        max_attempts=2,
        clock=FakeClock(),
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert parsed.echoed == "phase-1"
    assert step.cap_hit == "rounds"
    assert step.forced_final_turn is True
    rows = _rows(path)
    assert len(rows) == 7
    assert len({row["fingerprint"] for row in rows[1:]}) == 6

    install_openai_raise(monkeypatch)
    player = GrokClient(mode="replay", recording=path)
    replay_step = tool_request(
        player, example_registry(), agent="audit", final_turn_on_cap=True
    )
    again = call_with_retries(
        replay_step,
        list(messages),
        Echoed,
        max_attempts=2,
        clock=FakeClock(),
        rng=ZeroRandom(),
        attempt_log=[],
    )
    assert again.echoed == "phase-1"
    assert replay_step.cap_hit == "rounds"
    assert replay_step.forced_final_turn is True
    assert [row["name"] for row in replay_step.log] == ["echo_note", "echo_note", "echo_note", "echo_note"]
    assert replay_step.log[-1]["error"] == "blocked: tool_cap"


def test_replay_on_a_later_wall_clock_day_still_hits(tmp_path, monkeypatch, capsys):
    import decision.wording as wording
    import llm_client

    folder = tmp_path / "invoices"
    folder.mkdir()
    write_invoice(
        folder,
        "clean.txt",
        "Vendor: Widgets Inc.\nDue Date: 2026-02-01\nWidgetA 2\nTotal: 500.00\n",
    )
    key_path = tmp_path / "key.json"
    key_path.write_text(
        json.dumps(
            {
                "invoices": {
                    "data/invoices/clean.txt": {
                        "outcome": "APPROVE",
                        "primary_reason": "None (all checks pass)",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    recording = tmp_path / "day.jsonl"
    body = extraction()
    seen = install_openai(
        monkeypatch,
        [
            _response(body),
            _response(FRAUD_PASS),
            _response('{"risks_considered": ["none"]}'),
            _response('{"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []}'),
        ],
    )
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    _freeze_today(monkeypatch, wording, date(2026, 3, 3))
    _freeze_today(monkeypatch, llm_client, date(2026, 3, 3))
    recorded = eval_main(
        [
            "--invoices",
            str(folder),
            "--answer-key",
            str(key_path),
            "--record",
            str(recording),
            "--as-of",
            "2026-11-01",
        ]
    )
    recorded_out = capsys.readouterr().out
    assert recorded == 0
    assert "score 1/1" in recorded_out
    assert "tokens total=48 cost_usd=0.000070" in recorded_out
    assert _rows(recording)[0]["as_of"] == "2026-11-01"
    blob = "\n".join(message.get("content") or "" for kwargs in seen for message in kwargs["messages"])
    assert "Today's date is 2026-11-01." in blob
    assert "2026-03-03" not in blob

    install_openai_raise(monkeypatch)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    _freeze_today(monkeypatch, wording, date(2027, 12, 12))
    _freeze_today(monkeypatch, llm_client, date(2027, 12, 12))
    replayed = eval_main(
        [
            "--invoices",
            str(folder),
            "--answer-key",
            str(key_path),
            "--replay",
            str(recording),
        ]
    )
    replayed_out = capsys.readouterr().out
    assert replayed == 0
    assert "score 1/1" in replayed_out
    assert "tokens total=48 cost_usd=0.000070" in replayed_out
    assert "replay miss:" not in replayed_out
    assert "2027-12-12" not in replayed_out


def test_record_prompt_path_is_repo_relative_and_replays_from_another_directory(
    tmp_path, db_path, monkeypatch
):
    from llm_client import GrokClient

    invoice = (ROOT / "data/invoices/invoice_1001.txt").resolve()
    body = extraction(
        invoice_number="INV-1001",
        invoice_date="2026-01-15",
        due_date="2026-02-01",
        line_items=[
            {"item": "WidgetA", "description": "WidgetA", "quantity": 10, "unit_price": 250, "amount": 2500, "note": None},
            {"item": "WidgetB", "description": "WidgetB", "quantity": 5, "unit_price": 500, "amount": 2500, "note": None},
        ],
        subtotal=5000,
        total=5000,
        payment_terms="Net 15",
    )
    replies = [
        _response(body),
        _response(FRAUD_PASS),
        _response('{"risks_considered": ["none"]}'),
        _response('{"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []}'),
    ]

    def source_path(seen):
        for kwargs in seen:
            if task_of(kwargs["messages"]) != "audit":
                continue
            payload = json.loads(kwargs["messages"][-1]["content"])
            return payload["extracted"]["source"]["path"]
        raise AssertionError("audit request was not sent")

    live_seen = install_openai(monkeypatch, list(replies))
    live = GrokClient(api_key="test-key")
    live_result = run_invoice(
        invoice,
        db_path,
        live,
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=2,
        as_of=date(2026, 11, 1),
    )
    assert live_result["outcome"] == "approve"
    assert source_path(live_seen) == str(invoice)

    recording = tmp_path / "path.jsonl"
    record_seen = install_openai(monkeypatch, list(replies))
    recorder = GrokClient(api_key="test-key", mode="record", recording=recording, as_of=date(2026, 11, 1))
    recorded = run_invoice(
        invoice,
        db_path,
        recorder,
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=2,
    )
    assert recorded["outcome"] == "approve"
    assert source_path(record_seen) == "data/invoices/invoice_1001.txt"
    assert str(invoice) not in source_path(record_seen)

    install_openai_raise(monkeypatch)
    monkeypatch.chdir(tmp_path)
    player = GrokClient(mode="replay", recording=recording)
    replayed = run_invoice(
        invoice,
        db_path,
        player,
        clock=FakeClock(),
        rng=ZeroRandom(),
        max_attempts=2,
    )
    assert replayed["outcome"] == "approve"
    assert replayed["payment"].status == "paid"


def test_missing_replay_file_fails_and_a_present_file_can_fail(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    missing = eval_main(["--replay", str(tmp_path / "missing.jsonl")])
    skipped = capsys.readouterr()
    assert missing == 1
    assert "replay recording not found:" in skipped.err
    assert "skip:" not in skipped.out

    with pytest.raises(SystemExit):
        eval_main(["--record", str(tmp_path / "a.jsonl"), "--replay", str(tmp_path / "b.jsonl")])

    assert eval_main(["--as-of", "March"]) == 2

    folder = tmp_path / "invoices"
    folder.mkdir()
    write_invoice(folder, "clean.txt", "Vendor: Widgets Inc.\nWidgetA\nTotal: 500.00\n")
    key_path = tmp_path / "key.json"
    key_path.write_text(
        json.dumps(
            {
                "invoices": {
                    "data/invoices/clean.txt": {
                        "outcome": "APPROVE",
                        "primary_reason": "None (all checks pass)",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    recording = tmp_path / "header.jsonl"
    recording.write_text(
        json.dumps({"as_of": "2026-11-01", "model": DEFAULT_MODEL, "record": "header"}) + "\n",
        encoding="utf-8",
    )
    install_openai_raise(monkeypatch)
    failed = eval_main(
        ["--invoices", str(folder), "--answer-key", str(key_path), "--replay", str(recording)]
    )
    out = capsys.readouterr().out
    assert failed == 1
    assert "skip:" not in out
    assert "score 0/1" in out
    assert "replay miss: no recording for fingerprint " in out
    assert "tokens total=0 cost_usd=0.000000" in out
    assert "payment=not_paid" in out


def test_fingerprint_covers_the_ledger_tool_like_any_other_tool():
    from pipeline.audit import LEDGER_LINE
    from pipeline.tools import audit_registry
    from tests.test_tool_loop import payable_state

    state = payable_state()
    schemas = audit_registry(state.invoice, {"WidgetA": 15}, state.raw_text or "").schemas()
    names = [item["function"]["name"] for item in schemas]
    assert "check_payment_ledger" in names
    without = [item for item in schemas if item["function"]["name"] != "check_payment_ledger"]
    with_sentence = [{"role": "system", "content": f"TASK audit\n{LEDGER_LINE}"}]
    without_sentence = [{"role": "system", "content": "TASK audit"}]
    full = _fp(with_sentence, tools=schemas, tool_choice="auto")
    assert full != _fp(with_sentence, tools=without, tool_choice="auto")
    assert full != _fp(without_sentence, tools=schemas, tool_choice="auto")


def test_a_temp_ledger_path_does_not_change_the_fingerprint():
    root = Path(tempfile.gettempdir()).resolve().as_posix().rstrip("/")
    ordered = f"{root}/payments-ordered-aaaaaaaa.db"
    fresh = f"{root}/payments-bbbbbbbb.db"
    left = [{"role": "user", "content": f"ledger file {ordered} failed"}]
    right = [{"role": "user", "content": f"ledger file {fresh} failed"}]
    assert _fp(left) == _fp(right)
    assert _fp(left) != _fp([{"role": "user", "content": "ledger file other.db failed"}])


def test_ordered_replay_ignores_a_new_temp_ledger(tmp_path, monkeypatch, capsys):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "ledger_files": ["invoice_1001.txt"],
                "steps": [
                    {
                        "file": "data/invoices/invoice_1001.txt",
                        "outcome": "approve",
                        "reason_code": None,
                        "paid": True,
                    },
                    {
                        "file": "data/invoices/invoice_1001.txt",
                        "outcome": "hold",
                        "reason_code": "DUPLICATE_INVOICE",
                        "paid": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    body = extraction(
        invoice_number="INV-1001",
        invoice_date="2026-01-15",
        due_date="2026-02-01",
        line_items=[
            {
                "item": "WidgetA",
                "description": "WidgetA",
                "quantity": 10,
                "unit_price": 250,
                "amount": 2500,
                "note": None,
            },
            {
                "item": "WidgetB",
                "description": "WidgetB",
                "quantity": 5,
                "unit_price": 500,
                "amount": 2500,
                "note": None,
            },
        ],
        subtotal=5000,
        total=5000,
        payment_terms="Net 15",
    )
    audit = '{"verdict": "PASS", "feedback": [], "notes": "PASS", "cited_codes": []}'
    reason = '{"risks_considered": ["none"]}'
    recording = tmp_path / "ordered.jsonl"
    seen = install_openai(
        monkeypatch,
        [_response(body), _response(FRAUD_PASS), _response(reason), _response(audit), _response(body)],
    )
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    recorded = eval_main(
        ["--ordered", str(scenario), "--record", str(recording), "--as-of", "2026-11-01"]
    )
    recorded_out = capsys.readouterr().out
    assert recorded == 0, recorded_out
    blob = "\n".join(
        message.get("content") or "" for kwargs in seen for message in kwargs["messages"]
    )
    assert "payments-ordered-" not in blob
    assert "payments-" not in blob

    install_openai_raise(monkeypatch)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    replayed = eval_main(["--ordered", str(scenario), "--replay", str(recording)])
    replayed_out = capsys.readouterr().out
    assert replayed == 0, replayed_out
    assert "replay miss:" not in replayed_out
    assert "MISS" not in replayed_out


_RECORDINGS = {"samples.jsonl", "unseen.jsonl", "ordered.jsonl", "loop.jsonl", "fraud.jsonl", "passlist.jsonl"}
_RECORDING = re.compile(r"recordings/([A-Za-z0-9_.-]+\.jsonl)")


def _eval_commands(text: str) -> list[tuple[str, str]]:
    """Each python3 scripts/eval.py command and the recording it names."""

    found = []
    for match in re.finditer(r"python3\s+scripts/eval\.py\b[^`\n]*?(?=&&|\n|`|$)", text):
        command = re.sub(r"\s+", " ", match.group(0).strip())
        recording = _RECORDING.search(command)
        if recording is not None:
            found.append((command, recording.group(1)))
    return found


def _ci_commands(text: str) -> list[tuple[str, str]]:
    found = []
    for line in text.splitlines():
        if "python3 scripts/eval.py" not in line:
            continue
        command = re.sub(r"\s+", " ", line.split("run:", 1)[-1].strip())
        recording = _RECORDING.search(command)
        if recording is not None:
            found.append((command, recording.group(1)))
    return found


def test_documented_replay_commands_match_the_recording():
    docs = [ROOT / "README.md", ROOT / "recordings" / "README.md"]
    docs.extend(sorted((ROOT / "docs").glob("*.md")))
    commands = []
    for path in docs:
        commands.extend(_eval_commands(path.read_text(encoding="utf-8")))
    ci = _ci_commands((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert len(ci) == 6
    for command, name in commands + ci:
        assert name in _RECORDINGS, command
        assert (ROOT / "recordings" / name).is_file(), command
    readme = _eval_commands((ROOT / "README.md").read_text(encoding="utf-8"))
    readme_replay = [command for command, _name in readme if "--replay" in command]
    start = 0
    for command, _name in ci:
        assert command in readme_replay[start:], command
        start = readme_replay.index(command, start) + 1
    recorded = (ROOT / "recordings" / "README.md").read_text(encoding="utf-8")
    assert "These six were recorded on 2026-11-02 PT." in recorded


def _invoices(folder: Path) -> list[Path]:
    return sorted(path for path in folder.iterdir() if path.is_file() and path.suffix in {".txt", ".pdf", ".json", ".csv", ".xml"})


def test_every_provided_invoice_is_recorded_once():
    from llm_client import GrokClient
    from pipeline.ingestion import is_recorded

    samples = GrokClient(mode="replay", recording=ROOT / "recordings" / "samples.jsonl")
    unseen = GrokClient(mode="replay", recording=ROOT / "recordings" / "unseen.jsonl")
    provided = _invoices(ROOT / "data" / "invoices")
    extra = _invoices(ROOT / "data" / "extra_invoices")
    assert len(provided) == 20 and len(extra) == 21
    assert all(is_recorded(path, samples) for path in provided)
    assert all(is_recorded(path, unseen) for path in extra)
    assert not any(is_recorded(path, unseen) for path in provided)
    assert not any(is_recorded(path, samples) for path in extra)
    loop = GrokClient(mode="replay", recording=ROOT / "recordings" / "loop.jsonl")
    loop_files = _invoices(ROOT / "data" / "loop_invoices")
    assert len(loop_files) == 3
    assert all(is_recorded(path, loop) for path in loop_files)
    assert not any(is_recorded(path, loop) for path in provided + extra)
    passlist = GrokClient(mode="replay", recording=ROOT / "recordings" / "passlist.jsonl")
    passlist_files = _invoices(ROOT / "data" / "passlist_invoices")
    assert len(passlist_files) == 5
    assert all(is_recorded(path, passlist) for path in passlist_files)
    assert samples.usage_summary()["total_tokens"] == 0


def test_a_new_invoice_is_not_recorded_and_has_does_not_consume(tmp_path):
    from llm_client import GrokClient
    from pipeline.ingestion import is_recorded

    client = GrokClient(mode="replay", recording=ROOT / "recordings" / "samples.jsonl")
    fresh = write_invoice(tmp_path, "new.txt", "Vendor: New Co\nWidgetA\nTotal: 5.00\n")
    assert is_recorded(fresh, client) is False
    assert is_recorded(tmp_path / "missing.txt", client) is False
    known = ROOT / "data" / "invoices" / "invoice_1001.txt"
    assert is_recorded(known, client) and is_recorded(known, client)


def test_replayed_grok_rejects_the_injection_invoice_2107(tmp_path, db_path):
    from llm_client import GrokClient

    client = GrokClient(mode="replay", recording=ROOT / "recordings" / "unseen.jsonl")
    result = run_invoice(
        ROOT / "data" / "extra_invoices" / "invoice_2107.pdf",
        db_path,
        client,
        as_of=client.as_of,
        ledger_path=tmp_path / "payments.db",
    )
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "EMBEDDED_INSTRUCTIONS"
    assert result["payment"].status == "not_paid"
