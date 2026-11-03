"""A long space run after a label stays fast. The amount read does not change."""

import time
from decimal import Decimal

import pytest

from decision.printed_amounts import balances_due, printed_totals

_LABELS = [
    "Balance",
    "Amount Due",
    "Total Due",
    "Please remit",
    "Remaining to pay",
    "balance_due",
    "Total",
]


def test_long_whitespace_after_a_label_is_fast():
    texts = [
        "Balance" + " " * 5000,
        "Balance" + "\n" * 5000,
        "Total" + " " * 5000,
        "Total" + "\n" * 5000,
    ]
    for sep in ":,=>":
        texts.append("Balance" + sep + " " * 5000)
        texts.append("Balance" + " " * 5000 + sep)
    for sep in ":=>":
        texts.append("Total" + sep + " " * 5000)
    for text in texts:
        started = time.perf_counter()
        balances_due(text)
        printed_totals(text)
        assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("label", _LABELS)
@pytest.mark.parametrize("n", [40, 100, 200, 400])
def test_spaces_after_a_label_are_not_an_amount(label, n):
    assert balances_due(label + " " * n + "see stub") == []


@pytest.mark.parametrize("n", [100, 200, 400])
def test_blank_lines_and_an_unclosed_paren_are_not_an_amount(n):
    assert balances_due("Balance" + "\n" * n + "x") == []
    assert balances_due("Balance (" + " " * n + "x") == []


@pytest.mark.parametrize("n", [1000, 10000])
def test_digit_runs_keep_the_same_amount(n):
    digits = "9" * n
    assert balances_due("Balance " + digits) == [Decimal(digits)]
    assert balances_due("Balance " + "9," * n + "x") == [Decimal(digits)]
    assert balances_due("Balance 1." + digits + ".") == [Decimal("1." + digits)]
    assert balances_due("Balance   " * n) == []
    assert balances_due("balance_" * n) == []
