# Recordings

These JSONL files are saved model replies. Replay reads them and does not call the network, so a score can be checked with no API key.

Five sets are committed. All five were recorded again on 2026-11-02 PT with `--as-of 2026-11-01`, because the audit prompt changed and every audit request has a new fingerprint.

- `recordings/samples.jsonl` scores `data/invoices/`
- `recordings/unseen.jsonl` scores `data/extra_invoices/`
- `recordings/ordered.jsonl` scores `data/ordered_scenario.json` on one shared ledger
- `recordings/loop.jsonl` scores the loop set, `data/loop_invoices/` against `data/loop_answer_key.json`
- `recordings/warning.jsonl` scores the warning set, `data/warning_invoices/` against `data/warning_answer_key.json`

Each is one live run. The first samples recording that day scored 19/20, because extraction read 1009 so that validation rejected it for `MISSING_FIELD` instead of `BAD_QUANTITY`. Samples was recorded once more and scored 20/20. That miss was before the audit, which never runs on a reject. The warning set scores 1/3: the audit passed both labeled holds. It is kept as recorded and is not run in CI.

A missing recording fails the run. A file that is present still exits 1 when the score is short of perfect.

Each file starts with a header line, then one line per model request.

```json
{"as_of":"2026-11-01","model":"grok-4.20-0309-non-reasoning","record":"header"}
{"answer":{"content":"...","tool_calls":null},"fingerprint":"<sha256>","model":"grok-4.20-0309-non-reasoning","usage":{"completion_tokens":0,"prompt_tokens":0,"total_tokens":0}}
```

`as_of` is the date the prompts were told was today. Replay uses that date, not the machine clock. Live runs do not read this file and still use the wall clock. These five were recorded on 2026-11-02 PT. The commands below pass `--as-of 2026-11-01`, the day the earlier recordings used, so the prompts see the same today. Omit `--as-of` and the header stores the calendar date at the start of that run instead.

The fingerprint hashes the model, the full message list (including tool results), the tools offered or null, tool_choice or null, response_format or null, and temperature. It does not hash the API key, a timestamp, or a request id. A temp payments ledger path (`payments-*.db` or `payments-ordered-*.db` under the system temp directory) is replaced with `payments-ledger.db` before the hash, so a new temp file still matches. Record and replay also store a repo-relative invoice path in the audit prompt, so a different checkout directory still matches. Live prompts keep the path they were given.

Run these from the repo root, with `XAI_API_KEY` set in the environment. Do not put the key in the command.

```bash
python3 scripts/eval.py --as-of 2026-11-01 --record recordings/samples.jsonl
python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --as-of 2026-11-01 --record recordings/unseen.jsonl
python3 scripts/eval.py --ordered --as-of 2026-11-01 --record recordings/ordered.jsonl
python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --as-of 2026-11-01 --record recordings/loop.jsonl
python3 scripts/eval.py --invoices data/warning_invoices --answer-key data/warning_answer_key.json --as-of 2026-11-01 --record recordings/warning.jsonl
```

Replay a file with `--replay` in place of `--record`.
