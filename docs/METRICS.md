# Replay metrics

Every replay number is from a committed recording, run on 2026-11-01 PT, with no API key. `python3 setup_db.py` was run first. Each command exited 0. The score, token, cost, call, and repeat figures are the stdout of `scripts/eval.py`. The live API runs are in [Live runs](#live-runs).

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats |
|---|---|---|---|---|---|---|
| Samples, tools off | `XAI_TOOLS=0 python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 34199 | 0.051456 | 0 | 0 |
| Unseen, tools off | `XAI_TOOLS=0 python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 39976 | 0.058625 | 0 | 0 |
| Ordered, tools off | `XAI_TOOLS=0 python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 17358 | 0.026695 | 0 | 0 |
| Samples, tools on | `python3 scripts/eval.py --replay recordings/samples_tools.jsonl` | 20/20 | 73608 | 0.102365 | 42 | 1 |
| Unseen, tools on | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen_tools.jsonl` | 21/21 | 86882 | 0.119278 | 49 | 1 |
| Ordered, tools on | `python3 scripts/eval.py --ordered --replay recordings/ordered_tools.jsonl` | 11 steps, all ok | 33870 | 0.047920 | 17 | 1 |

The ordered command prints one line per step, then the ledger line. All 11 steps were ok. The ledger line was `ledger 3 rows: invoice_1001.txt, invoice_1011.pdf, invoice_1004.json`, and it was ok.

Warning matches do not change the exit code. Samples were 19/19 and unseen were 9/9, with the audit tools and without them. The ordered command does not print warnings.

No replay call was blocked by the tool cap. No replay result had `tool_cap_hit` or `forced_final_turn`.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`. It sums the `usage` objects stored in the recording. It does not call the API.

The rates are the published rates for `grok-4.20-0309-non-reasoning`: $1.25 per 1,000,000 input tokens and $2.50 per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are $2.50 and $5.00. `prompt_tokens` is not split into a cached-input rate. The sum is quantized to six decimal places, half up. None of these six recordings needed the long-context rate.

## Live runs

These runs were on 2026-11-01 PT, commit `400d7b9`. Each cost is the captured usage priced at the rates above. Wall time is the whole CLI command, end minus start, with the invoices run one after another.

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

Per-invoice time is wall seconds divided by the invoice count, to two decimal places. Per-invoice cost is the run cost divided by the invoice count, half up to six decimal places. Samples are 20 invoices. Unseen are 21.

Live, tools on: 2.85–3.90 s and $0.004923–$0.006428 per invoice. The low time is 57/20. The high time is 82/21. The low cost is $0.098465/20. The high cost is $0.134998/21.

Live, tools off: 2.40–2.57 s and $0.002571–$0.002821 per invoice. The times are 48/20 and 54/21. The costs are $0.051425/20 and $0.059249/21.
