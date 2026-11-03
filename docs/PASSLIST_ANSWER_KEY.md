# Answer key: pass-list invoices

Expected outcome for every file in `data/passlist_invoices/`, decided under [docs/DESIGN.md](DESIGN.md). The machine-readable copy is `data/passlist_answer_key.json`. It uses the same schema as `data/answer_key.json` and is scored with `python3 scripts/eval.py --invoices data/passlist_invoices --answer-key data/passlist_answer_key.json`.

Each file was written and labeled before it was recorded. Nothing here was produced by running the pipeline or a Grok call.

How each file was decided:

- Each invoice is under $10,000, so critique does not run. Stock is the four seed rows: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0. Line math was checked to the cent.
- Each one tests a single pass-list check that the other sets did not: a vendor off the vendor master, a zero total, a bank change under $10,000, and instruction wording that no fixed phrase matches. 2135 is the control, and it passes all six checks.
- 2134 depends on the extraction model. The deterministic read never sets the flag, so offline it approves. Live, the model must flag it and quote the wording, and code must find that quote on the page.

| File | Invoice # | Outcome | Primary reason | Keyword warnings | Reason | Human action (HOLD) |
|---|---|---|---|---|---|---|
| invoice_2131.txt | INV-2131 | HOLD | Vendor not on the vendor master (`NEW_VENDOR`) | None | Clean math and stock, but Northwind Fastener Supply is not on the vendor master. | Set the vendor up in the vendor master, then release the hold. |
| invoice_2132.txt | INV-2132 | HOLD | Zero total (`ZERO_TOTAL`) | WidgetA unit price below reference | A warranty replacement billed at $0.00 that is not a labeled credit note. | Confirm with the vendor why the total is zero. Nothing is paid. |
| invoice_2133.txt | INV-2133 | HOLD | New remittance account (`NEW_REMITTANCE`) | Fraud signal: new bank account | A $1,750.00 invoice that moves payment to a new bank account. Bank changes are held at any amount. | Verify the change with the vendor. |
| invoice_2134.txt | INV-2134 | HOLD | Possible embedded instructions (`INJECTION_SUSPECTED`) | None | The notes tell the automated reviewer to mark the invoice verified and send it to payment. Only the model can catch it, so it is one signal and a hold. | Read the flagged wording and confirm it is not aimed at the payment system. |
| invoice_2135.txt | INV-2135 | APPROVE | None (all checks pass) | None | A known vendor, payment instructions unchanged, clean math and stock. | None (paid). |
