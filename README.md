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

Live runs the same day matched those scores: samples 20/20 three times, unseen 21/21 three times, and the ordered ledger ok. The 21 unseen invoices and their answer key were written by the author, not by Acme, so treat that score as a regression check rather than an independent test.

### Quick start

Python 3.12 (the version CI uses). No API key needed.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest
python3 scripts/eval.py --replay recordings/samples.jsonl
python3 main.py --invoice_path=data/invoices/invoice_1001.txt --reset-ledger --replay recordings/samples.jsonl
streamlit run ui/app.py
```

That runs the test suite, scores the 20 samples from the recorded Grok run (exits 0 at 20/20), walks one invoice through every agent, and opens the review screen in Replay mode. The databases and logs always go in the repository root (or `INVOICE_DATA_DIR`). Invoice and recording paths are relative to where you run, so the commands above assume the repository root. How each agent works, and why code decides, is in [docs/DESIGN.md](docs/DESIGN.md).

![Review screen from the recorded Grok run](docs/review_screen.png)

The review screen shows ten sample invoices from that recorded run.

[![Demo of the review screen replaying the recorded Grok run](docs/demo.gif)](docs/demo.mp4)

**[Watch the full demo video (MP4)](docs/demo.mp4)**

Demo: the review screen replays the recorded Grok run with no API key, shows approve, reject and hold outcomes with their reasons and audit trail, pays held invoice 1012 from the hold queue, and refuses a second payment.

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

### Invoice 2106

Invoice 2106 is the one unseen test invoice that should be approved, and on Oct 31 it was held on every run. Grok's critique raised a stock concern that was not real (WidgetB, 8 against 10), and the audit called its dates future dates. On Nov 1, [PR #14](https://github.com/timothytracy/galatiq-case-invoices/pull/14) gave the critique and audit prompts the code's own findings, and it drops, with a log entry, only the concerns that contradict them. The unseen set then scored 21/21 on all three runs.

When we tried Grok lookup tools on the critique in [PR #20](https://github.com/timothytracy/galatiq-case-invoices/pull/20), 2106 broke again. It first hit the call and round limits, then got a fresh critique hold, until the critique's tools were cut to one lookup per item. That PR was closed, and the tools went to the audit step only in [PR #21](https://github.com/timothytracy/galatiq-case-invoices/pull/21). With audit tools on by default in [PR #27](https://github.com/timothytracy/galatiq-case-invoices/pull/27), the unseen set scored 21/21 three times. On one of those runs 2106 hit the call limit on the way and still got the right answer.

### What I cut, and why

| Cut | Why | Production next step |
|---|---|---|
| Email ingestion | The brief's invoices arrive by email, but parsing mailboxes adds nothing to the decision logic. The pipeline takes one file, which is what an inbox watcher would hand it. | A mailbox watcher that drops attachments into the same CLI path. |
| OCR for scanned PDFs | All provided PDFs have a text layer. OCR-style damage ("2O26") is handled at the text level. | Grok vision or Tesseract for image-only PDFs, feeding the same extraction step. |
| Currency conversion | Paying from a static, stale FX rate is a real money risk, and it changes no outcome on this data. Non-USD invoices are held, never converted. | A dated rate feed, with finance confirming the rate on the hold. |
| A vendor master (bank account on file) | The brief supplies no vendor data. Renames and bank changes are caught from the invoice text. | A vendor table with the bank account on file, so a bank change is a code check, not a judgment call. |
| Purchase-order matching | The brief supplies no PO data. Stock and reference prices stand in for the PO check. | Match each invoice line to an open PO line for item, quantity, and price before approval. |
| Live inventory | Stock is a read-only snapshot so each invoice gets the same answer whatever ran before it. | Read stock and PO coverage from the system of record at decision time. |
| A second approver | One reviewer can release a vendor-identity or bank-change hold. | Two-person approval for those holds. |
| Multi-process safety | Two workers could each pay half of a split invoice. The prototype runs one process at a time. | A database lock or queue around the split check and payment. |
| Auth and a shared review queue | The brief says run locally. Review notes are local files. | Sign-in, roles, and a durable queue so a release is attributed to a person. |
| A dedicated fraud agent | Fraud signals are caught by validation rules and the approval and critique steps, which covers every fraud case in this data. A separate agent was not needed for an MVP. | A fraud agent with its own prompt, tools, and labeled set, so it can be evaluated and improved without touching the rest of the pipeline. |
| Offline mode (fake model) | Not in the brief, and replay already runs every provided invoice with no key. The fake model stays as a test fixture. | None. |
| Memory from human reviews | Each run starts fresh. A reviewer's decision and note are recorded, but the agents never read them. | When a reviewer resolves a hold with a justification, they choose whether the agents should remember it. A kept lesson, such as a confirmed vendor rename, goes into the context of later runs. |

Duplicate detection almost made this list. At first I judged each invoice file on its own content and listed duplicates as a known limitation. Then I thought about what that means in practice: a system that pays every invoice it is shown pays the same bill twice whenever a vendor resends it, sends it in another format, or sends a revised copy, and over time that loses real money. So I built a lasting payments ledger instead. Validation checks it on arrival for an exact duplicate, the same invoice in a different format, a revision, or a split, where invoices under $10,000 from one vendor within 30 days add up to more than that. Any match is a hold, not a second payment. The audit step can read the same ledger before payment, and a unique index on vendor and invoice number blocks a second payment even if both checks miss.

## How to run

Replay first. No API key.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate
python3 scripts/eval.py --replay recordings/samples.jsonl
python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl
python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl
```

Each of those three exits 0 when the score is perfect. The unseen scorer checks the outcome plus the primary reason code, or a listed alternate code from the answer key. `invoice_2115.txt` is the example: the deterministic parser rejects the misread total `$3,5OO.00` as `MISSING_FIELD`, and the recorded model read gives the listed alternate `TOTAL_MISMATCH`. Add `--verbose` to also see each stage's JSON record. CI runs the same three after the tests and does not set an API key.

**Loops in action.** Critique and audit are defense in depth behind validation: code decides first, and the loops can only tighten what it passed. They were tested over 3 live runs of the loop set in `data/loop_invoices/`; replay it with `python3 scripts/eval.py --invoices data/loop_invoices --answer-key data/loop_answer_key.json --replay recordings/loop.jsonl` (CI does too).

### Reviewer quick path

To watch one invoice go through every agent with no API key:

```bash
python3 main.py --invoice_path=data/invoices/invoice_1001.txt --reset-ledger --replay recordings/samples.jsonl
```

`--replay` answers each Grok call from the recorded live run, so the same code runs with no network. `--reset-ledger` deletes `payments.db` first, so the invoice is not seen as already paid. The output is one JSON record per stage: ingestion, validation, approval, audit with its tool calls, and payment. Then comes `Paid 5000.00 to Widgets Inc.` and a final `result` record with the outcome. Any file in `data/invoices/` works the same way. Try `invoice_1002.txt` for a stock reject or `invoice_1012.txt` for a hold.

`inventory.db`, `payments.db`, and `logs/` live in the data folder: the repository root, or `INVOICE_DATA_DIR` when it is set. The folder you run from does not change it, so the command line, the review screen, and a release all read the same ledger. `--reset-db` deletes `inventory.db` and rebuilds it from the seed. `--reset-ledger` deletes `payments.db`, including the release records stored there, so a held invoice must be rerun before it can be approved. `--reset` does both. `scripts/eval.py --reset` and `--reset-db` rebuild inventory only; eval does not use `payments.db`. They do not change `recordings/`, `data/`, or `logs/`.

A live run needs `XAI_API_KEY` in the environment or in `.env` (see `.env.example`).

```bash
cp .env.example .env
# set XAI_API_KEY in .env
python3 main.py --invoice_path=data/invoices/invoice_1001.txt
```

The review screen is `streamlit run ui/app.py`. With no key it uses replay, which covers every provided invoice. A new invoice needs `XAI_API_KEY`.

A reviewer releases a hold with `approve_and_pay`. This demo replays two recorded invoices, then tries to release each. It uses a temporary ledger and does not call the model.

```bash
python3 scripts/approve_hold_demo.py --demo
```

`invoice_1012.txt` is a `VENDOR_RENAMED` hold, and it is paid: `Paid 9975.00 to QuickShip Distributers (formerly FastShip Ltd.)`. `invoice_1002.txt` is a stock reject, and it stays unpaid with reason `NOT_PAYABLE`. Both keep their pipeline outcome.

## Where to read next

- [REQUIREMENTS.md](REQUIREMENTS.md) — the original brief, unchanged
- [docs/DESIGN.md](docs/DESIGN.md) — the agents, why code decides, approve, reject, hold, warnings, and the knobs
- [docs/METRICS.md](docs/METRICS.md) — replay and live-run numbers
- [docs/ANSWER_KEY.md](docs/ANSWER_KEY.md) and [docs/EXTRA_ANSWER_KEY.md](docs/EXTRA_ANSWER_KEY.md) — the labeled outcomes
- [docs/graph.md](docs/graph.md) — the generated graph
- [recordings/README.md](recordings/README.md) — how the three recordings were made
