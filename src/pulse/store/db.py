"""SQLite review store (T1.4, arch §4.3).

Timestamps are stored as UTC ISO-8601 strings at one-second precision
(`2026-10-05T03:12:40+00:00`), so string comparison in SQL matches time order.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pulse.models import Classification, Review, RunRecord, Taxonomy, Theme

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id     TEXT PRIMARY KEY,
    store         TEXT NOT NULL,
    rating        INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    text          TEXT NOT NULL,
    review_date   TEXT NOT NULL,
    app_version   TEXT,
    thumbs_up     INTEGER NOT NULL DEFAULT 0,
    script        TEXT NOT NULL CHECK (script IN ('latin', 'devanagari', 'other')),
    word_count    INTEGER NOT NULL,
    has_redaction INTEGER NOT NULL,
    ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_date ON reviews (review_date);

CREATE TABLE IF NOT EXISTS taxonomies (
    version     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    themes_json TEXT NOT NULL,
    approved    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS classifications (
    review_id        TEXT NOT NULL REFERENCES reviews (review_id) ON DELETE CASCADE,
    taxonomy_version TEXT NOT NULL REFERENCES taxonomies (version),
    theme_id         TEXT,
    sentiment        TEXT,
    mentions_person  INTEGER,
    status           TEXT NOT NULL CHECK (status IN ('ok', 'unclassified')),
    model            TEXT,
    prompt_version   TEXT,
    classified_at    TEXT NOT NULL,
    PRIMARY KEY (review_id, taxonomy_version)
);
CREATE INDEX IF NOT EXISTS idx_classifications_theme
    ON classifications (taxonomy_version, theme_id);

CREATE TABLE IF NOT EXISTS runs (
    week             TEXT PRIMARY KEY,
    window_start     TEXT NOT NULL,
    window_end       TEXT NOT NULL,
    taxonomy_version TEXT,
    status           TEXT NOT NULL,
    stats_json       TEXT,
    pulse_json       TEXT,
    doc_id           TEXT,
    doc_url          TEXT,
    doc_section_start_index INTEGER,
    doc_section_end_index   INTEGER,
    doc_revision_id  TEXT,
    draft_id         TEXT,
    draft_message_id TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""

# Version -> statements that upgrade the previous version to it.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (  # delivery state for the append-only pulse Doc and the Gmail draft (T7.2, T7.3)
        "ALTER TABLE runs ADD COLUMN doc_section_start_index INTEGER",
        "ALTER TABLE runs ADD COLUMN doc_section_end_index INTEGER",
        "ALTER TABLE runs ADD COLUMN doc_revision_id TEXT",
        "ALTER TABLE runs ADD COLUMN draft_message_id TEXT",
    ),
}

_RUN_COLUMNS = (
    "week",
    "window_start",
    "window_end",
    "taxonomy_version",
    "status",
    "stats_json",
    "pulse_json",
    "doc_id",
    "doc_url",
    "doc_section_start_index",
    "doc_section_end_index",
    "doc_revision_id",
    "draft_id",
    "draft_message_id",
    "created_at",
    "updated_at",
)

_REVIEW_COLUMNS = (
    "review_id, store, rating, text, review_date, app_version, thumbs_up, "
    "script, word_count, has_redaction, ingested_at"
)


class SchemaVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpsertResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0

    def __add__(self, other: UpsertResult) -> UpsertResult:
        return UpsertResult(
            self.new + other.new,
            self.updated + other.updated,
            self.unchanged + other.unchanged,
        )


def ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def from_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and if needed create) the store, applying the schema."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
            ).fetchone()
            version = None
            if row is not None:
                found = conn.execute("SELECT version FROM schema_version").fetchone()
                version = None if found is None else int(found["version"])
            if version is not None and version > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"{path} has schema version {version}, this code expects {SCHEMA_VERSION}"
                )
            if version is not None and version < SCHEMA_VERSION:
                _migrate(conn, version)
            conn.executescript(_SCHEMA)
            if version is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    except BaseException:
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    for target in range(version + 1, SCHEMA_VERSION + 1):
        if target not in _MIGRATIONS:
            raise SchemaVersionError(f"no migration from schema version {target - 1} to {target}")
        for statement in _MIGRATIONS[target]:
            conn.execute(statement)
        conn.execute("UPDATE schema_version SET version = ?", (target,))


# --- reviews -------------------------------------------------------------------------------


def upsert_reviews(conn: sqlite3.Connection, reviews: Iterable[Review]) -> UpsertResult:
    """Insert new reviews; update ones whose text or rating changed (the author edited them).

    An edited review loses its classifications so it is classified again.
    """
    new = updated = unchanged = 0
    with conn:
        for review in reviews:
            existing = conn.execute(
                "SELECT text, rating, review_date FROM reviews WHERE review_id = ?",
                (review.review_id,),
            ).fetchone()
            values = _review_values(review)
            if existing is None:
                conn.execute(
                    f"INSERT INTO reviews ({_REVIEW_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                new += 1
            elif existing["text"] != review.text or existing["rating"] != review.rating:
                conn.execute(
                    "UPDATE reviews SET store=?, rating=?, text=?, review_date=?, app_version=?, "
                    "thumbs_up=?, script=?, word_count=?, has_redaction=?, ingested_at=? "
                    "WHERE review_id=?",
                    (*values[1:], review.review_id),
                )
                conn.execute("DELETE FROM classifications WHERE review_id = ?", (review.review_id,))
                updated += 1
            else:
                conn.execute(
                    "UPDATE reviews SET thumbs_up=?, app_version=? WHERE review_id=?",
                    (review.thumbs_up, review.app_version, review.review_id),
                )
                unchanged += 1
    return UpsertResult(new, updated, unchanged)


def reviews_in_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[Review]:
    rows = conn.execute(
        f"SELECT {_REVIEW_COLUMNS} FROM reviews "
        "WHERE review_date >= ? AND review_date <= ? ORDER BY review_date, review_id",
        (ts(start), ts(end)),
    ).fetchall()
    return [_review_from_row(row) for row in rows]


def unclassified_reviews(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    taxonomy_version: str,
    min_words: int,
) -> list[Review]:
    """Reviews in the window with no classification row (ok or unclassified) for this version."""
    rows = conn.execute(
        f"SELECT {_REVIEW_COLUMNS} FROM reviews r "
        "WHERE r.review_date >= ? AND r.review_date <= ? AND r.word_count >= ? "
        "AND NOT EXISTS (SELECT 1 FROM classifications c "
        "  WHERE c.review_id = r.review_id AND c.taxonomy_version = ?) "
        "ORDER BY r.review_date, r.review_id",
        (ts(start), ts(end), min_words, taxonomy_version),
    ).fetchall()
    return [_review_from_row(row) for row in rows]


def count_reviews(conn: sqlite3.Connection, store: str | None = None) -> int:
    if store is None:
        row = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM reviews WHERE store = ?", (store,)).fetchone()
    return int(row[0])


def date_range(
    conn: sqlite3.Connection, store: str | None = None
) -> tuple[datetime, datetime] | None:
    """Oldest and newest stored review dates, optionally for one store."""
    where, args = ("WHERE store = ?", (store,)) if store else ("", ())
    row = conn.execute(
        f"SELECT MIN(review_date), MAX(review_date) FROM reviews {where}", args
    ).fetchone()
    if row[0] is None:
        return None
    return from_ts(row[0]), from_ts(row[1])


def iter_reviews(conn: sqlite3.Connection) -> Iterator[Review]:
    for row in conn.execute(f"SELECT {_REVIEW_COLUMNS} FROM reviews"):
        yield _review_from_row(row)


def delete_reviews(conn: sqlite3.Connection, review_ids: Iterable[str]) -> int:
    """Delete reviews and their classifications. Returns the number deleted."""
    ids = list(review_ids)
    with conn:
        conn.executemany("DELETE FROM classifications WHERE review_id = ?", ((i,) for i in ids))
        cursor = conn.executemany("DELETE FROM reviews WHERE review_id = ?", ((i,) for i in ids))
    return cursor.rowcount if ids else 0


def iter_review_texts(conn: sqlite3.Connection) -> Iterator[tuple[str, str]]:
    """(review_id, text) for every stored review, for privacy scans."""
    for row in conn.execute("SELECT review_id, text FROM reviews"):
        yield row["review_id"], row["text"]


# --- classifications -----------------------------------------------------------------------


def save_classifications(conn: sqlite3.Connection, items: Iterable[Classification]) -> int:
    count = 0
    with conn:
        for item in items:
            conn.execute(
                "INSERT OR REPLACE INTO classifications (review_id, taxonomy_version, theme_id, "
                "sentiment, mentions_person, status, model, prompt_version, classified_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    item.review_id,
                    item.taxonomy_version,
                    item.theme_id,
                    item.sentiment,
                    None if item.mentions_person is None else int(item.mentions_person),
                    item.status,
                    item.model,
                    item.prompt_version,
                    ts(item.classified_at),
                ),
            )
            count += 1
    return count


def classifications_for(conn: sqlite3.Connection, taxonomy_version: str) -> list[Classification]:
    rows = conn.execute(
        "SELECT * FROM classifications WHERE taxonomy_version = ? ORDER BY review_id",
        (taxonomy_version,),
    ).fetchall()
    return [
        Classification(
            review_id=row["review_id"],
            taxonomy_version=row["taxonomy_version"],
            status=row["status"],
            theme_id=row["theme_id"],
            sentiment=row["sentiment"],
            mentions_person=None
            if row["mentions_person"] is None
            else bool(row["mentions_person"]),
            model=row["model"],
            prompt_version=row["prompt_version"],
            classified_at=from_ts(row["classified_at"]),
        )
        for row in rows
    ]


# --- taxonomies ----------------------------------------------------------------------------


def save_taxonomy(conn: sqlite3.Connection, taxonomy: Taxonomy) -> None:
    """Save a taxonomy version. Saving one as approved makes it the only approved version."""
    themes_json = json.dumps([theme.model_dump() for theme in taxonomy.themes], ensure_ascii=False)
    with conn:
        if taxonomy.approved:
            conn.execute(
                "UPDATE taxonomies SET approved = 0 WHERE version != ?", (taxonomy.version,)
            )
        conn.execute(
            "INSERT OR REPLACE INTO taxonomies (version, created_at, themes_json, approved) "
            "VALUES (?,?,?,?)",
            (taxonomy.version, ts(taxonomy.created_at), themes_json, int(taxonomy.approved)),
        )


def get_taxonomy(conn: sqlite3.Connection, version: str) -> Taxonomy | None:
    row = conn.execute("SELECT * FROM taxonomies WHERE version = ?", (version,)).fetchone()
    return None if row is None else _taxonomy_from_row(row)


def list_taxonomies(conn: sqlite3.Connection) -> list[Taxonomy]:
    rows = conn.execute("SELECT * FROM taxonomies ORDER BY created_at, version").fetchall()
    return [_taxonomy_from_row(row) for row in rows]


def labels_for(conn: sqlite3.Connection, taxonomy_version: str) -> dict[str, Classification]:
    """Classifications for one taxonomy version, keyed by review_id."""
    return {item.review_id: item for item in classifications_for(conn, taxonomy_version)}


def review_texts(conn: sqlite3.Connection, review_ids: Iterable[str]) -> dict[str, str]:
    ids = list(review_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT review_id, text FROM reviews WHERE review_id IN ({marks})", ids)
    return {row["review_id"]: row["text"] for row in rows}


def active_taxonomy(conn: sqlite3.Connection) -> Taxonomy | None:
    row = conn.execute("SELECT * FROM taxonomies WHERE approved = 1").fetchone()
    return None if row is None else _taxonomy_from_row(row)


# --- runs ----------------------------------------------------------------------------------


def get_run(conn: sqlite3.Connection, week: str) -> RunRecord | None:
    row = conn.execute("SELECT * FROM runs WHERE week = ?", (week,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    for key in ("window_start", "window_end", "created_at", "updated_at"):
        data[key] = from_ts(data[key])
    return RunRecord.model_validate(data)


def upsert_run(conn: sqlite3.Connection, run: RunRecord) -> None:
    existing = get_run(conn, run.week)
    created_at = existing.created_at if existing else run.created_at
    values = run.model_dump()
    values.update(
        window_start=ts(run.window_start),
        window_end=ts(run.window_end),
        created_at=ts(created_at),
        updated_at=ts(run.updated_at),
    )
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO runs ({', '.join(_RUN_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_RUN_COLUMNS))})",
            tuple(values[column] for column in _RUN_COLUMNS),
        )


# --- row mapping ---------------------------------------------------------------------------


def _review_values(review: Review) -> tuple[object, ...]:
    return (
        review.review_id,
        review.store,
        review.rating,
        review.text,
        ts(review.review_date),
        review.app_version,
        review.thumbs_up,
        review.script,
        review.word_count,
        int(review.has_redaction),
        ts(review.ingested_at),
    )


def _review_from_row(row: sqlite3.Row) -> Review:
    return Review(
        review_id=row["review_id"],
        store=row["store"],
        rating=row["rating"],
        text=row["text"],
        review_date=from_ts(row["review_date"]),
        app_version=row["app_version"],
        thumbs_up=row["thumbs_up"],
        script=row["script"],
        word_count=row["word_count"],
        has_redaction=bool(row["has_redaction"]),
        ingested_at=from_ts(row["ingested_at"]),
    )


def _taxonomy_from_row(row: sqlite3.Row) -> Taxonomy:
    return Taxonomy(
        version=row["version"],
        created_at=from_ts(row["created_at"]),
        themes=[Theme.model_validate(item) for item in json.loads(row["themes_json"])],
        approved=bool(row["approved"]),
    )
