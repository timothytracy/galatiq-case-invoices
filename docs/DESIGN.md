# Decision policy

Every invoice ends in one outcome: approve, reject, or hold. Every outcome carries a reason code and a plain-English reason. What each agent does, and how it fails, is in the [README](../README.md#extended-details).

The numbers live in [policy.toml](../policy.toml). `decision/policy.py` loads that file at startup. A missing key, a wrong type, or a nonsense value stops the process and names the key.

## Acme's process

The brief describes four manual steps. This is what replaces each one.

| Today at Acme | What the system does |
|---|---|
| Staff type invoice fields by hand from emailed PDFs. | Grok extracts the fields. Code checks each item name against the source text, and an OCR fix is kept only when the math reconciles. A name that is not in the text is a hold, not a guess. |
| Staff check a legacy inventory database that is inconsistent. | One SQLite table, opened read-only. Names match after case, spaces, and hyphens are normalized. Quantities are summed per item. A near-miss name is rejected with "Likely meant X." and is never substituted. |
| A VP approves by email chain. | Code applies written rules. A USD total over $10,000 gets one critique, and an audit with read-only tools can only tighten. A hold names one action for one owner, and `approve_and_pay` records the release. |
| Staff pay through the banking API. | `mock_payment` runs only after an audit PASS or a reviewer release. The payments ledger blocks a duplicate or a split before money moves. |

Each kind of hold has one owner. These roles are proposed. The brief does not name Acme's teams.

| Hold kind | Codes | Owner | Can a reviewer pay it? |
|---|---|---|---|
| Vendor identity or bank change | `VENDOR_RENAMED`, `NEW_REMITTANCE`, `FRAUD_CLUSTER` | Accounts payable, with the vendor-master owner | Yes. A second approver is the production next step. |
| Invoice history | `REVISED_INVOICE` | Accounts payable | Yes |
| Invoice history | `DUPLICATE_INVOICE`, `SPLIT_INVOICE` | Accounts payable | No |
| Price | `PRICE_SPIKE` | Purchasing, which owns the PO price | Yes |
| Currency or credit | `NON_USD`, `CREDIT_NOTE` | Finance and accounting | No |
| Extraction trust | `ITEM_NOT_IN_SOURCE` | Accounts payable checks the document | No |
| Review escalation | `CRITIQUE_HOLD`, `AUDIT_ESCALATED` | The approver who replaces the VP email chain | Yes, except at the send-back cap |
| System failure | `SYSTEM_ERROR`, `BAD_REASONING` | Engineering on call reruns the invoice | No |

## Approve

Approve when all checks pass and there are no blocking findings.

A USD total over $10,000 gets one critique. That review can only make the decision stricter. Being over the threshold is not a risk finding by itself.

## Reject

Reject means the vendor must send something new. Every reject names that next step.

| Finding | Next step for the vendor |
|---|---|
| Missing required field (vendor, due date, line items, total) | Resend the invoice with the missing field filled in. |
| Quantity that is zero, negative, or not a number | Resend with corrected quantities. |
| Unknown item | Resend with the correct item name. Code adds "Likely meant X." only when exactly one inventory name is one letter away, after case, spaces, and hyphens are removed. The item is still rejected and is never substituted. |
| Stock shortfall | Resend for the quantity in stock, or amend the PO. |
| Total or line-math mismatch | Resend with corrected totals. |
| Unreadable file | Resend in a readable format. |
| Embedded instructions aimed at the system | Resend a clean invoice. Recorded as a security finding. |
| Negative amount that is not a labeled credit note | Resend with corrected amounts, or send a labeled credit note. |

Embedded instructions are rejected only when the model flags them and the text matches an imperative phrase aimed at the system. The phrase check looks for "ignore previous instructions" or "skip review". The bare word "approve" does not match. Ordinary invoices say things like "PO approved by".

## Hold

Hold when a person can do something the system cannot. Every hold names that action.

| Finding | Action for a person |
|---|---|
| Non-USD currency | Finance confirms the currency and amount. Nothing is converted and nothing is paid. |
| Duplicate invoice | Do not pay it again. The same vendor and invoice number are already in the payments ledger. |
| Split invoice | Do not pay it. This invoice is at or under the approval threshold, and with this vendor's other paid invoices inside the split window it is over that threshold. |
| Revised invoice | Confirm it replaces the original and that the original was not paid. |
| Labeled credit note whose math reconciles | Accounting applies the credit. Nothing is paid. |
| Renamed vendor | Verify it is the same company. |
| Two or more fraud signals together (urgency pressure, wire-only payment, a new remittance account) | Verify with the vendor using contact details already on file. |
| Unit price 50% or more above reference | Verify the price with the vendor. |
| Clean invoice over $10,000 with a new remittance or bank account | Verify the change with the vendor. A bank-account change is the most common invoice-fraud pattern. |
| Item name not in source text | Check the item name against the invoice. |
| Grok or system failure after retries | Rerun the invoice or process it by hand. Tracked as a system error, not a business decision. |

`DUPLICATE_INVOICE` and `SPLIT_INVOICE` read `payments.db`. Only the payment step writes it. The match key is the vendor name, casefolded with whitespace collapsed, plus the invoice number after normalization. The split window is `split_window_days`, 30 days. A date that does not parse is outside the window. Another vendor does not count. How the ledger behaves across files is in the [README](../README.md#tools-the-ledger-and-the-fact-guard).

Compare `abs(unit_price)` with the reference price. If `abs(unit_price) >= reference * 1.50`, the invoice is a `PRICE_SPIKE` hold on its own. That spike is not a fraud signal. A smaller difference, including a price below the reference, stays a price-variance warning. A spike line still records that warning. Non-USD invoices are not price-checked. Reject still beats this hold.

An extracted item name that is not in the invoice text is an `ITEM_NOT_IN_SOURCE` hold. The text is the file for `.txt`, JSON, CSV, and XML, and the PDF text layer for a PDF. A missing or empty source fails closed. The name is not corrected, and nothing is paid.

## Warnings

Warnings are recorded and shown. On their own they do not change the outcome.

- A single fraud signal.
- A total within 2% under the $10,000 threshold, and still under it.
- A unit price that differs from the reference by less than 50%, compared on `abs(unit_price)`.
- A descriptor on an item name, such as "WidgetA (rush order)". The descriptor is split into a note and the item is matched as WidgetA.
- A non-commercial vendor address. It is never a hold by itself.

Item names are matched after normalizing case, spaces, and hyphens.

## Ground rules

- Reject wins over hold. Hold wins over approve. When a reject also has fraud signals or a hold, those findings stay in the reason.
- The model reads and flags. Code decides. The same findings always give the same outcome. This keeps every payment traceable to a rule, and it means text inside an invoice cannot loosen a decision. The reasoning is in the [README](../README.md#why-code-decides).
- Each invoice is judged on its own facts plus the payments ledger. The ledger can only add a hold. Scored eval gives each invoice a fresh ledger, so a scored result does not depend on order.
- An OCR number fix is kept only when the math reconciles. An O read as a 0 in an amount or a date is fixed only when the lines and totals add up afterwards.
- No payment without an audit PASS, or a reviewer release of a payable hold.
- Non-USD is never paid. Nothing is converted.
- Critique and audit can only tighten. Critique sets aside a stock claim whose summed quantity is covered by available stock, a totals-mismatch claim when the totals reconcile, and a future-date claim on the invoice date or the due date when that date is not after today. Audit sets aside that same future-date claim. Every other concern is kept, including a disagreement between the raw text and the extraction. A send-back stands while any feedback remains. An escalation does too, and either stands when the audit cites a finding code. Every set-aside is logged.

Invoice 1003 is a reject for the stock shortfall. Its urgency and wire-transfer signals stay on the record. Invoice 1004 approves. A non-commercial address on a clean invoice the brief lists as passing is a warning. The revised file stays a hold because it is a revision.

## Releasing a hold

`approve_and_pay` in `pipeline/approve_hold.py` is how a reviewer pays a held invoice. The pipeline outcome stays what the run recorded. Each decision is appended to a log and is not written back over that outcome.

`PAYABLE_HOLD_CODES` in `decision/outcome.py` lists the holds a person may release: `REVISED_INVOICE`, `VENDOR_RENAMED`, `NEW_REMITTANCE`, `FRAUD_CLUSTER`, `PRICE_SPIKE`, `CRITIQUE_HOLD`, and `AUDIT_ESCALATED`. A hold is payable only when every saved finding and every fresh finding is one of those codes or a warning. Two fraud signals count as the fraud-cluster hold. `AUDIT_ESCALATED` is not payable when `send_back_cap` is set or the recorded message is the send-back cap sentence.

A reject is never paid. Neither is a hold with a reject-class finding, or a closed hold: `ITEM_NOT_IN_SOURCE`, `NON_USD`, `CREDIT_NOTE`, `DUPLICATE_INVOICE`, `SPLIT_INVOICE`, `SYSTEM_ERROR`, or `BAD_REASONING`. The total must be greater than zero, the currency must be USD, and the amount must equal the recorded total. An OCR number such as `INV 1O12` stays held.

A release re-checks the live source file and the live inventory without the model. Matching the vendor, invoice number, total, and each line's name and quantity is a sanity check. Text already in the file passes, so that check does not show the record is unchanged. Before the release file is written, the run stores its sha256 in `payments.db` when the outcome is a payable hold, or NULL otherwise. Approval pays only when the file's sha256 equals that row.

The split and same-text checks run at validation, and the payment write checks them again inside the ledger lock. A second writer sees the first row, so two processes cannot each pay half of a split.

## Knobs

| Knob | Value | What it does |
|---|---|---|
| `scrutiny_threshold` | 10000 | A USD total strictly over this goes to critique once. Being over it is not a risk by itself. |
| `critique_cap` | 1 | One critique pass, then approval again. |
| `audit_send_back_cap` | 3 | The audit can send the extraction back three times. The next send-back becomes a hold. |
| `allowed_currencies` | USD | Any other currency is a hold. Nothing is converted. |
| `money_tolerance` | 0.01 | Line math must match the printed total within one cent. |
| `model_retry_attempts` | 5 | Then the invoice is a `SYSTEM_ERROR` hold and is not paid. `XAI_MAX_ATTEMPTS` can override one run. |
| `near_threshold_ratio` | 0.02 | A total within 2% under the threshold, and still under it, is a warning. |
| `price_spike_ratio` | 0.50 | `abs(unit_price)` at least 50% above the USD reference is a `PRICE_SPIKE` hold. |
| `reference_prices` | WidgetA 250, WidgetB 500, GadgetX 750 | USD references. A smaller gap stays a price-variance warning. |
| `split_window_days` | 30 | Same vendor, printed dates inside this window. |

Retries back off 1, 2, 4, 8, then 16 seconds, plus up to 25% jitter. After those attempts, or on HTTP 400, 401, or 403, the invoice is a `SYSTEM_ERROR` hold and nothing is paid.

Reference prices live in `policy.toml`. The inventory table stays the four seed rows: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0. Approving an invoice does not decrease stock.

Stock stays fixed so that each invoice is judged on its own facts, the same way the answer keys were written. If an approve drew stock down, an outcome would depend on which files ran first. After invoice 1001 (WidgetA 10) and invoice 1006 (WidgetA 5), WidgetA would be 0, and invoice 1010 (WidgetA 12) would turn from an approve into a stock reject. Replay, the live runs, and scored eval each expect one result per file, whatever ran before it. The payments ledger is the only state carried across files, and it can only add a hold.

## Limitations

- The fact guard trusts the category on the model's claim. A stock sentence labeled `other` is kept.
- The ledger row is written just before the mock payment. A failure after that insert needs the row removed, or the write delayed until the gateway confirms.
- One reviewer can release `FRAUD_CLUSTER` or `NEW_REMITTANCE`. A second approver for vendor-risk holds is the production next step.
- `logs/` is trusted the same way `payments.db` is. Anyone who can edit those files can edit the ledger.
- Split detection is unit-tested on synthetic invoices only. No sample file is a split.
- Non-USD invoices are never converted.
- Stock is the four seed rows, opened read-only, and it does not move when a bill is approved.
- A note on the hold queue does not pay, and it does not change the pipeline outcome.
