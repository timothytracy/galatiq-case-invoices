"""Sample-invoice graph output with the test fake LLM.

The snapshot is one row per file: outcome, reason code, and reason.
as_of is fixed inside run_samples, so the row does not depend on today's date.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from db import create_database
from tests.fake_llm_eval import run_samples

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fake_llm_eval.json"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "inventory.db"
    create_database(path)
    return path


def test_fake_llm_eval_matches_captured_output(db_path, monkeypatch):
    class Frozen(date):
        @classmethod
        def today(cls):
            return cls(2035, 6, 1)

    monkeypatch.setattr("decision.wording.date", Frozen)
    actual = run_samples(db_path)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert actual == expected
    assert actual["as_of"] == "2026-10-31"
