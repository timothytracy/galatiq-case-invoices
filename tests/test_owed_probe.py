"""Every layout from the amount-owed probe. Fake model, no network.

A case marked hold is an amount owed below the total. A case marked paid
is ordinary wording, a credit marker, or an amount equal to the total.
"""

import importlib.util
import json

import pytest

from decision.printed_amounts import balances_due
from tests.test_loop_set import LOOP, _codes, _read, _run
from tests.fakes import scripted, write_invoice

pytestmark = pytest.mark.usefixtures("no_network")

_2120 = (LOOP / "invoice_2120.txt").read_text(encoding="utf-8")
_2117 = (LOOP / "invoice_2117.txt").read_text(encoding="utf-8")
_BAL = "Balance Due: $8,000.00"
_DEP = "Less deposit received 2026-08-15 (ref DEP-3302): -$2,500.00"


def _line(new):
    return _2120.replace(_BAL, new)


def _add(extra, where="Payment Terms:"):
    return _2117.replace(where, extra + "\n" + where)


def _pdf(tmp_path, name, text):
    spec = importlib.util.spec_from_file_location(
        "generate_pdf", LOOP.parents[1] / "data" / "extra_invoices" / "generate_pdf.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / name
    path.write_bytes(module.build_pdf(text.splitlines()))
    return path


def _csv(extra):
    return (
        "field,value\n"
        "invoice_number,INV-2131\n"
        "vendor,Ironbridge Components Inc.\n"
        "item,WidgetA\nitem,WidgetB\nitem,GadgetX\n"
        "total,10500.00\n"
        f"{extra}"
    )


def _json(**extra):
    body = {
        "vendor": "Ironbridge Components Inc.",
        "items": ["WidgetA", "WidgetB", "GadgetX"],
        "total": 10500.0,
    }
    body.update(extra)
    return json.dumps(body)


# hold: the page states $8,000 against a $10,500 total.
HOLD = {
    "q01 of the total": _line("Balance Due: $8,000.00 of $10,500.00"),
    "q02 credit or debit": _line("Balance Due: $8,000.00 Credit or debit accepted"),
    "q03 credit cards": _line("Balance Due: $8,000.00 Credit cards accepted"),
    "q04 bare 8000": _line("Balance Due 8000"),
    "q05 xml balancedue": _line("<BalanceDue>8000.00</BalanceDue>"),
    "q06 balancedue text": _line("BalanceDue: 8,000.00"),
    "q07 balance remaining": _line("Balance remaining: $8,000.00"),
    "q08 amount remaining": _line("Amount remaining: $8,000.00"),
    "q09 net due": _line("Net due: $8,000.00"),
    "q10 now due": _line("Now due: $8,000.00"),
    "q11 pay this amount": _line("Pay this amount: $8,000.00"),
    "q12 outstanding": _line("Outstanding: $8,000.00"),
    "q13 after deposit paren": _line("Amount Due (after deposit): $8,000.00"),
    "q14 credit card fee line": _line("Balance Due: $8,000.00\nCredit card payments incur a 3% fee."),
    "q16 blank line": _line("Balance Due:\n\n$8,000.00"),
    "q19 paren note": _line("Amount Due: $8,000.00 (balance after deposit)"),
    "q20 remit the balance of": _line("Please remit the balance of $8,000.00"),
    "q21 remit by date": _line("Remit $8,000.00 by 2026-10-30"),
    "q22 items shipped": _line("Balance Due: $8,000.00 items shipped in full"),
    "q23 dot leaders usd": _line("AMOUNT DUE ..... USD 8,000.00"),
    "q24 dash usd": _line("Balance due - USD 8000"),
    "q25 amount payable bare": _line("Amount payable: 8000"),
    "q26 json BalanceDue": _json(BalanceDue=8000),
    "q27 json balance": _json(balance=8000),
    "q28 json amount_due string": _json(amount_due="8000"),
    "q29 json nested": _json(payment={"balance_due": 8000.0}),
    "q30 csv Balance Due": _csv("Balance Due,8000\n"),
    "q31 csv amount_remaining": _csv("amount_remaining,8000.00\n"),
}

PAID = {
    "q18 cr marker": _line("Balance Due: $8,000.00 CR"),
    "m01 date month": _add("Amount due: 15 November 2026"),
    "m02 slash date": _add("Balance due: 10/30/2026"),
    "m03 iso date": _add("Total due: 2026-10-15"),
    "m04 percent": _2117.replace(
        "Payment Terms: Net 30",
        "Payment Terms: Deposit 0%, balance due: 50% on delivery, 50% Net 30",
    ),
    "m05 ordinal": _2117.replace(
        "standard freight.", "standard freight. Balance due: 2nd installment in March."
    ),
    "m06 street": _add("Please remit: 4500 Industrial Way, Dayton, OH 45401"),
    "m07 opening balance": _add("Opening balance: $1,200.00"),
    "m08 deposit balance": _add("Deposit balance: $2,500.00"),
    "m09 zero account balance due": _add("Account Balance Due: $0.00"),
    "m10 credit balance": _add("Credit balance: $200.00"),
    "m11 statement balance": _add("Last statement balance: $1,200.00"),
    "m12 equal plus credit card": _add("Balance Due: $10,625.00 Credit card accepted"),
    "m13 within days": _2117.replace(
        "Payment Terms: Net 30", "Payment Terms: Amount due within 30 days"
    ),
    "m14 upon receipt": _2117.replace(
        "Payment Terms: Net 30", "Payment Terms: Total due upon receipt"
    ),
    "m15 units backorder": _2117.replace(
        "standard freight.", "standard freight. Balance - 3 units on backorder."
    ),
    "m16 pallets": _2117.replace(
        "standard freight.", "standard freight. Balance: 2 pallets to follow."
    ),
    "m17 days net": _2117.replace(
        "Payment Terms: Net 30", "Payment Terms: Balance due: 30 days net"
    ),
    "m18 due by date": _add("Amount due by: 2026-10-15"),
    "m19 remit suite": _add("Please remit to\nSuite 200, 4500 Industrial Way"),
    "m20 remit box": _add("Please remit:\nPO Box 45, Dayton OH"),
    "m21 none": _add("Remaining balance: none"),
    "m22 equal owing": _add("Balance owing: $10,625.00"),
    "m23 one of two": _add("Balance due: 1 of 2 shipments"),
    "m24 reference dash": _add("Ref: BALANCE-2026-77"),
    "m25 percent receipt": _2117.replace(
        "Payment Terms: Net 30", "Payment Terms: Amount due: 100% on receipt"
    ),
    "m26 previous balance due": _add("Previous balance due: $500.00"),
    "m27 freight line": _add("Amount Due: $10,625.00\n$125.00 freight included"),
    "m28 shipping line": _add("Balance due on the total below\n$125.00 shipping included"),
    "m29 json days": _json(notes="balance due: 30 days after delivery"),
    "m30 json nov": _json(payment_terms="Amount due: 15 Nov 2026"),
    "m31 json street": _json(remit_to="Please remit: 4500 Industrial Way, Dayton OH"),
    "m32 csv days": _csv("notes,balance due: 30 days\n"),
    "m33 csv equal": _csv("balance_due,10500.00\n"),
    "m36 json units": _json(
        line_items=[
            {"item": "WidgetA", "description": "Balance: 2 units remain on PO"},
            {"item": "WidgetB"},
            {"item": "GadgetX"},
        ]
    ),
}


def _suffix(name):
    if name.startswith(("q26", "q27", "q28", "q29", "m29", "m30", "m31", "m36")):
        return ".json"
    if name.startswith(("q30", "q31", "m32", "m33")):
        return ".csv"
    return ".txt"


def _go(db_path, tmp_path, name, text, base):
    path = write_invoice(tmp_path, name.split()[0] + _suffix(name), text)
    critique = {"concerns": [], "stricter": False}
    if base == "2117":
        handler, _ = scripted(_read("invoice_2117.txt"), critique=critique)
    else:
        handler, _ = scripted(_read("invoice_2120.txt", total="10500.00"), critique=critique)
    return _run(db_path, path, handler)


@pytest.mark.parametrize("name", sorted(HOLD))
def test_owed_layout_holds(db_path, tmp_path, name):
    result = _go(db_path, tmp_path, name, HOLD[name], "2120")
    assert result["outcome"] == "hold", name
    assert result["reason_code"] == "BALANCE_DUE", name
    assert result["payment"].status == "not_paid", name


@pytest.mark.parametrize("name", sorted(PAID))
def test_wording_is_not_an_amount_owed(db_path, tmp_path, name):
    # m29 and later are 2120-shaped files. m01 through m28 are 2117.
    base = "2120" if name.startswith("q") or name[:3] >= "m29" else "2117"
    result = _go(db_path, tmp_path, name, PAID[name], base)
    assert "BALANCE_DUE" not in _codes(result), (name, balances_due(PAID[name]))
    assert result["outcome"] == "approve", name
    assert result["payment"].status == "paid", name


def test_pdf_bare_amount_and_of_total_hold(db_path, tmp_path):
    for name, text in (
        ("q32.pdf", _line("Balance Due 8000")),
        ("q33.pdf", _line("Balance Due: $8,000.00 of $10,500.00")),
        ("q34.pdf", _line("Amount\nowing\n$8,000.00")),
    ):
        path = _pdf(tmp_path, name, text)
        handler, _ = scripted(_read("invoice_2120.txt", total="10500.00"))
        result = _run(db_path, path, handler)
        assert result["outcome"] == "hold", name
        assert result["reason_code"] == "BALANCE_DUE", name
        assert result["payment"].status == "not_paid", name


def test_pdf_date_and_street_are_paid(db_path, tmp_path):
    for name, text in (
        ("m34.pdf", _add("Amount due: 15 November 2026")),
        ("m35.pdf", _add("Please remit: 4500 Industrial Way, Dayton, OH 45401")),
    ):
        path = _pdf(tmp_path, name, text)
        handler, _ = scripted(_read("invoice_2117.txt"), critique={"concerns": [], "stricter": False})
        result = _run(db_path, path, handler)
        assert "BALANCE_DUE" not in _codes(result), name
        assert result["outcome"] == "approve", name
        assert result["payment"].status == "paid", name
