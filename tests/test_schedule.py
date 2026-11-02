"""Weekly schedule parsing for the climate attributes."""

from datetime import UTC, datetime

import pytest

from custom_components.danfoss_link.schedule import (
    build_day,
    cc_today,
    day_periods,
    parse_period,
    schedule_attributes,
)


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


def test_parse_period() -> None:
    assert parse_period("06:00-08:30") == (360, 510)
    assert parse_period(" 22:00 - 24:00 ") == (1320, 1440)
    for bad in ("06:00", "08:00-06:00", "06:10-08:00", "06:00-24:15", "25:00-26:00", "6-8"):
        with pytest.raises(ValueError):
            parse_period(bad)


def test_build_day_fills_away_and_merges() -> None:
    day = build_day([(960, 1350), (360, 480), (450, 600)])
    assert [(p["startTimeFromMidnight"], p["endTimeFromMidnight"], p["state"]) for p in day] == [
        ("00:00:00", "06:00:00", "Away"),
        ("06:00:00", "10:00:00", "AtHome"),
        ("10:00:00", "16:00:00", "Away"),
        ("16:00:00", "22:30:00", "AtHome"),
        ("22:30:00", "1.00:00:00", "Away"),
    ]
    assert build_day([]) == [
        {"startTimeFromMidnight": "00:00:00", "endTimeFromMidnight": "1.00:00:00", "state": "Away"}
    ]
    assert day_periods(build_day([(0, 1440)])) == ["00:00-24:00"]
