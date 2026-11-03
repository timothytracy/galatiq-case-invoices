# Replay metrics

Every replay number is from a committed recording, with no API key. Each set was recorded once, live, on 2026-11-02 PT with `--as-of 2026-11-01`. A replay creates `inventory.db` from the seed when the file is missing. The score, token, cost, call, and repeat figures are the stdout of `scripts/eval.py`. The live API runs are in [Live runs](#live-runs).

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats |
|---|---|---|---|---|---|---|
| Samples | `python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 78797 | 0.117528 | 38 | 0 |
| Unseen | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 112186 | 0.163493 | 56 | 0 |
| Ordered | `python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 31413 | 0.048786 | 12 | 0 |
| Loop | `python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --replay recordings/loop.jsonl` | 3/3 | 15073 | 0.021749 | 8 | 0 |
| Fraud | `python3 scripts/eval.py --invoices data/fraud_invoices --answer-key data/fraud_answer_key.json --replay recordings/fraud.jsonl` | 5/5 | 31908 | 0.051323 | 10 | 0 |

Each command exited 0. Fraud review held invoice 1004 as `FRAUD_SUSPECTED` on its vendor address, which is its label; an approve is also accepted ([why](ANSWER_KEY.md#rulings-on-1003-and-1004)). Under the strict-approve label 1004 had when these sets were recorded, samples scored 19/20 and the ordered scenario failed; the recordings were not changed. The ordered scenario expects 1004 held and a ledger of two rows, and also accepts 1004 paid and a ledger of three. Either way `invoice_1004_revised.json` is a `REVISED_INVOICE` hold. The ledger line was `ledger 2 rows: invoice_1001.txt, invoice_1011.pdf`.

The unseen scorer checks the outcome plus the primary reason code, or a listed alternate code from the answer key.

No replay call was blocked by the tool cap. No replay result had `tool_cap_hit` or `forced_final_turn`.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`. It sums the `usage` objects stored in the recording. It does not call the API.

The rates are the published rates for `grok-4.20-0309-non-reasoning` and `grok-4.20-0309-reasoning`, which are the same: $1.25 per 1,000,000 input tokens and $2.50 per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are $2.50 and $5.00. Reasoning tokens are billed as output, so output is the larger of `completion_tokens` and `total_tokens` minus `prompt_tokens`. `prompt_tokens` is not split into a cached-input rate. The sum is quantized to six decimal places, half up. None of these recordings needed the long-context rate.

Fraud review's share of each recording:

| Set | Reviews | Tokens | Reasoning tokens | Cost USD |
|---|---|---|---|---|
| Samples | 7 | 13217 | 6707 | 0.025608 |
| Unseen | 8 | 16692 | 8883 | 0.032788 |
| Ordered | 3 | 6026 | 3239 | 0.011908 |
| Loop | 1 | 1844 | 895 | 0.003536 |
| Fraud | 5 | 11647 | 6549 | 0.023409 |

## Live runs

The five recordings above are live runs. The fraud set also ran live three more times on 2026-11-02 PT, without recording, and scored 5/5 each time: 2121, 2124, and 2125 were `FRAUD_SUSPECTED` holds, and 2123 and 2126 were paid. Those runs kept only their score lines.

Before fraud review, main's live runs were on 2026-11-01 PT, commit `400d7b9`. Each cost is the captured usage priced at the rates above. Wall time is the whole CLI command, end minus start, with the invoices run one after another.

| Set | Score | Cost USD | Wall s |
|---|---|---|---|
| samples | 20/20 | 0.102653 | 62 |
| samples | 20/20 | 0.098994 | 58 |
| samples | 20/20 | 0.098465 | 57 |
| unseen | 21/21 | 0.134998 | 82 |
| unseen | 21/21 | 0.119826 | 63 |
| unseen | 21/21 | 0.118861 | 62 |
| ordered | 11 steps ok, ledger matches | 0.047564 | 28 |

One unseen run hit the tool-call cap on `invoice_2106`. The forced final turn still gave the correct outcome.

Per-invoice time is wall seconds divided by the invoice count, to two decimal places. Per-invoice cost is the run cost divided by the invoice count, half up to six decimal places. Samples are 20 invoices. Unseen are 21.

Main, live: 2.85–3.90 s and $0.004923–$0.006428 per invoice. The low time is 57/20. The high time is 82/21. The low cost is $0.098465/20. The high cost is $0.134998/21.

## How often each loop fired

`--record` calls the API and saves every reply. Replaying a recording re-runs the same code on the same replies, so these counts are exact for the recorded runs. `scripts/loop_stats.py` produces them with no API key.

```bash
python3 scripts/loop_stats.py --replay recordings/samples.jsonl
python3 scripts/loop_stats.py --replay recordings/unseen.jsonl --invoices data/extra_invoices
python3 scripts/loop_stats.py --replay recordings/ordered.jsonl --ordered
python3 scripts/loop_stats.py --replay recordings/loop.jsonl --invoices data/loop_invoices
python3 scripts/loop_stats.py --replay recordings/fraud.jsonl --invoices data/fraud_invoices
```

| Loop | Samples (20) | Unseen (21) | Ordered (11) | Loop (3) | Fraud (5) |
|---|---|---|---|---|---|
| Fraud reviews run | 7 | 8 | 3 | 1 | 5 |
| Fraud-review holds | 1 | 0 | 1 | 0 | 3 |
| Fraud-review holds dropped by code | 0 | 0 | 0 | 0 | 0 |
| Audits run | 6 | 9 | 2 | 1 | 2 |
| Audit send-backs | 0 | 1 | 0 | 0 | 0 |
| Schema repairs | 0 | 1 | 0 | 0 | 0 |
| Critiques run | 0 | 1 | 0 | 1 | 0 |
| Critique holds | 0 | 0 | 0 | 0 | 0 |
| Fact-guard set-asides | 0 | 2 | 0 | 0 | 0 |
| Audit tool calls | 38 | 56 | 12 | 8 | 10 |
| Audits with tools whose verdict was not PASS | 0 | 1 | 0 | 0 | 0 |
| Model retries | 0 | 0 | 0 | 0 | 0 |

Fraud review runs only on invoices that validation passed. The one hold in samples, and the one in ordered, is 1004. Every fraud-review hold in these runs quoted text that code found on the page, so none was dropped.

The one audit in unseen that was not PASS is the send-back. The next pass gave the labeled outcome. No tool result disagreed with what code had already checked.
