"""Process-wide payment keys must not leak from one test into the next."""

from pathlib import Path

import pytest

from db import ensure_inventory_db
from pipeline.payment import _USED_KEYS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clear_used_payment_keys():
    _USED_KEYS.clear()
    yield
    _USED_KEYS.clear()


@pytest.fixture(autouse=True)
def no_recording_env(monkeypatch):
    """Record and replay stay off, so a developer shell cannot redirect a test."""

    monkeypatch.delenv("XAI_MODE", raising=False)
    monkeypatch.delenv("XAI_RECORDING", raising=False)


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """A developer .env must not put a real key back into a test."""

    monkeypatch.setattr("llm_client.load_dotenv", lambda path=None: None)
    monkeypatch.setattr("ui.runner.load_dotenv", lambda path=None: None)


@pytest.fixture(autouse=True)
def release_records(tmp_path, monkeypatch):
    """Pipeline release records stay out of the repo logs/ directory."""

    monkeypatch.setenv("INVOICE_RELEASE_DIR", str(tmp_path / "releases"))


@pytest.fixture
def inventory_db(tmp_path):
    """Seed inventory file for this test. Not the repository inventory.db."""

    path = tmp_path / "inventory.db"
    ensure_inventory_db(path)
    return path


@pytest.fixture(autouse=True)
def isolate_inventory_db(inventory_db, monkeypatch):
    """Point the default inventory path at this test's temporary file.

    The review screen calls run_one without a database path, and that helper
    uses ui.runner.DEFAULT_DB. Patching it here keeps the test off the
    repository file. The screen itself is unchanged.
    """

    monkeypatch.setenv("INVENTORY_DB", str(inventory_db))
    monkeypatch.setattr("ui.runner.DEFAULT_DB", inventory_db)


@pytest.fixture(scope="session", autouse=True)
def repo_root_inventory_is_not_created():
    """Fail the session if a test creates inventory.db at the repository root."""

    path = ROOT / "inventory.db"
    existed = path.exists()
    yield
    if not existed:
        assert not path.exists(), "a test created inventory.db at the repository root"
