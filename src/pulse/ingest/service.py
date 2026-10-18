"""Ingest service (T2.6): fetch -> scrub -> normalise -> filter -> hash ID -> upsert.

Scrubbing happens before anything is stored. Only reviews that pass `sources.filters` are
stored (more than 8 words, no emoji, English); stored reviews that no longer pass the current
filters are removed, so a filter change takes effect on the next ingest.

A source that fails or stops early doesn't stop the run: reviews it already returned are kept
and the report flags a partial ingest (arch §11).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from pulse.config import ReviewFilters, Settings
from pulse.ingest.base import ReviewSource, SourceError, hash_review_id
from pulse.ingest.csv_import import CsvSource
from pulse.ingest.normalize import (
    detect_script,
    has_emoji,
    has_redaction,
    is_hinglish,
    word_count,
)
from pulse.ingest.play_store import PlayStoreSource
from pulse.models import RawReview, Review, utc_now
from pulse.privacy.scrubber import scan, scrub
from pulse.store import db

log = logging.getLogger(__name__)

FLUSH_EVERY = 500


@dataclass
class IngestReport:
    fetched: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    pruned: int = 0
    filtered: Counter[str] = field(default_factory=Counter)
    redactions: Counter[str] = field(default_factory=Counter)
    per_source: Counter[str] = field(default_factory=Counter)
    partial: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict[str, int | float]:
        counts: dict[str, int | float] = {
            "fetched": self.fetched,
            "new": self.new,
            "updated": self.updated,
            "duplicates": self.duplicates,
            "kept": self.fetched - sum(self.filtered.values()),
            "pruned": self.pruned,
        }
        counts.update({f"filtered.{reason}": n for reason, n in sorted(self.filtered.items())})
        counts.update({f"fetched.{name}": n for name, n in sorted(self.per_source.items())})
        counts.update({f"redactions.{kind}": n for kind, n in sorted(self.redactions.items())})
        return counts


def prepare_review(raw: RawReview, *, salt: str, now: datetime) -> tuple[Review, dict[str, int]]:
    """Scrub and normalise one review. Returns the review and its redaction counts."""
    scrubbed = scrub(raw.text)
    review = Review(
        review_id=hash_review_id(raw.store, raw.store_review_id, salt=salt),
        store=raw.store,
        rating=raw.rating,
        text=scrubbed.text,
        review_date=raw.review_date,
        app_version=raw.app_version,
        thumbs_up=raw.thumbs_up,
        script=detect_script(scrubbed.text),
        word_count=word_count(scrubbed.text),
        has_redaction=has_redaction(scrubbed.text),
        ingested_at=now,
    )
    return review, scrubbed.counts


def rejection_reason(review: Review, filters: ReviewFilters) -> str | None:
    """Why a review is filtered out (`short`, `emoji`, `language`), or None to keep it."""
    if review.word_count < filters.min_words:
        return "short"
    if filters.drop_emoji and has_emoji(review.text):
        return "emoji"
    if filters.english_only and (review.script != "latin" or is_hinglish(review.text)):
        return "language"
    return None


def build_sources(settings: Settings) -> list[ReviewSource]:
    sources: list[ReviewSource] = []
    play = settings.sources.play_store
    if play.enabled:
        app = settings.app.play_store
        sources.append(
            PlayStoreSource(
                app.package_id,
                lang=app.lang,
                country=app.country,
                page_size=play.page_size,
                page_delay_seconds=play.page_delay_seconds,
                max_reviews=play.max_reviews,
            )
        )
    csv_cfg = settings.sources.csv
    if csv_cfg.enabled and csv_cfg.path is not None:
        sources.append(
            CsvSource(
                csv_cfg.path,
                csv_cfg.columns,
                store=csv_cfg.store,
                timezone=csv_cfg.timezone,
                date_format=csv_cfg.date_format,
            )
        )
    return sources


def ingest(
    conn: sqlite3.Connection,
    sources: Iterable[ReviewSource],
    since: datetime,
    *,
    salt: str,
    filters: ReviewFilters,
    now: datetime | None = None,
) -> IngestReport:
    now = now or utc_now()
    report = IngestReport()
    for source in sources:
        buffer: list[Review] = []
        try:
            for raw in source.fetch(since):
                review, redactions = prepare_review(raw, salt=salt, now=now)
                report.fetched += 1
                report.per_source[source.name] += 1
                reason = rejection_reason(review, filters)
                if reason is not None:
                    report.filtered[reason] += 1
                    continue
                buffer.append(review)
                report.redactions.update(redactions)
                if len(buffer) >= FLUSH_EVERY:
                    _flush(conn, buffer, report)
        except SourceError as exc:
            report.partial[source.name] = str(exc)
            log.warning("partial ingest", extra={"source": source.name, "reason": str(exc)})
        finally:
            _flush(conn, buffer, report)
    report.pruned = prune_store(conn, filters)
    return report


def prune_store(conn: sqlite3.Connection, filters: ReviewFilters) -> int:
    """Delete stored reviews (and their classifications) that fail the current filters."""
    failing = [
        review.review_id
        for review in db.iter_reviews(conn)
        if rejection_reason(review, filters) is not None
    ]
    return db.delete_reviews(conn, failing)


def scan_store(conn: sqlite3.Connection) -> int:
    """Number of stored reviews in which the scrubber patterns still find something."""
    return sum(1 for _, text in db.iter_review_texts(conn) if scan(text))


def _flush(conn: sqlite3.Connection, buffer: list[Review], report: IngestReport) -> None:
    if not buffer:
        return
    result = db.upsert_reviews(conn, buffer)
    report.new += result.new
    report.updated += result.updated
    report.duplicates += result.unchanged
    buffer.clear()
