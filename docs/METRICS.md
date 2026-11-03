# Replay metrics

Every replay number is from a committed recording, run on 2026-11-02 PT, with no API key. A replay creates `inventory.db` from the seed when the file is missing. Each command exited 0 except the warning set, which exits 1 at 1/3. The score, token, cost, call, and repeat figures are the stdout of `scripts/eval.py`. The live API runs are in [Live runs](#live-runs).

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats |
|---|---|---|---|---|---|---|
| Samples | `python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 79566 | 0.109865 | 44 | 1 |
| Unseen | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 93549 | 0.127723 | 50 | 1 |
| Ordered | `python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 36459 | 0.051250 | 19 | 2 |
| Loop | `python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --replay recordings/loop.jsonl` | 3/3 | 15490 | 0.021231 | 8 | 1 |
| Warning | `python3 scripts/eval.py --invoices data/warning_invoices --answer-key data/warning_answer_key.json --replay recordings/warning.jsonl` | 1/3 | 27870 | 0.036979 | 14 | 1 |

The ordered command prints one line per step, then the ledger line. All 11 steps were ok. The ledger line was `ledger 3 rows: invoice_1001.txt, invoice_1011.pdf, invoice_1004.json`, and it was ok.

The unseen scorer checks the outcome plus the primary reason code, or a listed alternate code from the answer key. It also accepts a listed alternate outcome and reason pair: 2103 may be an approve or an `AUDIT_ESCALATED` hold. It was an approve in every run.

The warning set's two misses are 2121 and 2122. Both are labeled `AUDIT_ESCALATED` holds, and the audit passed both, so both were paid. 2123, labeled approve, was paid. See [the warning answer key](WARNING_ANSWER_KEY.md).

Warning matches do not change the exit code. Samples were 19/19, unseen 9/9, and the warning set 3/3. The loop set has no warnings, and the ordered command does not print warnings.

No replay call was blocked by the tool cap. No replay result had `tool_cap_hit` or `forced_final_turn`.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`. It sums the `usage` objects stored in the recording. It does not call the API.

The rates are the published rates for `grok-4.20-0309-non-reasoning`: $1.25 per 1,000,000 input tokens and $2.50 per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are $2.50 and $5.00. `prompt_tokens` is not split into a cached-input rate. The sum is quantized to six decimal places, half up. None of these recordings needed the long-context rate.

## Live runs

These runs were on 2026-11-02 PT, commit `07e1ddb`, with the audit prompt that lets the audit hold on a quoted warning. A live run without `--record` prints no cost line, so only score and wall time are known. Wall time is the whole CLI command, end minus start, with the invoices run one after another.

| Set | Score | Wall s |
|---|---|---|
| samples | 19/20 | 86 |
| samples | 20/20 | 74 |
| samples | 20/20 | 87 |
| unseen | 21/21 | 75 |
| unseen | 21/21 | 85 |
| unseen | 21/21 | 92 |
| ordered | 11 steps ok, ledger matches | 56 |
| loop | 3/3 | 15 |
| warning | 1/3 | 19 |
| warning | 1/3 | 18 |
| warning | 1/3 | 20 |

The samples miss was `invoice_1009.json`. Extraction read it so that validation rejected it for `MISSING_FIELD` instead of `BAD_QUANTITY`. It was still a reject and unpaid. The audit does not run on a reject, so the prompt change did not cause it. The first samples recording that day had the same miss, and samples was recorded again.

The recordings are live runs too: `--record` calls the API and saves every reply. Their wall times were samples 69 s for the 19/20 attempt and 78 s for the kept one, unseen 81 s, ordered 38 s, loop 13 s, and warning 18 s.

Per-invoice time is wall seconds divided by the invoice count, to two decimal places. Per-invoice cost is the run cost divided by the invoice count, half up to six decimal places. Samples are 20 invoices. Unseen are 21.

Live: 3.57–4.38 s per invoice. The low time is 75/21. The high time is 92/21. Cost, from the recorded samples and unseen runs: $0.005493–$0.006082 per invoice. The low cost is $0.109865/20. The high cost is $0.127723/21.

These are slower and cost more than the runs on 2026-11-01 PT at commit `400d7b9`, which were 2.85–3.90 s and $0.004923–$0.006428 per invoice. The audit prompt is longer, the audit made a few more tool calls, and the API's speed varies by day, so this change is not measured apart from the others.

## How often each loop fired

The live runs above kept only their score and time. The committed recordings are also live runs, and replaying a recording re-runs the same code on the same replies, so these counts are exact for those runs. `scripts/loop_stats.py` produces them with no API key.

```bash
python3 scripts/loop_stats.py --replay recordings/samples.jsonl
python3 scripts/loop_stats.py --replay recordings/unseen.jsonl --invoices data/extra_invoices
python3 scripts/loop_stats.py --replay recordings/ordered.jsonl --ordered
python3 scripts/loop_stats.py --replay recordings/loop.jsonl --invoices data/loop_invoices
python3 scripts/loop_stats.py --replay recordings/warning.jsonl --invoices data/warning_invoices
```

| Loop | Samples (20) | Unseen (21) | Ordered (11) | Loop (3) | Warning (3) |
|---|---|---|---|---|---|
| Audits run | 7 | 8 | 3 | 1 | 3 |
| Audit send-backs | 0 | 0 | 0 | 0 | 0 |
| Schema repairs | 0 | 1 | 0 | 1 | 0 |
| Critiques run | 0 | 1 | 0 | 1 | 0 |
| Critique holds | 0 | 0 | 0 | 0 | 0 |
| Fact-guard set-asides | 0 | 2 | 0 | 1 | 0 |
| Audit tool calls | 44 | 50 | 19 | 8 | 14 |
| Tool calls that affected the verdict | 0 | 0 | 0 | 0 | 0 |
| Audit warning holds | 0 | 0 | 0 | 0 | 0 |
| Model retries | 0 | 0 | 0 | 0 | 0 |

The audit runs only on invoices that validation passed. Every audit in these runs returned PASS, and the model asked for no send-back and no warning hold.

In unseen, the one schema repair and both set-asides were on `invoice_2106.txt`, the clean $11,500 bill. The critique's first reply failed JSON validation, and the repair turn fixed it. That critique asked for a stricter decision on two claims: the invoice date 2026-04-16 and the due date 2026-05-16 were "after today". Today was 2026-11-01, so the guard set both aside. No concern was left, so the bill stayed an approve and no critique hold was raised. Without the guard, a correct $11,500 approve would have been held. The loop set's repair and set-aside were the same pattern on `invoice_2117.txt`, whose due date 2026-10-15 the critique called "after today".

A tool call counts as affecting the verdict when the audit made it and the verdict was not PASS. None did. As a second check, no tool result disagreed with what code had already checked. No item was uncovered or unknown, no total failed to reconcile, no prior payment or split was found, and no tool returned an error. The tools confirmed the facts the audit was already given. In a single process, validation catches a duplicate or a split before the audit runs. The ledger tool is there for a payment written by another process in between.
