"""Process-wide payment keys must not leak from one test into the next."""

import pytest

from pipeline.payment import _USED_KEYS


@pytest.fixture(autouse=True)
def clear_used_payment_keys():
    _USED_KEYS.clear()
    yield
    _USED_KEYS.clear()
