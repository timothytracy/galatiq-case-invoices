# Answer key: warning invoices

Expected outcome for every file in `data/warning_invoices/`, decided under [docs/DESIGN.md](DESIGN.md). The machine-readable copy is `data/warning_answer_key.json`. It uses the same schema as `data/answer_key.json` and is scored with `python3 scripts/eval.py --invoices data/warning_invoices --answer-key data/warning_answer_key.json`.

Each file was written and labeled before the audit prompt change. Nothing here was produced by running the pipeline or a Grok call.

How each file was decided:

- Each invoice is under $10,000, so critique does not run. Stock is the four seed rows: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0. Line math was checked to the cent.
- Each carries exactly one warning that the audit may weigh, and no hold or reject finding, so code alone approves. The deterministic checks were run on each file to confirm that.
- The label says whether a careful reader should hold on that warning. A hold is labeled when the page text makes the warning's innocent reading unlikely. The text that raised the warning is not enough on its own.

| File | Invoice # | Outcome | Primary reason | Warning | Reason | Human action (HOLD) |
|---|---|---|---|---|---|---|
| invoice_2121.txt | INV-2121 | HOLD | Audit escalation (`AUDIT_ESCALATED`) | Fraud signal: wire transfer ("Wire transfer only") | WidgetA 4 and WidgetB 2 are in stock at the reference prices, and the lines add up to $2,000.00. Wire-only payment is one fraud signal. The notes say "Do not call our office to confirm payment details" and give only a free-mail contact. A real vendor does not forbid confirmation, so the innocent reading of wire-only is unlikely. | Review the quoted text with the vendor, then release or keep the hold. |
| invoice_2122.txt | INV-2122 | HOLD | Audit escalation (`AUDIT_ESCALATED`) | Unit price $1,050.00 for GadgetX differs from the reference price $750.00. | WidgetA 4 and GadgetX 2 are in stock, and the lines add up to $3,100.00. GadgetX is 40% above reference, under the 50% spike ratio, so code records a price warning. The notes say the price is "per phone agreement with your buyer" and "Do not compare to PO or contract pricing." That removes the innocent reading of an ordinary price change. | Review the quoted text with the vendor, then release or keep the hold. |
| invoice_2123.txt | INV-2123 | APPROVE | None (all checks pass) | Fraud signal: wire transfer ("Wire transfer only") | WidgetB 3 and GadgetX 1 are in stock at the reference prices, and the lines add up to $2,250.00. Wire-only payment is one fraud signal, but the invoice cites a PO, remits to the banking details on the vendor master file, and gives a phone number. The innocent reading stands. | None (paid). |

## Results

In the recorded run on 2026-11-02 PT and in three live runs that day, the set scored 1/3. The audit returned PASS on all three invoices and gave no `warning_hold`, so 2121 and 2122 were approved and paid. 2123 was correctly paid. The prompt was not changed after these runs. Run counts and costs are in [METRICS.md](METRICS.md).

`invoice_2103.csv` in the unseen set is the same kind of case read the other way: a single fraud signal on an invoice its answer key labels approve. Its key now also accepts an `AUDIT_ESCALATED` hold. It was approved in every run.
