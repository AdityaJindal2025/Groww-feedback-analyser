"""Review classification (T4.4, T4.5).

Reviews are sent in chunks of `chunk_size`, one synchronous call each, one after another so the
cached system prompt (rules + taxonomy) is reused. Each chunk's results are saved as soon as it
returns, so an interrupted run resumes with only the unfinished reviews.

Code checks every chunk: each review must come back exactly once with a known theme. Reviews
that are missing or invalid get one retry on their own; if still bad, or if Claude declines the
chunk, they are stored as `unclassified` (excluded from shares). Rate-limit and connection
errors stop the run instead, so a re-run can pick up where it left off.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pulse.llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMRefusal,
    LLMTruncated,
    Usage,
)
from pulse.llm.outputs import LabelBatch, ReviewLabel
from pulse.llm.prompts import Prompt, format_reviews
from pulse.models import OTHER_THEME_ID, Classification, Review, Taxonomy, utc_now
from pulse.store import db

log = logging.getLogger(__name__)


@dataclass
class ClassifyReport:
    considered: int = 0
    ok: int = 0
    unclassified: int = 0
    chunks: int = 0
    retried: int = 0
    by_theme: Counter[str] = field(default_factory=Counter)
    usage: Usage | None = None

    @property
    def other_share(self) -> float:
        return self.by_theme[OTHER_THEME_ID] / self.ok if self.ok else 0.0

    def counts(self) -> dict[str, int | float]:
        counts: dict[str, int | float] = {
            "considered": self.considered,
            "classified": self.ok,
            "unclassified": self.unclassified,
            "chunks": self.chunks,
            "retried": self.retried,
            "other_share": round(self.other_share, 4),
        }
        counts.update({f"theme.{k}": v for k, v in sorted(self.by_theme.items())})
        return counts


def system_prompt(prompt: Prompt, taxonomy: Taxonomy) -> str:
    """Deterministic: the same taxonomy always renders the same bytes (keeps the cache valid)."""
    return prompt.render(themes=[theme.model_dump() for theme in taxonomy.themes])


def classify_reviews(
    conn: sqlite3.Connection | None,
    reviews: Sequence[Review],
    taxonomy: Taxonomy,
    llm: LLMClient,
    prompt: Prompt,
    *,
    chunk_size: int,
    now: datetime | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> tuple[ClassifyReport, list[Classification]]:
    """Classify `reviews`; save each chunk to `conn` as it completes (if given)."""
    system = system_prompt(prompt, taxonomy)
    valid = taxonomy.theme_ids() | {OTHER_THEME_ID}
    report = ClassifyReport(considered=len(reviews))
    results: list[Classification] = []
    chunks = [reviews[i : i + chunk_size] for i in range(0, len(reviews), chunk_size)]
    for n, chunk in enumerate(chunks, start=1):
        labels, usage, retried = _label_chunk(chunk, system, valid, llm)
        report.usage = usage if report.usage is None else report.usage + usage
        report.chunks += 1
        report.retried += retried
        stamp = now or utc_now()
        model = usage.model if usage.calls else None
        rows = [
            _classification(review, labels.get(review.review_id), taxonomy, prompt, model, stamp)
            for review in chunk
        ]
        if conn is not None:
            db.save_classifications(conn, rows)
        for row in rows:
            if row.status == "ok" and row.theme_id is not None:
                report.ok += 1
                report.by_theme[row.theme_id] += 1
            else:
                report.unclassified += 1
        results.extend(rows)
        log.info("classified chunk", extra={"chunk": n, "of": len(chunks), "reviews": len(chunk)})
        if on_chunk:
            on_chunk(n, len(chunks))
    return report, results


def _label_chunk(
    chunk: Sequence[Review], system: str, valid: set[str], llm: LLMClient
) -> tuple[dict[str, ReviewLabel], Usage, int]:
    """Labels keyed by review_id; one retry for reviews that come back missing or invalid."""
    labels: dict[str, ReviewLabel] = {}
    usage = Usage(model="none")
    pending = list(chunk)
    retried = 0
    for attempt in (1, 2):
        user, ids = format_reviews(pending)
        try:
            result = llm.call_structured("classify", system, user, LabelBatch)
        except (LLMRefusal, LLMInvalidOutput, LLMTruncated) as exc:
            log.warning("classify chunk failed", extra={"attempt": attempt, "error": str(exc)})
            if attempt == 1 and len(pending) > 1:
                retried += len(pending)
                continue
            break
        usage = result.usage if usage.calls == 0 else usage + result.usage
        for item in result.output.items:
            review_id = ids.get(item.rid)
            if review_id is not None and item.theme_id in valid:
                labels[review_id] = item
        pending = [r for r in pending if r.review_id not in labels]
        if not pending:
            break
        if attempt == 1:
            retried += len(pending)
    return labels, usage, retried


def _classification(
    review: Review,
    label: ReviewLabel | None,
    taxonomy: Taxonomy,
    prompt: Prompt,
    model: str | None,
    stamp: datetime,
) -> Classification:
    if label is None:
        return Classification(
            review_id=review.review_id,
            taxonomy_version=taxonomy.version,
            status="unclassified",
            model=model,
            prompt_version=prompt.version,
            classified_at=stamp,
        )
    return Classification(
        review_id=review.review_id,
        taxonomy_version=taxonomy.version,
        status="ok",
        theme_id=label.theme_id,
        sentiment=label.sentiment,
        mentions_person=label.mentions_person,
        model=model,
        prompt_version=prompt.version,
        classified_at=stamp,
    )
