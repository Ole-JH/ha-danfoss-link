"""Weekly schedule parsing for the climate attributes."""

from datetime import UTC, datetime

from custom_components.danfoss_link.schedule import cc_today, day_periods, schedule_attributes


def _period(start: str, end: str, state: str) -> dict:
    return {"startTimeFromMidnight": start, "endTimeFromMidnight": end, "state": state}


def test_day_periods_lists_non_away_sorted() -> None:
    day = [
        _period("22:00:00", "1.00:00:00", "Asleep"),
        _period("00:00:00", "06:15:00", "Away"),
        _period("06:15:00", "22:00:00", "AtHome"),
    ]
    assert day_periods(day) == ["06:15-22:00", "22:00-24:00 asleep"]


def test_day_periods_tolerates_bad_data() -> None:
    assert day_periods(None) == []
    assert day_periods([{"state": "AtHome"}, "x", _period("bad", "08:00:00", "AtHome")]) == []


def test_cc_today_uses_cc_offset() -> None:
    # Sunday 23:30 UTC is Monday 00:30 on a CC at +01:00.
    now = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)
    assert cc_today({"systemTime": "2026-11-02T00:30:00+01:00"}, now) == "monday"


def test_schedule_attributes_empty_without_schedule() -> None:
    assert schedule_attributes({}, {}) == {}
    assert schedule_attributes({"schedule": {}}, {}) == {}
