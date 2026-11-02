# Replay metrics

Every number below is from a replay of a committed recording, run on 2026-11-02 with no API key. The score, token, cost, call, and repeat lines are the stdout of `scripts/eval.py`. The tool names, the `check_payment_ledger` counts, and the cap fields are from a second pass over the same recordings on the same day. That pass counted `tool_calls` on the graph result. Its call totals and repeat totals match the eval lines. No live API run was made for this file.

`python3 setup_db.py` was run first. Each command exited 0.

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats | `check_payment_ledger` | Cap or forced turn |
|---|---|---|---|---|---|---|---|---|
| Samples | `python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 34199 | 0.051456 | 0 | 0 | 0 | none |
| Unseen | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 39976 | 0.058625 | 0 | 0 | 0 | none |
| Ordered | `python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 17358 | 0.026695 | 0 | 0 | 0 | none |
| Samples, audit tools | `XAI_TOOLS=1 python3 scripts/eval.py --replay recordings/samples_tools.jsonl` | 20/20 | 73608 | 0.102365 | 42 | 1 | 7 | none |
| Unseen, audit tools | `XAI_TOOLS=1 python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen_tools.jsonl` | 21/21 | 86882 | 0.119278 | 49 | 1 | 8 | none |

The ordered command does not print `score`. It prints one line per step, then the ledger line, then the tool line, then the token line. All 11 steps were `ok`. The ledger line was `ledger 3 rows: invoice_1001.txt, invoice_1011.pdf, invoice_1004.json`, matching the expected files, and it was `ok`.

Warning matches, which do not change the exit code: samples 19/19, unseen 9/9, samples with the audit tools 19/19, unseen with the audit tools 9/9. Those are the sums of the `warnings=` fields on the eval lines. The ordered command does not print warnings.

## Where the tool columns come from

`scripts/eval.py` prints `tools calls=N repeats=K`. Repeats are included in N. It does not print tool names, `check_payment_ledger` counts, or cap fields.

The second pass, same recordings, counted each logged call:

- Samples and unseen and ordered: no tool calls, so no ledger calls and no cap fields.
- Samples with the audit tools: `check_payment_ledger` 7, `lookup_item` 16, `recompute_totals` 7, `get_policy_rule` 12. Those add to 42, the eval call count. The 7 ledger calls are one each on `invoice_1001.txt`, `invoice_1004.json`, `invoice_1006.csv`, `invoice_1010.txt`, `invoice_1011.pdf`, `invoice_1011.txt`, and `invoice_1015.csv`. Those are the invoices whose eval line had `tools` greater than 0. The one repeat is `get_policy_rule` on round 2 of `invoice_1004.json`. The eval line for that file is `tools=6`, and 6 is the length of its call list, including the repeat.
- Unseen with the audit tools: `check_payment_ledger` 8, `lookup_item` 19, `recompute_totals` 8, `get_policy_rule` 14. Those add to 49. The 8 ledger calls are one each on `invoice_2003.txt`, `invoice_2008.txt`, `invoice_2101.txt`, `invoice_2102.csv`, `invoice_2103.csv`, `invoice_2104.json`, `invoice_2105.txt`, and `invoice_2106.txt`. The one repeat is `get_policy_rule` on round 2 of `invoice_2008.txt`. That file's eval line is `tools=8`.

No call was logged with error `blocked: tool_cap`. No audit result had `tool_cap_hit` or `forced_final_turn`.

Rejects and holds that stop at validation had `tools=0` on the eval line. Audit is the only stage that calls tools, and validation already routes a reject or a hold to `reject_log`.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`, printed only on record and replay. It sums the `usage` objects stored in the recording. It does not call the API.

Pending approval. The rates in that file are the published rates for `grok-4.20-0309-non-reasoning`: 1.25 USD per 1,000,000 input tokens and 2.50 USD per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are 2.50 and 5.00. `prompt_tokens` is not split into a cached-input rate, so the whole prompt is billed at the input rate. The sum is quantized to six decimal places, half up. None of these five recordings needed the long-context rate: the method switches on a single prompt, and the printed totals are the whole run.

## Live variance

These replays do not show model variance. The same file feeds the same saved replies, so the score, the token total, and the cost line come from the recording, not from a new sample of the model. A live run is where the wording can change. This file does not report a live-versus-replay score gap. No live API run was made for it, and each recording stores one saved pass. The header of each of the five files is `as_of` 2026-11-01 and model `grok-4.20-0309-non-reasoning`.
