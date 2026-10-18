"""Review source interface and review-ID hashing (T2.3)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from pulse.models import RawReview


class SourceError(RuntimeError):
    """A source failed or stopped early. Reviews already yielded are still usable."""


class ReviewSource(Protocol):
    name: str

    def fetch(self, since: datetime) -> Iterator[RawReview]:
        """Yield reviews dated at or after `since`, newest first where the source allows."""
        ...


def hash_review_id(store: str, store_review_id: str, *, salt: str) -> str:
    """`sha256(salt + store + store_review_id)`. The store's own ID is never persisted."""
    return hashlib.sha256((salt + store + store_review_id).encode("utf-8")).hexdigest()
