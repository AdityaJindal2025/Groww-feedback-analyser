from datetime import UTC, datetime

import pytest

from pulse.timeutil import (
    IST,
    last_completed_week,
    parse_iso_week,
    week_of,
    weeks_between,
    window_for,
)


def test_parse_iso_week() -> None:
    start, end = parse_iso_week("2026-W41")
    assert start == datetime(2026, 10, 5, 0, 0, 0, tzinfo=IST)
    assert end == datetime(2026, 10, 11, 23, 59, 59, tzinfo=IST)


def test_twelve_week_window() -> None:
    start, end = window_for("2026-W41", 12)
    assert start == datetime(2026, 7, 20, tzinfo=IST)
    assert end == datetime(2026, 10, 11, 23, 59, 59, tzinfo=IST)


def test_year_boundary() -> None:
    start, end = parse_iso_week("2026-W01")
    assert start.date().isoformat() == "2025-12-29"
    assert end.date().isoformat() == "2026-01-04"


def test_last_completed_week() -> None:
    assert last_completed_week(datetime(2026, 10, 16, 10, 0, tzinfo=IST)) == "2026-W41"


def test_last_completed_week_on_monday_morning() -> None:
    assert last_completed_week(datetime(2026, 10, 12, 0, 30, tzinfo=IST)) == "2026-W41"


def test_week_of_uses_ist() -> None:
    # Sunday 20:00 UTC is already Monday 01:30 IST.
    assert week_of(datetime(2026, 10, 11, 20, 0, tzinfo=UTC)) == "2026-W42"
    assert week_of(datetime(2026, 10, 11, 18, 0, tzinfo=UTC)) == "2026-W41"


@pytest.mark.parametrize("bad", ["2026-41", "2026-W5", "W41", "2026-W54"])
def test_bad_week_strings(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_iso_week(bad)


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValueError):
        week_of(datetime(2026, 10, 11))


def test_weeks_between_window() -> None:
    start, end = window_for("2026-W41", 12)
    weeks = weeks_between(start, end)
    assert len(weeks) == 12
    assert weeks[0] == "2026-W30"
    assert weeks[-1] == "2026-W41"
