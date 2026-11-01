"""Local hold-queue notes. This file does not import the pipeline or payment."""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_PATH = ROOT / "output" / "reviews" / "hold_reviews.json"

def load_reviews(path: Path | None = None) -> list[dict]:
    """Reviews already saved. A missing file means there are none."""
    destination = path or REVIEW_PATH
    if not destination.is_file():
        return []
    reviews = json.loads(destination.read_text(encoding="utf-8")).get("reviews")
    if not isinstance(reviews, list):
        raise ValueError(f"{destination} has no reviews list")
    return reviews

def record_review(
    *, file: str, pipeline_outcome: str, pipeline_reason_code: str | None,
    pipeline_action: str | None, reviewer_outcome: str, note: str, path: Path | None = None,
) -> dict:
    """Append a reviewer's approve or reject. Does not pay or change the pipeline outcome."""
    if pipeline_outcome != "hold":
        raise ValueError("only a hold can be recorded")
    if reviewer_outcome not in {"approve", "reject"}:
        raise ValueError("reviewer_outcome must be approve or reject")
    text = note.strip()
    if not text:
        raise ValueError("A note is required.")
    destination = path or REVIEW_PATH
    reviews = [row for row in load_reviews(destination) if row.get("file") != file]
    entry = {
        "file": file, "pipeline_outcome": pipeline_outcome,
        "pipeline_reason_code": pipeline_reason_code, "pipeline_action": pipeline_action,
        "reviewer_outcome": reviewer_outcome, "note": text,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    reviews.append(entry)
    reviews.sort(key=lambda row: row.get("file") or "")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"reviews": reviews}, indent=2) + "\n", encoding="utf-8")
    return entry
