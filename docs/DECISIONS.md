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

`python3 main.py --invoice_path=<file>` processes one file. It prints one JSON object per stage and writes the same records to `logs/<filename>.json`. A missing `XAI_API_KEY` stops the process with a non-zero exit before any invoice is read. `main.py` calls `setup_db.py`'s creator only when `inventory.db` is missing. The run opens the file with `sqlite3.connect("file:inventory.db?mode=ro", uri=True)`.

`scripts/eval.py` starts a new graph for each of the 20 sample files and scores the outcome and primary reason code against `data/answer_key.json`. Warning matches are printed and do not change the exit code.

## Caps

The numbers live in `policy.toml`. `rules.py` loads that file at startup and rejects a missing key, a wrong type, or a nonsense value (negative cap, empty currency list) with an error that names the key.

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

## What is Python and what is the model

Python decides. The model writes.

- Every problem a stage raises is a `Finding` (`code`, `message`, `severity`, `item`). `outcome_from_findings` is the one place that turns that list into an outcome: a blocking reject-class code rejects, a hold-class code holds, and neither approves. Reject wins over hold. Warnings stay on the finding list and do not decide. The plain-English reason names the lower-precedence findings too. `KEY_REASON_TO_CODE` in `rules.py` is the only map from the answer key's finding names to these codes.
- Validation sums quantities per item and compares them to the fixed snapshot. `quantity > stock` fails. `quantity == stock` passes. Approving an invoice does not change stock. The README table says INV-1001, INV-1004, and INV-1006 all pass, and WidgetA on those three is 10 + 3 + 5 = 18, which is more than 15, so the stock balance cannot be decrementing.
- Line math is recomputed. Printed line amounts, the subtotal, and subtotal + tax + shipping must match within 0.01.
- Currency other than USD is a hold. The message names the code that was found, for example `Non-USD currency (EUR).` `€`, `EUR`, `euro`, and `euros` count, in any case, as do currency fields in JSON, XML, and CSV. A missing currency on a document with `$` amounts is USD. Nothing is converted.
- One fraud signal (urgency, a wire transfer, a new remittance account, or a unit price more than 50% above the reference) is a warning. Two or more, with no reject, are a hold. The word `formerly` is a renamed-vendor hold, not a fraud signal. A new bank account on an otherwise clean invoice over $10,000 is a hold on its own. A hard failure (bad quantity, unknown item, stock, math, missing vendor / due date / total / lines, embedded instructions, a negative amount that is not a labeled credit note) is a reject and wins over a hold.
- A USD total over 10000 is not an automatic reject and is not itself a risk. It is the critique loop. The critique may only make the decision stricter, and a concern that only says the total is over the threshold is ignored.
- USD reference prices live in `policy.toml`: WidgetA 250, WidgetB 500, GadgetX 750. A different unit price is a warning. The inventory table is still the four README stock rows.
- A revised invoice (a revision marker, or the words "revised invoice") is a hold. A labeled credit note whose math reconciles is a hold. Any other negative quantity or total is a reject.
- Embedded instructions are a reject only when the extraction model flags them and the text matches an imperative system-directed phrase (`ignore previous instructions`, `approve this invoice`, `system:`, `skip review`). The bare word `approve` does not match.
- A non-commercial address is a warning. Extraction sets the flag when the address looks like a residence, a famous non-business landmark, or a well-known fictional address, and gives a short reason. The only deterministic cue is an illustrative `Apt` or `Apartment`. It does not change the outcome.
- The audit compares extracted fields with the raw text, checks the decision against the findings, recomputes the math and the fraud signals, and checks that critique concerns were named in the revised reasoning. It can only make the outcome stricter. It cannot turn a hard failure or a non-USD invoice into a payment. An escalation or send-back is dropped only when its `cited_codes` list is non-empty and every code is a warning. That drop is recorded as `AUDIT_NOTE`.
- Extraction, the risks and conclusion inside the approval reasoning, the critique, and the audit verdict are Grok calls. The client is the OpenAI SDK pointed at `https://api.x.ai/v1`. The model is `XAI_MODEL`, default `grok-4.20-0309-non-reasoning`. `rules_applied` and `evidence` are filled in Python from the real findings and amounts, so the model cannot invent the facts. Revisions append the previous reasoning when approval runs again after critique, and when the audit tightens a decision.

The reasoning object on the decision has `rules_applied`, `evidence`, `risks_considered`, and `conclusion`. It is stored on the stage log.

## Known limitations / cut scope

Duplicates are not detected across files. Each invoice is evaluated independently. Payment will not run twice for the same audit key inside one process (an in-memory set). A second copy of the same bill is a different file path, so it can still be approved on its own. In production, check a persistent payments ledger before paying.

A revision marker is a hold for that invoice alone. There is still no invoice-number memory, so the hold asks a person to confirm the original was not paid. It does not look up the other file.

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
