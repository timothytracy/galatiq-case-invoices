# Interview notes

Short answers a presenter can defend from this tree. Scores are the replays in [METRICS.md](METRICS.md), run on 2026-11-02 with no API key. Design facts are the code and the policy. New policy text is marked Pending approval there.

## Design

**Why more than one agent?** The brief asks for ingestion, validation, approval with a critique on a large bill, and payment or a logged rejection. Those are different jobs, and two of them loop: critique returns to approval, and the audit can send the extraction back. LangGraph is the picture of that. The stage bodies stay ordinary functions under `pipeline/`. `graph.py` only wires nodes and edges. CrewAI or AutoGen would add role-play around the same steps.

**Why are the decisions in code?** The same findings have to produce the same outcome. `outcome_from_findings` is the one place a finding list becomes approve, reject, or hold. Reject wins over hold. Hold wins over approve. The model extracts, lists risks, critiques, and returns an audit verdict. It does not pick the outcome. `rules_applied` and `evidence` are filled in Python from the real findings and amounts.

**Why not decrement stock?** The README lists INV-1001, INV-1004, and INV-1006 as passing, and WidgetA on those three is 10 + 3 + 5 = 18 against a snapshot of 15. The check is per invoice, against a fixed snapshot. `quantity == stock` passes. Approving a bill does not write the inventory file. The workflow opens it read-only.

**Why is a non-USD invoice a hold?** `allowed_currencies` is USD. Nothing is converted, and reference prices are not compared. Finance confirms the currency and the amount. Payment also refuses a currency outside that list if one ever reached it.

## Safety

**What can the model decide?** It can extract fields, set the embedded-instruction flag and the non-commercial-address flag, list `risks_considered`, list critique concerns and a stricter flag, and return PASS, SEND_BACK, or ESCALATE with notes and cited codes. When the audit tools (XAI_TOOLS) are in use, it can also choose which of those tools to call.

**What can it not decide?** It cannot approve a hard failure, pay a non-USD invoice, substitute an item name, or loosen a decision. Unknown items stay rejects. "Likely meant X." is added only when exactly one inventory name is one letter away, and the item is still rejected. Embedded instructions reject only when the model flag is set and the text matches an imperative phrase aimed at the system. The bare word "approve" does not match. A non-commercial address is a warning, not a hold by itself.

**What does tighten-only mean?** Critique may move an approve to a hold. It may not move a hold or a reject back to approve. Audit cannot turn a hard failure into a payment. A concern that only says the total is over 10000 is ignored. The fact guard may set aside a stock claim, a totals claim, or a future-date claim when the code already checked the opposite, and only in the cases in `docs/DECISION_POLICY.md`. Everything else is kept. A send-back stands while any feedback remains.

**What can the ledger tool change?** Pending approval. `check_payment_ledger` is read-only and takes no arguments. It is bound to this invoice. A hit can add `DUPLICATE_INVOICE` or `SPLIT_INVOICE`. A miss adds nothing. In a single process it is redundant: validation already holds those cases and the graph never reaches audit. It does not check the raw-text hash, so it is also narrower than validation. The new case it can see is a payment another process wrote between validation and audit. The unique index already stops a second payment of the same vendor and invoice number. The remaining hole is a split across two processes.

## Determinism

**Why temperature 0?** Every model request sends `temperature` 0. The fingerprint hashes that value, so a different temperature would not replay.

**What does replay prove?** It proves this code still handles the saved replies the same way, with no key and no network. It does not prove the model would say the same thing tomorrow. The five replays on 2026-11-02 scored 20/20, 21/21, 11 ordered steps all ok, 20/20 with the audit tools, and 21/21 with the audit tools. Costs and token totals are in [METRICS.md](METRICS.md).

**Where is the live variance?** Not in a second replay of the same file. The live passes are in [METRICS.md](METRICS.md) under Live runs. The scores match the recordings. The audit-tool call counts do not. Cost was not logged for the nine runs at `6ee5d927`. Where a live cost is logged, the method is the one in that log, not the replay rate.

**What is a replay miss?** The fingerprint is missing from the file. That error is not retried and it does not fall through to the network. The invoice is a `SYSTEM_ERROR` hold and nothing is paid. A tools recording played back without the audit tools misses, and the reverse misses, because the tools are part of the hash.

## Tools

**Who gets the audit tools (XAI_TOOLS)?** Audit only. Critique is not wired to the registry. Critique already receives `verified_by_code` (today's date, stock, whether the totals reconcile, the reference prices, and whether the dates are after today). The four tools are `lookup_item`, `recompute_totals`, `get_policy_rule`, and, pending approval, `check_payment_ledger`.

**What are the caps?** `TOOL_ROUND_CAP` is 3 and `TOOL_CALL_CAP` is 8, both in `llm_client.py`. An identical repeat in that step returns the earlier result, does not run the tool again, and does not count against the 8. The round still counts. A schema repair sends no tools. On the 2026-11-02 replays the only repeats were one `get_policy_rule` on `invoice_1004.json` and one on `invoice_2008.txt`. No call was blocked and no invoice hit either cap.

**What is the forced turn?** If audit hits the round cap or the call cap, it does not hold immediately. It gets one more model call with no tools, using the tool results already gathered, and asks for the verdict. The audit log then has `tool_cap_hit` (`rounds` or `calls`) and `forced_final_turn`. If that reply fails, including a schema failure after the normal repair, the invoice is a `SYSTEM_ERROR` hold and nothing is paid. None of the five replays took that path.

**Why does the review screen show tool calls?** Invoice detail reads `tool_calls` off the stage log. A repeat is marked. A call with error `blocked: tool_cap` is marked blocked. Calls that only exist on a failed attempt stay in the list and are marked as a failed attempt. A run with no calls says "No tool calls."

## Cost and failure

**What does a run cost?** Replay prints `tokens total=N cost_usd=X` from the usage stored in the recording. The method is in [METRICS.md](METRICS.md): published rates for `grok-4.20-0309-non-reasoning`, short-context under 200,000 prompt tokens, long-context at or above that, no separate cached-input rate, six decimal places. Pending approval on that price. The five replay costs on 2026-11-02 were 0.051456, 0.058625, 0.026695, 0.102365, and 0.119278 USD. The two larger ones are the runs that offered the audit tools. Those runs also used more tokens (73608 and 86882, against 34199 and 39976).

**What fails closed?** A missing API key exits before any invoice, on a live run. Retries are timeout, 429, 5xx, network, JSON that is still invalid after the schema repair, and approval wording that refuses the input or contradicts an approve. The backoff is 1, 2, 4, 8, 16 seconds plus up to 25% jitter, 5 attempts unless `XAI_MAX_ATTEMPTS` overrides one run. After that, or on 400, 401, or 403, the outcome is a `SYSTEM_ERROR` hold and nothing is paid. Bad approval wording that survives the retries is `BAD_REASONING`. A locked or corrupt ledger is a `SYSTEM_ERROR` hold, at validation, at payment, and, pending approval, inside the ledger tool. A missing ledger is empty, not an error. An unreadable file is a reject, `UNREADABLE`. An extracted item name missing from the source text is an `ITEM_NOT_IN_SOURCE` hold, and a missing source fails closed.

**What is still weak?** The fact guard trusts the category the model puts on a concern. A stock sentence labeled `other` is kept. The ledger row is written before the mock payment. Two processes can each pay half of a split. The ledger tool does not close that hole in a single process, and it does not see the raw-text duplicate. The hold-queue note does not pay and does not change the pipeline outcome. Reference prices cover WidgetA, WidgetB, and GadgetX only. Non-USD is never converted. Stock is the four seed rows and does not move. These replays do not measure live variance.

**What would you do next?** Assign concern categories in code, so a mislabeled sentence can still be set aside. Write the ledger row after a confirmed payment, or mark it pending until the gateway says so. Keep the decision in code either way. Give the hold queue a real authorization path. The note today is local only. Read stock from the system of record at decision time, still in code, still without paying a bill the snapshot does not cover.
