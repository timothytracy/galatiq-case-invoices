"""Process-wide payment keys must not leak from one test into the next."""

import pytest

from pipeline.payment import _USED_KEYS


@pytest.fixture(autouse=True)
def clear_used_payment_keys():
    _USED_KEYS.clear()
    yield
    _USED_KEYS.clear()


@pytest.fixture(autouse=True)
def tools_default_on(monkeypatch):
    """Drop XAI_TOOLS so a test sees the process default, which is on.

    A test that needs the rollback sets XAI_TOOLS=0. Record and replay stay
    off too, so a developer shell cannot redirect a test.
    """

    monkeypatch.delenv("XAI_TOOLS", raising=False)
    monkeypatch.delenv("XAI_MODE", raising=False)
    monkeypatch.delenv("XAI_RECORDING", raising=False)
