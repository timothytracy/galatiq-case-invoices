# Phase 1 decisions

The README is the brief. This file records the calls Phase 1 had to make so a later phase, and a live interview, can change them on purpose.

Phase 1 runs the four stages end to end. Ingestion really parses. Validation and approval are stubs: they do not apply the rules below, and they do not read `data/labels.json`. The label file is the answer key for later phases.

## How to run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 setup_db.py
python3 main.py --invoice_path=data/invoices/invoice_1001.txt --offline
python3 main.py --invoice_path=data/invoices --offline
python3 main.py --batch=data/invoices --offline
```

Use `python3` when `python` is not on PATH. The flag name is `--invoice_path`, as in the README. A folder given to that flag is a batch. `--batch` is the same thing. `--offline` forces offline mode.

`main.py` creates `inventory.db` in the current directory if the file is missing. Logs go to `./logs/`. One file per invoice, named `<original filename>.json`, holds four stage records. `logs/summary.json` is the run summary. `invoice_1011.pdf` and `invoice_1011.txt` therefore cannot overwrite each other.

Copy `.env.example` to `.env` if you have a key. The program never prints the key.

## Policies used for the answer key

These match `data/labels.json` and `rules.py`.

1. **Stock is a fixed snapshot.** Approving an invoice does not reduce stock. The README table says INV-1001, INV-1004, and INV-1006 all pass. WidgetA on those three invoices is 10 + 3 + 5 = 18, which is more than 15, so a decrementing stock balance cannot be what that table means.
2. **Quantities are summed per item** on one invoice before the stock check. `quantity > stock` fails. `quantity == stock` passes. INV-1004 R1 asks for 5 GadgetX and stock is 5, so stock is not why that file is rejected.
3. **A duplicate or revision is never paid twice.** Invoice numbers are normalized (`1002` and `INV 1012` both become `INV-1002` / `INV-1012`). In a batch, files are sorted by filename. The first file for a number is judged on its own merits. A later file with the same number is not paid. Across runs, a later phase should remember paid invoice numbers. Phase 1 does not remember them yet.
4. **EUR converts to USD at 1.10** before the $10,000 check. The payment itself is the printed amount in the printed currency. `mock_payment` has no currency argument; the payment log stores the currency beside the amount. 4125 EUR × 1.10 = 4537.50 USD, which is under $10,000.
5. **The orchestrator is plain Python** in `pipeline.py`. No LangGraph, CrewAI, or AutoGen.

`needs_human: true` means do not call `mock_payment`. The decision is still `approve` or `reject`. In this answer key, a hold is `reject` plus `needs_human`, so it cannot be confused with a clean approval.

Over $10,000 USD means extra scrutiny in a later phase, not an automatic reject. No sample invoice is both clean and over $10,000, so that loop does not change any label. A later critique loop may downgrade an approval. It must not override a hard failure (stock, unknown item, bad quantity, bad arithmetic, duplicate).

Arithmetic, for later phases: subtotal + tax + shipping must match the printed total within $0.01. The parser keeps the printed total when it does not match. Shipping on INV-1010 counts.

## Answer key

Labels assume the full sample set, processed in filename order. A file that is only a duplicate can look fine on its own. INV-1013 fails stock and arithmetic even when run alone.

| File | Decision | Needs human | Why |
|---|---|---|---|
| invoice_1001.txt | approve | no | Within stock. README: passes. |
| invoice_1002.txt | reject | no | GadgetX 20 > 5. README: stock mismatch. |
| invoice_1003.txt | reject | no | FakeItem stock is 0. README: out of stock or suspicious. |
| invoice_1004.json | approve | no | Within stock. README: passes. |
| invoice_1004_revised.json | reject | yes | Second file for INV-1004. Never pay a revision again. |
| invoice_1005.json | reject | no | GadgetX 8 > 5. |
| invoice_1006.csv | approve | no | Within stock. README: passes. |
| invoice_1007.csv | reject | no | WidgetA 20 > 15, WidgetB 15 > 10, and the printed total is $110 low. |
| invoice_1008.txt | reject | no | SuperGizmo and MegaSprocket are unknown. README: unknown item. |
| invoice_1009.json | reject | no | Negative quantity. README: data integrity. |
| invoice_1010.txt | approve | no | Summed WidgetA is 12, which is within 15. |
| invoice_1011.pdf | approve | no | Within stock. First file for INV-1011. |
| invoice_1011.txt | reject | no | Duplicate of INV-1011. |
| invoice_1012.pdf | reject | yes | Vendor rename. See the call below. |
| invoice_1012.txt | reject | no | Duplicate of INV-1012. |
| invoice_1013.json | reject | no | Summed stock fails, and the printed total is $50 high. |
| invoice_1013.pdf | reject | no | Same failures, and it is the second INV-1013 file. |
| invoice_1014.xml | approve | no | Within stock. EUR amount is under $10,000 after conversion. |
| invoice_1015.csv | approve | no | Within stock. |
| invoice_1016.json | reject | no | WidgetC is unknown. README: unknown item. |

Seven files approve: 1001, 1004, 1006, 1010, 1011.pdf, 1014, 1015. Thirteen reject. Two of the rejects are holds (`needs_human`): the 1004 revision and 1012.pdf.

## Calls that were ambiguous

The analysis of the brief marked these as judgment calls. This is the choice, and the alternative.

1. **INV-1004 R1.** Choice: do not auto-pay. `invoice_1004.json` sorts first and is the payable copy. The revision is `reject` + `needs_human`, because a person may still owe a delta and paying both would pay $7,830 against a $5,940 bill. Alternative: pay the revision and ignore the original. Not chosen, because the safe side is paying once.
2. **Format twins (1011, 1012, 1013).** Choice: filename order, first file wins, later files are not paid. `invoice_1011.pdf` sorts before `invoice_1011.txt`. Same for 1012. For 1013 the json sorts before the pdf, and both fail validation anyway. Alternative: pay whichever file is processed, with no memory. Not chosen, because the same bill would be paid twice. Run alone, 1011.txt would pass the other rules. The label describes the full set.
3. **INV-1012 vendor rename.** The line is `QuickShip Distributers (formerly FastShip Ltd.)`. Stock and arithmetic pass, and $9,975 is under $10,000, so a literal reading approves it. Choice: `reject` + `needs_human`, and do not auto-pay. There is no vendor master to confirm a rename, and a rename is a common fraud pattern. This is the call that drops the approve count from the literal 8 to 7. Alternative: approve it and leave the rename as a note.
4. **EUR and the $10,000 line.** Choice: fixed rate 1.10, documented in `rules.py` as `EUR_USD_RATE`. Pay 4125 EUR, not 4537.50 USD. The rate does not change INV-1014's outcome at any realistic rate. Alternative: ignore currency and compare 4125 to 10000 directly. The result is the same for this invoice; the rate is still written down so a larger EUR invoice would be handled the same way.
5. **INV-1010 rush price ($300 vs $250) and the EUR unit prices.** Choice: not a reject. No unit-price table was added. The sample prices are not one catalog (250 USD, 300 rush, 240 discount, 225 EUR), so a price table would be invented. The `inventory` table is exactly the four README rows and has no extra columns. Alternative: add a price list and flag the rush line.
6. **INV-1005's address (1600 Pennsylvania Ave) and INV-1006's vendor name echoing Acme.** Choice: not reject rules. 1005 is rejected for stock. There is no vendor master. Alternative: treat the address as fraud.
7. **Date quirks.** INV-1001's due date is a day off from "Net 15". INV-1002's due date equals the invoice date. INV-1003 says due "yesterday". Choice: keep the printed text (OCR digit repair aside) and do not reject on dates alone. 1003 is rejected because of FakeItem. Alternative: reject any due date that is not after the invoice date.
8. **Over $10,000.** Choice: scrutiny later, not an automatic reject. Hard failures still reject. Alternative: reject every total over $10,000. Not chosen, because the brief asks for scrutiny, and several over-$10,000 invoices are already rejected for stock or arithmetic.

## What Phase 1 actually decides

The approval stub approves when the parser found a vendor, an invoice number, at least one line item, and a total greater than zero. Otherwise it rejects. If validation has already set `passed` to false, it rejects for that too. Phase 1 validation never does, so the sample runs do not hit that line. The stub does not itself check stock, duplicates, fraud, arithmetic, or the threshold. It does record `amount_usd` using `rules.to_usd`, so the log shows the number later phases will compare to $10,000.

That means the stub will not match this answer key. On the 20 sample files it approves 19 and rejects only INV-1009, because the vendor is blank and the total is negative. The label also rejects INV-1009, for the negative quantity. The other 12 label rejects are approved by the stub. That gap is expected.

## Parsing

Readers live in `parsers.py`. They are generic format readers, not branches on invoice number or file name.

- JSON objects, including a vendor object and a revision field.
- CSV `field,value` rows with repeated item keys, and CSV one-row-per-line files with a summary footer.
- XML with `invoice_number`, `line_items/item`, and `totals`.
- Text, email text (`From:`), and PDF text extracted with `pypdf`.
- Item codes drop internal spaces and a trailing parenthetical (`Widget A` → `WidgetA`, `WidgetA (rush order)` → item `WidgetA`, note `rush order`).
- A letter O inside a numeric token becomes 0 (`2O26`, `$3,500.O0`). `source.raw_text` keeps the original characters.
- Relative dates such as `yesterday` stay as printed.

## Grok client

`llm_client.py` uses the `openai` package with `base_url` `https://api.x.ai/v1` and chat completions at temperature 0. The default model is `grok-4.7`, override with `XAI_MODEL`. The key is `XAI_API_KEY`.

Offline is automatic when the key is empty. `INVOICE_OFFLINE=1` (also `true`, `yes`, `on`) or `--offline` forces it. Offline `complete()` returns immediately and does not import the SDK. Phase 1's pipeline does not call the model either way. `--smoke` is the one call that does, and it prints status and latency only.

## Database

`db.py` creates:

```sql
CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)
```

Seed rows, unchanged: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0. No other tables. An existing non-empty database is not reseeded and not overwritten.

## Shapes later phases share

Defined in `models.py`:

- `ExtractedInvoice` and `LineItem` for ingestion.
- `ValidationReport` and `Finding` for validation. `passed` is null while the stage is a stub.
- `ApprovalDecision` for approval (`approve` or `reject`, `needs_human`, `reasons`).
- `PaymentRecord` for payment (`paid` or `not_paid`).
- `StageRecord` for one log line.

Finding codes are open strings. Later phases can use `stock_mismatch`, `unknown_item`, `negative_quantity`, `math_mismatch`, `duplicate`, `vendor_rename`, and `missing_field` without a schema change. Phase 1 emits `stub`.
