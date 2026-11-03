# Answer key: fraud invoices

Expected outcome for every file in `data/fraud_invoices/`, decided under [docs/DESIGN.md](DESIGN.md). The machine-readable copy is `data/fraud_answer_key.json`. It uses the same schema as `data/answer_key.json` and is scored with `python3 scripts/eval.py --invoices data/fraud_invoices --answer-key data/fraud_answer_key.json`.

Each file was written and labeled before the fraud review prompt. Nothing here was produced by running the pipeline or a Grok call.

How each file was decided:

- Each invoice is under $10,000, so critique does not run. Stock is the four seed rows: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0. Line math was checked to the cent.
- None has a reject or hold finding, so code alone approves each one. That was checked by running each file through the graph with the deterministic read and a model that passes everything.
- 2124, 2125, and 2126 raise no keyword finding at all. 2121 and 2123 raise one `FRAUD_SIGNAL` each, for wire-only payment.
- A hold is labeled when a careful AP reviewer would stop the payment to verify it: payment redirected away from the vendor, verification discouraged, or a vendor identity or contact that cannot be checked from the page. An approve is labeled when the page has an innocent reading that nothing on it undercuts.

| File | Invoice # | Outcome | Primary reason | Keyword warnings | Reason | Human action (HOLD) |
|---|---|---|---|---|---|---|
| invoice_2121.txt | INV-2121 | HOLD | Fraud suspected (`FRAUD_SUSPECTED`) | Fraud signal: wire transfer | Wire-only payment, a note forbidding a call to confirm payment details, and only a free-mail contact. Verification is blocked. | Verify payment details with the vendor using contact details already on file. |
| invoice_2123.txt | INV-2123 | APPROVE | None (all checks pass) | Fraud signal: wire transfer | Wire-only payment, but a PO, remittance to the banking details on the vendor master file, and a phone number. | None (paid). |
| invoice_2124.txt | INV-2124 | HOLD | Fraud suspected (`FRAUD_SUSPECTED`) | None | Asks that the invoice be settled to a differently named company that now services the vendor's receivables, with that company as the only contact and no PO. Payment is redirected away from the vendor. | Verify payment details with the vendor using contact details already on file. |
| invoice_2125.txt | INV-2125 | HOLD | Fraud suspected (`FRAUD_SUSPECTED`) | None | The remit-to party differs from the vendor, AP is asked to update its records so payments go there, and the only contact is a free-mail address while the usual contact is away. | Verify payment details with the vendor using contact details already on file. |
| invoice_2126.txt | INV-2126 | APPROVE | None (all checks pass) | None | A warehouse move, payment instructions unchanged, ACH to the account on file, a PO, and an invitation to call the number on file. | None (paid). |

Every approve in `data/answer_key.json` and `data/extra_answer_key.json` is a strict APPROVE, with no alternate outcome. Those are the false-alarm controls for the fraud review, including 2103 (wire-only). 1004 is not one of them: it is labeled a `FRAUD_SUSPECTED` hold, with an approve also accepted; see [Rulings on 1003 and 1004](ANSWER_KEY.md#rulings-on-1003-and-1004).
