# Replay metrics

Every replay number is from a committed recording, with no API key. Samples, unseen, and the ordered ledger were recorded on 2026-11-01 PT. The loop set was recorded on 2026-11-02 PT with `--as-of 2026-11-01`. A replay creates `inventory.db` from the seed when the file is missing. Each command exited 0. The score, token, cost, call, and repeat figures are the stdout of `scripts/eval.py`. The live API runs are in [Live runs](#live-runs).

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats |
|---|---|---|---|---|---|---|
| Samples | `python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 73608 | 0.102365 | 42 | 1 |
| Unseen | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 86882 | 0.119278 | 49 | 1 |
| Ordered | `python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 33870 | 0.047920 | 17 | 1 |
| Loop set | `python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --replay recordings/loop.jsonl` | 3/3 | 13230 | 0.018188 | 8 | 0 |

The ordered command prints one line per step, then the ledger line. All 11 steps were ok. The ledger line was `ledger 3 rows: invoice_1001.txt, invoice_1011.pdf, invoice_1004.json`, and it was ok.

The unseen scorer checks the outcome plus the primary reason code, or a listed alternate code from the answer key.

Warning matches do not change the exit code. Samples were 19/19 and unseen were 9/9. The ordered command does not print warnings.

No replay call was blocked by the tool cap. No replay result had `tool_cap_hit` or `forced_final_turn`.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`. It sums the `usage` objects stored in the recording. It does not call the API.

The rates are the published rates for `grok-4.20-0309-non-reasoning`: $1.25 per 1,000,000 input tokens and $2.50 per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are $2.50 and $5.00. `prompt_tokens` is not split into a cached-input rate. The sum is quantized to six decimal places, half up. None of these four recordings needed the long-context rate. The loop recording was made on 2026-11-02 PT with `--as-of 2026-11-01`.

## Live runs

These runs were on 2026-11-01 PT, commit `400d7b9`. Each cost is the captured usage priced at the rates above. Wall time is the whole CLI command, end minus start, with the invoices run one after another.

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

Live: 2.85–3.90 s and $0.004923–$0.006428 per invoice. The low time is 57/20. The high time is 82/21. The low cost is $0.098465/20. The high cost is $0.134998/21.

## How often each loop fired

The seven runs above kept only their score and cost lines. The four committed recordings are also live runs: `--record` calls the API and saves every reply. Replaying a recording re-runs the same code on the same replies, so these counts are exact for those runs. `scripts/loop_stats.py` produces them with no API key. The loop set's score is in the table above.

```bash
python3 scripts/loop_stats.py --replay recordings/samples.jsonl
python3 scripts/loop_stats.py --replay recordings/unseen.jsonl --invoices data/extra_invoices
python3 scripts/loop_stats.py --replay recordings/ordered.jsonl --ordered
```

| Loop | Samples (20) | Unseen (21) | Ordered (11) |
|---|---|---|---|
| Audits run | 7 | 8 | 3 |
| Audit send-backs | 0 | 0 | 0 |
| Schema repairs | 0 | 1 | 0 |
| Critiques run | 0 | 1 | 0 |
| Critique holds | 0 | 0 | 0 |
| Fact-guard set-asides | 0 | 2 | 0 |
| Audit tool calls | 42 | 49 | 17 |
| Tool calls that affected the verdict | 0 | 0 | 0 |
| Model retries | 0 | 0 | 0 |

The audit runs only on invoices that validation passed. Every audit in these runs returned PASS, and the model asked for no send-back.

The one schema repair and both set-asides were on `invoice_2106.txt`, the clean $11,500 bill. The critique's first reply failed JSON validation, and the repair turn fixed it. That critique asked for a stricter decision on two claims: the invoice date 2026-04-16 and the due date 2026-05-16 were "after today". Today was 2026-11-01, so the guard set both aside. No concern was left, so the bill stayed an approve and no critique hold was raised. Without the guard, a correct $11,500 approve would have been held.

A tool call counts as affecting the verdict when the audit made it and the verdict was not PASS. None did. As a second check, no tool result disagreed with what code had already checked. No item was uncovered or unknown, no total failed to reconcile, no prior payment or split was found, and no tool returned an error. The tools confirmed the facts the audit was already given. In a single process, validation catches a duplicate or a split before the audit runs. The ledger tool is there for a payment written by another process in between.

One of the seven runs above hit the tool-call cap on `invoice_2106`, and the forced final turn gave the correct outcome. That run was not recorded, so its other counts are not known.
