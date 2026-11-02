# Decisions

The README above the implementation notes is the brief. This file is the set of calls the prototype makes, so they can be changed on purpose in an interview.

## Why LangGraph

The workflow is a short state machine with two cycles. Critique returns to approval. The audit can return to ingestion with field notes. LangGraph's `StateGraph` is that picture: each stage is a node, and a conditional edge is a routing rule. The library also draws the diagram in `docs/graph.md`.

CrewAI and AutoGen add role-play around the same steps. This flow does not need that. A hand-rolled loop would hide the same routes inside `if` statements. The stage bodies stay ordinary functions, one module per stage under `pipeline/`. Each invoice format has its own reader under `parsers/`. `graph.py` only wires nodes and edges.

## How to run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# set XAI_API_KEY in .env
python3 setup_db.py
python3 main.py --invoice_path=data/invoices/invoice_1001.txt
python3 scripts/eval.py
python3 scripts/export_graph.py
```

`python3 main.py --invoice_path=<file>` processes one file. It prints one JSON object per stage and writes the same records to `logs/<filename>.json`. A missing `XAI_API_KEY` stops the process with a non-zero exit before any invoice is read. `main.py` calls `setup_db.py`'s creator only when `inventory.db` is missing. The run opens the file with `sqlite3.connect("file:inventory.db?mode=ro", uri=True)`. Replay, with no key, is [Record and replay](#record-and-replay).

`scripts/eval.py` starts a new graph for each of the 20 sample files and scores the outcome and primary reason code against `data/answer_key.json`. Warning matches are printed and do not change the exit code.

## Caps

The numbers live in `policy.toml`. `decision/policy.py` loads that file at startup and rejects a missing key, a wrong type, or a nonsense value (negative cap, empty currency list) with an error that names the key.

| Knob | Value | Where |
|---|---|---|
| Scrutiny threshold | 10000 | `scrutiny_threshold`. A total in an allowed currency over this goes approval → critique → approval, then to audit. |
| Allowed currencies | USD | `allowed_currencies`. Anything else is a hold. No conversion. |
| Required fields | vendor, due date, line items, total | `required_fields`. |
| Money tolerance | 0.01 | `money_tolerance`. |
| Critique | 1 | `critique_cap`. |
| Audit send-back | 3 | `audit_send_back_cap`. The next send-back verdict escalates to a hold. |
| Model attempts | 5 | `model_retry_attempts`. `XAI_MAX_ATTEMPTS` still overrides one run. |
| Backoff | 1, 2, 4, 8, 16 seconds plus up to 25% jitter | `BACKOFF_SECONDS` in `llm_client.py`. |
| Schema repair | 2 tries inside one attempt | `SCHEMA_REPAIR_LIMIT`. The second try is the repair. |

There is no durable work list and no extra database table. When the attempts are used up, or the error is 400, 401, or 403, the outcome is `hold` with reason code `SYSTEM_ERROR` and nothing is paid. The log line is `agent step failed after N attempts: <error type>`.

When `XAI_TOOLS` is on, a hit on the audit's 3-round or 8-call cap does not hold immediately. The step gets one plain model call with no tools, using the tool results already gathered and the verified facts, and asks for the verdict. The cap has already spent the budget, so that reply is the last chance. An API error, or a schema failure after the normal repair, is a `SYSTEM_ERROR` hold and nothing is paid. That bound keeps cost and latency from growing. Approved on 2026-11-01.

Pending approval. When the audit tools (XAI_TOOLS) are in use, audit may also call `check_payment_ledger`. Critique does not get tools.

The tool takes no arguments. It is bound to this invoice's vendor, invoice number, and the run's ledger path, so the model cannot name another vendor. It is read-only. Its duplicate check matches that vendor and invoice number only. It does not match the raw-text hash. Validation does, so the tool is a narrower lookup than the validation duplicate check. It also reports the same vendor's paid total inside `split_window_days` against `scrutiny_threshold`, using the same split helper as validation.

In a single process the tool is redundant with validation. A duplicate or a split is already a hold at validation, and the graph goes to `reject_log` before audit, so audit never calls the tool on those invoices. The tool can only tighten a decision that reached audit. A hit adds a `DUPLICATE_INVOICE` or `SPLIT_INVOICE` finding, and the audit cannot pay over that hit. A miss adds nothing, so it cannot loosen the decision. If the model calls it, the tool can surface a payment another process wrote between validation and audit. The payment step's unique index already stops a second payment of the same vendor and invoice number, so the new case is a split under a concurrent writer.

A missing ledger means no prior payments. A locked or corrupt ledger is not reported as empty. `sqlite3.Error` leaves the tool. The tool loop treats that as a non-retryable failure. The invoice is a `SYSTEM_ERROR` hold and nothing is paid. That is the same outcome validation uses when it cannot read the ledger. Caps, repeat reuse, and the forced final turn are unchanged. Scored eval still uses a fresh ledger per invoice.

## Record and replay

Pending approval. Replay is a regression check. It feeds saved model replies back through this code, with no API key and no network. Record once with `--record`, then `--replay` the file. A live run with neither flag does not open a recording. `--record` and `--replay` are mutually exclusive, and both work with `--ordered`.

The fingerprint is a SHA-256 of canonical JSON. The hashed fields are the model, the full message list (including tool results), the tools offered or null, tool_choice or null, response_format or null, and temperature. The request sends temperature 0. The hash does not include the API key, a timestamp, or a request id. A temporary payments ledger path (`payments-*.db` or `payments-ordered-*.db` under the system temp directory) is replaced with `payments-ledger.db` before the hash, so a new temp file still matches. Record and replay also put a repo-relative invoice path in the audit prompt. Live prompts keep the path they were given.

A replay miss (no saved reply for that fingerprint) is a non-retryable error. It is not a live call. The invoice is a `SYSTEM_ERROR` hold and nothing is paid.

The recording starts with a header line that stores `as_of`, the date the prompts were told was today, and the model name. Replay uses that date, not the machine clock. `--as-of` on a recording run stores that date. Omit it and the header stores the calendar date at the start of the run. The five committed files each have `"as_of": "2026-11-01"` in the header.

`scripts/eval.py` on record or replay prints `tokens total=N cost_usd=X` from the usage objects stored on each request line. The price is in `recording.py`: the published short-context rate for `grok-4.20-0309-non-reasoning` when `prompt_tokens` is under 200,000, and the long-context rate at or above that. Prompt tokens are not split into a cached-input rate. Pending approval on that price.

CI (`.github/workflows/ci.yml`) runs `setup_db.py`, the tests, and the five replays. It does not set an API key. The two `_tools` recordings match a run with `XAI_TOOLS=1`. The other three match a run that does not offer the audit tools. The tools change the audit prompt, so the fingerprints do not cross. The commands are in [recordings/README.md](../recordings/README.md).

## What is Python and what is the model

Python decides. The model writes.

- Every problem a stage raises is a `Finding` (`code`, `message`, `severity`, `item`). `outcome_from_findings` is the one place that turns that list into an outcome: a blocking reject-class code rejects, a hold-class code holds, and neither approves. Reject wins over hold. Warnings stay on the finding list and do not decide. The plain-English reason names the lower-precedence findings too. `KEY_REASON_TO_CODE` in `decision/wording.py` is the only map from the answer key's finding names to these codes.
- Validation sums quantities per item and compares them to the fixed snapshot. `quantity > stock` fails. `quantity == stock` passes. Approving an invoice does not change stock. The README table says INV-1001, INV-1004, and INV-1006 all pass, and WidgetA on those three is 10 + 3 + 5 = 18, which is more than 15, so the stock balance cannot be decrementing.
- Line math is recomputed. Printed line amounts, the subtotal, and subtotal + tax + shipping must match within 0.01.
- Currency other than USD is a hold. The message names the code that was found, for example `Non-USD currency (EUR).` `€`, `EUR`, `euro`, and `euros` count, in any case, as do currency fields in JSON, XML, and CSV. A missing currency on a document with `$` amounts is USD. Nothing is converted.
- One fraud signal (urgency, a wire transfer, or a new remittance account) is a warning. Two or more, with no reject, are a hold. A unit price 50% or more above its USD reference is a `PRICE_SPIKE` hold on its own and is not a fraud signal. A smaller price difference is a warning. The word `formerly` is a renamed-vendor hold, not a fraud signal. A new bank account on an otherwise clean invoice over $10,000 is a hold on its own. A hard failure (bad quantity, unknown item, stock, math, missing vendor / due date / total / lines, embedded instructions, a negative amount that is not a labeled credit note) is a reject and wins over a hold. An unknown item's reason adds "Likely meant X." only when exactly one inventory name is one letter away; the item is not substituted. An extracted item name that is not in the invoice text is an `ITEM_NOT_IN_SOURCE` hold, and the reject still wins.
- A USD total over 10000 is not an automatic reject and is not itself a risk. It is the critique loop. The critique may only make the decision stricter, and a concern that only says the total is over the threshold is ignored.
- USD reference prices live in `policy.toml`: WidgetA 250, WidgetB 500, GadgetX 750. Price checks compare `abs(unit_price)` with that reference, so a -250 credit line for WidgetA is not a variance. Below `price_spike_ratio` (0.50) the difference is a warning. At or above it, the line is a `PRICE_SPIKE` hold and still keeps the warning. The inventory table is still the four README stock rows.
- A revised invoice (a revision marker, or the words "revised invoice") is a hold. A labeled credit note whose math reconciles is a hold. Any other negative quantity or total is a reject.
- Embedded instructions are a reject only when the extraction model flags them and the text matches an imperative system-directed phrase (`ignore previous instructions`, `approve this invoice`, `system:`, `skip review`). The bare word `approve` does not match.
- A non-commercial address is a warning. Extraction sets the flag when the address looks like a residence, a famous non-business landmark, or a well-known fictional address, and gives a short reason. The only deterministic cue is an illustrative `Apt` or `Apartment`. It does not change the outcome.
- The audit compares extracted fields with the raw text, checks the decision against the findings, recomputes the math and the fraud signals, and checks that critique concerns were named in the revised reasoning. It can only make the outcome stricter. It cannot turn a hard failure or a non-USD invoice into a payment. An escalation or send-back is dropped only when its `cited_codes` list is non-empty and every code is a warning. That drop is recorded as `AUDIT_NOTE`.
- Extraction, the risks and conclusion inside the approval reasoning, the critique, and the audit verdict are Grok calls. The client is the OpenAI SDK pointed at `https://api.x.ai/v1`. The model is `XAI_MODEL`, default `grok-4.20-0309-non-reasoning`. `rules_applied` and `evidence` are filled in Python from the real findings and amounts, so the model cannot invent the facts. Revisions append the previous reasoning when approval runs again after critique, and when the audit tightens a decision.

The reasoning object on the decision has `rules_applied`, `evidence`, `risks_considered`, and `conclusion`. It is stored on the stage log.

## Payments ledger

Approved on 2026-11-01. `payments.db`, next to `inventory.db`, records each accepted payment. Only the payment step writes it. The match key is the vendor name, casefolded with whitespace collapsed, plus the invoice number after normalization (`INV 1004` and `INV-1004` are the same). The same raw text (sha256) is the same match. A second file with the same vendor, number, and total is the same match. A revised bill, or a different total, for a number that was already paid is the same match, and the revised-invoice finding stays on the list.

`DUPLICATE_INVOICE` is a hold: do not pay it again. `SPLIT_INVOICE` is a hold: this total is at or under the approval threshold, and this total plus the same vendor's paid totals whose printed invoice dates fall within `split_window_days` (30) is over the threshold. A date that does not parse is outside the window. Another vendor does not count. No sample file is a split. The split cases are unit-tested on synthetic invoices.

A missing or empty file means no prior payments. A locked or corrupt ledger is not treated as empty. A `sqlite3.Error` on the read, at validation, or on the write, at payment, is a `SYSTEM_ERROR` hold and nothing is paid. The reader opens the file read-only. The writer waits 5 seconds on a lock (`_LOCK_TIMEOUT` in `ledger/payments.py`).

The unique index on `(vendor_key, invoice_number)` refuses a second row, including across processes. That refusal pays nothing and is a hold (`DUPLICATE_INVOICE`, and `PAYMENT_REFUSED` is also recorded). It does not crash. The row is written just before the mock payment. With a real gateway, a failure after the insert would need the row removed or the order changed (write after a confirmed payment, or a pending status).

The in-memory authorization key still refuses a second use of the same audit key inside one process. Scored eval gives each invoice a fresh temporary ledger and deletes it when the call returns, so the answer keys stay as they are. `scripts/eval.py --ordered` runs `data/ordered_scenario.json` on one shared temporary ledger, not on `payments.db`. `python3 main.py --reset-ledger` deletes `payments.db` before that one file. A missing API key does not delete it, because the client is created first.

The split and same-text checks run at validation and assume one process at a time. Two processes can each pay half of a split before either has written. The unique index does not stop that, because the invoice numbers differ.

A revision of a number that has not been paid is still only `REVISED_INVOICE`. On a fresh ledger, `invoice_1004_revised.json` is that case. Once `invoice_1004.json` has been paid, the revision is `DUPLICATE_INVOICE` and the revised-invoice finding stays. The ordered scenario is that second case: the step is a duplicate hold, and the scenario still requires `REVISED_INVOICE` on the finding list.

## Known limitations / cut scope

The payments ledger is the section above. The two gaps that remain are the write-before-the-mock and the single-process split check.

USD reference prices are in `policy.toml`. The rush line on INV-1010 is a descriptor note and a price warning, not a reject. The EUR invoice is not price-checked. The inventory table is the four README rows and has no extra columns.

INV-1005 is rejected because GadgetX 8 exceeds stock 5. Its address is recorded as a note. Due dates are kept as printed. A value that is not a date, such as "yesterday", is a warning. A missing due date is a reject because the brief requires that field. Agents are told today's date so a model does not treat April 2026 as the future.

## Answer key

Each label is the rule result for that file alone. The one-line text is `data/labels.json`.

| File | Outcome | Why |
|---|---|---|
| invoice_1001.txt | approve | WidgetA 10 and WidgetB 5 are inside the snapshot. README: passes. |
| invoice_1002.txt | reject | GadgetX 20 exceeds stock 5. README: stock mismatch. |
| invoice_1003.txt | reject | FakeItem quantity 100 exceeds stock 0. README: out of stock or suspicious. |
| invoice_1004.json | approve | WidgetA 3 and WidgetB 2 are inside the snapshot. README: passes. |
| invoice_1004_revised.json | hold | Revision R1. Stock and math pass. A person confirms it replaces an unpaid original. |
| invoice_1005.json | reject | GadgetX 8 exceeds stock 5. |
| invoice_1006.csv | approve | WidgetA 5 and WidgetB 3 are inside the snapshot. README: passes. |
| invoice_1007.csv | reject | WidgetA 20 and WidgetB 15 exceed stock, and the printed total is 110.00 low. |
| invoice_1008.txt | reject | SuperGizmo and MegaSprocket are not in the database. README: unknown item. |
| invoice_1009.json | reject | WidgetA quantity is negative. README: data integrity. The vendor and due date are also missing, and the subtotal does not match the lines. |
| invoice_1010.txt | approve | Summed WidgetA is 12, which is inside 15. Shipping is included in the total. |
| invoice_1011.pdf | approve | WidgetA 6 and WidgetB 3 are inside the snapshot. |
| invoice_1011.txt | approve | Same lines as the PDF. Judged on its own. |
| invoice_1012.pdf | hold | The vendor line says formerly FastShip. |
| invoice_1012.txt | hold | The same formerly line. Judged on its own. |
| invoice_1013.json | reject | Summed WidgetA 22, WidgetB 18, and GadgetX 9 exceed stock, and the printed total is 50.00 high. |
| invoice_1013.pdf | reject | Same stock and total failures as the JSON file. |
| invoice_1014.xml | hold | Currency is EUR. |
| invoice_1015.csv | approve | WidgetA 10, WidgetB 5, and GadgetX 2 are inside the snapshot. |
| invoice_1016.json | reject | WidgetC is not in the database. README: unknown item. |

Seven approve, nine reject, four hold. `scripts/eval.py` scores outcome and primary reason code against `data/answer_key.json`. `data/labels.json` is the original label file and is not read by eval.

## Twins

A twin is labeled from its own file. None of these outcomes differ from the original file:

- `invoice_1004.json` approves. `invoice_1004_revised.json` is a hold because it is marked as a revision, even though its own stock and math pass.
- `invoice_1011.txt` and `invoice_1011.pdf` both approve.
- `invoice_1012.txt` and `invoice_1012.pdf` are both holds, because both contain the formerly line.
- `invoice_1013.pdf` and `invoice_1013.json` both reject, for the same stock and total failures.
