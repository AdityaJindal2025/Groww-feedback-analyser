"""Classification quality check (T4.6).

1. `pulse eval export` writes 100 reviews from the coverage sample to
   `data/labelled_sample.jsonl` (gitignored: it holds real, scrubbed review text), each with an
   empty `label`. It never overwrites an existing file, so hand labels are safe.
2. A person fills in `label` with a theme id (or "other") for each row.
3. `pulse eval classify` classifies those reviews afresh (nothing is saved) and reports
   agreement overall and per theme, plus a confusion table. Target: ≥80% before M2 sign-off.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pulse.analysis.classifier import classify_reviews
from pulse.analysis.taxonomy import coverage_sample
from pulse.config import Settings
from pulse.llm.client import LLMClient, Usage
from pulse.llm.prompts import load_prompt
from pulse.models import OTHER_THEME_ID, Taxonomy
from pulse.store import db
from pulse.timeutil import window_for

LABEL_FILE = Path("data/labelled_sample.jsonl")
UNCLASSIFIED = "(unclassified)"


class EvalError(ValueError):
    pass


def export_sheet(
    conn: sqlite3.Connection, settings: Settings, week: str, path: Path, *, n: int = 100
) -> int:
    if path.exists():
        raise EvalError(f"{path} already exists; not overwriting hand labels")
    start, end = window_for(week, settings.window.weeks)
    reviews = db.reviews_in_window(conn, start, end)
    analysis = settings.analysis
    sample = coverage_sample(
        reviews,
        size=analysis.coverage_sample_size,
        min_per_rating=analysis.coverage_min_per_rating,
        seed=analysis.sample_seed,
    )
    picked = random.Random(analysis.sample_seed).sample(sample, min(n, len(sample)))
    picked.sort(key=lambda r: (r.review_date, r.review_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for review in picked:
            row = {
                "review_id": review.review_id,
                "rating": review.rating,
                "text": review.text,
                "label": None,
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(picked)


@dataclass
class EvalReport:
    total: int = 0
    agree: int = 0
    per_theme: dict[str, list[int]] = field(default_factory=dict)  # gold theme -> [n, correct]
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)  # (gold, predicted)
    usage: Usage | None = None

    @property
    def agreement(self) -> float:
        return self.agree / self.total if self.total else 0.0


def evaluate(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLMClient,
    taxonomy: Taxonomy,
    path: Path,
) -> EvalReport:
    if not path.exists():
        raise EvalError(f"{path} not found; run `pulse eval export` and label it first")
    valid = taxonomy.theme_ids() | {OTHER_THEME_ID}
    gold: dict[str, str] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        label = row.get("label")
        if label is None:
            continue
        if label not in valid:
            raise EvalError(f"{path}:{line_no}: unknown label {label!r} for {taxonomy.version}")
        gold[row["review_id"]] = label
    if not gold:
        raise EvalError(f"{path} has no labelled rows yet")
    reviews = [r for r in db.iter_reviews(conn) if r.review_id in gold]
    missing = len(gold) - len(reviews)
    if missing:
        raise EvalError(f"{missing} labelled reviews are no longer in the store")
    reviews.sort(key=lambda r: r.review_id)
    _, results = classify_reviews(
        None,
        reviews,
        taxonomy,
        llm,
        load_prompt("classify", settings.paths.prompts),
        chunk_size=settings.llm.classify.chunk_size,
    )
    report = EvalReport()
    for item in results:
        expected = gold[item.review_id]
        predicted = item.theme_id if item.status == "ok" and item.theme_id else UNCLASSIFIED
        report.total += 1
        correct = int(predicted == expected)
        report.agree += correct
        counts = report.per_theme.setdefault(expected, [0, 0])
        counts[0] += 1
        counts[1] += correct
        report.confusion[(expected, predicted)] += 1
    return report
