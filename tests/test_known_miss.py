"""A listed known miss stays visible on the score line and does not fail the run."""

import json
from pathlib import Path

from scripts.eval import score_line, score_mark

ROOT = Path(__file__).resolve().parents[1]
ROW = {"outcome": "HOLD", "known_miss": {"outcome": "APPROVE", "why": "audit reads past it"}}


def test_the_listed_miss_is_known():
    assert score_mark(False, "APPROVE", ROW) == "KNOWN_MISS"


def test_a_different_wrong_outcome_is_still_a_miss():
    assert score_mark(False, "REJECT", ROW) == "MISS"


def test_getting_the_label_says_the_entry_can_go():
    assert score_mark(True, "HOLD", ROW) == "KNOWN_MISS_FIXED"


def test_a_row_without_a_known_miss_is_unchanged():
    assert score_mark(True, "HOLD", {"outcome": "HOLD"}) == "ok"
    assert score_mark(False, "APPROVE", {"outcome": "HOLD"}) == "MISS"


def test_the_score_line_counts_known_misses():
    assert score_line(7, 0, 7) == "score 7/7"
    assert score_line(6, 1, 7) == "score 6/7 (1 known miss)"
    assert score_line(5, 2, 7) == "score 5/7 (2 known misses)"


def test_no_answer_key_lists_a_known_miss():
    listed = {}
    for key in sorted((ROOT / "data").glob("*answer_key*.json")):
        rows = json.loads(key.read_text(encoding="utf-8"))["invoices"]
        names = [name for name, row in rows.items() if row.get("known_miss")]
        if names:
            listed[key.name] = names
    assert listed == {}
