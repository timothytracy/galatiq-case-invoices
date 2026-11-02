# Decision policy

**Status:** Approved on 2026-10-31. The code now matches this policy.

## Purpose

Every invoice ends in exactly one outcome: **Approve**, **Reject**, or **Hold**. Every outcome carries a reason code and a plain-English reason.

## Approve

Approve when all checks pass and there are no blocking findings.

An invoice over $10,000 gets one extra review (the critique). That review can only make the decision stricter, never looser. Being over the threshold is **not** a risk finding by itself.

## Reject

Reject means the vendor must send something new. Every reject names that next step.

| Finding | Next step for the vendor |
|---|---|
| Missing required field (vendor, due date, line items, total) | Resend the invoice with the missing field filled in. |
| Quantity that is zero, negative, or not a number | Resend with corrected quantities. |
| Unknown item | Resend with the correct item name. Code adds "Likely meant X." only when exactly one inventory name is one letter away, after case, spaces, and hyphens are removed. The item is still rejected and is never substituted. Approved on 2026-11-01. |
| Stock shortfall | Resend for the quantity in stock, or amend the PO. |
| Total or line-math mismatch | Resend with corrected totals. |
| Unreadable file | Resend in a readable format. |
| Embedded instructions aimed at the system | Resend a clean invoice. Recorded as a security finding. |
| Negative amount that is not a labeled credit note | Resend with corrected amounts, or send a labeled credit note. |

Embedded instructions are rejected only when **both** the LLM flags them **and** a deterministic code check matches. The code check looks for imperative phrases aimed at the system (for example "ignore previous instructions" or "skip review"), never the bare word "approve", because normal invoices say things like "PO approved by".

## Hold

Hold only when a person can do something the system can't. Every hold names that action.

| Finding | Action for a person |
|---|---|
| Non-USD currency | Finance confirms the currency and amount. Nothing is converted and nothing is paid. |
| Duplicate invoice | Do not pay it again. The same vendor and invoice number are already in the payments ledger. Pending approval. |
| Split invoice | Do not pay it. This invoice is at or under the approval threshold, and with this vendor's other paid invoices inside the split window it is over that threshold. Pending approval. |
| Revised invoice | Confirm it replaces the original and that the original was not paid. |
| Labeled credit note whose math reconciles | Accounting applies the credit. Nothing is paid. |
| Renamed vendor | Verify it is the same company. |
| Two or more fraud signals together (urgency pressure, wire-only payment, a new remittance account) | Verify with the vendor using contact details already on file. |
| Unit price 50% or more above reference | Verify the price with the vendor. |
| Clean invoice over $10,000 with a new remittance or bank account | Verify the change with the vendor. A bank-account change is the most common invoice-fraud pattern. |
| Item name not in source text | Check the item name against the invoice. |
| Grok or system failure after retries | Rerun the invoice or process it by hand. Tracked as a system error, not a business decision. |

Duplicate invoice (`DUPLICATE_INVOICE`) and split invoice (`SPLIT_INVOICE`) read the payments ledger. Pending approval. The ledger is `payments.db`, a separate file next to `inventory.db`, and only the payment step writes it. The match key is the vendor name, casefolded with whitespace collapsed, plus the invoice number after normalization (`INV 1004` and `INV-1004` are the same). The same document text is the same match. A second format with the same vendor, number, and total is the same match. A revised bill, or a different total, for a number that was already paid is the same match, and the revised-invoice finding stays on the list. A revision is not a duplicate when that number has not been paid: on a fresh ledger, invoice 1004's revision is still only "Revised invoice".

Split invoice: this total is at or under the approval threshold, and this total plus the same vendor's paid totals whose printed invoice dates fall within `split_window_days` is over the threshold. The proposed default is 30 days. A date that does not parse is outside the window. Another vendor does not count. No sample file is a split. The check is unit-tested on synthetic invoices only.

A unique index on that match key refuses a second payment. The refusal pays nothing and is a hold, not a crash. Eval gives each invoice a fresh ledger. `python3 main.py --reset-ledger` deletes the persistent ledger before the run.

An extracted item name that is not in the invoice text is a hold (`ITEM_NOT_IN_SOURCE`). The text is the file contents for `.txt`, JSON, CSV, and XML, and the PDF text layer for a PDF. Every supported format has that text. If it is missing or empty, the check fails closed and the invoice is held. A parenthetical descriptor is already split off, so the base name must still appear: "WidgetA (rush order)" is checked as WidgetA. The comparison removes case, spaces, and hyphens, the same way item matching does. The name is not corrected, and nothing is paid. The outcome comes from the code (HOLD_CODES), not the severity field. Severity blocking is kept for rejects, so every hold finding carries warning. Approved on 2026-11-01.

Compare `abs(unit_price)` with the reference price. If `abs(unit_price) >= reference * 1.50`, the invoice is a hold on its own: reason "Unit price 50% or more above reference" (reason code PRICE_SPIKE). That spike is not a fraud signal and does not count toward the two-fraud-signal hold. A smaller difference, including a price below the reference, stays a price-variance warning. A spike line still records its price-variance warning. Non-USD invoices are not price-checked, because nothing is converted. The ratio is `price_spike_ratio` in `policy.toml`. Reject still beats this hold. Approved on 2026-10-31.

## Warnings

Warnings are recorded in the findings and shown in the logs. On their own they don't change the outcome.

- A single fraud signal.
- A total within 2% under the $10,000 threshold.
- A unit price that differs from our reference price by less than 50%, compared on `abs(unit_price)`. A price 50% or more above the reference is the hold above, not a warning and not a fraud signal.
- A descriptor on an item name, such as "WidgetA (rush order)". The descriptor is split into a note and the item is matched as WidgetA.
- A non-commercial vendor address. It is flagged for the reviewer and is never a hold by itself. The finding says "Vendor address looks non-commercial and is noted," then the address. It is raised when the extraction model marks the address. That note includes the model's short reason, or "the extraction model flagged the address" when none was given. The same warning is raised when the printed address contains "Apt" or "Apartment". An otherwise clean invoice stays an Approve. That is the 1004 rule below.

Item names are matched exactly, after normalizing only case, spaces, and hyphens.

## Ground rules

- **Precedence: Reject over Hold over Approve.** If any Reject condition applies, the outcome is Reject, even when fraud signals or other Hold conditions are also present. Those signals are still recorded as findings and mentioned in the reason, so a reviewer sees them.
- **The LLM reads and flags; code decides.** The same findings always give the same outcome.
- **Each invoice is judged on its own facts plus the payments ledger.** The ledger can only add a hold, never an approve. Eval gives each invoice a fresh ledger, so a scored result does not depend on order. Pending approval.
- **OCR number fixes are allowed only if the math reconciles.** For example, an O read as a 0 in an amount or date is fixed only when the lines and totals add up afterwards.
- **A paid bill is checked on the next file.** The match key is the vendor (casefolded, whitespace collapsed) and the invoice number. The same document text is the same match. A second file with that key, or the same text, is a duplicate hold and is not paid. Same vendor, different invoice numbers, each at or under the approval threshold, are a split hold when their totals inside the split window go over the threshold. Pending approval.
- **No payment without an audit PASS.**
- **Non-USD is never paid.**
- **Critique and audit can only tighten.** Critique may set aside three things: a stock claim for an item whose summed quantity is covered by available stock, a totals-mismatch claim when the totals reconcile, and a future-date claim on the invoice date or the due date when that date is not after today. Audit may set aside that same future-date claim. Every other concern is kept, including the audit's notes that the raw text and the extraction disagree. A send-back stands while any feedback remains. An escalation does too, and either stands when the audit cites a finding code. Every set-aside is logged. Neither step can loosen a decision. Approved on 2026-11-01.

## Alignment with the original README

We keep the README's groupings:

- 1002 and 1003 are flagged.
- 1008 and 1016 are both unknown items, and get the same outcome.
- 1009 is a data-integrity problem.
- 1001, 1004, and 1006 pass validation.

Decided on 2026-10-31: **1003 is a Reject (stock shortfall).** Its urgency and wire-transfer signals are recorded as findings and named in the reason.

Decided on 2026-10-31: **a non-commercial address flagged on an otherwise clean invoice that the README lists as passing is noted, not held.** So 1004 (742 Evergreen Terrace) is an Approve with the address recorded as a warning.

## Known follow-ups

The duplicate and split rows above, and the two ledger ground rules, are pending approval. Split detection is unit-tested on synthetic invoices only.
