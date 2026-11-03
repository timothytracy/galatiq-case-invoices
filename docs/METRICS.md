# Replay metrics

Every replay number is from a committed recording, with no API key. Each set was recorded live on 2026-11-02 PT with `--as-of 2026-11-01`, after critique began running on every USD invoice. A replay creates `inventory.db` from the seed when the file is missing. The score, token, cost, call, and repeat figures are the stdout of `scripts/eval.py`. The live API runs are in [Live runs](#live-runs).

| Run | Command | Score | Tokens | Cost USD | Calls | Repeats |
|---|---|---|---|---|---|---|
| Samples | `python3 scripts/eval.py --replay recordings/samples.jsonl` | 20/20 | 116612 | 0.186961 | 38 | 0 |
| Unseen | `python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl` | 21/21 | 151001 | 0.238564 | 49 | 1 |
| Ordered | `python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl` | 11 steps, all ok | 42504 | 0.068056 | 12 | 0 |
| Loop | `python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --replay recordings/loop.jsonl` | 3/3 | 18398 | 0.028484 | 7 | 0 |
| Fraud | `python3 scripts/eval.py --invoices data/fraud_invoices --answer-key data/fraud_answer_key.json --replay recordings/fraud.jsonl` | 5/5 | 43124 | 0.071635 | 10 | 0 |
| Pass list | `python3 scripts/eval.py --invoices data/passlist_invoices --answer-key data/passlist_answer_key.json --replay recordings/passlist.jsonl` | 5/5 | 19144 | 0.029756 | 6 | 0 |
| Judgment | `python3 scripts/eval.py --invoices data/judgment_invoices --answer-key data/judgment_answer_key.json --replay recordings/judgment.jsonl` | 7/7 | 139400 | 0.215299 | 49 | 0 |

All seven commands exit 0 and run in CI. Judgment scores 7/7; its live runs are in [Judgment set](#judgment-set). Fraud review held invoice 1004 as `FRAUD_SUSPECTED` on its vendor address in the samples recording, which is its label; an approve is also accepted ([why](ANSWER_KEY.md#rulings-on-1003-and-1004)). The ordered scenario expects 1004 held and a ledger of two rows, and also accepts 1004 paid and a ledger of three. In this ordered recording fraud review held 1004, so the ledger line was `ledger 2 rows: invoice_1001.txt, invoice_1011.pdf`. Either way `invoice_1004_revised.json` is a `REVISED_INVOICE` hold.

The unseen scorer checks the outcome plus the primary reason code, or a listed alternate code from the answer key.

Outside the judgment set, no replay call was blocked by the tool cap and no result had `tool_cap_hit` or `forced_final_turn`. In the judgment recording audit hit the cap once, on a trap critique had already held.

## What recording these sets changed

All seven sets were recorded again when critique began running on every USD invoice, not only totals over $10,000; a total over $10,000 is now marked VP scrutiny. Before recording, every set ran live three times ([Live runs](#live-runs)). The first round of those three runs held clean invoices, and no label was changed:

- Critique held unseen 2105 ($9,880) twice and 2103 ($1,750) once on routine delivery notes. It quoted "The receiver is out on Thursdays." and "Dock appointment is flexible." and read them as goods not yet delivered. The quote gate could not drop them, because the quotes were on the page. The critique prompt now says shipping, receiving, packing, and dock or delivery-scheduling notes are routine logistics and never a reason to hold, and that a delivery hold needs the page to say outright that fewer units were delivered than billed, that billed units were returned or cancelled, or that the stated price basis does not apply.
- In the third run, audit held 2105 claiming Quarry Lane Fasteners was a new vendor. It is on the vendor master, and the only quote was the `Vendor:` line. A quote of the vendor line is now structured-only, like a date, item, or total line, because code checks the vendor master.

The second round of three runs, on the final code, scored every set perfectly each time, and run 1 is the committed recording.

Unseen, loop, and judgment were then recorded once more when critique moved to `grok-4.20-0309-reasoning`, the model fraud review uses. Their critique calls carry a new model, so the old lines no longer matched. Unseen and loop matched their labels on the first recording. Samples, ordered, fraud, and pass list ran no critique then and were not recorded again at that step.

The six earlier sets were recorded again because the critique and audit prompts changed: both now ask for a quote and an innocent reading, and both compare what is billed with what the page says was ordered, delivered, priced, or returned. Samples, ordered, loop, fraud, and pass list matched their labels on the first recording. Unseen did not, and no label was changed:

- In the first unseen recording under the new prompts, audit escalated 2003 citing `REVISED_INVOICE` and 2105 citing `FUTURE_DATE`. Neither code was a finding on either invoice, and a cited code skipped the quote gate. Audit now keeps a cited code only when code found it on this invoice; any other citation is logged as `cited_code_not_found`, and an escalation left with no code and no quote passes. That recording replays at 21/21 under the change, which touches no prompt.

The earlier recordings, made when critique first saw the page and extraction began returning the quote and the action behind an instructions flag, had two first attempts that missed a label:

- In the first two recordings of the unseen set, Grok flagged 2003's `VOID quote Q-1844…` line as instructions to the system, with that line as its quote. A model-only flag then held a clean invoice. The fix was a structured field: extraction says what the wording asks for (approve, pay, skip the review, ignore the rules, or other), and code accepts a model-only flag only for one of the first four. Grok calls 2003's line `other` and 2134's `skip_review` or `pay`.
- In the next recording, the unaccepted flag still reached the audit prompt, and audit sent 2003 back twice before it passed. Ingestion now clears a flag code did not accept, keeping the quote and action in the record. The unseen set was recorded again with no send-back.

## Cost method

The cost line is `tokens total=N cost_usd=X` from `recording.py`. It sums the `usage` objects stored in the recording. It does not call the API.

The rates are the published rates for `grok-4.20-0309-non-reasoning` and `grok-4.20-0309-reasoning`, which are the same: $1.25 per 1,000,000 input tokens and $2.50 per 1,000,000 output tokens when `prompt_tokens` is under 200,000. At or above 200,000 prompt tokens the rates are $2.50 and $5.00. Reasoning tokens are billed as output, so output is the larger of `completion_tokens` and `total_tokens` minus `prompt_tokens`. `prompt_tokens` is not split into a cached-input rate. The sum is quantized to six decimal places, half up. None of these recordings needed the long-context rate.

Critique and fraud review call `grok-4.20-0309-reasoning`; extraction and audit call the default model.

Critique's share of each recording, counting schema repairs:

| Set | Calls | Tokens | Reasoning tokens | Cost USD |
|---|---|---|---|---|
| Samples | 6 | 24655 | 16219 | 0.051639 |
| Unseen | 8 | 31118 | 19633 | 0.064213 |
| Ordered | 2 | 6579 | 3861 | 0.013280 |
| Loop | 1 | 3326 | 1871 | 0.006585 |
| Fraud | 2 | 8094 | 5320 | 0.016939 |
| Pass list | 1 | 3125 | 1762 | 0.006205 |
| Judgment | 8 | 30870 | 16171 | 0.059973 |

Critique is $0.218833 of the $0.838755 the seven recordings cost together, $0.0078 per call. Across the three final live runs it was $0.0078–$0.0080 per call and $0.219–$0.231 per run of all seven sets. Before critique ran on every USD invoice, the seven recordings cost $0.683331 together, so the change adds about $0.16–$0.18 per pass over the 82 files, or 23–26%.

Fraud review's share of each recording:

| Set | Reviews | Tokens | Reasoning tokens | Cost USD |
|---|---|---|---|---|
| Samples | 7 | 13367 | 6831 | 0.025983 |
| Unseen | 8 | 16610 | 8769 | 0.032585 |
| Ordered | 3 | 5827 | 3052 | 0.011410 |
| Loop | 1 | 1826 | 888 | 0.003491 |
| Fraud | 5 | 10990 | 5923 | 0.021779 |
| Pass list | 1 | 1850 | 915 | 0.003575 |
| Judgment | 7 | 14152 | 7279 | 0.027580 |

## Live runs

Every set ran live three times on 2026-11-02 PT with `--as-of 2026-11-01` on the final code, each recorded; run 1 is committed. Every run of every set scored perfectly: samples 20/20, unseen 21/21, ordered all 11 steps ok with a two-row ledger, loop 3/3, fraud 5/5, pass list 5/5, and judgment 7/7. 1004 was a `FRAUD_SUSPECTED` hold in every samples and ordered run. The three runs of all seven sets cost $0.838755, $0.851231, and $0.858674.

| Set | Cost USD, runs 1–3 |
|---|---|
| Samples | 0.186961, 0.183085, 0.181516 |
| Unseen | 0.238564, 0.241268, 0.251928 |
| Ordered | 0.068056, 0.068675, 0.070039 |
| Loop | 0.028484, 0.027739, 0.027445 |
| Fraud | 0.071635, 0.071443, 0.072238 |
| Pass list | 0.029756, 0.030376, 0.032235 |
| Judgment | 0.215299, 0.228645, 0.223273 |

From the committed recordings, per-invoice cost is $0.009348 for samples ($0.186961/20) and $0.011360 for unseen ($0.238564/21). The earlier timing runs, before critique ran on every invoice, took 4.90–6.81 s per invoice; these runs were not timed.

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
| Fraud-review holds | 1 | 0 | 1 | 0 | 3 | 0 | 0 |
| Fraud-review holds dropped by code | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Audits run | 6 | 8 | 2 | 1 | 2 | 1 | 7 |
| Audit send-backs | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Schema repairs | 0 | 1 | 0 | 0 | 0 | 0 | 2 |
| Critiques run | 6 | 8 | 2 | 1 | 2 | 1 | 7 |
| Critique holds | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| Critique set-asides (fact guard and quote gate) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Audit set-asides (fact guard, quote gate, unfound citations) | 0 | 5 | 0 | 0 | 1 | 0 | 2 |
| Audit tool calls | 38 | 49 | 12 | 7 | 10 | 6 | 49 |
| Audits with tools whose verdict was not PASS | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| Model retries | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Outcomes changed by fraud review | 1 | 0 | 1 | 0 | 3 | 0 | 0 |
| Outcomes changed by critique | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| Outcomes changed by audit | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

An outcome is changed by a step when validation decided one outcome and the final outcome is different, with that step's code as the primary reason (`outcomes_changed` in the script's output). Fraud review runs only on invoices that validation passed. The one hold in samples is 1004. Every fraud-review hold in these runs quoted text that code found on the page, so none was dropped.

Outside the judgment set, critique and audit changed no outcome. Critique ran on every USD invoice that validation and fraud review passed, 20 across those six sets, and raised nothing that held. Audit's five set-asides in unseen are a citation of a code that was not a finding and a quote of a structured line on 2003, and on 2105 two future-date claims on dates before 2026-11-01 and an escalation that quoted nothing (`no_quoted_concern`). In fraud, audit cited `FRAUD_SIGNAL` on an invoice that did not have it (`cited_code_not_found`). In those sets both are a second reader that found nothing code had missed. In the judgment set, code finds nothing, and critique changed four outcomes in the recorded run, all four traps. Audit's "not PASS" verdicts there are escalations that stood only after critique's hold, or were set aside; audit changed no outcome on its own.

## Judgment set

`data/judgment_invoices/` ([answer key](JUDGMENT_ANSWER_KEY.md)) holds seven invoices on which code alone approves all seven: in stock, at reference prices, lines reconcile, vendor on the master. Four are traps whose own notes say the bill should not be paid as printed (a partial shipment billed in full, an expired quote, returned goods still billed, and one under $10,000, which is not marked VP scrutiny). Three are twins with similar but benign wording. On the final code, with critique on every USD invoice, it ran live six times on 2026-11-02 PT with `--as-of 2026-11-01`: three in the full runs of all seven sets and three on their own after the critique prompt change. The first full run is `recordings/judgment.jsonl`. The others were recorded too but not committed.

| Invoice | Expected | Held | Held by | Notes |
|---|---|---|---|---|
| 2140 partial shipment billed in full | HOLD | 6/6 | critique 6 | |
| 2142 quote expired before the invoice date | HOLD | 6/6 | critique 6 | |
| 2144 returned goods still billed | HOLD | 6/6 | critique 6 | |
| 2146 partial shipment, under $10,000 | HOLD | 6/6 | critique 6 | |
| 2141 twin of 2140 | APPROVE | 0/6 | | |
| 2143 twin of 2142 | APPROVE | 0/6 | | |
| 2145 twin of 2144 | APPROVE | 0/6 | | |

The traps were held 24/24, all by critique. Twins were held 0/18. Fraud review held nothing. Each run scored 7/7 and cost $0.214–$0.229; critique was $0.060–$0.065 of that, $0.0075–$0.0081 per call including schema repairs. Every critique hold quoted the page's notes, and code found each quote, so the quote gate dropped no critique concern. On audit, code set aside 2–5 claims per run. Audit hit its tool cap on a trap critique had already held, so no cap changed an outcome.

On the default model, five earlier runs held the traps over $10,000 15/15 (13 by critique, 2 by fraud review on 2144) and the twins 6/15: 2141 5/5 and 2145 1/5. On 2141 Grok gave the correct innocent reading and still set `stricter`. Moving critique to the reasoning model removed those six false holds and cost about $0.04 more per run. The judgment set is small and written by the author, so this is a regression check, not a measured rate.

Before critique ran on every invoice, critique saw only the three traps over $10,000. In five runs on the reasoning model they were held 15/15 and the twins 0/15, but 2146 was caught 0/5: audit read its note as informational every time, so 2146 was a listed `known_miss` and the set scored 6/7. Those runs cost $0.198–$0.219. In the first round of three runs with critique on every invoice, before the logistics wording was added to the critique prompt, 2146 was held 3/3 and the twins 0/9, but 2142 was paid once and 2144 was held once by fraud review instead of critique.

Two rounds of the set came before those five. They are not in the rates, and each changed the data or the code, not a label:

- The first round's due dates fell after 2026-11-01. Critique and audit raised those as future dates, which code confirmed, so every twin was held and audit send-backs led extraction to rewrite the lines. The dates moved to September with October due dates, and 2145's twin wording ("replaced ... at no extra charge") was rewritten because it really did contradict billing for the units.
- In the second round, every 2141 hold was a concern that repeated the note word for word. A concern now has to say what the quoted wording contradicts; one that only repeats its quote is set aside as `concern_restates_quote`. 2141 was still held on the default model, now with a stated reason; the reasoning model passes it.
