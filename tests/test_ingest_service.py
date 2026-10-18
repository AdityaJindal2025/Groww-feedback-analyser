"""Ingest service and `pulse ingest` (T2.6). All review content is synthetic."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pulse.cli import app
from pulse.config import ReviewFilters
from pulse.ingest.base import SourceError
from pulse.ingest.service import ingest, prepare_review, rejection_reason, scan_store
from pulse.manifest import load_manifest
from pulse.models import RawReview
from pulse.privacy.scrubber import scan
from pulse.store import db
from pulse.timeutil import IST, window_for
from tests.conftest import SALT, SETTINGS_PATH

WEEK = "2026-W41"
START, END = window_for(WEEK, 12)

# The first three pass the default filters; the last three are dropped (short, emoji, Hinglish).
TEXTS = [
    "Withdrawal pending for 3 days, please call me on 9876543210",
    "KYC rejected twice even though my PAN ABCDE1234F is valid",
    "Mail support@example.com never replies to my ticket ORD12345678 at all",
    "good app",
    "Great app for SIP investing, very smooth experience overall 👍",
    "app bahut slow hai aur paisa withdraw nahi ho raha yaar",
]
DEFAULT_FILTERS = ReviewFilters()
NO_FILTERS = ReviewFilters(min_words=0, drop_emoji=False, english_only=False)


def raw(i: int, when: datetime, text: str | None = None) -> RawReview:
    return RawReview(
        store="play_store",
        store_review_id=f"gp:{i}",
        rating=(i % 5) + 1,
        text=text if text is not None else TEXTS[i % len(TEXTS)],
        review_date=when,
    )


class ListSource:
    name = "fake"

    def __init__(self, reviews: list[RawReview], error: str | None = None) -> None:
        self.reviews = reviews
        self.error = error

    def fetch(self, since: datetime) -> Iterator[RawReview]:
        for review in self.reviews:
            if review.review_date >= since:
                yield review
        if self.error:
            raise SourceError(self.error)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "pulse.db")
    yield connection
    connection.close()


def sample(n: int = 12) -> list[RawReview]:
    return [raw(i, END - timedelta(days=i)) for i in range(n)]


def test_prepare_review_scrubs_and_normalises(now: datetime) -> None:
    review, counts = prepare_review(raw(0, END), salt=SALT, now=now)
    assert review.text == "Withdrawal pending for 3 days, please call me on [PHONE]"
    assert counts == {"PHONE": 1}
    assert review.has_redaction
    assert review.script == "latin"
    assert review.word_count == 10
    assert review.review_id != "gp:0" and len(review.review_id) == 64
    assert review.ingested_at == now


def test_second_ingest_reports_no_new(conn: sqlite3.Connection) -> None:
    first = ingest(conn, [ListSource(sample())], START, salt=SALT, filters=NO_FILTERS)
    second = ingest(conn, [ListSource(sample())], START, salt=SALT, filters=NO_FILTERS)
    assert (first.fetched, first.new) == (12, 12)
    assert (second.new, second.updated, second.duplicates) == (0, 0, 12)
    assert db.count_reviews(conn) == 12


def test_store_is_clean_after_ingest(conn: sqlite3.Connection) -> None:
    report = ingest(conn, [ListSource(sample())], START, salt=SALT, filters=NO_FILTERS)
    assert scan_store(conn) == 0
    assert all(scan(text) == [] for _, text in db.iter_review_texts(conn))
    assert report.redactions == {"PHONE": 2, "PAN": 2, "EMAIL": 2, "ID": 2}


def test_store_ids_are_never_persisted(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ingest(conn, [ListSource(sample())], START, salt=SALT, filters=NO_FILTERS)
    conn.commit()
    assert b"gp:" not in (tmp_path / "pulse.db").read_bytes()


def test_reviews_before_window_are_ignored(conn: sqlite3.Connection) -> None:
    reviews = [raw(1, START - timedelta(seconds=1)), raw(2, START)]
    report = ingest(conn, [ListSource(reviews)], START, salt=SALT, filters=NO_FILTERS)
    assert report.fetched == 1


def test_partial_source_keeps_what_it_fetched(conn: sqlite3.Connection) -> None:
    report = ingest(
        conn, [ListSource(sample(3), error="network down")], START, salt=SALT, filters=NO_FILTERS
    )
    assert report.partial == {"fake": "network down"}
    assert db.count_reviews(conn) == 3


def test_report_counts_for_manifest(conn: sqlite3.Connection) -> None:
    counts = ingest(conn, [ListSource(sample())], START, salt=SALT, filters=NO_FILTERS).counts()
    assert counts["fetched"] == 12
    assert counts["fetched.fake"] == 12
    assert counts["redactions.PHONE"] == 2


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (TEXTS[0], None),
        ("Eight words exactly here, one two three four", "short"),
        ("Nine words is enough to keep this one here", None),
        (TEXTS[3], "short"),
        (TEXTS[4], "emoji"),
        ("Five stars ★ for the easy and quick mutual fund flow", "emoji"),
        (TEXTS[5], "language"),
        ("बहुत अच्छा ऐप है लेकिन withdrawal में बहुत समय लगता है ठीक", "language"),
        ("Brokerage is too high, please reduce it for small retail investors", None),
    ],
)
def test_rejection_reason(text: str, reason: str | None, now: datetime) -> None:
    review, _ = prepare_review(raw(0, END, text), salt=SALT, now=now)
    assert rejection_reason(review, DEFAULT_FILTERS) == reason


def test_filters_drop_reviews_before_storage(conn: sqlite3.Connection) -> None:
    report = ingest(conn, [ListSource(sample())], START, salt=SALT, filters=DEFAULT_FILTERS)
    assert report.fetched == 12
    assert report.filtered == {"short": 2, "emoji": 2, "language": 2}
    assert db.count_reviews(conn) == 6
    assert report.counts()["kept"] == 6
    assert report.counts()["filtered.emoji"] == 2


def test_stored_reviews_failing_filters_are_pruned(conn: sqlite3.Connection) -> None:
    ingest(conn, [ListSource(sample())], START, salt=SALT, filters=NO_FILTERS)
    assert db.count_reviews(conn) == 12

    report = ingest(conn, [], START, salt=SALT, filters=DEFAULT_FILTERS)

    assert report.pruned == 6
    assert db.count_reviews(conn) == 6


# --- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli_config(tmp_path: Path) -> Path:
    """Settings with the Play Store off and a synthetic CSV export as the only source."""
    export = tmp_path / "export.jsonl"
    rows = [
        {
            "id": f"r{i}",
            "text": TEXTS[i % len(TEXTS)],
            "rating": (i % 5) + 1,
            "date": (END - timedelta(days=i * 3)).isoformat(),
        }
        for i in range(30)
    ]
    export.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows))
    data = yaml.safe_load(SETTINGS_PATH.read_text())
    data["sources"]["play_store"]["enabled"] = False
    data["sources"]["csv"].update(
        {
            "enabled": True,
            "path": str(export),
            "columns": {
                "text": "text",
                "rating": "rating",
                "review_date": "date",
                "store_review_id": "id",
            },
        }
    )
    data["paths"] = {"db": str(tmp_path / "data" / "pulse.db"), "runs": str(tmp_path / "runs")}
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return path


def test_cli_ingest_twice(cli_config: Path, tmp_path: Path, salt: str) -> None:
    runner = CliRunner()
    args = ["ingest", "--week", WEEK, "--config", str(cli_config)]

    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    # 30 rows, 2 older than the 12-week window; of the 28, 13 fail the filters.
    assert "Fetched 28; dropped 13 (8 words or fewer 5, emoji 4, not English 4)" in first.output
    assert "15 new" in first.output
    assert "Privacy scan: clean" in first.output

    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    assert "0 new" in second.output

    manifest = load_manifest(tmp_path / "runs", WEEK)
    assert manifest is not None
    stage = manifest.stages["ingest"]
    assert stage.status == "done"
    assert stage.counts["new"] == 0
    assert stage.counts["duplicates"] == 15
    assert stage.counts["filtered.language"] == 4
    assert stage.counts["privacy_hits"] == 0
    assert stage.counts["in_window"] == 15
    for text in TEXTS:
        assert text not in (tmp_path / "runs" / WEEK / "manifest.json").read_text()


def test_cli_ingest_needs_salt(cli_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_ID_SALT", raising=False)
    monkeypatch.chdir(cli_config.parent)  # no .env here
    result = CliRunner().invoke(app, ["ingest", "--config", str(cli_config)])
    assert result.exit_code == 4
    assert "PULSE_ID_SALT" in result.output


def test_cli_rejects_bad_week(cli_config: Path, salt: str) -> None:
    result = CliRunner().invoke(app, ["ingest", "--week", "2026-41", "--config", str(cli_config)])
    assert result.exit_code == 4


def test_cli_window_dates(cli_config: Path, salt: str) -> None:
    result = CliRunner().invoke(
        app, ["ingest", "--week", WEEK, "--weeks", "8", "--config", str(cli_config)]
    )
    assert result.exit_code == 0, result.output
    assert "17 Aug 2026 – 11 Oct 2026" in result.output
    assert datetime(2026, 8, 17, tzinfo=IST) == window_for(WEEK, 8)[0]
