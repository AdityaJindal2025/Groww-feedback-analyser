"""Google Play adapter (T2.4). Fixture records are synthetic; the names in them are fake and
exist to prove identity fields are dropped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pulse.ingest.base import SourceError
from pulse.ingest.play_store import PlayStoreSource, to_raw_review, to_utc
from pulse.models import RawReview

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
FAKE_NAME = "Fakey McTestface"
IDENTITY_VALUES = (FAKE_NAME, "https://example.com/avatar.png", "Thanks for the feedback!")


def record(i: int, at: datetime) -> dict[str, Any]:
    return {
        "reviewId": f"gp:fake-{i}",
        "userName": FAKE_NAME,
        "userImage": "https://example.com/avatar.png",
        "content": f"Synthetic review number {i} about slow withdrawals",
        "score": (i % 5) + 1,
        "thumbsUpCount": i,
        "reviewCreatedVersion": "12.3.0" if i % 2 else None,
        "at": at.replace(tzinfo=None),  # the library returns naive local time
        "replyContent": "Thanks for the feedback!",
        "repliedAt": at.replace(tzinfo=None),
        "appVersion": "12.3.0" if i % 2 else None,
    }


class Token:
    def __init__(self, token: str | None) -> None:
        self.token = token


class FakePlay:
    """Serves pages of `page_size` reviews, one per hour going back from NOW."""

    def __init__(self, total: int, *, fail_first: int = 0, empty_first: int = 0) -> None:
        self.reviews = [record(i, to_local(NOW - timedelta(hours=i))) for i in range(total)]
        self.calls: list[dict[str, Any]] = []
        self.fail_first = fail_first
        self.empty_first = empty_first

    def __call__(self, **kwargs: Any) -> tuple[list[dict[str, Any]], Token]:
        self.calls.append(kwargs)
        if self.fail_first:
            self.fail_first -= 1
            raise ConnectionError("synthetic network error")
        if self.empty_first:
            self.empty_first -= 1
            return [], Token(None)
        token = kwargs["continuation_token"]
        offset = int(token.token) if token else 0
        size = kwargs["count"]
        page = self.reviews[offset : offset + size]
        nxt = offset + size
        return page, Token(str(nxt) if nxt < len(self.reviews) else None)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone()  # aware, in the machine's local zone


def source(fake: FakePlay, **kwargs: Any) -> PlayStoreSource:
    options: dict[str, Any] = {"page_size": 10, "page_delay_seconds": 0, "sleep": lambda _: None}
    options.update(kwargs)
    return PlayStoreSource("com.example.app", fetch_page=fake, **options)


def test_identity_fields_are_dropped() -> None:
    raw = to_raw_review(record(1, to_local(NOW)))
    assert raw is not None
    dumped = raw.model_dump_json()
    for value in IDENTITY_VALUES:
        assert value not in dumped
    assert set(RawReview.model_fields) == {
        "store",
        "store_review_id",
        "rating",
        "text",
        "review_date",
        "app_version",
        "thumbs_up",
    }


def test_field_mapping() -> None:
    raw = to_raw_review(record(3, to_local(NOW)))
    assert raw is not None
    assert raw.store == "play_store"
    assert raw.store_review_id == "gp:fake-3"
    assert raw.rating == 4
    assert raw.thumbs_up == 3
    assert raw.app_version == "12.3.0"
    assert raw.review_date == NOW
    assert raw.review_date.tzinfo is not None


def test_naive_at_is_local_time() -> None:
    local = NOW.astimezone()
    assert to_utc(local.replace(tzinfo=None)) == NOW


def test_missing_required_fields_are_skipped() -> None:
    item = record(1, to_local(NOW))
    del item["score"]
    assert to_raw_review(item) is None


def test_stops_at_window_start() -> None:
    fake = FakePlay(100)
    since = NOW - timedelta(hours=24, minutes=30)  # reviews 0..24 are in range
    reviews = list(source(fake).fetch(since))
    assert len(reviews) == 25
    assert all(r.review_date >= since for r in reviews)
    assert len(fake.calls) == 3  # pages 0-9, 10-19, 20-29 (the last crosses `since`)


def test_follows_continuation_tokens() -> None:
    fake = FakePlay(100)
    list(source(fake).fetch(NOW - timedelta(hours=35)))
    assert fake.calls[0]["continuation_token"] is None
    assert fake.calls[1]["continuation_token"].token == "10"
    assert fake.calls[0]["app_id"] == "com.example.app"
    assert fake.calls[0]["lang"] == "en" and fake.calls[0]["country"] == "in"


def test_end_of_pages_before_window_start_is_partial() -> None:
    fake = FakePlay(15)
    src = source(fake)
    got: list[RawReview] = []
    with pytest.raises(SourceError, match="no further pages"):
        for review in src.fetch(NOW - timedelta(days=30)):
            got.append(review)
    assert len(got) == 15


def test_max_reviews_cap() -> None:
    fake = FakePlay(100)
    got: list[RawReview] = []
    with pytest.raises(SourceError, match="max_reviews"):
        for review in source(fake, max_reviews=12).fetch(NOW - timedelta(days=30)):
            got.append(review)
    assert len(got) == 12


def test_retries_errors_with_backoff() -> None:
    fake = FakePlay(5, fail_first=2)
    sleeps: list[float] = []
    reviews = list(source(fake, sleep=sleeps.append).fetch(NOW - timedelta(hours=2, minutes=30)))
    assert len(reviews) == 3
    assert sleeps[:2] == [2.0, 4.0]


def test_retries_empty_pages() -> None:
    fake = FakePlay(5, empty_first=1)
    reviews = list(source(fake).fetch(NOW - timedelta(hours=2, minutes=30)))
    assert len(reviews) == 3


def test_gives_up_after_three_failures() -> None:
    fake = FakePlay(5, fail_first=3)
    with pytest.raises(SourceError, match="failed after 3 attempts: ConnectionError"):
        list(source(fake).fetch(NOW - timedelta(days=1)))


def test_waits_between_pages() -> None:
    fake = FakePlay(40)
    sleeps: list[float] = []
    list(source(fake, page_delay_seconds=1.5, sleep=sleeps.append).fetch(NOW - timedelta(hours=25)))
    assert sleeps == [1.5, 1.5]


@pytest.mark.live
def test_live_page_has_no_identity_fields() -> None:
    src = PlayStoreSource(
        "com.nextbillion.groww", page_size=20, max_reviews=20, page_delay_seconds=0
    )
    reviews: list[RawReview] = []
    with pytest.raises(SourceError):  # the cap is hit long before the window start
        for review in src.fetch(datetime.now(UTC) - timedelta(days=60)):
            reviews.append(review)
    assert len(reviews) == 20
    for review in reviews:
        assert set(review.model_dump()) == set(RawReview.model_fields)
        assert review.review_date.tzinfo is not None
        assert 1 <= review.rating <= 5
