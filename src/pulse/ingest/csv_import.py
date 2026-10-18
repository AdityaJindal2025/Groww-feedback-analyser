"""CSV / JSON import adapter (T2.5): a fallback for manually exported public review datasets.

A column mapping (pipeline field -> column name) says which columns to read; every other
column is ignored. The adapter refuses to run if any mapped column looks like an identity
column, so a mapping can't pull reviewer names, emails or devices into the pipeline.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pulse.models import RawReview

log = logging.getLogger(__name__)

IDENTITY_COLUMN = re.compile(r"name|user|author|email|device|phone", re.IGNORECASE)
REQUIRED_FIELDS = ("text", "rating", "review_date")
OPTIONAL_FIELDS = ("store_review_id", "app_version", "thumbs_up")


class CsvImportError(ValueError):
    pass


class CsvSource:
    name = "csv"

    def __init__(
        self,
        path: Path,
        columns: Mapping[str, str],
        *,
        store: str = "csv",
        timezone: str = "Asia/Kolkata",
        date_format: str | None = None,
    ) -> None:
        unknown = set(columns) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
        if unknown:
            raise CsvImportError(f"unknown fields in column mapping: {', '.join(sorted(unknown))}")
        missing = [name for name in REQUIRED_FIELDS if name not in columns]
        if missing:
            raise CsvImportError(f"column mapping is missing required fields: {', '.join(missing)}")
        identity = sorted(col for col in columns.values() if IDENTITY_COLUMN.search(col))
        if identity:
            raise CsvImportError(
                "refusing to import: mapped columns look like reviewer identity data: "
                + ", ".join(identity)
            )
        self.path = path
        self.columns = dict(columns)
        self.store = store
        self.tz = ZoneInfo(timezone)
        self.date_format = date_format
        self.skipped = 0

    def fetch(self, since: datetime) -> Iterator[RawReview]:
        for line_no, row in self._rows():
            raw = self._to_raw(row)
            if raw is None:
                self.skipped += 1
                log.warning("csv row skipped", extra={"row": line_no})
                continue
            if raw.review_date >= since:
                yield raw

    def _rows(self) -> Iterator[tuple[int, Mapping[str, Any]]]:
        if not self.path.exists():
            raise CsvImportError(f"import file not found: {self.path}")
        suffix = self.path.suffix.lower()
        if suffix == ".csv":
            with self.path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                absent = [
                    col for col in self.columns.values() if col not in (reader.fieldnames or [])
                ]
                if absent:
                    raise CsvImportError(f"columns not in {self.path.name}: {', '.join(absent)}")
                for line_no, row in enumerate(reader, start=2):
                    yield line_no, row
        elif suffix == ".jsonl":
            with self.path.open(encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if line.strip():
                        yield line_no, json.loads(line)
        elif suffix == ".json":
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise CsvImportError(f"{self.path.name}: expected a JSON array of objects")
            yield from enumerate(data, start=1)
        else:
            raise CsvImportError(f"unsupported import format: {suffix} (use .csv, .json or .jsonl)")

    def _to_raw(self, row: Mapping[str, Any]) -> RawReview | None:
        def get(name: str) -> Any:
            column = self.columns.get(name)
            return row.get(column) if column else None

        text = get("text")
        try:
            rating = int(float(get("rating")))
            review_date = self._parse_date(str(get("review_date")))
        except (TypeError, ValueError):
            return None
        if text is None or not 1 <= rating <= 5:
            return None
        text = str(text)
        store_review_id = get("store_review_id")
        if store_review_id in (None, ""):
            # No ID column: derive a stable one from the review itself, for de-duplication.
            digest = hashlib.sha256(f"{review_date.isoformat()}|{text}".encode()).hexdigest()
            store_review_id = digest
        thumbs = get("thumbs_up")
        try:
            thumbs_up = int(float(thumbs)) if thumbs not in (None, "") else 0
        except (TypeError, ValueError):
            thumbs_up = 0
        version = get("app_version")
        return RawReview(
            store=self.store,
            store_review_id=str(store_review_id),
            rating=rating,
            text=text,
            review_date=review_date,
            app_version=str(version) if version not in (None, "") else None,
            thumbs_up=max(thumbs_up, 0),
        )

    def _parse_date(self, value: str) -> datetime:
        value = value.strip()
        if self.date_format:
            parsed = datetime.strptime(value, self.date_format)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=self.tz)
