"""Sample-invoice graph output with the test fake LLM.

Outcomes match data/labels.json. The snapshot includes finding codes and, on
a paid invoice, the audit authorization. Wall-clock timestamps and absolute
paths are normalized. The approval prompt text is not in this snapshot.
"""

import json
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


def test_fake_llm_eval_matches_captured_output(db_path):
    actual = run_samples(db_path)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert actual == expected
