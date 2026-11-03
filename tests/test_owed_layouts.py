"""Amount-owed layouts from the second probe set. Fake model, no network."""

import pytest

from decision.printed_amounts import balances_due
from tests.fakes import scripted, write_invoice
from tests.test_loop_set import _codes, _read
from tests.test_owed_probe import LOOP, _pdf, _run

pytestmark = pytest.mark.usefixtures("no_network")

_2120 = (LOOP / "invoice_2120.txt").read_text(encoding="utf-8")
_2117 = (LOOP / "invoice_2117.txt").read_text(encoding="utf-8")
_BAL = "Balance Due: $8,000.00"

HOLD = {
    "r01 'Amount Due Now: $8,000.00'": 'Amount Due Now: $8,000.00',
    "r02 'Total Amount Due: $8,000.00'": 'Total Amount Due: $8,000.00',
    "r03 'Balance Payable: $8,000.00'": 'Balance Payable: $8,000.00',
    "r04 'Remaining Balance: $8,000.00'": 'Remaining Balance: $8,000.00',
    "r05 'Amount Outstanding: $8,000.00'": 'Amount Outstanding: $8,000.00',
    "r06 'Unpaid balance: $8,000.00'": 'Unpaid balance: $8,000.00',
    "r07 'Due Now: $8,000.00'": 'Due Now: $8,000.00',
    "r08 'BALANCE DUE $8,000.00'": 'BALANCE DUE $8,000.00',
    "r09 'Amount still owed: $8,000.00'": 'Amount still owed: $8,000.00',
    "r10 'You owe: $8,000.00'": 'You owe: $8,000.00',
    "r11 'Please pay: $8,000.00'": 'Please pay: $8,000.00',
    "r12 'Balance Due: $8,000 of $10,500' (no cents)": 'Balance Due: $8,000 of $10,500',
    "r13 'Balance Due: 8,000.00 USD of 10,500.00 USD'": 'Balance Due: 8,000.00 USD of 10,500.00 USD',
    "r14 'Balance Due: 8000 of 10500' (bare ints)": 'Balance Due: 8000 of 10500',
    "r15 'Balance Due: 8,000 of 10,500' (no $ no cents)": 'Balance Due: 8,000 of 10,500',
    "r16 'Balance Due: $8,000.00 (2 items)'": 'Balance Due: $8,000.00 (2 items)',
    "r17 'Balance Due: $8,000.00 for 3 items'": 'Balance Due: $8,000.00 for 3 items',
    "r18 'Balance Due: $8,000.00 on 3 of 5 items'": 'Balance Due: $8,000.00 on 3 of 5 items',
    "r19 'Balance Due: $8,000.00 units 1-3'": 'Balance Due: $8,000.00 units 1-3',
    "r20 'Balance Due: 8000 USD' (bare, code after)": 'Balance Due: 8000 USD',
    "r21 'Balance Due: 8,000 USD'": 'Balance Due: 8,000 USD',
    "r22 'Balance Due: 8000 dollars'": 'Balance Due: 8000 dollars',
    "r23 'Balance Due 8000.00'": 'Balance Due 8000.00',
    "r24 'balance due 8000' lowercase bare": 'balance due 8000',
    "r25 'Balance Due: $8,000' (no cents)": 'Balance Due: $8,000',
    "r26 'Balance Due: $8,000.00 on 2026-10-30'": 'Balance Due: $8,000.00 on 2026-10-30',
    "r27 'Balance Due by 2026-10-30: $8,000.00'": 'Balance Due by 2026-10-30: $8,000.00',
    "r28 'Balance Due (Net 30): $8,000.00'": 'Balance Due (Net 30): $8,000.00',
    "r29 'Balance Due: $8,000.00 (76% of total)'": 'Balance Due: $8,000.00 (76% of total)',
    "r30 'Balance Due: USD 8,000.00 after 25% deposit'": 'Balance Due: USD 8,000.00 after 25% deposit',
    "r31 'Balance Due: $8,000.00*' footnote": 'Balance Due: $8,000.00*',
    "r32 'Balance Due ($): 8,000.00'": 'Balance Due ($): 8,000.00',
    "r33 'Balance Due ..... 8,000.00' no $": 'Balance Due ............... 8,000.00',
    "r34 'Balance Due | $8,000.00' pipe": 'Balance Due | $8,000.00',
    "r35 markdown '| Balance Due | $8,000.00 |'": '| Balance Due | $8,000.00 |',
    "r36 NBSP 'Balance\xa0Due: $8,000.00'": 'Balance\xa0Due: $8,000.00',
    "r37 'Balance Due: $8,000.00.' trailing period": 'Balance Due: $8,000.00.',
    "r38 'Balance Due: Eight thousand dollars ($8,000.00)'": 'Balance Due: Eight thousand dollars ($8,000.00)',
    "r39 'Balance due: $8,000.00 / $10,500.00'": 'Balance due: $8,000.00 / $10,500.00',
    "r40 'Balance Due: $8,000.00 credit memo applied'": 'Balance Due: $8,000.00 credit memo applied',
    "r41 'Balance Due:' two blank lines '$8,000.00'": 'Balance Due:\n\n\n$8,000.00',
    "r42 'Balance Due:' whitespace line '$8,000.00'": 'Balance Due:\n    \n$8,000.00',
    "r43 'Balance Due' / '(after deposit)' / '$8,000.00'": 'Balance Due\n(after deposit)\n$8,000.00',
    "r44 'Balance' / blank / 'Due' / blank / '$8,000.00'": 'Balance\n\nDue\n\n$8,000.00',
    "r45 'Balance Due' newline bare '8000'": 'Balance Due\n8000',
    "r46 three-column 'Subtotal  Deposit  Balance Due' / values": 'Subtotal        Deposit        Balance Due\n$10,500.00      -$2,500.00     $8,000.00',
    "r47 two-column 'Payment Terms  Balance Due' / 'Net 30  $8,000.00'": 'Payment Terms      Balance Due\nNet 30             $8,000.00',
    "r48 two-column 'Balance Due  Payment Terms' (q15)": 'Balance Due        Payment Terms\n$8,000.00          Net 30',
    "r49 'Amount Due: $8,000.00 USD' (2120 notes kept)": 'Amount Due: $8,000.00 USD',
    "r50 'Total Due (after deposit): $8,000.00'": 'Total Due (after deposit): $8,000.00',
}

PAID = {
    "s01 'Ref: DUE-2026-11'": 'Ref: DUE-2026-11',
    "s02 'PO: REMIT-4500'": 'PO: REMIT-4500',
    "s03 'Invoice ID: BAL-DUE-8000'": 'Invoice ID: BAL-DUE-8000',
    "s04 'Customer ID: BALANCE2026'": 'Customer ID: BALANCE2026',
    "s05 'Batch: REMIT2026'": 'Batch: REMIT2026',
    "s06 'Ref: BALANCE 2026'": 'Ref: BALANCE 2026',
    "s07 'Ref: BALANCE-2026' (no tail)": 'Ref: BALANCE-2026',
    "s08 'Ref: OUTSTANDING-77'": 'Ref: OUTSTANDING-77',
    "s09 'Ref: AMOUNTDUE-2026-77'": 'Ref: AMOUNTDUE-2026-77',
    "s10 'Remittance advice ref: RA-2026-0042'": 'Remittance advice ref: RA-2026-0042',
    "s11 'Remit ID 2026'": 'Remit ID 2026',
    "s12 'Balance due date: 2026-10-30'": 'Balance due date: 2026-10-30',
    "s13 'Amount due on 30 Oct 2026'": 'Amount due on 30 Oct 2026',
    "s14 'Balance Due: 30-Oct-2026'": 'Balance Due: 30-Oct-2026',
    "s15 'Total due: 2026/10/30'": 'Total due: 2026/10/30',
    "s16 'Balance due: 10.30.2026'": 'Balance due: 10.30.2026',
    "s17 'Amount Due: 2026.10.30'": 'Amount Due: 2026.10.30',
    "s18 'Amount due: 30 Oct'": 'Amount due: 30 Oct',
    "s19 'Balance Due: October 30'": 'Balance Due: October 30',
    "s20 'Amount due: Net 45'": 'Amount due: Net 45',
    "s21 'Balance due: 25% deposit, 75% on delivery'": 'Balance due: 25% deposit, 75% on delivery',
    "s22 'Balance due: 1.5% interest after 30 days'": 'Balance due: 1.5% interest after 30 days',
    "s23 'Amount due: 50.0% on delivery'": 'Amount due: 50.0% on delivery',
    "s24 'Balance due: 25 percent on delivery'": 'Balance due: 25 percent on delivery',
    "s25 'Balance Due: 1st of month'": 'Balance Due: 1st of month',
    "s26 'Balance due in 3rd quarter'": 'Balance due in 3rd quarter',
    "s27 'Balance due: 3rd installment'": 'Balance due: 3rd installment',
    "s28 'Remit: 4500, Industrial Way'": 'Remit: 4500, Industrial Way, Dayton OH',
    "s29 'Remit: 4500 W. Industrial Way'": 'Remit: 4500 W. Industrial Way',
    "s30 'Remit: 4500-B Industrial Way'": 'Remit: 4500-B Industrial Way',
    "s31 'Please remit to 1200 Commerce Blvd'": 'Please remit to 1200 Commerce Blvd, Suite 300',
    "s32 'Remit: 1200'": 'Remit: 1200\nCommerce Blvd, Suite 300',
    "s33 'Outstanding: 0'": 'Outstanding: 0',
    "s34 'Balance Due: $10,625.00 of $10,625.00'": 'Balance Due: $10,625.00 of $10,625.00',
    "s35 'Balance Due: $0.00 - paid in full'": 'Balance Due: $0.00 - paid in full',
    "s36 'Balance remaining: $0.00'": 'Balance remaining: $0.00',
    "s37 'Balance Due: 3 items backordered'": 'Balance Due: 3 items backordered',
    "s38 'Balance due: 12 units'": 'Balance due: 12 units',
    "s39 'Late fee 1.5% per month on balance due'": 'Late fee 1.5% per month on balance due',
    "s40 'Outstanding (questions): 937.555.0100'": 'Outstanding (questions): 937.555.0100',
    "s41 'Balance due inquiries: 937-555-0100'": 'Balance due inquiries: 937-555-0100',
    "s42 'Net due: 30 days'": 'Net due: 30 days',
    "s43 'Total due: 10,625.00 USD' equal": 'Total due: 10,625.00 USD',
    "s44 'Amount payable: 10625' equal bare": 'Amount payable: 10625',
    "s45 'Balance Due' newline '2026-10-30'": 'Balance Due\n2026-10-30',
    "s46 'Amount due:' blank 'Net 30'": 'Amount due:\n\nNet 30',
    "s47 'Deposit due: $0.00'": 'Deposit due: $0.00',
    "s48 'Previous amount due: $2,000.00 (paid)'": 'Previous amount due: $2,000.00 (paid)',
    "s49 'Statement balance: $3,000.00'": 'Statement balance: $3,000.00',
    "s50 'Prior balance: $500.00'": 'Prior balance: $500.00',
    "s51 'Balance carried forward: $500.00'": 'Balance carried forward: $500.00',
    "s52 'Remit:' newline '$ via ACH 041000124'": 'Remit:\nACH routing 041000124',
    "s53 'Remit: 041000124 (ACH routing)'": 'Remit: 041000124 (ACH routing)',
    "s54 'Balance due: 2 of 3 shipments, $10,625.00'": 'Balance due: 2 of 3 shipments',
    "s55 freight two-column 'Freight  Balance Due' / '$125.00  $10,625.00'": 'Freight            Balance Due\n$125.00            $10,625.00',
    "s56 freight two-column 'Balance Due  Freight' / '$10,625.00  $125.00'": 'Balance Due        Freight\n$10,625.00         $125.00',
    "s57 m28 'Balance due on the total below' / '$125.00 shipping included'": 'Balance due on the total below\n$125.00 shipping included',
    "s58 'Amount due includes freight' / '$125.00 freight'": 'Amount due includes freight\n$125.00 freight',
    "s59 'Outstanding PO' / '$125.00 freight'": 'Outstanding PO lines\n$125.00 freight',
    "s60 'Account balance: $8,000.00' (documented paid)": 'Account balance: $8,000.00',
}

FILES = [
    ("r51 XML <AmountDue currency='USD'>8000.00</AmountDue>", 'r51.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2141</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><AmountDue currency="USD">8000.00</AmountDue></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'hold', '2120'),
    ('r52 XML <balance_due>8000</balance_due>', 'r52.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2141</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><balance_due>8000</balance_due></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'hold', '2120'),
    ('r53 XML <amount_remaining>8000.00</amount_remaining>', 'r53.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2141</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><amount_remaining>8000.00</amount_remaining></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'hold', '2120'),
    ('r54 XML <BalanceDue>8,000.00</BalanceDue> in file', 'r54.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2141</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><BalanceDue>8,000.00</BalanceDue></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'hold', '2120'),
    ("r55 JSON 'balanceDue': '$8,000.00'", 'r55.json', '{\n  "invoice_number": "INV-2132",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "balanceDue": "$8,000.00"\n}', 'hold', '2120'),
    ("r56 JSON 'amount_remaining': 8000", 'r56.json', '{\n  "invoice_number": "INV-2132",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "amount_remaining": 8000\n}', 'hold', '2120'),
    ("r57 JSON 'remaining_balance': 8000", 'r57.json', '{\n  "invoice_number": "INV-2132",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "remaining_balance": 8000\n}', 'hold', '2120'),
    ("r58 JSON nested totals.amount_due '8000.00'", 'r58.json', '{\n  "invoice_number": "INV-2132",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "totals": {\n    "amount_due": "8000.00"\n  }\n}', 'hold', '2120'),
    ("r59 JSON 'balance_due': 8000.5 (odd cents)", 'r59.json', '{\n  "invoice_number": "INV-2132",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "balance_due": 8000.5\n}', 'hold', '2120'),
    ("r60 JSON 'outstanding': 8000", 'r60.json', '{\n  "invoice_number": "INV-2132",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "outstanding": 8000\n}', 'hold', '2120'),
    ('r61 CSV \'balance due,"$8,000.00"\' quoted', 'r61.csv', 'field,value\ninvoice_number,INV-2131\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\nbalance due,"$8,000.00"\npayment_terms,Net 30\n', 'hold', '2120'),
    ('r62 CSV \'Balance_Due,"8,000.00"\'', 'r62.csv', 'field,value\ninvoice_number,INV-2131\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\nBalance_Due,"8,000.00"\npayment_terms,Net 30\n', 'hold', '2120'),
    ("r63 CSV 'amount_due,8000'", 'r63.csv', 'field,value\ninvoice_number,INV-2131\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\namount_due,8000\npayment_terms,Net 30\n', 'hold', '2120'),
    ("r64 CSV 'net_due,8000.00'", 'r64.csv', 'field,value\ninvoice_number,INV-2131\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\nnet_due,8000.00\npayment_terms,Net 30\n', 'hold', '2120'),
    ("r65 PDF 'Balance Due: $8,000.00 items shipped in full'", 'r65.pdf', 'INVOICE\n\nVendor: Ironbridge Components Inc.\nInvoice Number: INV-2120\nDate: 2026-09-30\nDue Date: 2026-10-30\n\nItems:\n  WidgetA    qty: 12    unit price: $250.00\n  WidgetB    qty: 9     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nTotal Amount: $10,500.00\nLess deposit received 2026-08-15 (ref DEP-3302): -$2,500.00\nBalance Due: $8,000.00 items shipped in full\n\nPayment Terms: Net 30, balance only\nNotes: PO-7790. Deposit already received against this order; remit the balance due only.\n', 'hold', '2120'),
    ("r66 PDF 'Balance Due:' blank '$8,000.00'", 'r66.pdf', 'INVOICE\n\nVendor: Ironbridge Components Inc.\nInvoice Number: INV-2120\nDate: 2026-09-30\nDue Date: 2026-10-30\n\nItems:\n  WidgetA    qty: 12    unit price: $250.00\n  WidgetB    qty: 9     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nTotal Amount: $10,500.00\nLess deposit received 2026-08-15 (ref DEP-3302): -$2,500.00\nBalance Due:\n\n$8,000.00\n\nPayment Terms: Net 30, balance only\nNotes: PO-7790. Deposit already received against this order; remit the balance due only.\n', 'hold', '2120'),
    ("r67 PDF 'Amount remaining: $8,000.00'", 'r67.pdf', 'INVOICE\n\nVendor: Ironbridge Components Inc.\nInvoice Number: INV-2120\nDate: 2026-09-30\nDue Date: 2026-10-30\n\nItems:\n  WidgetA    qty: 12    unit price: $250.00\n  WidgetB    qty: 9     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nTotal Amount: $10,500.00\nLess deposit received 2026-08-15 (ref DEP-3302): -$2,500.00\nAmount remaining: $8,000.00\n\nPayment Terms: Net 30, balance only\nNotes: PO-7790. Deposit already received against this order; remit the balance due only.\n', 'hold', '2120'),
    ("r68 PDF 'Balance Due: 8000 USD'", 'r68.pdf', 'INVOICE\n\nVendor: Ironbridge Components Inc.\nInvoice Number: INV-2120\nDate: 2026-09-30\nDue Date: 2026-10-30\n\nItems:\n  WidgetA    qty: 12    unit price: $250.00\n  WidgetB    qty: 9     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nTotal Amount: $10,500.00\nLess deposit received 2026-08-15 (ref DEP-3302): -$2,500.00\nBalance Due: 8000 USD\n\nPayment Terms: Net 30, balance only\nNotes: PO-7790. Deposit already received against this order; remit the balance due only.\n', 'hold', '2120'),
    ("r69 PDF 'Balance Due: $8,000 of $10,500'", 'r69.pdf', 'INVOICE\n\nVendor: Ironbridge Components Inc.\nInvoice Number: INV-2120\nDate: 2026-09-30\nDue Date: 2026-10-30\n\nItems:\n  WidgetA    qty: 12    unit price: $250.00\n  WidgetB    qty: 9     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nTotal Amount: $10,500.00\nLess deposit received 2026-08-15 (ref DEP-3302): -$2,500.00\nBalance Due: $8,000 of $10,500\n\nPayment Terms: Net 30, balance only\nNotes: PO-7790. Deposit already received against this order; remit the balance due only.\n', 'hold', '2120'),
    ("r70 PDF 'Balance Due: $8,000.00 Credit cards accepted'", 'r70.pdf', 'INVOICE\n\nVendor: Ironbridge Components Inc.\nInvoice Number: INV-2120\nDate: 2026-09-30\nDue Date: 2026-10-30\n\nItems:\n  WidgetA    qty: 12    unit price: $250.00\n  WidgetB    qty: 9     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nTotal Amount: $10,500.00\nLess deposit received 2026-08-15 (ref DEP-3302): -$2,500.00\nBalance Due: $8,000.00 Credit cards accepted\n\nPayment Terms: Net 30, balance only\nNotes: PO-7790. Deposit already received against this order; remit the balance due only.\n', 'hold', '2120'),
    ('s61 XML <payment_terms>Balance due: 25% on delivery</payment_terms>', 's61.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2142</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total></totals>\n  <payment_terms>Balance due: 25% on delivery</payment_terms>\n</invoice>\n', 'paid', '2120'),
    ('s62 XML <reference>BALANCE-2026-77</reference>', 's62.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2143</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><reference>BALANCE-2026-77</reference></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'paid', '2120'),
    ('s63 XML <remit_to>4500 Industrial Way</remit_to>', 's63.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2144</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><remit_to>4500 Industrial Way</remit_to></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'paid', '2120'),
    ('s64 XML <balance_due_date>2026-10-30</balance_due_date>', 's64.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2145</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><balance_due_date>2026-10-30</balance_due_date></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'paid', '2120'),
    ('s65 XML <balance_due>10500.00</balance_due> equal', 's65.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<invoice>\n  <header><invoice_number>INV-2146</invoice_number><vendor>Ironbridge Components Inc.</vendor><date>2026-09-30</date><due_date>2026-10-30</due_date><currency>USD</currency></header>\n  <line_items>\n    <item><name>WidgetA</name><quantity>12</quantity><unit_price>250.00</unit_price></item>\n    <item><name>WidgetB</name><quantity>9</quantity><unit_price>500.00</unit_price></item>\n    <item><name>GadgetX</name><quantity>4</quantity><unit_price>750.00</unit_price></item>\n  </line_items>\n  <totals><subtotal>10500.00</subtotal><tax_rate>0</tax_rate><tax_amount>0.00</tax_amount><total>10500.00</total><balance_due>10500.00</balance_due></totals>\n  <payment_terms>Net 30</payment_terms>\n</invoice>\n', 'paid', '2120'),
    ("s66 JSON 'reference': 'BALANCE-2026-77'", 's66.json', '{\n  "invoice_number": "INV-2147",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "reference": "BALANCE-2026-77"\n}', 'paid', '2120'),
    ("s67 JSON 'balance_due_date': '2026-10-30'", 's67.json', '{\n  "invoice_number": "INV-2148",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "balance_due_date": "2026-10-30"\n}', 'paid', '2120'),
    ("s68 JSON 'terms': 'balance due 25% on delivery'", 's68.json', '{\n  "invoice_number": "INV-2149",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "terms": "balance due 25% on delivery"\n}', 'paid', '2120'),
    ("s69 JSON 'remit_to': '1200 Commerce Blvd'", 's69.json', '{\n  "invoice_number": "INV-2150",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "remit_to": "1200 Commerce Blvd, Suite 300"\n}', 'paid', '2120'),
    ("s70 JSON 'amount_due': 10500 equal int", 's70.json', '{\n  "invoice_number": "INV-2151",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "amount_due": 10500\n}', 'paid', '2120'),
    ("s71 JSON 'customer_id': 'BALANCE2026'", 's71.json', '{\n  "invoice_number": "INV-2152",\n  "vendor": "Ironbridge Components Inc.",\n  "date": "2026-09-30",\n  "due_date": "2026-10-30",\n  "line_items": [\n    {\n      "item": "WidgetA",\n      "quantity": 12,\n      "unit_price": 250.0\n    },\n    {\n      "item": "WidgetB",\n      "quantity": 9,\n      "unit_price": 500.0\n    },\n    {\n      "item": "GadgetX",\n      "quantity": 4,\n      "unit_price": 750.0\n    }\n  ],\n  "subtotal": 10500.0,\n  "tax": 0.0,\n  "total": 10500.0,\n  "customer_id": "BALANCE2026"\n}', 'paid', '2120'),
    ("s72 CSV 'ref,BALANCE-2026-77'", 's72.csv', 'field,value\ninvoice_number,INV-2153\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\nref,BALANCE-2026-77\npayment_terms,Net 30\n', 'paid', '2120'),
    ("s73 CSV 'balance_due_date,2026-10-30'", 's73.csv', 'field,value\ninvoice_number,INV-2154\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\nbalance_due_date,2026-10-30\npayment_terms,Net 30\n', 'paid', '2120'),
    ("s74 CSV 'remit,4500 Industrial Way'", 's74.csv', 'field,value\ninvoice_number,INV-2155\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\nremit,4500 Industrial Way\npayment_terms,Net 30\n', 'paid', '2120'),
    ("s75 CSV 'outstanding,0'", 's75.csv', 'field,value\ninvoice_number,INV-2156\nvendor,Ironbridge Components Inc.\ndate,2026-09-30\ndue_date,2026-10-30\nitem,WidgetA\nquantity,12\nunit_price,250.00\nitem,WidgetB\nquantity,9\nunit_price,500.00\nitem,GadgetX\nquantity,4\nunit_price,750.00\nsubtotal,10500.00\ntax,0.00\ntotal,10500.00\noutstanding,0\npayment_terms,Net 30\n', 'paid', '2120'),
    ("s76 PDF 'Ref: BALANCE-2026-77'", 's76.pdf', 'INVOICE\n\nVendor: Larkspur Fabrication LLC\nInvoice Number: INV-2117\nDate: 2026-09-15\nDue Date: 2026-10-15\n\nItems:\n  WidgetA    qty: 14    unit price: $250.00\n  WidgetB    qty: 8     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nShipping: $125.00\nTotal Amount: $10,625.00\n\nRef: BALANCE-2026-77\nPayment Terms: Net 30\nNotes: PO-7716. Quarterly restock, standard freight.\n', 'paid', '2117'),
    ("s77 PDF 'Balance due: 10.30.2026'", 's77.pdf', 'INVOICE\n\nVendor: Larkspur Fabrication LLC\nInvoice Number: INV-2117\nDate: 2026-09-15\nDue Date: 2026-10-15\n\nItems:\n  WidgetA    qty: 14    unit price: $250.00\n  WidgetB    qty: 8     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nShipping: $125.00\nTotal Amount: $10,625.00\n\nBalance due: 10.30.2026\nPayment Terms: Net 30\nNotes: PO-7716. Quarterly restock, standard freight.\n', 'paid', '2117'),
    ("s78 PDF 'Balance due: 25% deposit, 75% on delivery'", 's78.pdf', 'INVOICE\n\nVendor: Larkspur Fabrication LLC\nInvoice Number: INV-2117\nDate: 2026-09-15\nDue Date: 2026-10-15\n\nItems:\n  WidgetA    qty: 14    unit price: $250.00\n  WidgetB    qty: 8     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nShipping: $125.00\nTotal Amount: $10,625.00\n\nBalance due: 25% deposit, 75% on delivery\nPayment Terms: Net 30\nNotes: PO-7716. Quarterly restock, standard freight.\n', 'paid', '2117'),
    ("s79 PDF 'Customer ID: BALANCE2026'", 's79.pdf', 'INVOICE\n\nVendor: Larkspur Fabrication LLC\nInvoice Number: INV-2117\nDate: 2026-09-15\nDue Date: 2026-10-15\n\nItems:\n  WidgetA    qty: 14    unit price: $250.00\n  WidgetB    qty: 8     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nShipping: $125.00\nTotal Amount: $10,625.00\n\nCustomer ID: BALANCE2026\nPayment Terms: Net 30\nNotes: PO-7716. Quarterly restock, standard freight.\n', 'paid', '2117'),
    ("s80 PDF freight two-column 'Freight  Balance Due'", 's80.pdf', 'INVOICE\n\nVendor: Larkspur Fabrication LLC\nInvoice Number: INV-2117\nDate: 2026-09-15\nDue Date: 2026-10-15\n\nItems:\n  WidgetA    qty: 14    unit price: $250.00\n  WidgetB    qty: 8     unit price: $500.00\n  GadgetX    qty: 4     unit price: $750.00\n\nSubtotal: $10,500.00\nTax (0%): $0.00\nShipping: $125.00\nTotal Amount: $10,625.00\n\nFreight            Balance Due\n$125.00            $10,625.00\nPayment Terms: Net 30\nNotes: PO-7716. Quarterly restock, standard freight.\n', 'paid', '2117'),
]


def _text_hold(name):
    return _2120.replace(_BAL, HOLD[name])


def _text_paid(name):
    return _2117.replace("Payment Terms:", PAID[name] + "\nPayment Terms:")


def _go(db_path, tmp_path, name, text, base, pdf=False):
    head = name.split()[0]
    if pdf:
        path = _pdf(tmp_path, head + ".pdf", text)
    elif text.lstrip().startswith("{"):
        path = write_invoice(tmp_path, head + ".json", text)
    elif text.lstrip().startswith("<"):
        path = write_invoice(tmp_path, head + ".xml", text)
    elif text.startswith("field,"):
        path = write_invoice(tmp_path, head + ".csv", text)
    else:
        path = write_invoice(tmp_path, head + ".txt", text)
    critique = {"concerns": [], "stricter": False}
    if base == "2117":
        handler, _ = scripted(_read("invoice_2117.txt"), critique=critique)
    else:
        handler, _ = scripted(_read("invoice_2120.txt", total="10500.00"), critique=critique)
    return _run(db_path, path, handler)


@pytest.mark.parametrize("name", sorted(HOLD))
def test_owed_layout_holds(db_path, tmp_path, name):
    text = _text_hold(name)
    result = _go(db_path, tmp_path, name, text, "2120")
    assert result["outcome"] == "hold", (name, balances_due(text))
    assert result["reason_code"] == "BALANCE_DUE", name
    assert result["payment"].status == "not_paid", name


@pytest.mark.parametrize("name", sorted(PAID))
def test_wording_is_not_an_amount_owed(db_path, tmp_path, name):
    text = _text_paid(name)
    result = _go(db_path, tmp_path, name, text, "2117")
    assert "BALANCE_DUE" not in _codes(result), (name, balances_due(text))
    assert result["outcome"] == "approve", name
    assert result["payment"].status == "paid", name


@pytest.mark.parametrize(
    ("name", "fname", "text", "want", "base"),
    FILES,
    ids=[row[0] for row in FILES],
)
def test_file_layout(db_path, tmp_path, name, fname, text, want, base):
    result = _go(db_path, tmp_path, name, text, base, pdf=fname.endswith(".pdf"))
    if want == "hold":
        assert result["outcome"] == "hold", (name, balances_due(text))
        assert result["reason_code"] == "BALANCE_DUE", name
        assert result["payment"].status == "not_paid", name
        return
    assert "BALANCE_DUE" not in _codes(result), (name, balances_due(text))
    assert result["outcome"] == "approve", name
    assert result["payment"].status == "paid", name

