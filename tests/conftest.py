"""Process-wide payment keys must not leak from one test into the next."""

import pytest

from pipeline.payment import _USED_KEYS


@pytest.fixture(autouse=True)
def clear_used_payment_keys():
    _USED_KEYS.clear()
    yield
    _USED_KEYS.clear()


@pytest.fixture(autouse=True)
def tools_off_unless_a_test_sets_them(monkeypatch):
    """XAI_TOOLS stays off in tests. A test that wants tools sets it itself."""

    monkeypatch.delenv("XAI_TOOLS", raising=False)
