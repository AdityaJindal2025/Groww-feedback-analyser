"""Theme discovery inputs and taxonomy versions (T4.1–T4.3).

Discovery reads every review in the window (about 1,500 after the ingest filters, about 100k
tokens), so rare themes aren't lost to sampling. A separate stratified coverage sample is used
to check how well a proposed taxonomy covers the data, and for hand-labelling (T4.6).

Taxonomy versions live in the `taxonomies` table (used at run time) and are mirrored to
`config/themes.yaml` so they can be reviewed and version-controlled. Exactly one version is
approved at a time.
"""

from __future__ import annotations

import random
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from pulse.llm.outputs import TaxonomyProposal
from pulse.models import Review, Taxonomy, Theme, utc_now
from pulse.store import db
from pulse.timeutil import week_of


class TaxonomyError(ValueError):
    pass


# --- inputs (T4.1) -------------------------------------------------------------------------


def discovery_reviews(
    reviews: Sequence[Review], *, max_reviews: int, sample_size: int, seed: int
) -> list[Review]:
    """All reviews, or a stratified sample if the window is unusually large."""
    if len(reviews) <= max_reviews:
        return list(reviews)
    return coverage_sample(reviews, size=sample_size, min_per_rating=0, seed=seed)


def coverage_sample(
    reviews: Sequence[Review], *, size: int, min_per_rating: int, seed: int
) -> list[Review]:
    """A reproducible sample stratified by star rating, then spread across weeks.

    Each rating gets at least `min_per_rating` reviews (or all it has); the rest of the sample is
    shared out in proportion to the window's rating mix.
    """
    by_rating: dict[int, list[Review]] = defaultdict(list)
    for review in sorted(reviews, key=lambda r: r.review_id):
        by_rating[review.rating].append(review)
    quotas = _rating_quotas({r: len(v) for r, v in by_rating.items()}, size, min_per_rating)
    rng = random.Random(seed)
    sample: list[Review] = []
    for rating in sorted(by_rating):
        sample.extend(_spread_across_weeks(by_rating[rating], quotas[rating], rng))
    return sorted(sample, key=lambda r: (r.review_date, r.review_id))


def _rating_quotas(counts: dict[int, int], size: int, minimum: int) -> dict[int, int]:
    total = sum(counts.values())
    size = min(size, total)
    fixed: dict[int, int] = {}
    # Ratings whose proportional share falls below the minimum get the minimum (or all they have).
    while True:
        free = {r: c for r, c in counts.items() if r not in fixed}
        free_slots = size - sum(fixed.values())
        free_total = sum(free.values())
        newly = {
            r: min(minimum, c)
            for r, c in free.items()
            if free_total and free_slots * c / free_total < min(minimum, c)
        }
        if not newly:
            break
        fixed.update(newly)
    free = {r: c for r, c in counts.items() if r not in fixed}
    free_slots = size - sum(fixed.values())
    free_total = sum(free.values()) or 1
    exact = {r: free_slots * c / free_total for r, c in free.items()}
    quotas = {r: int(v) for r, v in exact.items()}
    # Largest remainder, so quotas add up exactly to the sample size.
    leftover = free_slots - sum(quotas.values())
    for r in sorted(exact, key=lambda r: (exact[r] - quotas[r], -r), reverse=True)[:leftover]:
        quotas[r] += 1
    quotas.update(fixed)
    return {r: min(q, counts[r]) for r, q in quotas.items()}


def _spread_across_weeks(reviews: list[Review], n: int, rng: random.Random) -> list[Review]:
    by_week: dict[str, list[Review]] = defaultdict(list)
    for review in reviews:
        by_week[week_of(review.review_date)].append(review)
    pools = [by_week[week] for week in sorted(by_week)]
    for pool in pools:
        rng.shuffle(pool)
    picked: list[Review] = []
    while len(picked) < n and any(pools):
        for pool in pools:
            if pool and len(picked) < n:
                picked.append(pool.pop())
    return picked


# --- versions (T4.2, T4.3) -----------------------------------------------------------------


def next_version(conn: sqlite3.Connection) -> str:
    numbers = [
        int(t.version[1:])
        for t in db.list_taxonomies(conn)
        if t.version.startswith("v") and t.version[1:].isdigit()
    ]
    return f"v{max(numbers, default=0) + 1}"


def save_proposal(
    conn: sqlite3.Connection,
    proposal: TaxonomyProposal,
    themes_path: Path,
    *,
    now: datetime | None = None,
) -> Taxonomy:
    taxonomy = Taxonomy(
        version=next_version(conn),
        created_at=now or utc_now(),
        themes=[Theme(**theme.model_dump()) for theme in proposal.themes],
        approved=False,
    )
    db.save_taxonomy(conn, taxonomy)
    write_themes_file(conn, themes_path)
    return taxonomy


def approve(conn: sqlite3.Connection, version: str, themes_path: Path) -> Taxonomy:
    taxonomy = db.get_taxonomy(conn, version)
    if taxonomy is None:
        known = ", ".join(t.version for t in db.list_taxonomies(conn)) or "none"
        raise TaxonomyError(f"no taxonomy version {version!r} (known: {known})")
    approved = taxonomy.model_copy(update={"approved": True})
    db.save_taxonomy(conn, approved)
    write_themes_file(conn, themes_path)
    return approved


def require_active(conn: sqlite3.Connection) -> Taxonomy:
    taxonomy = db.active_taxonomy(conn)
    if taxonomy is None:
        raise TaxonomyError(
            "No approved taxonomy. Run `pulse themes discover`, review the proposal, then "
            "`pulse themes approve <version>`."
        )
    return taxonomy


def write_themes_file(conn: sqlite3.Connection, path: Path) -> None:
    """Mirror every taxonomy version to YAML (deterministic, newest last)."""
    data: dict[str, Any] = {
        "versions": [
            {
                "version": t.version,
                "created_at": t.created_at.isoformat(timespec="seconds"),
                "approved": t.approved,
                "themes": [theme.model_dump() for theme in t.themes],
            }
            for t in db.list_taxonomies(conn)
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Theme taxonomies (generated by `pulse themes`; the database is the source of truth)\n"
    )
    path.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
