# Decision policy

**Status:** Approved by the captain on 2026-10-31. Code changes to match this policy will follow; until they land, the pipeline's behavior may differ from it (see "Known follow-ups").

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
| Unknown item | Resend with the correct item name. We suggest "likely meant X" but never correct it silently. |
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
| Revised invoice | Confirm it replaces the original and that the original was not paid. |
| Labeled credit note whose math reconciles | Accounting applies the credit. Nothing is paid. |
| Renamed vendor | Verify it is the same company. |
| Non-commercial vendor address | Verify the vendor. |
| Two or more fraud signals together (urgency pressure, wire-only payment, a new remittance account) | Verify with the vendor using contact details already on file. |
| Unit price 50% or more above the USD reference | Verify the price with the vendor. |
| Clean invoice over $10,000 with a new remittance or bank account | Verify the change with the vendor. A bank-account change is the most common invoice-fraud pattern. |
| Grok or system failure after retries | Rerun the invoice or process it by hand. Tracked as a system error, not a business decision. |

## Warnings

Warnings are recorded in the findings and shown in the logs. On their own they don't change the outcome.

- A single fraud signal.
- A total within 2% under the $10,000 threshold.
- A unit price that differs from our reference price. The check uses the absolute unit price. A price 50% or more above the reference is also the hold above.
- A descriptor on an item name, such as "WidgetA (rush order)". The descriptor is split into a note and the item is matched as WidgetA.

Item names are matched exactly, after normalizing only case, spaces, and hyphens.

## Ground rules

- **Precedence: Reject over Hold over Approve.** If any Reject condition applies, the outcome is Reject, even when fraud signals or other Hold conditions are also present. Those signals are still recorded as findings and mentioned in the reason, so a reviewer sees them.
- **The LLM reads and flags; code decides.** The same findings always give the same outcome.
- **Each invoice is judged on its own.** Processing order never changes a result.
- **OCR number fixes are allowed only if the math reconciles.** For example, an O read as a 0 in an amount or date is fixed only when the lines and totals add up afterwards.
- **Duplicates are out of scope.** This is a known limitation. The production fix is a check against a payments ledger.
- **No payment without an audit PASS.**
- **Non-USD is never paid.**

## Alignment with the original README

We keep the README's groupings:

- 1002 and 1003 are flagged.
- 1008 and 1016 are both unknown items, and get the same outcome.
- 1009 is a data-integrity problem.
- 1001, 1004, and 1006 pass validation.

The captain's call: **1003 is a Reject (stock shortfall).** Its urgency and wire-transfer signals are recorded as findings and named in the reason.

The captain's call: **a non-commercial address flagged on an otherwise clean invoice that the README lists as passing is noted, not held.** So 1004 (742 Evergreen Terrace) is an Approve with the address recorded as a warning.

## Known follow-ups

- The agents must be given today's date. Dates in April 2026 are not in the future, but the audit has treated them as if they were.
- The critique currently treats every invoice over $10,000 as risky just for being over the threshold, so clean invoices over $10,000 never get approved. That breaks the Approve rule above.
