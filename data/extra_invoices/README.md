# Extra invoices

Not part of the 20 samples. Each label is the rule result for that file alone. Nothing here was fitted to a model run.

- invoice_2001.pdf — Clean USD total of 11100.00, so approval must go through critique once before audit. WidgetA 10 and WidgetB 5 are inside the snapshot and the math matches, so the rule outcome is approve. The loop is guaranteed by the total. Critique can still set stricter, and that flag is the model's; this bill has no fraud phrase for the rules to tighten.
- invoice_2002.json — USD total of 12844.00, so critique runs, plus a replacement remittance account that is not one of the fraud phrases (formerly, wire transfer, pay immediately, urgent). Validation still approves. The rules never set stricter, so the label is approve. A stricter decision on this file would be the model, not the rules.
- invoice_2003.txt — The quote due date, the draft WidgetA quantity of 12, and the quote total of 4500.00 are marked void. The bill is WidgetA 8, WidgetB 4, due 2026-04-15, total 4050.00, which approves. Audit send-back is model-dependent: it runs only after validation has already approved, and Python does not send back a wrong due date on its own. A misread that breaks stock or math is a reject and is not retried.
- invoice_2004.csv — WidgetA 9 and WidgetA 8 each fit in stock 15. The sum is 17, which does not. The lines, tax, and total match. Reject.
- invoice_2005.xml — Lines, subtotal, and 10% tax match. Shipping 75.00 is printed and left out of the total (2475.00 instead of 2550.00). Reject.
- invoice_2006.json — Due date is absent. Vendor, lines, and total are present and the math matches. Reject.
- invoice_2007.xml — The vendor is Northwind Parts (formerly Northwind Supply). Stock and math pass, so the only finding is the rename flag. Manual review.
- invoice_2008.txt — Odd spacing, a wrapped note, O/0 in the dates and amounts, and dock lB (the letter l standing in for 1). The bill is WidgetA 10, WidgetB 4, GadgetX 2, total 6150.00. Approve. The label is that reading. A live run still depends on the model extracting it.
