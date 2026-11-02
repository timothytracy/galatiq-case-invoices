# Replay metrics

Every number in the replay table is from a replay of a committed recording, run on 2026-11-01 PT with no API key. The score, token, cost, call, and repeat lines are the stdout of `scripts/eval.py`. The tool names, the `check_payment_ledger` counts, and the cap fields are from a second pass over the same recordings on the same day. That pass counted `tool_calls` on the graph result. Its call totals and repeat totals match the eval lines. The live API runs are in [Live runs](#live-runs).

`python3 setup_db.py` was run first. Each command exited 0.

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats | `check_payment_ledger` | Cap or forced turn |
|---|---|---|---|---|---|---|---|---|
| Samples | `XAI_TOOLS=0 python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 34199 | 0.051456 | 0 | 0 | 0 | none |
| Unseen | `XAI_TOOLS=0 python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 39976 | 0.058625 | 0 | 0 | 0 | none |
| Ordered | `XAI_TOOLS=0 python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 17358 | 0.026695 | 0 | 0 | 0 | none |
| Samples, audit tools | `python3 scripts/eval.py --replay recordings/samples_tools.jsonl` | 20/20 | 73608 | 0.102365 | 42 | 1 | 7 | none |
| Unseen, audit tools | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen_tools.jsonl` | 21/21 | 86882 | 0.119278 | 49 | 1 | 8 | none |
| Ordered, audit tools | `python3 scripts/eval.py --ordered --replay recordings/ordered_tools.jsonl` | 11 steps, all ok | 33870 | 0.047920 | 17 | 1 | 3 | none |

The ordered command does not print `score`. It prints one line per step, then the ledger line, then the tool line, then the token line. All 11 steps were `ok`. The ledger line was `ledger 3 rows: invoice_1001.txt, invoice_1011.pdf, invoice_1004.json`, matching the expected files, and it was `ok`.

Warning matches, which do not change the exit code: samples 19/19, unseen 9/9, samples with the audit tools 19/19, unseen with the audit tools 9/9. Those are the sums of the `warnings=` fields on the eval lines. The ordered command does not print warnings.

## Where the tool columns come from

`scripts/eval.py` prints `tools calls=N repeats=K`. Repeats are included in N. It does not print tool names, `check_payment_ledger` counts, or cap fields.

The second pass, same recordings, counted each logged call:

- Samples and unseen and ordered: no tool calls, so no ledger calls and no cap fields.
- Samples with the audit tools: `check_payment_ledger` 7, `lookup_item` 16, `recompute_totals` 7, `get_policy_rule` 12. Those add to 42, the eval call count. The 7 ledger calls are one each on `invoice_1001.txt`, `invoice_1004.json`, `invoice_1006.csv`, `invoice_1010.txt`, `invoice_1011.pdf`, `invoice_1011.txt`, and `invoice_1015.csv`. Those are the invoices whose eval line had `tools` greater than 0. The one repeat is `get_policy_rule` on round 2 of `invoice_1004.json`. The eval line for that file is `tools=6`, and 6 is the length of its call list, including the repeat.
- Unseen with the audit tools: `check_payment_ledger` 8, `lookup_item` 19, `recompute_totals` 8, `get_policy_rule` 14. Those add to 49. The 8 ledger calls are one each on `invoice_2003.txt`, `invoice_2008.txt`, `invoice_2101.txt`, `invoice_2102.csv`, `invoice_2103.csv`, `invoice_2104.json`, `invoice_2105.txt`, and `invoice_2106.txt`. The one repeat is `get_policy_rule` on round 2 of `invoice_2008.txt`. That file's eval line is `tools=8`.
- Ordered with the audit tools, counted from that replay's printed stage logs: `check_payment_ledger` 3, `lookup_item` 6, `recompute_totals` 3, `get_policy_rule` 5. Those add to 17, the eval call count. The one repeat is `get_policy_rule` on round 2 of `invoice_1004.json`. No call was blocked and no result had `tool_cap_hit` or `forced_final_turn`.

No call was logged with error `blocked: tool_cap`. No audit result had `tool_cap_hit` or `forced_final_turn`.

Rejects and holds that stop at validation had `tools=0` on the eval line. Audit is the only stage that calls tools, and validation already routes a reject or a hold to `reject_log`.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`, printed only on record and replay. It sums the `usage` objects stored in the recording. It does not call the API.

Pending approval. The rates in that file are the published rates for `grok-4.20-0309-non-reasoning`: 1.25 USD per 1,000,000 input tokens and 2.50 USD per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are 2.50 and 5.00. `prompt_tokens` is not split into a cached-input rate, so the whole prompt is billed at the input rate. The sum is quantized to six decimal places, half up. None of these six recordings needed the long-context rate: the method switches on a single prompt, and the printed totals are the whole run.

## Live runs

These figures are from `live_runs_evidence.md` (sha256 `d5d550b820ac81efe811844da1cad8fa84b4ea352440800ba810153c47980eff`), the box logs from 2026-11-01 PT. A replay of a saved file does not move. The live passes are where the call counts moved. Scores did not.

### At `6ee5d927`

Nine runs, each exit 0, one attempt, no payments database. No cost was logged for any of them. The log reports no tool cap hits.

| Run | Set | Audit tools | Start–end (PT) | Score |
|---|---|---|---|---|
| 1 | samples | no | 17:57:27–17:58:14 | 20/20 |
| 2 | samples | no | 17:58:14–17:59:09 | 20/20 |
| 3 | samples | no | 17:59:09–17:59:56 | 20/20 |
| 4 | unseen | no | 17:59:56–18:00:58 | 21/21 |
| 5 | unseen | no | 18:00:58–18:02:11 | 21/21 |
| 6 | unseen | no | 18:02:11–18:03:15 | 21/21 |
| 7 | ordered | no | 18:03:15–18:03:48 | 11/11 |
| 8 | samples | yes | 18:03:48–18:09:12 | 20/20 |
| 9 | unseen | yes | 18:09:12–18:10:39 | 21/21 |

Runs 1–7 are the log's tools-off lines. Runs 8 and 9 are `tools_samples` and `tools_unseen`.

### At `a4c0e43d`

Nine runs the same day, each exit 0, one attempt. Cost was logged. The log sums usage at 2.00 USD per 1,000,000 uncached input tokens, 0.20 USD per 1,000,000 cached input tokens, and 6.00 USD per 1,000,000 output tokens. That is not the replay rate in [Cost method](#cost-method).

| Run | Set | Audit tools | Start (PT) | Score | Calls / repeats | Ledger calls | Cap / forced | Cost |
|---|---|---|---|---|---|---|---|---|
| 1 | samples | yes | 18:37:25 | 20/20 | 44/3 | 7 | 0/0 | 0.123 |
| 2 | samples | yes | 18:39:04 | 20/20 | 38/1 | 7 | 0/0 | 0.101 |
| 3 | samples | yes | 18:40:18 | 20/20 | 40/1 | 7 | 0/0 | 0.108 |
| 4 | unseen | yes | 18:41:28 | 21/21 | 49/1 | 8 | 0/0 | 0.113 |
| 5 | unseen | yes | 18:42:39 | 21/21 | 49/1 | 8 | 0/0 | 0.105 |
| 6 | unseen | yes | 18:43:50 | 21/21 | 51/0 | 8 | 0/0 | 0.112 |
| 7 | ordered | yes | 18:45:03 | 11/11 steps ok | 16/1 | 3 | 0/0 | 0.042 |
| 8 | samples | no | 18:45:39 | 20/20 | 0/0 | 0 | not logged | 0.066 |
| 9 | unseen | no | 18:46:31 | 21/21 | 0/0 | 0 | not logged | 0.074 |

The ordered line also records 3 ledger rows, `1001.txt`, `1011.pdf`, and `1004.json`, matching the expected files. The cap column is `0/0` on the audit-tool runs. On the two runs without the audit tools the log has a dash, so cap and forced turn were not logged there. The log says `check_payment_ledger` was called once in every audit that ran (7/7, 8/8, and 3/3), always with arguments `{}`, and every result was `already_paid` false, `same_vendor_paid_in_window` 0.00, and `over_split_threshold` false. The ordered duplicates were held at validation and never reached audit, so the tool did not change a decision.

The same cost method, on main `22acca19`, is logged as samples 0.099, 0.085, and 0.097 (the log's average 0.093) and unseen 0.126, 0.140, and 0.114 (average 0.127). That log gives the audit-tool averages here as 0.111 and 0.110. It does not give clock times for those earlier six. Its tools-off pair is 0.072 and 0.076. It says no invoice changed outcome, reason code, or matched-warning count against those earlier audit-tool runs. One tools-off difference is logged: `invoice_1005.json` warnings 1/1 on run 8 above, and 0/1 on `gc41_7`.

The six committed recordings include the live passes in that file, recorded on 2026-11-01 PT. Each header stores that day as `as_of` 2026-11-01. Samples, unseen, and ordered were recorded at `e3da9725`. The two earlier audit-tool files were recorded at `282a959d`. `ordered_tools.jsonl` is the default-settings ordered recording. Their scores, token totals, and costs are the replay table. The file lists ordered as 11/11.

Scores on the live runs match those recordings: samples 20/20, unseen 21/21, ordered 11/11. The call counts on the audit-tool passes do not. The samples recording is 42 calls and 1 repeat. The three passes above are 44/3, 38/1, and 40/1. The unseen recording is 49 calls and 1 repeat. The three passes above are 49/1, 49/1, and 51/0. Cost is absent for every run at `6ee5d927`. A dollar figure in this section uses the cached-input method. A dollar figure in the replay table uses the rate in Cost method. They are not two prints of one formula.
