"""Google Play adapter (T2.4): public, logged-out reviews via `google-play-scraper`.

Only allowlisted fields are copied into `RawReview`; `userName`, `userImage`, `replyContent`
and `repliedAt` are never read.

Two library behaviours shape this adapter (see Docs/spikes.md):
- `reviews()` swallows request errors and reports them as "no more pages". An empty page is
  therefore retried with backoff before it's accepted as the end.
- `at` is a naive datetime in the machine's local timezone (`datetime.fromtimestamp`). It is
  converted to UTC by interpreting it as local time.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from pulse.ingest.base import SourceError
from pulse.models import RawReview

log = logging.getLogger(__name__)

STORE = "play_store"

FetchPage = Callable[..., tuple[list[dict[str, Any]], Any]]


def _default_fetch_page(**kwargs: Any) -> tuple[list[dict[str, Any]], Any]:
    from google_play_scraper import Sort, reviews

    result: tuple[list[dict[str, Any]], Any] = reviews(sort=Sort.NEWEST, **kwargs)
    return result


def to_raw_review(item: Mapping[str, Any]) -> RawReview | None:
    """Map one scraper record onto the allowlist. Returns None if required fields are missing."""
    review_id = item.get("reviewId")
    score = item.get("score")
    at = item.get("at")
    if not review_id or score is None or not isinstance(at, datetime):
        return None
    version = item.get("reviewCreatedVersion") or item.get("appVersion")
    return RawReview(
        store=STORE,
        store_review_id=str(review_id),
        rating=int(score),
        text=item.get("content") or "",
        review_date=to_utc(at),
        app_version=str(version) if version else None,
        thumbs_up=int(item.get("thumbsUpCount") or 0),
    )


def to_utc(at: datetime) -> datetime:
    """Naive values are local time (the library uses `datetime.fromtimestamp`)."""
    return at.astimezone(UTC)


class PlayStoreSource:
    name = STORE

    def __init__(
        self,
        package_id: str,
        *,
        lang: str = "en",
        country: str = "in",
        page_size: int = 200,
        page_delay_seconds: float = 1.5,
        max_reviews: int = 30000,
        max_attempts: int = 3,
        fetch_page: FetchPage | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.package_id = package_id
        self.lang = lang
        self.country = country
        self.page_size = page_size
        self.page_delay_seconds = page_delay_seconds
        self.max_reviews = max_reviews
        self.max_attempts = max_attempts
        self._fetch_page = fetch_page or _default_fetch_page
        self._sleep = sleep
        self.pages = 0
        self.skipped = 0

    def fetch(self, since: datetime) -> Iterator[RawReview]:
        """Yield reviews dated at or after `since`, newest first.

        Raises SourceError after yielding if it stopped before reaching `since` (cap hit, or the
        store stopped returning pages), so the caller can flag a partial ingest.
        """
        token: Any = None
        yielded = 0
        while True:
            items, next_token = self._page(token)
            if not items:
                raise SourceError("Play Store returned no more pages before the window start")
            self.pages += 1
            oldest: datetime | None = None
            for item in items:
                raw = to_raw_review(item)
                if raw is None:
                    self.skipped += 1
                    continue
                oldest = raw.review_date if oldest is None else min(oldest, raw.review_date)
                if raw.review_date < since:
                    continue
                yield raw
                yielded += 1
                if yielded >= self.max_reviews:
                    if oldest >= since:
                        raise SourceError(
                            f"max_reviews cap ({self.max_reviews}) reached before the window start"
                        )
                    return
            log.info("play_store page", extra={"page": self.pages, "yielded": yielded})
            if oldest is not None and oldest < since:
                return
            if next_token is None or getattr(next_token, "token", None) is None:
                raise SourceError("Play Store has no further pages before the window start")
            token = next_token
            self._sleep(self.page_delay_seconds)

    def _page(self, token: Any) -> tuple[list[dict[str, Any]], Any]:
        """One page, retried with exponential backoff when it errors or comes back empty."""
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                items, next_token = self._fetch_page(
                    app_id=self.package_id,
                    lang=self.lang,
                    country=self.country,
                    count=self.page_size,
                    continuation_token=token,
                )
            except Exception as exc:  # the library raises bare exceptions on HTTP/parse errors
                last_error = exc
                items, next_token = [], None
            if items:
                return items, next_token
            if attempt < self.max_attempts:
                log.warning(
                    "play_store empty page, retrying",
                    extra={"attempt": attempt, "page": self.pages + 1},
                )
                self._sleep(2.0**attempt)
        if last_error is not None:
            raise SourceError(
                f"Play Store request failed after {self.max_attempts} attempts: "
                f"{type(last_error).__name__}"
            ) from last_error
        return [], None
