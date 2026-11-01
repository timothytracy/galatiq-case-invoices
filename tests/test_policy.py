"""policy.toml is the only copy of the business-rule knobs."""

from decimal import Decimal
from pathlib import Path

import pytest

from rules import PolicyError, load_policy

GOOD = """\
scrutiny_threshold = "10000"
allowed_currencies = ["USD"]
required_fields = ["vendor", "due_date", "line_items", "total"]
money_tolerance = "0.01"
critique_cap = 1
audit_send_back_cap = 3
model_retry_attempts = 5
near_threshold_ratio = "0.02"
price_fraud_ratio = "0.50"
residential_address_markers = ["Terrace", "Lane", "Court", "Circle", "Place", "Way", "Trail"]
civic_address_markers = ["Pennsylvania Ave", "Pennsylvania Avenue", "White House"]

[reference_prices]
WidgetA = "250"
WidgetB = "500"
GadgetX = "750"
"""


def test_shipped_policy_matches_todays_behavior(tmp_path):
    policy = load_policy()
    assert policy.scrutiny_threshold == Decimal("10000")
    assert policy.allowed_currencies == ["USD"]
    assert policy.required_fields == ["vendor", "due_date", "line_items", "total"]
    assert policy.money_tolerance == Decimal("0.01")
    assert policy.critique_cap == 1
    assert policy.audit_send_back_cap == 3
    assert policy.model_retry_attempts == 5

    copy = load_policy(_write(tmp_path, GOOD))
    assert copy == policy


def _write(directory: Path, text: str) -> Path:
    path = directory / "policy.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text, key",
    [
        (GOOD.replace('scrutiny_threshold = "10000"\n', ""), "scrutiny_threshold"),
        (GOOD.replace("critique_cap = 1", 'critique_cap = "many"'), "critique_cap"),
        (GOOD.replace("audit_send_back_cap = 3", "audit_send_back_cap = -1"), "audit_send_back_cap"),
        (GOOD.replace('allowed_currencies = ["USD"]', "allowed_currencies = []"), "allowed_currencies"),
        (GOOD.replace('money_tolerance = "0.01"', 'money_tolerance = "-0.01"'), "money_tolerance"),
    ],
)
def test_bad_policy_names_the_key(tmp_path, text, key):
    with pytest.raises(PolicyError, match=key):
        load_policy(_write(tmp_path, text))
