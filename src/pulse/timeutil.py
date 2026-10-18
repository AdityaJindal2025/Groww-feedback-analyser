"""ISO-week and window maths (T1.3), all in Asia/Kolkata.

A week runs Monday 00:00:00 to Sunday 23:59:59 local time. Timestamps are compared at
one-second precision throughout (the store truncates to seconds), so the inclusive end is exact.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_WEEK = re.compile(r"(\d{4})-W(\d{2})")


def parse_iso_week(week: str, tz: ZoneInfo = IST) -> tuple[datetime, datetime]:
    """`"2026-W41"` -> (Mon 2026-10-05 00:00:00, Sun 2026-10-11 23:59:59) in `tz`."""
    match = _WEEK.fullmatch(week.strip())
    if not match:
        raise ValueError(f"expected an ISO week like 2026-W41, got {week!r}")
    year, number = int(match.group(1)), int(match.group(2))
    try:
        monday = date.fromisocalendar(year, number, 1)
    except ValueError as exc:
        raise ValueError(f"{week} is not a valid ISO week") from exc
    start = datetime.combine(monday, time.min, tz)
    end = datetime.combine(monday + timedelta(days=6), time(23, 59, 59), tz)
    return start, end


def window_for(week: str, weeks: int, tz: ZoneInfo = IST) -> tuple[datetime, datetime]:
    """The `weeks`-week window that ends with `week` (inclusive)."""
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    start, end = parse_iso_week(week, tz)
    return start - timedelta(weeks=weeks - 1), end


def week_of(dt: datetime, tz: ZoneInfo = IST) -> str:
    if dt.tzinfo is None:
        raise ValueError("week_of needs a timezone-aware datetime")
    year, number, _ = dt.astimezone(tz).isocalendar()
    return f"{year}-W{number:02d}"


def last_completed_week(now: datetime, tz: ZoneInfo = IST) -> str:
    """The most recent ISO week that has fully ended at `now`."""
    if now.tzinfo is None:
        raise ValueError("last_completed_week needs a timezone-aware datetime")
    return week_of(now.astimezone(tz) - timedelta(weeks=1), tz)


def weeks_between(start: datetime, end: datetime, tz: ZoneInfo = IST) -> list[str]:
    """ISO weeks from the one containing `start` to the one containing `end`, oldest first."""
    weeks: list[str] = []
    cursor = start.astimezone(tz)
    while cursor <= end:
        label = week_of(cursor, tz)
        if not weeks or weeks[-1] != label:
            weeks.append(label)
        cursor += timedelta(weeks=1)
    last = week_of(end, tz)
    if weeks[-1] != last:
        weeks.append(last)
    return weeks
