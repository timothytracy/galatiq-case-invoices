# Acme invoice agents: Grok reads, code decides who gets paid

[![CI](https://github.com/timothytracy/galatiq-case-invoices/actions/workflows/ci.yml/badge.svg)](https://github.com/timothytracy/galatiq-case-invoices/actions/workflows/ci.yml)

**Grok reads and questions every invoice, and written rules decide whether it is paid, so no money moves without an audit PASS or a named person's release.**

The pipeline reads one invoice, checks stock, math, and the payments ledger, and either pays it or stops with a reason. Grok extracts the fields, critiques a USD total over $10,000, and audits. Code chooses approve, reject, or hold. Of the 41 labeled invoices, 15 are approved, 16 are rejected, and 10 are held. The pipeline never pays a reject or a hold. A reviewer can release some holds for payment.

Replay of the three committed recordings, no API key, on 2026-11-01 PT:

| Set | Score |
|---|---|
| 20 samples | 20/20 |
| 21 unseen | 21/21 |
| Ordered ledger | 11 steps ok, ledger matches |

Live runs the same day matched those scores: samples 20/20 three times, unseen 21/21 three times, and the ordered ledger ok.

![Review screen from the recorded Grok run](docs/review_screen.png)

The review screen shows ten sample invoices from that recorded run.

### Business impact

The brief describes $2M a year in manual processing, a 30% error rate, and five-day delays.

- **Speed.** In the live runs on 2026-11-01 PT, an invoice took 2.85–3.90 s, against five days by hand.
- **Cost.** Grok cost $0.004923–$0.006428 per invoice. Each figure is that run's wall time or cost divided by its invoice count. The runs are in [docs/METRICS.md](docs/METRICS.md).
- **Errors.** All 41 labeled outcomes were correct in replay and in three live runs of each set, and no reject or hold was paid. Forty-one invoices is a small set, so read this as no errors seen, not as a measured rate to compare with 30%.
- **People.** 31 of the 41 invoices, 76%, need no one at Acme. The 15 approves are paid. The 16 rejects go back to the vendor with the fix to make. The other 10, 24%, are holds. Each hold names one action for a person, such as confirming a currency or a bank-account change, instead of starting an email chain.
- **Savings.** The labor saved is the share of invoices that need no person times today's cost per invoice. On this set that share is 76%. Acme's real mix and volume set the dollar figure, so none is claimed here.

### What changes for Acme

| Today at Acme | What the system does instead |
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

## How to run

Replay first. No API key.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate
python3 setup_db.py
python3 scripts/eval.py --replay recordings/samples.jsonl
python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl
python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl
```

Each of those three exits 0 when the score is perfect. Add `--verbose` to also see each stage's JSON record. CI runs the same three after the tests and does not set an API key.

### Reviewer quick path

To watch one invoice go through every agent with no API key:

```bash
python3 main.py --invoice_path=data/invoices/invoice_1001.txt --reset-ledger --replay recordings/samples.jsonl
```

`--replay` answers each Grok call from the recorded live run, so the same code runs with no network. `--reset-ledger` deletes `payments.db` first, so the invoice is not seen as already paid. The output is one JSON record per stage: ingestion, validation, approval, audit with its tool calls, and payment. Then comes `Paid 5000.00 to Widgets Inc.` and a final `result` record with the outcome. Any file in `data/invoices/` works the same way. Try `invoice_1002.txt` for a stock reject or `invoice_1012.txt` for a hold.

A live run needs `XAI_API_KEY` in the environment or in `.env` (see `.env.example`).

```bash
cp .env.example .env
# set XAI_API_KEY in .env
python3 main.py --invoice_path=data/invoices/invoice_1001.txt
```

The review screen is `streamlit run ui/app.py`. Offline mode needs no API key.

A reviewer releases a hold with `approve_and_pay`. This demo runs the offline pipeline on two files, then tries to release each. It uses a temporary ledger and does not call the model.

```bash
python3 scripts/approve_hold_demo.py --demo
```

`invoice_1012.txt` is a `VENDOR_RENAMED` hold, and it is paid: `Paid 9975.00 to QuickShip Distributers (formerly FastShip Ltd.)`. `invoice_1002.txt` is a stock reject, and it stays unpaid with reason `NOT_PAYABLE`. Both keep their pipeline outcome.

## Agents

`graph.py` wires a LangGraph `StateGraph`. Each agent is a node. Ingestion, approval, critique, and audit call Grok. Validation, payment, and `reject_log` are code. Critique returns to approval. Audit can send the extraction back to ingestion. Holds and rejects both end at `reject_log`. Nothing on that path is paid. Payment runs only after an audit PASS on an approve.

```mermaid
flowchart TD
  startNode([Start]) --> ingestion
  ingestion["ingestion (Grok)"]
  validation["validation (code)"]
  approval["approval (Grok)"]
  critique["critique (Grok)"]
  audit["audit (Grok)"]
  payment["payment (code)"]
  rejectLog["reject_log (code)"]
  finishNode([End])
  ingestion -->|"invoice extracted"| validation
  ingestion -->|"unreadable file or model failure"| rejectLog
  validation -->|"checks pass"| approval
  validation -->|"reject or hold"| rejectLog
  approval -->|"USD total over 10000 and critique not used"| critique
  approval -->|"otherwise"| audit
  approval -->|"model failure or no decision"| rejectLog
  critique -->|"back to approval"| approval
  critique -->|"model failure"| rejectLog
  audit -->|"SEND_BACK and send-backs under 3"| ingestion
  audit -->|"outcome approve"| payment
  audit -->|"reject, hold, model failure, or send-back cap"| rejectLog
  payment --> finishNode
  rejectLog --> finishNode
```

## Key decisions

### Why code decides

Grok does the reading and the judging: it extracts the fields, lists the risks, critiques a large bill, and audits with tools. Code turns those findings into approve, reject, or hold. Paying money needs the same invoice to get the same answer every time, and every answer has to be traceable to a rule. So a model can make a decision stricter but never looser, and text inside an invoice cannot talk the system into paying it. The trade-off is less model autonomy. In return, no reject or hold was paid in any labeled run.

### Rules

- Code decides: reject wins over hold, hold wins over approve, and critique and audit can only tighten.
- Audit has tools that read inventory, totals, policy, and the ledger, cap at 3 rounds and 8 calls, and cannot pay.
- The same vendor and invoice number is a `DUPLICATE_INVOICE` hold, and a same-vendor cluster inside the split window that crosses the threshold is a `SPLIT_INVOICE` hold.
- Scored eval uses a fresh ledger per invoice, and `scripts/eval.py --ordered` shares one ledger.
- Replay runs this code on saved replies with no API key and no network, and a miss is a `SYSTEM_ERROR` hold that pays nothing.
- A non-USD invoice is a hold, and nothing is converted.
- Invoice 1003 is a stock reject, and its fraud signals stay on the record.
- Invoice 1004 approves: a non-commercial address on a clean invoice the brief lists as passing is a warning.

## Extended details

The opening is the two-minute read. What follows is how each agent does its job, and how it fails.

Grok is `XAI_MODEL`, default `grok-4.20-0309-non-reasoning`, called through the OpenAI SDK at `https://api.x.ai/v1`. Temperature is 0.

### Ingestion

Grok extracts one JSON object from the file text. PDFs contribute their text layer. There is no network fetch. Code normalizes item names and the invoice number, and it copies each item name as printed.

An unreadable file is a reject, `UNREADABLE`. A model failure after the retries is a hold, `SYSTEM_ERROR`. Nothing is paid.

### Validation

Validation is Python. It checks required fields, quantities, unknown items, summed stock, line math within one cent, currency, revisions, credit notes, fraud wording, USD price spikes, and each item name against the source text.

A reject or a hold stops here and never reaches approval, critique, or audit. A ledger it cannot read is a `SYSTEM_ERROR` hold. A missing or empty source text is an `ITEM_NOT_IN_SOURCE` hold. An unknown item stays a reject. The reason adds "Likely meant X." only when exactly one inventory name is one letter away, and the item is never substituted.

### Approval

Approval does not choose the outcome. The decision is already approve, or hold when critique has made it stricter. Grok returns `risks_considered`. Python writes the rules, the evidence, and the conclusion.

A model failure, or no decision, goes to `reject_log` and is not paid. Wording that refuses the input or contradicts an approve, after the retries, is a `BAD_REASONING` hold and is not paid.

### Critique

Critique runs when the currency is USD, the total is over $10,000, and this pass has not been critiqued. A send-back resets that counter. The model returns concerns and a stricter flag. It can only turn an approve into a hold.

A model failure goes to `reject_log`. A concern that only restates the dollar threshold is dropped. The fact guard can set aside a covered-stock claim, a totals claim when the math already reconciles, or a future-date claim when that date is not after today.

### Audit

Grok returns PASS, SEND_BACK, or ESCALATE. Python will not loosen the result. A PASS on a reject still ends at `reject_log`. While send-backs are under 3, a SEND_BACK returns to ingestion. The fourth send-back is an `AUDIT_ESCALATED` hold.

Tools are capped at 3 rounds and 8 calls. A cap hit gets one final reply with no tools. If that reply fails, or any tool fails, the invoice is a `SYSTEM_ERROR` hold and nothing is paid. A replay miss is the same hold, and it does not call the network.

### Payment

Payment runs only with an unused authorization whose vendor, amount, and currency match the invoice. The mock prints `Paid {amount:.2f} to {vendor}` and returns success. Any other case is `not_paid`. A refused authorization adds `PAYMENT_REFUSED` and does not change the outcome. The unique index on vendor and invoice number blocks a second payment.

### Reject log

Reject log is the other exit. Rejects are logged as stage `reject_log`. Holds are logged as stage `hold`. The graph node is the same. The record includes the outcome, the reason code, the next action, and a `not_paid` payment row. A reject names what the vendor must resend. A hold names what a person does.

Reject log also writes a release record. A reviewer can pay a hold from that record only when every finding is a releasable hold or a warning. The rules are in [docs/DESIGN.md](docs/DESIGN.md#releasing-a-hold).

### Tools, the ledger, and the fact guard

Audit is the only stage with tools. Critique is not given them.

The four tools are `lookup_item`, `recompute_totals`, `get_policy_rule`, and `check_payment_ledger`. They are read-only and bound to the current invoice. They do not open the database, and they do not pay. A hit on the ledger tool can add a `DUPLICATE_INVOICE` or `SPLIT_INVOICE` finding. A miss adds nothing. An identical repeat returns the earlier result, does not run the tool again, and does not count against the 8-call cap. The round still counts.

`check_payment_ledger` takes no arguments. It reports prior payments for this vendor and invoice number, plus that vendor's paid total inside the split window. Two workers, or a payment made outside this run, can write the ledger after validation and before audit. The tool reads that payment. In one process, validation already holds a duplicate or a split, so those invoices never reach the tool.

`payments.db` sits next to `inventory.db`. Only the payment step writes it. The match key is the vendor name, casefolded with whitespace collapsed, plus the normalized invoice number. `INV 1004` and `INV-1004` are the same number. The same raw text is the same match. A revision of a number that has not been paid is still only `REVISED_INVOICE`. After that original is paid, the revision is `DUPLICATE_INVOICE` and the revised-invoice finding stays. The split window is 30 days. No sample file is a split. The row is written just before the mock payment. `python3 main.py --reset-ledger` deletes `payments.db` before that one file.

Critique and audit receive the facts the code already checked: today's date, stock, whether the totals reconcile, the reference prices, and whether the dates are after today. On a critique, the guard sets aside a stock claim whose summed quantity is covered, a totals-mismatch claim when the totals already reconcile, and a future-date claim on the invoice date or the due date when that date is not after today. On an audit, the same future-date claim can be set aside. Every other concern is kept. A send-back stands while any feedback remains. An escalation does too, and either stands when the audit cites a finding code. Every set-aside is logged.

`invoice_2106.txt` is a clean $11,500 approve. The guard exists so a critique cannot hold that bill on a stock or date claim the code has already checked.

Invoice 2106 is the one unseen test invoice that should be approved, and on Oct 31 it was held on every run. Grok's critique raised a stock concern that was not real (WidgetB, 8 against 10), and the audit called its dates future dates. On Nov 1, [PR #14](https://github.com/timothytracy/galatiq-case-invoices/pull/14) gave the critique and audit prompts the code's own findings, and it drops, with a log entry, only the concerns that contradict them. The unseen set then scored 21/21 on all three runs.

When we tried Grok lookup tools on the critique in [PR #20](https://github.com/timothytracy/galatiq-case-invoices/pull/20), 2106 broke again. It first hit the call and round limits, then got a fresh critique hold, until the critique's tools were cut to one lookup per item. That PR was closed, and the tools went to the audit step only in [PR #21](https://github.com/timothytracy/galatiq-case-invoices/pull/21). With audit tools on by default in [PR #27](https://github.com/timothytracy/galatiq-case-invoices/pull/27), the unseen set scored 21/21 three times. On one of those runs 2106 hit the call limit on the way and still got the right answer.

The reject, hold, and warning tables are in [docs/DESIGN.md](docs/DESIGN.md).

## Where to read next

- [REQUIREMENTS.md](REQUIREMENTS.md) — the original brief, unchanged
- [docs/DESIGN.md](docs/DESIGN.md) — approve, reject, hold, warnings, and the knobs
- [docs/METRICS.md](docs/METRICS.md) — replay and live-run numbers
- [docs/ANSWER_KEY.md](docs/ANSWER_KEY.md) and [docs/EXTRA_ANSWER_KEY.md](docs/EXTRA_ANSWER_KEY.md) — the labeled outcomes
- [docs/graph.md](docs/graph.md) — the generated graph
- [recordings/README.md](recordings/README.md) — how the three recordings were made
