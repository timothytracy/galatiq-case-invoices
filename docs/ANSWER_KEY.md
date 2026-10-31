# Answer key: original sample invoices

This is the expected outcome for each of the 20 files in `data/invoices/`, decided under [docs/DECISION_POLICY.md](DECISION_POLICY.md). That includes its precedence rule: **Reject over Hold over Approve**. When a Reject condition applies, any Hold findings or fraud signals are still recorded in Warnings and named in the reason. This file is docs only and is not wired into eval. The machine-readable copy is `data/answer_key.json`.

How each file was decided:
- Each file was read on its own. PDF and text twins each get their own row.
- Stock comes from the four README seed rows: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0. Quantities are summed per item.
- Line math was checked to the cent.

Notes on the columns:
- **Primary reason code.** The policy names findings but defines no code identifiers, so this column uses the policy's own finding names.
- **Price warnings.** The repo has no reference price table. These warnings compare against the price the other sample invoices use for the same item, and they change no outcome.
- **Source.** `README` means the README scenario table specifies how that invoice is grouped. The outcome itself comes from the policy.

| File | Invoice # | Outcome | Primary reason code | Warnings | Reason | Human action (HOLD) / Vendor next step (REJECT) | Source |
|---|---|---|---|---|---|---|---|
| invoice_1001.txt | INV-1001 | APPROVE | None (all checks pass) | None | WidgetA 10 (stock 15) and WidgetB 5 (stock 10) are in stock and the lines add up to the $5,000.00 total. | None (paid). | README |
| invoice_1002.txt | 1002 | REJECT | Stock shortfall | None | GadgetX quantity 20 is more than the 5 in stock; the line math ($15,000.00) is correct. | Resend for the quantity in stock, or amend the PO. | README |
| invoice_1003.txt | INV-1003 | REJECT | Stock shortfall | Fraud signal: urgency pressure ("URGENT - Pay immediately to avoid penalties!!!", terms Immediate); Fraud signal: wire transfer ("Wire transfer preferred"); Due date reads "yesterday", not a date | FakeItem quantity 100 is more than the 0 in stock; the invoice also pushes urgent payment and a wire transfer, which are recorded as fraud signals. | Resend for the quantity in stock, or amend the PO. (Fraud signals recorded for the reviewer.) | README + captain's call |
| invoice_1004.json | INV-1004 | APPROVE | None (all checks pass) | Vendor address 742 Evergreen Terrace, Springfield, IL 62704 is a well-known fictional home address | Stock and math pass ($1,750.00 + 8% tax = $1,890.00); the vendor address looks residential, which is noted, not held, per the captain's call. | None (paid). | README + captain's call |
| invoice_1004_revised.json | INV-1004 (R1) | HOLD | Revised invoice | Same vendor address as 1004 (noted, not held, on 1004) | This is revision R1 of INV-1004 ("Revised invoice - additional items added per PO amendment"); stock (GadgetX 5 of 5) and math ($5,500.00 + 8% = $5,940.00) pass. | Confirm it replaces the original INV-1004 and that the original was not paid. | Policy judgment |
| invoice_1005.json | INV-1005 | REJECT | Stock shortfall | Hold finding recorded: non-commercial vendor address (1600 Pennsylvania Ave, Washington, DC 20500) | GadgetX quantity 8 is more than the 5 in stock; the vendor address is also non-commercial, recorded as a finding. | Resend for the quantity in stock, or amend the PO. (Address finding recorded for the reviewer.) | Policy judgment |
| invoice_1006.csv | INV-1006 | APPROVE | None (all checks pass) | None | WidgetA 5 and WidgetB 3 are in stock and the lines add up to the $2,750.00 total. | None (paid). | README |
| invoice_1007.csv | INV-1007 | REJECT | Stock shortfall | Also a reject finding: total mismatch (14,750.00 + 885.00 = 15,635.00, printed 15,525.00) | WidgetA 20 (stock 15) and WidgetB 15 (stock 10) are short, and the printed total is $110.00 less than subtotal plus tax. | Resend for the quantity in stock, or amend the PO. Also resend with corrected totals. | Policy judgment |
| invoice_1008.txt | INV-1008 | REJECT | Unknown item | Total $9,900.00 is within 2% under the $10,000 threshold | SuperGizmo and MegaSprocket are not in the inventory database. | Resend with the correct item names (no confident "likely meant" match). | README |
| invoice_1009.json | INV-1009 | REJECT | Quantity that is zero, negative, or not a number | Also reject findings: missing vendor, missing due date, line sum -250.00 vs printed subtotal 1,000.00, negative total -250.00 not labeled a credit note | WidgetA quantity is -5, the vendor and due date are blank, and the totals do not reconcile. | Resend with corrected quantities, the missing vendor and due date filled in, and corrected totals. | README |
| invoice_1010.txt | INV-1010 | APPROVE | None (all checks pass) | Descriptor on item name: "WidgetA (rush order)" matched as WidgetA, "rush order" kept as a note; Unit price $300.00 for the rush WidgetA line differs from $250.00 (see reference-price note) | WidgetA 12 in total (8 + 4 rush, stock 15), WidgetB 4 and GadgetX 2 are in stock, and $6,700.00 + $335.00 tax + $150.00 shipping = $7,185.00. | None (paid). | Policy judgment |
| invoice_1011.pdf | INV-1011 | APPROVE | None (all checks pass) | None | WidgetA 6 and WidgetB 3 are in stock and the lines add up to the $3,000.00 total (no subtotal or tax lines printed). | None (paid). | Policy judgment |
| invoice_1011.txt | INV-1011 | APPROVE | None (all checks pass) | None | WidgetA 6 and WidgetB 3 are in stock and the lines add up to the $3,000.00 total (subtotal and 0% tax printed). | None (paid). | Policy judgment |
| invoice_1012.pdf | INV 1012 | HOLD | Renamed vendor | Total $9,975.00 is within 2% under the $10,000 threshold; OCR fixes "2O26" and "$3,500.O0" allowed because the math reconciles ($9,500.00 + 5% = $9,975.00) | The vendor is "QuickShip Distributers (formerly FastShip Ltd.)"; stock (Widget A 12, WidgetB 7, Gadget X 4) and math pass. | Verify it is the same company. | Policy judgment |
| invoice_1012.txt | INV 1012 | HOLD | Renamed vendor | Total $9,975.00 is within 2% under the $10,000 threshold; OCR fixes "2O26" and "$3,500.O0" allowed because the math reconciles ($9,500.00 + 5% = $9,975.00) | The vendor is "QuickShip Distributers (formerly FastShip Ltd.)"; stock (Widget A 12, WidgetB 7, Gadget X 4) and math pass. | Verify it is the same company. | Policy judgment |
| invoice_1013.json | INV-1013 | REJECT | Stock shortfall | Also a reject finding: total mismatch (21,040.00 + 1,472.80 = 22,512.80, printed 22,562.80); Unit prices $240.00 and $480.00 (volume discount) differ from $250.00 and $500.00 (see reference-price note) | Summed per item, WidgetA 22 (stock 15), WidgetB 18 (stock 10) and GadgetX 9 (stock 5) are short, and the printed total is $50.00 more than subtotal plus tax. | Resend for the quantity in stock, or amend the PO. Also resend with corrected totals. | Policy judgment |
| invoice_1013.pdf | INV-1013 | REJECT | Stock shortfall | Also a reject finding: total mismatch (21,040.00 + 1,472.80 = 22,512.80, printed 22,562.80); Unit prices $240.00 and $480.00 (volume discount) differ from $250.00 and $500.00 (see reference-price note) | Summed per item, WidgetA 22 (stock 15), WidgetB 18 (stock 10) and GadgetX 9 (stock 5) are short, and the printed total is $50.00 more than subtotal plus tax. | Resend for the quantity in stock, or amend the PO. Also resend with corrected totals. | Policy judgment |
| invoice_1014.xml | INV-1014 | HOLD | Non-USD currency | None | The invoice is in EUR (4,125.00); stock and math pass, but non-USD is never paid or converted. | Finance confirms the currency and amount. Nothing is converted and nothing is paid. | Policy judgment |
| invoice_1015.csv | INV-1015 | APPROVE | None (all checks pass) | None | WidgetA 10, WidgetB 5 and GadgetX 2 are in stock and the lines add up to the $6,500.00 total. | None (paid). | Policy judgment |
| invoice_1016.json | INV-1016 | REJECT | Unknown item | None | WidgetC is not in the inventory database; the other lines and the $3,233.00 total are correct. | Resend with the correct item name (no confident "likely meant" match). | README |

**Counts:** APPROVE 7, REJECT 9, HOLD 4 (20 files).

The "clean invoice over $10,000 with a new bank account" Hold doesn't apply to any file here, because no sample invoice has bank or remittance details. Every invoice over $10,000 (1002, 1003, 1005, 1007, 1013) is rejected for stock, so the critique-over-$10K follow-up changes no outcome in this set.

## Resolved by the captain

- **1003:** REJECT for a stock shortfall (FM-GC13 precedence rule). The urgency and wire-transfer signals are recorded as findings. This replaces the earlier HOLD call.
- **1004:** APPROVE. A non-commercial address on an otherwise clean invoice that the README lists as passing is noted, not held (FM-GC14). invoice_1004_revised.json stays a HOLD because it is a revised invoice.
