from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pulse.models import Classification, Review, RunRecord, Taxonomy, Theme
from pulse.store import db
from pulse.timeutil import window_for

T0 = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "data" / "pulse.db")
    yield connection
    connection.close()


def make_review(
    review_id: str,
    *,
    when: datetime = T0,
    text: str = "App is slow today",
    rating: int = 2,
    words: int | None = None,
) -> Review:
    return Review(
        review_id=review_id,
        store="play_store",
        rating=rating,
        text=text,
        review_date=when,
        app_version="1.0",
        thumbs_up=0,
        script="latin",
        word_count=len(text.split()) if words is None else words,
        has_redaction=False,
        ingested_at=T0,
    )


def make_taxonomy(version: str, approved: bool = False) -> Taxonomy:
    return Taxonomy(
        version=version,
        created_at=T0,
        approved=approved,
        themes=[Theme(id="speed", name="Speed", definition="App speed.")],
    )


def test_reviews_table_has_only_allowlisted_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reviews)")}
    assert columns == {
        "review_id",
        "store",
        "rating",
        "text",
        "review_date",
        "app_version",
        "thumbs_up",
        "script",
        "word_count",
        "has_redaction",
        "ingested_at",
    }


def test_upsert_twice_keeps_row_count(conn: sqlite3.Connection) -> None:
    reviews = [make_review(f"r{i}") for i in range(5)]
    first = db.upsert_reviews(conn, reviews)
    second = db.upsert_reviews(conn, reviews)
    assert (first.new, first.unchanged) == (5, 0)
    assert (second.new, second.updated, second.unchanged) == (0, 0, 5)
    assert db.count_reviews(conn) == 5


def test_edited_review_is_updated_and_loses_classification(conn: sqlite3.Connection) -> None:
    db.upsert_reviews(conn, [make_review("r1")])
    db.save_taxonomy(conn, make_taxonomy("v1", approved=True))
    db.save_classifications(
        conn,
        [
            Classification(
                review_id="r1",
                taxonomy_version="v1",
                status="ok",
                theme_id="speed",
                sentiment="negative",
                mentions_person=False,
                classified_at=T0,
            )
        ],
    )

    result = db.upsert_reviews(conn, [make_review("r1", text="Fixed now, works well", rating=5)])

    assert result.updated == 1
    assert db.count_reviews(conn) == 1
    assert db.classifications_for(conn, "v1") == []
    stored = db.reviews_in_window(conn, T0 - timedelta(days=1), T0 + timedelta(days=1))
    assert stored[0].rating == 5


def test_reviews_in_window_bounds_are_inclusive(conn: sqlite3.Connection) -> None:
    start, end = window_for("2026-W41", 8)
    db.upsert_reviews(
        conn,
        [
            make_review("before", when=start - timedelta(seconds=1)),
            make_review("first", when=start),
            make_review("last", when=end),
            make_review("after", when=end + timedelta(seconds=1)),
        ],
    )
    ids = [r.review_id for r in db.reviews_in_window(conn, start, end)]
    assert ids == ["first", "last"]


def test_timestamps_round_trip_as_aware_utc(conn: sqlite3.Connection) -> None:
    db.upsert_reviews(conn, [make_review("r1")])
    (review,) = db.reviews_in_window(conn, T0, T0)
    assert review.review_date == T0
    assert review.review_date.utcoffset() == timedelta(0)


def test_unclassified_reviews(conn: sqlite3.Connection) -> None:
    db.upsert_reviews(
        conn,
        [
            make_review("done"),
            make_review("todo"),
            make_review("short", text="good", words=1),
            make_review("failed"),
        ],
    )
    db.save_taxonomy(conn, make_taxonomy("v1", approved=True))
    db.save_classifications(
        conn,
        [
            Classification(
                review_id="done",
                taxonomy_version="v1",
                status="ok",
                theme_id="speed",
                sentiment="negative",
                mentions_person=False,
                classified_at=T0,
            ),
            Classification(
                review_id="failed", taxonomy_version="v1", status="unclassified", classified_at=T0
            ),
        ],
    )
    start, end = T0 - timedelta(days=1), T0 + timedelta(days=1)

    todo = db.unclassified_reviews(conn, start, end, "v1", min_words=4)

    assert [r.review_id for r in todo] == ["todo"]
    db.save_taxonomy(conn, make_taxonomy("v2"))
    assert len(db.unclassified_reviews(conn, start, end, "v2", min_words=4)) == 3


def test_only_one_approved_taxonomy(conn: sqlite3.Connection) -> None:
    db.save_taxonomy(conn, make_taxonomy("v1", approved=True))
    assert db.active_taxonomy(conn) is not None
    db.save_taxonomy(conn, make_taxonomy("v2", approved=True))
    active = db.active_taxonomy(conn)
    assert active is not None and active.version == "v2"
    v1 = db.get_taxonomy(conn, "v1")
    assert v1 is not None and not v1.approved


def test_no_active_taxonomy(conn: sqlite3.Connection) -> None:
    db.save_taxonomy(conn, make_taxonomy("v1"))
    assert db.active_taxonomy(conn) is None


def test_runs_round_trip_and_keep_created_at(conn: sqlite3.Connection) -> None:
    start, end = window_for("2026-W41", 12)
    run = RunRecord(
        week="2026-W41",
        window_start=start,
        window_end=end,
        status="ingested",
        created_at=T0,
        updated_at=T0,
    )
    db.upsert_run(conn, run)
    later = run.model_copy(
        update={
            "status": "delivered",
            "doc_id": "doc-1",
            "created_at": T0 + timedelta(days=1),
            "updated_at": T0 + timedelta(days=1),
        }
    )
    db.upsert_run(conn, later)
    stored = db.get_run(conn, "2026-W41")
    assert stored is not None
    assert stored.status == "delivered" and stored.doc_id == "doc-1"
    assert stored.created_at == T0
    assert db.get_run(conn, "2026-W40") is None


def test_schema_version_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "pulse.db"
    db.connect(path).close()
    raw = sqlite3.connect(path)
    with raw:
        raw.execute("UPDATE schema_version SET version = 99")
    raw.close()
    with pytest.raises(db.SchemaVersionError):
        db.connect(path)


def test_date_range(conn: sqlite3.Connection) -> None:
    assert db.date_range(conn) is None
    db.upsert_reviews(conn, [make_review("a", when=T0), make_review("b", when=T0 + timedelta(1))])
    assert db.date_range(conn) == (T0, T0 + timedelta(days=1))


def test_migrates_schema_1_to_2(tmp_path: Path) -> None:
    """A version-1 store gains the delivery columns on `runs` and keeps its rows."""
    path = tmp_path / "pulse.db"
    raw = sqlite3.connect(path)
    with raw:
        raw.executescript(
            "CREATE TABLE schema_version (version INTEGER NOT NULL);"
            "INSERT INTO schema_version VALUES (1);"
            "CREATE TABLE runs (week TEXT PRIMARY KEY, window_start TEXT NOT NULL, "
            "window_end TEXT NOT NULL, taxonomy_version TEXT, status TEXT NOT NULL, "
            "stats_json TEXT, pulse_json TEXT, doc_id TEXT, doc_url TEXT, draft_id TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO runs (week, window_start, window_end, status, created_at, updated_at) "
            "VALUES ('2026-W40', '2026-07-13T00:00:00+00:00', '2026-10-04T00:00:00+00:00', "
            "'composed', '2026-10-05T00:00:00+00:00', '2026-10-05T00:00:00+00:00');"
        )
    raw.close()
    conn = db.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
    run = db.get_run(conn, "2026-W40")
    assert run is not None and run.status == "composed" and run.doc_section_end_index is None
    db.upsert_run(conn, run.model_copy(update={"doc_section_end_index": 42, "draft_id": "d"}))
    stored = db.get_run(conn, "2026-W40")
    assert stored is not None and stored.doc_section_end_index == 42
    conn.close()
