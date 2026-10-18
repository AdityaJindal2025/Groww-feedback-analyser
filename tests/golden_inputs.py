"""The hand-written review fixture behind the golden and privacy end-to-end tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pulse.ingest.csv_import import CsvSource
from tests.conftest import REPO_ROOT

REVIEWS = REPO_ROOT / "tests" / "fixtures" / "reviews_synthetic.jsonl"
COLUMNS = {
    "text": "text",
    "rating": "rating",
    "review_date": "date",
    "thumbs_up": "thumbs_up",
    "store_review_id": "id",
}
FAILING_FILTERS = 6  # the last rows fail the ingest filters on purpose


def source(path: Path = REVIEWS) -> CsvSource:
    return CsvSource(path, COLUMNS, store="play_store", timezone="Asia/Kolkata")


def golden_rows(path: Path = REVIEWS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
