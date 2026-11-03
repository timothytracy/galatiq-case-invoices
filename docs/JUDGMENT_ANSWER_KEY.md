# Answer key: judgment invoices

Expected outcome for every file in `data/judgment_invoices/`, decided under [docs/DESIGN.md](DESIGN.md). The machine-readable copy is `data/judgment_answer_key.json`, scored with `python3 scripts/eval.py --invoices data/judgment_invoices --answer-key data/judgment_answer_key.json`.

This set tests the three-way match: whether what is billed agrees with what the page says was ordered, delivered, priced, or returned. Every invoice is in stock at the reference prices, its lines reconcile, its vendor is on the vendor master, and code finds nothing, so each row has `code_only_outcome` APPROVE. No code rule exists for any of these cases on purpose. Each trap needs a different reading of the page's wording, which is model work. A critique or audit hold stands only when it quotes that wording and code finds the quote on the page (see the quote gate in DESIGN.md).

Each trap has a twin with similar wording that is benign, so a reviewer that holds every invoice with a note scores badly.

| File | Outcome | Primary reason | Why |
|---|---|---|---|
| invoice_2140.txt | HOLD | Critique hold | "Partial shipment: 10 of 15 WidgetA delivered ... balance on backorder", but all 15 WidgetA are billed. |
| invoice_2141.txt | APPROVE | None | Twin of 2140. Only the 10 delivered WidgetA are billed; the other 5 will be billed separately when they ship. |
| invoice_2142.txt | HOLD | Critique hold | Dated 2026-09-15, "Pricing per Quote Q-88, valid through Aug 31, 2026". The pricing basis had expired. |
| invoice_2143.txt | APPROVE | None | Twin of 2142. Quote Q-91 is valid through Dec 31, 2026. |
| invoice_2144.txt | HOLD | Critique hold | "All 4 GadgetX returned per RMA-112 ... No replacement has shipped", but the 4 GadgetX are still billed. |
| invoice_2145.txt | APPROVE | None | Twin of 2144. RMA-112 belongs to an earlier order and is settled; the 4 GadgetX billed are a new order, delivered in full. |
| invoice_2146.txt | HOLD | Audit escalation | $5,500.00, under the threshold, so critique does not run. "3 of 6 WidgetB delivered ... balance on backorder", but all 6 are billed. |

Left out: an invoice that says it "replaces INV-2090, which was voided". A replacement for a voided invoice is often legitimate, so its right outcome depends on whether INV-2090 was paid, and the ledger tool does not yet take a referenced invoice number.
