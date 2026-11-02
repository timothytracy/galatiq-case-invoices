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

The rates in that file are the published rates for `grok-4.20-0309-non-reasoning`: 1.25 USD per 1,000,000 input tokens and 2.50 USD per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are 2.50 and 5.00. `prompt_tokens` is not split into a cached-input rate, so the whole prompt is billed at the input rate. The sum is quantized to six decimal places, half up. None of these six recordings needed the long-context rate: the method switches on a single prompt, and the printed totals are the whole run.

## Live runs

These runs were on 2026-11-01 PT, commit `400d7b9`, request building identical to main. Each cost is the captured usage priced at the rates in [Cost method](#cost-method), the same rates `recording.py` uses. Wall time is the whole CLI command, end minus start, with the invoices run one after another. A replay of a saved file does not move. These are separate live calls.

| Set | Tools | Score | Cost USD | Wall s |
|---|---|---|---|---|
| samples | on | 20/20 | 0.102653 | 62 |
| samples | on | 20/20 | 0.098994 | 58 |
| samples | on | 20/20 | 0.098465 | 57 |
| unseen | on | 21/21 | 0.134998 | 82 |
| unseen | on | 21/21 | 0.119826 | 63 |
| unseen | on | 21/21 | 0.118861 | 62 |
| ordered | on | 11 steps ok, ledger matches | 0.047564 | 28 |
| samples | off (`XAI_TOOLS=0`) | 20/20 | 0.051425 | 48 |
| unseen | off (`XAI_TOOLS=0`) | 21/21 | 0.059249 | 54 |

One unseen tools-on run hit the tool-call cap on `invoice_2106`. The forced final turn still gave the correct outcome.

Per-invoice cost is the run cost divided by the invoice count. Samples are 20 invoices. Unseen are 21. Each quotient is half up to six decimal places, the same quantization `recording.py` uses for a run total. The rates are in [Cost method](#cost-method): $1.25 per million input tokens and $2.50 per million output tokens when the prompt is under 200,000 tokens, and $2.50 and $5.00 at or above that. `prompt_tokens` is not split into a cached-input rate.

Live, tools on:

- $0.102653 / 20 = $0.005133
- $0.098994 / 20 = $0.004950
- $0.098465 / 20 = $0.004923
- $0.134998 / 21 = $0.006428
- $0.119826 / 21 = $0.005706
- $0.118861 / 21 = $0.005660

That range is $0.004923 to $0.006428 per invoice.

Live, tools off:

- $0.051425 / 20 = $0.002571
- $0.059249 / 21 = $0.002821

That range is $0.002571 to $0.002821 per invoice.

The ordered live run is 11 steps, not 11 invoices: $0.047564 / 11 = $0.004324 per step.

Replay costs use the same division on the usage stored in the committed recordings:

- Samples, tools on: $0.102365 / 20 = $0.005118
- Unseen, tools on: $0.119278 / 21 = $0.005680
- Samples, tools off: $0.051456 / 20 = $0.002573
- Unseen, tools off: $0.058625 / 21 = $0.002792
- Ordered, tools on: $0.047920 / 11 = $0.004356 per step
- Ordered, tools off: $0.026695 / 11 = $0.002427 per step

Per-invoice time is wall seconds divided by the invoice count. The ordered row is 11 steps, so that quotient is per step. The quotients below are rounded to two decimal places.

Live, tools on, samples (20 invoices):

- 62 / 20 = 3.10 s
- 58 / 20 = 2.90 s
- 57 / 20 = 2.85 s

That range is 2.85 s to 3.10 s per invoice.

Live, tools on, unseen (21 invoices):

- 82 / 21 = 3.90 s
- 63 / 21 = 3.00 s
- 62 / 21 = 2.95 s

That range is 2.95 s to 3.90 s per invoice.

Live, tools off:

- Samples: 48 / 20 = 2.40 s per invoice
- Unseen: 54 / 21 = 2.57 s per invoice

Ordered, tools on: 28 / 11 = 2.55 s per step.
