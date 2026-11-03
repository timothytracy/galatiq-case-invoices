# Replay metrics

Every replay number is from a committed recording, with no API key. Each set was recorded once, live, on 2026-11-02 PT with `--as-of 2026-11-01`. A replay creates `inventory.db` from the seed when the file is missing. The score, token, cost, call, and repeat figures are the stdout of `scripts/eval.py`. The live API runs are in [Live runs](#live-runs).

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats |
|---|---|---|---|---|---|---|
| Samples | `python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 79866 | 0.117968 | 36 | 0 |
| Unseen | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 106322 | 0.155498 | 47 | 0 |
| Ordered | `python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 32572 | 0.050256 | 12 | 0 |
| Loop | `python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --replay recordings/loop.jsonl` | 3/3 | 15577 | 0.022305 | 8 | 0 |
| Fraud | `python3 scripts/eval.py --invoices data/fraud_invoices --answer-key data/fraud_answer_key.json --replay recordings/fraud.jsonl` | 5/5 | 34641 | 0.057444 | 10 | 0 |
| Pass list | `python3 scripts/eval.py --invoices data/passlist_invoices --answer-key data/passlist_answer_key.json --replay recordings/passlist.jsonl` | 5/5 | 14088 | 0.020765 | 5 | 0 |

Each command exited 0. Fraud review held invoice 1004 as `FRAUD_SUSPECTED` on its vendor address, which is its label; an approve is also accepted ([why](ANSWER_KEY.md#rulings-on-1003-and-1004)). The ordered scenario expects 1004 held and a ledger of two rows, and also accepts 1004 paid and a ledger of three. Either way `invoice_1004_revised.json` is a `REVISED_INVOICE` hold. The ledger line was `ledger 2 rows: invoice_1001.txt, invoice_1011.pdf`.

The unseen scorer checks the outcome plus the primary reason code, or a listed alternate code from the answer key.

No replay call was blocked by the tool cap. No replay result had `tool_cap_hit` or `forced_final_turn`.

## What recording these sets changed

These six recordings replace the earlier five because the prompts changed: critique now sees the page, and extraction returns the quote and the action behind an instructions flag. Two first attempts missed a label, and neither label was changed:

- In the first two recordings of the unseen set, Grok flagged 2003's `VOID quote Q-1844…` line as instructions to the system, with that line as its quote. A model-only flag then held a clean invoice. The fix was a structured field: extraction says what the wording asks for (approve, pay, skip the review, ignore the rules, or other), and code accepts a model-only flag only for one of the first four. Grok calls 2003's line `other` and 2134's `skip_review` or `pay`.
- In the next recording, the unaccepted flag still reached the audit prompt, and audit sent 2003 back twice before it passed. Ingestion now clears a flag code did not accept, keeping the quote and action in the record. The unseen set was recorded again with no send-back.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`. It sums the `usage` objects stored in the recording. It does not call the API.

The rates are the published rates for `grok-4.20-0309-non-reasoning` and `grok-4.20-0309-reasoning`, which are the same: $1.25 per 1,000,000 input tokens and $2.50 per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are $2.50 and $5.00. Reasoning tokens are billed as output, so output is the larger of `completion_tokens` and `total_tokens` minus `prompt_tokens`. `prompt_tokens` is not split into a cached-input rate. The sum is quantized to six decimal places, half up. None of these recordings needed the long-context rate.

Fraud review's share of each recording:

| Set | Reviews | Tokens | Reasoning tokens | Cost USD |
|---|---|---|---|---|
| Samples | 7 | 12323 | 5835 | 0.023373 |
| Unseen | 8 | 16504 | 8683 | 0.032318 |
| Ordered | 3 | 5831 | 3036 | 0.011420 |
| Loop | 1 | 1719 | 781 | 0.003224 |
| Fraud | 5 | 13724 | 8649 | 0.028601 |
| Pass list | 1 | 1718 | 785 | 0.003245 |

## Live runs

The six recordings above are live runs. Samples and unseen also ran live three more times on 2026-11-02 PT, without recording, and scored perfectly each time. 1004 was a `FRAUD_SUSPECTED` hold in all four samples runs. The unseen runs used the final code. The samples runs came before the last ingestion change, which only clears an instructions flag code did not accept; the samples recording replays unchanged under it. A run without `--record` does not print a cost, so these rows have none. Wall time is the whole CLI command, with the invoices run one after another.

| Set | Score | Wall s |
|---|---|---|
| samples | 20/20 | 98 |
| samples | 20/20 | 136 |
| samples | 20/20 | 106 |
| unseen | 21/21 | 143 |
| unseen | 21/21 | 134 |
| unseen | 21/21 | 133 |

Per invoice that is 4.90–6.81 s: 98/20 is the low and 143/21 is the high. From the recordings, per-invoice cost is $0.005898 for samples ($0.117968/20) and $0.007405 for unseen ($0.155498/21).

## How often each loop fired

`--record` calls the API and saves every reply. Replaying a recording re-runs the same code on the same replies, so these counts are exact for the recorded runs. `scripts/loop_stats.py` produces them with no API key.

```bash
python3 scripts/loop_stats.py --replay recordings/samples.jsonl
python3 scripts/loop_stats.py --replay recordings/unseen.jsonl --invoices data/extra_invoices
python3 scripts/loop_stats.py --replay recordings/ordered.jsonl --ordered
python3 scripts/loop_stats.py --replay recordings/loop.jsonl --invoices data/loop_invoices
python3 scripts/loop_stats.py --replay recordings/fraud.jsonl --invoices data/fraud_invoices
python3 scripts/loop_stats.py --replay recordings/passlist.jsonl --invoices data/passlist_invoices
```

| Loop | Samples (20) | Unseen (21) | Ordered (11) | Loop (3) | Fraud (5) | Pass list (5) |
|---|---|---|---|---|---|---|
| Fraud reviews run | 7 | 8 | 3 | 1 | 5 | 1 |
| Fraud-review holds | 1 | 0 | 1 | 0 | 3 | 0 |
| Fraud-review holds dropped by code | 0 | 0 | 0 | 0 | 0 | 0 |
| Audits run | 6 | 8 | 2 | 1 | 2 | 1 |
| Audit send-backs | 0 | 0 | 0 | 0 | 0 | 0 |
| Schema repairs | 0 | 1 | 0 | 0 | 0 | 0 |
| Critiques run | 0 | 1 | 0 | 1 | 0 | 0 |
| Critique holds | 0 | 0 | 0 | 0 | 0 | 0 |
| Fact-guard set-asides | 0 | 2 | 0 | 0 | 0 | 0 |
| Audit tool calls | 36 | 47 | 12 | 8 | 10 | 5 |
| Audits with tools whose verdict was not PASS | 0 | 0 | 0 | 0 | 0 | 0 |
| Model retries | 0 | 0 | 0 | 0 | 0 | 0 |
| Outcomes changed by fraud review | 1 | 0 | 1 | 0 | 3 | 0 |
| Outcomes changed by critique | 0 | 0 | 0 | 0 | 0 | 0 |
| Outcomes changed by audit | 0 | 0 | 0 | 0 | 0 | 0 |

An outcome is changed by a step when validation decided one outcome and the final outcome is different, with that step's code as the primary reason (`outcomes_changed` in the script's output). Fraud review runs only on invoices that validation passed. The one hold in samples, and the one in ordered, is 1004. Every fraud-review hold in these runs quoted text that code found on the page, so none was dropped.

Critique and audit changed no outcome in these runs. Critique ran on the two invoices over $10,000 that validation passed (2106 and 2117). On 2106 it raised two concerns that the invoice and due dates were in the future, and the fact guard set both aside because neither date is after 2026-11-01. Audit passed every invoice it saw. In these sets both are a second reader that found nothing code had missed. The unit tests cover what happens when one does.
