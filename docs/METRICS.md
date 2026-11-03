# Replay metrics

Every replay number is from a committed recording, with no API key. Each set was recorded live on 2026-11-02 PT with `--as-of 2026-11-01`. A replay creates `inventory.db` from the seed when the file is missing. The score, token, cost, call, and repeat figures are the stdout of `scripts/eval.py`. The live API runs are in [Live runs](#live-runs).

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats |
|---|---|---|---|---|---|---|
| Samples | `python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 83482 | 0.124295 | 36 | 0 |
| Unseen | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 115807 | 0.173696 | 50 | 1 |
| Ordered | `python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 42599 | 0.063823 | 18 | 1 |
| Loop | `python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --replay recordings/loop.jsonl` | 3/3 | 18318 | 0.028535 | 8 | 0 |
| Fraud | `python3 scripts/eval.py --invoices data/fraud_invoices --answer-key data/fraud_answer_key.json --replay recordings/fraud.jsonl` | 5/5 | 32968 | 0.052608 | 9 | 0 |
| Pass list | `python3 scripts/eval.py --invoices data/passlist_invoices --answer-key data/passlist_answer_key.json --replay recordings/passlist.jsonl` | 5/5 | 14505 | 0.021410 | 5 | 0 |
| Judgment | `python3 scripts/eval.py --invoices data/judgment_invoices --answer-key data/judgment_answer_key.json --replay recordings/judgment.jsonl` | 6/7 | 142695 | 0.218964 | 52 | 0 |

The first six commands exited 0. Judgment exits 1 at 6/7 and is not a CI step; its five live runs are in [Judgment set](#judgment-set). Fraud review held invoice 1004 as `FRAUD_SUSPECTED` on its vendor address in the samples recording, which is its label; an approve is also accepted ([why](ANSWER_KEY.md#rulings-on-1003-and-1004)). The ordered scenario expects 1004 held and a ledger of two rows, and also accepts 1004 paid and a ledger of three. In this ordered recording fraud review passed 1004, so the ledger line was `ledger 3 rows: invoice_1001.txt, invoice_1011.pdf, invoice_1004.json`. Either way `invoice_1004_revised.json` is a `REVISED_INVOICE` hold.

The unseen scorer checks the outcome plus the primary reason code, or a listed alternate code from the answer key.

Outside the judgment set, no replay call was blocked by the tool cap and no result had `tool_cap_hit` or `forced_final_turn`. In the judgment recording audit hit the cap twice, on 2140 and 2144, after critique had already held both.

## What recording these sets changed

Unseen, loop, and judgment were then recorded once more when critique moved to `grok-4.20-0309-reasoning`, the model fraud review uses. Their critique calls carry a new model, so the old lines no longer matched. Unseen and loop matched their labels on the first recording. Samples, ordered, fraud, and pass list ran no critique and still replay unchanged.

The six earlier sets were recorded again because the critique and audit prompts changed: both now ask for a quote and an innocent reading, and both compare what is billed with what the page says was ordered, delivered, priced, or returned. Samples, ordered, loop, fraud, and pass list matched their labels on the first recording. Unseen did not, and no label was changed:

- In the first unseen recording under the new prompts, audit escalated 2003 citing `REVISED_INVOICE` and 2105 citing `FUTURE_DATE`. Neither code was a finding on either invoice, and a cited code skipped the quote gate. Audit now keeps a cited code only when code found it on this invoice; any other citation is logged as `cited_code_not_found`, and an escalation left with no code and no quote passes. That recording replays at 21/21 under the change, which touches no prompt.

The earlier recordings, made when critique first saw the page and extraction began returning the quote and the action behind an instructions flag, had two first attempts that missed a label:

- In the first two recordings of the unseen set, Grok flagged 2003's `VOID quote Q-1844…` line as instructions to the system, with that line as its quote. A model-only flag then held a clean invoice. The fix was a structured field: extraction says what the wording asks for (approve, pay, skip the review, ignore the rules, or other), and code accepts a model-only flag only for one of the first four. Grok calls 2003's line `other` and 2134's `skip_review` or `pay`.
- In the next recording, the unaccepted flag still reached the audit prompt, and audit sent 2003 back twice before it passed. Ingestion now clears a flag code did not accept, keeping the quote and action in the record. The unseen set was recorded again with no send-back.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`. It sums the `usage` objects stored in the recording. It does not call the API.

The rates are the published rates for `grok-4.20-0309-non-reasoning` and `grok-4.20-0309-reasoning`, which are the same: $1.25 per 1,000,000 input tokens and $2.50 per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are $2.50 and $5.00. Reasoning tokens are billed as output, so output is the larger of `completion_tokens` and `total_tokens` minus `prompt_tokens`. `prompt_tokens` is not split into a cached-input rate. The sum is quantized to six decimal places, half up. None of these recordings needed the long-context rate.

Critique and fraud review call `grok-4.20-0309-reasoning`; extraction and audit call the default model.

Critique's share, counting schema repairs: unseen 1 call, 5149 tokens, 3807 reasoning, $0.011306; loop 1 call, 3470 tokens, 2144 reasoning, $0.007096; judgment 7 calls (6 critiques and 1 repair), 28123 tokens, 15412 reasoning, $0.055492.

Fraud review's share of each recording:

| Set | Reviews | Tokens | Reasoning tokens | Cost USD |
|---|---|---|---|---|
| Samples | 7 | 13215 | 6692 | 0.025603 |
| Unseen | 8 | 16336 | 8490 | 0.031898 |
| Ordered | 3 | 6060 | 3292 | 0.011993 |
| Loop | 1 | 1728 | 778 | 0.003246 |
| Fraud | 5 | 11447 | 6319 | 0.022909 |
| Pass list | 1 | 1754 | 804 | 0.003335 |
| Judgment | 7 | 13423 | 6587 | 0.025758 |

## Live runs

The seven recordings above are live runs. Before the critique and audit prompt change, samples and unseen also ran live three more times on 2026-11-02 PT, without recording, and scored perfectly each time. 1004 was a `FRAUD_SUSPECTED` hold in all four samples runs. The unseen runs used the final code. The samples runs came before the last ingestion change, which only clears an instructions flag code did not accept; the samples recording replays unchanged under it. A run without `--record` does not print a cost, so these rows have none. Wall time is the whole CLI command, with the invoices run one after another.

| Set | Score | Wall s |
|---|---|---|
| samples | 20/20 | 98 |
| samples | 20/20 | 136 |
| samples | 20/20 | 106 |
| unseen | 21/21 | 143 |
| unseen | 21/21 | 134 |
| unseen | 21/21 | 133 |

Per invoice that is 4.90–6.81 s: 98/20 is the low and 143/21 is the high. From the current recordings, per-invoice cost is $0.006215 for samples ($0.124295/20) and $0.008271 for unseen ($0.173696/21).

## How often each loop fired

`--record` calls the API and saves every reply. Replaying a recording re-runs the same code on the same replies, so these counts are exact for the recorded runs. `scripts/loop_stats.py` produces them with no API key.

```bash
python3 scripts/loop_stats.py --replay recordings/samples.jsonl
python3 scripts/loop_stats.py --replay recordings/unseen.jsonl --invoices data/extra_invoices
python3 scripts/loop_stats.py --replay recordings/ordered.jsonl --ordered
python3 scripts/loop_stats.py --replay recordings/loop.jsonl --invoices data/loop_invoices
python3 scripts/loop_stats.py --replay recordings/fraud.jsonl --invoices data/fraud_invoices
python3 scripts/loop_stats.py --replay recordings/passlist.jsonl --invoices data/passlist_invoices
python3 scripts/loop_stats.py --replay recordings/judgment.jsonl --invoices data/judgment_invoices
```

| Loop | Samples (20) | Unseen (21) | Ordered (11) | Loop (3) | Fraud (5) | Pass list (5) | Judgment (7) |
|---|---|---|---|---|---|---|---|
| Fraud reviews run | 7 | 8 | 3 | 1 | 5 | 1 | 7 |
| Fraud-review holds | 1 | 0 | 0 | 0 | 3 | 0 | 0 |
| Fraud-review holds dropped by code | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Audits run | 6 | 8 | 3 | 1 | 2 | 1 | 7 |
| Audit send-backs | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Schema repairs | 0 | 1 | 0 | 0 | 0 | 0 | 3 |
| Critiques run | 0 | 1 | 0 | 1 | 0 | 0 | 6 |
| Critique holds | 0 | 0 | 0 | 0 | 0 | 0 | 3 |
| Critique set-asides (fact guard and quote gate) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Audit set-asides (fact guard, quote gate, unfound citations) | 0 | 7 | 0 | 0 | 0 | 0 | 5 |
| Audit tool calls | 36 | 50 | 18 | 8 | 9 | 5 | 52 |
| Audits with tools whose verdict was not PASS | 0 | 0 | 0 | 0 | 0 | 0 | 3 |
| Model retries | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Outcomes changed by fraud review | 1 | 0 | 0 | 0 | 3 | 0 | 0 |
| Outcomes changed by critique | 0 | 0 | 0 | 0 | 0 | 0 | 3 |
| Outcomes changed by audit | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

An outcome is changed by a step when validation decided one outcome and the final outcome is different, with that step's code as the primary reason (`outcomes_changed` in the script's output). Fraud review runs only on invoices that validation passed. The one hold in samples is 1004. Every fraud-review hold in these runs quoted text that code found on the page, so none was dropped.

Outside the judgment set, critique and audit changed no outcome. Critique ran on the two invoices over $10,000 that validation passed (2106 and 2117) and raised nothing that held. Audit's seven set-asides in unseen are four future-date claims on dates before 2026-11-01, a `FUTURE_DATE` citation on 2105 that code did not find, and an escalation on each of 2003 and 2105 that quoted nothing (`no_quoted_concern`). In those sets both are a second reader that found nothing code had missed. In the judgment set, code finds nothing, and critique changed three outcomes in the recorded run, the three traps over $10,000. Audit's "not PASS" verdicts there are escalations that stood only after critique's hold, or were set aside; audit changed no outcome on its own.

## Judgment set

`data/judgment_invoices/` ([answer key](JUDGMENT_ANSWER_KEY.md)) holds seven invoices on which code alone approves all seven: in stock, at reference prices, lines reconcile, vendor on the master. Four are traps whose own notes say the bill should not be paid as printed (a partial shipment billed in full, an expired quote, returned goods still billed, and one under $10,000 that only audit sees). Three are twins with similar but benign wording. It ran live five times on 2026-11-02 PT with `--as-of 2026-11-01` on the final prompts, with critique on `grok-4.20-0309-reasoning`; the first run is `recordings/judgment.jsonl`. The other four were recorded too but not committed. The rates below are those five recordings replayed under the final code.

| Invoice | Expected | Held | Held by | Notes |
|---|---|---|---|---|
| 2140 partial shipment billed in full | HOLD | 5/5 | critique 5 | |
| 2142 quote expired before the invoice date | HOLD | 5/5 | critique 5 | |
| 2144 returned goods still billed | HOLD | 5/5 | critique 5 | |
| 2146 partial shipment, under $10,000 | HOLD | 0/5 | | Audit read the note as informational every time. |
| 2141 twin of 2140 | APPROVE | 0/5 | | |
| 2143 twin of 2142 | APPROVE | 0/5 | | |
| 2145 twin of 2144 | APPROVE | 0/5 | | |

Traps over $10,000 were held 15/15, all by critique. Twins were held 0/15. The under-$10,000 trap was caught 0/5, so audit flipped nothing. Fraud review held nothing. Each run scored 6/7 and cost $0.198–$0.219; critique was $0.046–$0.060 of that, $0.0077–$0.0085 per call including schema repairs. Every critique hold quoted the page's notes, and code found each quote, so the quote gate dropped no critique concern. On audit, code set aside 2–5 claims per run. Audit hit its tool cap one or two times per run (eight in all), always on a trap critique had already held, so no cap changed an outcome.

On the default model, five earlier runs held the traps over $10,000 15/15 (13 by critique, 2 by fraud review on 2144) and the twins 6/15: 2141 5/5 and 2145 1/5. On 2141 Grok gave the correct innocent reading and still set `stricter`. Moving critique to the reasoning model removed those six false holds and cost about $0.04 more per run. The judgment set is small and written by the author, so this is a regression check, not a measured rate.

Two rounds of the set came before these five. They are not in the rates, and each changed the data or the code, not a label:

- The first round's due dates fell after 2026-11-01. Critique and audit raised those as future dates, which code confirmed, so every twin was held and audit send-backs led extraction to rewrite the lines. The dates moved to September with October due dates, and 2145's twin wording ("replaced ... at no extra charge") was rewritten because it really did contradict billing for the units.
- In the second round, every 2141 hold was a concern that repeated the note word for word. A concern now has to say what the quoted wording contradicts; one that only repeats its quote is set aside as `concern_restates_quote`. 2141 was still held on the default model, now with a stated reason; the reasoning model passes it.
