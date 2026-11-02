# Galatiq Case: Invoice Processing Automation

[![CI](https://github.com/timothytracy/galatiq-case-invoices/actions/workflows/ci.yml/badge.svg)](https://github.com/timothytracy/galatiq-case-invoices/actions/workflows/ci.yml)

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 setup_db.py
python3 main.py --invoice_path=data/invoices/invoice_1001.txt
python3 scripts/eval.py
```

A live run reads `XAI_API_KEY` from the environment or from `.env` (see `.env.example`). The eval command scores the 20 sample invoices and needs that key. Replay does not. The sample check CI runs, with no key:

```bash
python3 setup_db.py
python3 scripts/eval.py --replay recordings/samples_tools.jsonl
XAI_TOOLS=0 python3 scripts/eval.py --replay recordings/samples.jsonl
```

The other recordings are in [recordings/README.md](recordings/README.md). Decisions are in [docs/DECISIONS.md](docs/DECISIONS.md). The walkthrough is [docs/DEMO.md](docs/DEMO.md). Replay scores are in [docs/METRICS.md](docs/METRICS.md). Interview notes are in [docs/QA.md](docs/QA.md).

## Background

Acme Corp is a PE-backed manufacturing firm losing **$2M/year** on manual invoice processing. Invoices arrive via email as PDFs in messy formats with frequent errors. Staff manually extract data, validate against a legacy inventory database (inconsistent), obtain VP approval (via email chains), and process payment (via a banking API).

**Current pain points:**
- 30% error rate
- 5-day processing delays
- Frustrated stakeholders

## Objective

Build a **multi-agent system** that automates the end-to-end invoice processing workflow. The system must run as a working prototype — not just designs or slides.

## Workflow

The system should handle four stages:

1. **Ingestion** — Extract structured data from invoice documents (PDFs, text files). Fields include: Vendor, Amount, Items (with quantities), and Due Date. Expect unstructured text, typos, missing data, and potentially fraudulent entries.

2. **Validation** — Verify extracted data against a mock inventory database (SQLite). Flag mismatches such as quantity exceeding available stock or items not found in inventory.

3. **Approval** — Simulate VP-level review with rule-based decision-making (e.g., invoices over $10K require additional scrutiny). The agent should reason through approval/rejection with a reflection or critique loop.

4. **Payment** — If approved, call a mock payment function. If rejected, log the rejection with reasoning.

## Technical Requirements

- **LLM Integration**: Use xAI's Grok as the core reasoning engine (via the xAI API at https://grok.x.ai). Other models are acceptable if you don't have an API key.
- **Multi-Agent Orchestration**: Use a framework such as LangGraph, CrewAI, AutoGen, or a custom solution.
- **Agent Capabilities**: Function calling / tool use, structured outputs, and self-correction loops.
- **Runtime**: Assume no internet for external APIs — simulate everything locally.
- **Tech Stack**: Python (preferred), with libraries like `langchain`, `crewai`, `autogen`, `pdfplumber`, `PyMuPDF`, etc. Run locally — no cloud deployment.

## Provided Resources

### Mock Invoice Data

Sample invoices are provided in the `data/invoices/` directory in various formats (PDF, CSV, JSON, TXT). Use these as inputs for testing. The data intentionally includes a mix of clean entries and problematic ones — identifying and handling issues is part of the challenge.

### Mock Inventory Database (Required Setup)

Before running the system, you **must** create a local SQLite database that the validation agent will check invoices against. The sample invoices in `data/invoices/` reference specific items and quantities — your database needs to contain matching inventory records so the validation stage can flag mismatches, out-of-stock items, and unknown products.

Below is a starter schema and seed data that covers the core items referenced across the provided invoices:

```python
import sqlite3

conn = sqlite3.connect('inventory.db')  # Persist to file so all agents can access it
cursor = conn.cursor()

cursor.execute('CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)')
cursor.execute("""
    INSERT INTO inventory VALUES
    ('WidgetA', 15),
    ('WidgetB', 10),
    ('GadgetX', 5),
    ('FakeItem', 0)
""")
conn.commit()
```

**Why this matters:** The sample invoices are designed to test your validation logic against this database. For example:

| Scenario | Invoice | What should happen |
|---|---|---|
| Normal order within stock | INV-1001, INV-1004, INV-1006 | Items found, quantities valid — passes validation |
| Quantity exceeds stock | INV-1002 (requests 20× GadgetX, only 5 in stock) | Flagged as stock mismatch |
| Fraudulent / zero-stock item | INV-1003 (references FakeItem, 0 stock) | Flagged as out of stock or suspicious |
| Item not in database at all | INV-1008 (SuperGizmo, MegaSprocket), INV-1016 (WidgetC) | Flagged as unknown item |
| Invalid data | INV-1009 (negative quantity) | Flagged as data integrity issue |

You may extend the seed data with additional items or columns (e.g., unit price, category) to support richer validation — the above is the minimum needed to exercise the provided test invoices. If you want your system to also validate pricing or vendor information, consider adding tables for those as well.

### Mock Payment API

```python
def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
```

### Grok API Setup

```python
from xai import Grok

client = Grok(api_key="your_key")
response = client.chat.completions.create(
    model="grok-3",
    messages=[{"role": "user", "content": "Reason about this..."}]
)
```

## Running the System

The system should be executable from the command line:

```bash
python main.py --invoice_path=data/invoices/invoice1.txt
```

Output should include structured logs and results.

## Evaluation Criteria

- **Functionality** — Does the system work end-to-end?
- **Code Quality** — Clean, testable, well-structured code with error handling and observability
- **Agentic Sophistication** — LLM integration, multi-agent flow, tool use, self-correction loops
- **Shipping Mindset** — Valuable MVP delivered under ambiguity; scope ruthlessly cut where needed
- **Presentation** — Clear translation of technical decisions to business impact
- **Above/Beyond** - Have you made it your own? Implemented additional features that make the solution feel great? Expanded assumptions? Added to test cases?
- **UI/UX** - Users will understand and enjoy using this system.

## Submission

Submit your solution as a link to a public GitHub repository — GitHub only (github.com).

## This prototype

The brief above is unchanged. The runnable system is a LangGraph workflow. Stage logic is ordinary Python, one module per stage under `pipeline/`. Each invoice format has its own reader under `parsers/`. `graph.py` only wires the nodes and edges. The diagram is `docs/graph.md`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# set XAI_API_KEY in .env
python3 setup_db.py
python3 main.py --invoice_path=data/invoices/invoice_1001.txt
python3 scripts/eval.py
```

Use `python3` when `python` is not on PATH. `--invoice_path` is one invoice file. The process prints a JSON log for each stage and saves the same records under `logs/`. A missing `XAI_API_KEY` exits non-zero before any invoice is processed. The model is `XAI_MODEL`, default `grok-4.20-0309-non-reasoning`, called through the OpenAI SDK at `https://api.x.ai/v1`.

`main.py` creates `inventory.db` only when the file is missing. The workflow then opens it read-only. The four README seed rows are unchanged. `scripts/eval.py` runs each of the 20 sample invoices through a new graph and scores them against `data/answer_key.json`. It checks the outcome and the primary reason code. It prints how many expected warnings showed up; that line does not change the exit code. It exits 0 only when every outcome and reason code match. It exits 1 when any of those do not match, and it exits 1 when `XAI_API_KEY` is missing.

Outcomes are approve, reject, or hold. Reject wins over hold, and hold wins over approve. Every reject names the vendor's next step. Every hold names what a person has to do. A failed Grok call, after retries, is a hold with reason code `SYSTEM_ERROR`. Warnings (one fraud signal, a total within 2% under $10,000, a unit price that differs from the reference by less than 50%, a parenthetical item descriptor, a non-commercial address) are recorded and do not change the outcome by themselves. A unit price whose absolute value is 50% or more above its USD reference is a `PRICE_SPIKE` hold, not a fraud signal, and the line still keeps the price-variance warning. Reference prices are USD amounts in `policy.toml` (WidgetA 250, WidgetB 500, GadgetX 750), not extra inventory columns. Non-USD invoices are not price-checked, because nothing is converted.

Hard rules are Python: stock (summed per item, equal to stock passes, the snapshot does not decrease), line math within 0.01, and currency. Any non-USD invoice is a hold, with no conversion. A revised invoice, a renamed vendor, a reconciling credit note, a unit price 50% or more above its reference, two or more fraud signals, and an extracted item name missing from the invoice text (`ITEM_NOT_IN_SOURCE`) are holds. An unknown item stays a reject. Its reason adds "Likely meant X." only when exactly one inventory name is one letter away, and the item is never substituted. A USD total over 10000 goes through one critique pass and back to approval. Being over that threshold is not a risk. The audit can send the extraction back at most 3 times, then it escalates. Critique and audit can only make a decision stricter. Extraction, critique, and audit prompts are given today's date, and tests can inject that date. An OCR O-to-0 fix is kept only when the math then reconciles. Model calls retry in memory on timeout, 429, 5xx, network errors, JSON that is still invalid after the schema-repair loop, and approval wording that refuses the input or contradicts an approve decision (backoff 1, 2, 4, 8, 16 seconds, 5 attempts by default). Those knobs are `policy.toml`. Every problem a stage raises is a finding with a stable code. `outcome_from_findings` in `decision/outcome.py` turns that list into approve, reject, or hold. Payment runs only when a PASS audit has issued a one-time authorization for that vendor, amount, and currency.

Pending approval. Audit tools are on by default. Unset, empty, `1`, `true`, `yes`, and `on` leave them on, decided on 2026-11-01. `0`, `false`, `off`, and `no` turn them off and restore the previous audit request. Any other value stops the run at startup, because a quiet guess would turn the tools on or off by accident.

The audit step may call three read-only tools bound to the current invoice: `lookup_item`, `recompute_totals`, and `get_policy_rule`. Critique does not get tools. The tools do not decide and do not pay. If audit hits the 3-round or 8-call cap, it gets one final reply with no tools. If that reply fails, the invoice is a `SYSTEM_ERROR` hold and nothing is paid. Approved on 2026-11-01. `scripts/eval.py` prints `tools=N` on each invoice and a `tools calls=N repeats=K` line for the run. Repeats are included in the call count.

Pending approval. With `XAI_TOOLS` on, audit may also call `check_payment_ledger`. It takes no arguments and only reads this invoice's vendor and invoice number on the run's ledger. Its duplicate check matches that pair only. In a single process, validation already holds duplicates and splits before audit. The tool gives the model that evidence to cite and can only tighten the decision. If the model calls it, the tool can surface a payment another process wrote between validation and audit; the payment step's unique index already stops a second payment of the same vendor and invoice number, so the new case is a split under a concurrent writer. A missing ledger means no prior payments. A locked or corrupt ledger is a `SYSTEM_ERROR` hold and nothing is paid.

Pending approval: replay is a regression check. It feeds saved model replies back through this code, with no API key and no network, and the score should match every time. The live runs are where model variance shows up. Record once with `--record`, then `--replay` the file. A live run with no flag is unchanged: same payloads, and it does not open a recording. `--record` and `--replay` are mutually exclusive, and both work with `--ordered`. On those two modes the prompts use the `as_of` date stored in the recording header (or `--as-of` when recording), and the audit prompt carries a repo-relative invoice path. The ordered run's temporary ledger path is not part of the fingerprint. A replay miss is a `SYSTEM_ERROR` hold and nothing is paid. `scripts/eval.py` then prints one extra line, `tokens total=N cost_usd=X`, from the recording's token counts. The price is the published short-context rate for this model, with the long-context rate once a prompt reaches 200k tokens.

```bash
python3 setup_db.py && python3 -m pytest -q && python3 scripts/eval.py --replay recordings/samples_tools.jsonl && python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen_tools.jsonl && python3 scripts/eval.py --ordered --replay recordings/ordered_tools.jsonl && XAI_TOOLS=0 python3 scripts/eval.py --replay recordings/samples.jsonl && XAI_TOOLS=0 python3 scripts/eval.py --invoices data/extra_invoices --answer-key data/extra_answer_key.json --replay recordings/unseen.jsonl && XAI_TOOLS=0 python3 scripts/eval.py --ordered --replay recordings/ordered.jsonl
```

Decisions are in `docs/DECISIONS.md`. The pipeline implements the policy approved on 2026-10-31 in [docs/DECISION_POLICY.md](docs/DECISION_POLICY.md) and scores against [docs/ANSWER_KEY.md](docs/ANSWER_KEY.md). Those two documents are the approved text.

## Known limitations / cut scope

`payments.db`, next to `inventory.db`, records each accepted payment. Only the payment step writes it. The same vendor and invoice number already paid is a `DUPLICATE_INVOICE` hold. The same vendor's invoices inside `split_window_days` are a `SPLIT_INVOICE` hold when their combined total crosses the approval threshold. Both holds are Approved on 2026-11-01. Scored eval gives each invoice a fresh ledger, so the answer keys stay as they are. `scripts/eval.py --ordered` runs the files in order on one shared ledger. `python3 main.py --reset-ledger` deletes `payments.db` before that run. The in-memory authorization key still refuses a second use of the same key inside one process.
