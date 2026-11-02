# Recordings

These JSONL files are saved model replies. Replay reads them and does not call the network, so a score can be checked with no API key.

There are five sets. Three are committed now, tools off, recorded with `--as-of 2026-11-01`:

- `recordings/samples.jsonl` scores `data/invoices/`
- `recordings/unseen.jsonl` scores `data/extra_invoices/`
- `recordings/ordered.jsonl` scores `data/ordered_scenario.json` on one shared ledger

Pending approval: the two tools-on sets, `recordings/samples_tools.jsonl` and `recordings/unseen_tools.jsonl`, come later. They wait until the ledger audit-tool change merges, because that change alters the tools-on prompt and tool list. CI skips those two steps only while the file is absent. A missing samples, unseen, or ordered file fails the run. A file that is present still exits 1 when the score is short of perfect.

Each file starts with a header line, then one line per model request.

```json
{"as_of":"2026-11-01","model":"grok-4.20-0309-non-reasoning","record":"header"}
{"answer":{"content":"...","tool_calls":null},"fingerprint":"<sha256>","model":"grok-4.20-0309-non-reasoning","usage":{"completion_tokens":0,"prompt_tokens":0,"total_tokens":0}}
```

`as_of` is the date the prompts were told was today. Replay uses that date, not the machine clock. Live runs do not read this file and still use the wall clock. Pending approval: these three were recorded with `--as-of 2026-11-01`. Omit `--as-of` and the header stores the calendar date at the start of that run instead.

The fingerprint hashes the model, the full message list (including tool results), the tools offered or null, tool_choice or null, response_format or null, and temperature. It does not hash the API key, a timestamp, or a request id. A temp payments ledger path (`payments-*.db` or `payments-ordered-*.db` under the system temp directory) is replaced with `payments-ledger.db` before the hash, so a new temp file still matches. Record and replay also store a repo-relative invoice path in the audit prompt, so a different checkout directory still matches. Live prompts keep the path they were given.

Run these from the repo root, with `XAI_API_KEY` set in the environment. Do not put the key in the command.

```bash
python3 scripts/eval.py --as-of 2026-11-01 --record recordings/samples.jsonl
python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --as-of 2026-11-01 --record recordings/unseen.jsonl
XAI_TOOLS=1 python3 scripts/eval.py --as-of 2026-11-01 --record recordings/samples_tools.jsonl
XAI_TOOLS=1 python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --as-of 2026-11-01 --record recordings/unseen_tools.jsonl
python3 scripts/eval.py --ordered --as-of 2026-11-01 --record recordings/ordered.jsonl
```

Replay any of the five with `--replay` in place of `--record`. Set `XAI_TOOLS=1` for the two `_tools` files. The tools flag changes the audit prompt, so a tools recording does not match a run with the flag off.
