"""Room weekly schedules in GET api/rooms, shown as climate attributes.

A day is a list of periods that cover 00:00-24:00:
{"startTimeFromMidnight": "06:00:00", "endTimeFromMidnight": "08:00:00", "state": "AtHome"}
Times are .NET TimeSpan strings; the end of the day is "1.00:00:00".
Away periods are the night/day setback, so only the other periods are listed.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from homeassistant.util import dt as dt_util

from .cc_time import cc_timezone

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TIMESPAN = re.compile(r"^(?:(\d+)\.)?(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$")


def _minutes(value: Any) -> int | None:
    """Minutes since midnight from a TimeSpan string, e.g. "1.00:00:00" -> 1440."""
    match = _TIMESPAN.match(value) if isinstance(value, str) else None
    if not match:
        return None
    days, hours, minutes = match.groups()
    return int(days or 0) * 1440 + int(hours) * 60 + int(minutes)


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def day_periods(periods: Any) -> list[str]:
    """The day's non-Away periods as "HH:MM-HH:MM", sorted.

    AtHome periods are plain; any other state gets it as a suffix,
    e.g. "22:00-24:00 asleep".
    """
    result: list[tuple[int, str]] = []
    for period in periods if isinstance(periods, list) else []:
        if not isinstance(period, dict) or period.get("state") == "Away":
            continue
        start = _minutes(period.get("startTimeFromMidnight"))
        end = _minutes(period.get("endTimeFromMidnight"))
        if start is None or end is None:
            continue
        text = f"{_clock(start)}-{_clock(end)}"
        state = period.get("state")
        if state and state != "AtHome":
            text += f" {re.sub(r'(?<!^)(?=[A-Z])', '_', state).lower()}"
        result.append((start, text))
    return [text for _, text in sorted(result)]


def cc_today(info: dict[str, Any], now: datetime | None = None) -> str:
    """Today's weekday key in the CC's local time (HA's time zone if unknown)."""
    now = now or dt_util.utcnow()
    tz = cc_timezone(info) or dt_util.get_default_time_zone()
    return WEEKDAYS[now.astimezone(tz).weekday()]


def schedule_attributes(room: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """schedule_today and schedule (per weekday) attributes, empty without a schedule."""
    schedule = room.get("schedule")
    if not isinstance(schedule, dict) or not any(day in schedule for day in WEEKDAYS):
        return {}
    week = {day: day_periods(schedule.get(day)) for day in WEEKDAYS}
    return {"schedule_today": week[cc_today(info)], "schedule": week}


# Writing: the app's editor holds the at-home periods, snaps them to 15
# minutes and fills the gaps with Away before sending a whole day.
_PERIOD = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
STEP_MINUTES = 15
DAY_MINUTES = 1440


def parse_period(text: str) -> tuple[int, int]:
    """ "06:00-08:30" -> (360, 510). Raises ValueError for a bad period."""
    match = _PERIOD.match(text) if isinstance(text, str) else None
    if not match:
        raise ValueError(text)
    h1, m1, h2, m2 = (int(part) for part in match.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if m1 >= 60 or m2 >= 60 or not 0 <= start < end <= DAY_MINUTES:
        raise ValueError(text)
    if start % STEP_MINUTES or end % STEP_MINUTES:
        raise ValueError(text)
    return start, end


def _timespan(minutes: int) -> str:
    if minutes == DAY_MINUTES:
        return "1.00:00:00"
    return f"{_clock(minutes)}:00"


def build_day(at_home: list[tuple[int, int]]) -> list[dict[str, str]]:
    """A full 00:00-24:00 day: the at-home periods (merged), Away in between."""
    merged: list[list[int]] = []
    for start, end in sorted(at_home):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    day: list[dict[str, str]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            day.append(_period_dict(cursor, start, "Away"))
        day.append(_period_dict(start, end, "AtHome"))
        cursor = end
    if cursor < DAY_MINUTES:
        day.append(_period_dict(cursor, DAY_MINUTES, "Away"))
    return day


def _period_dict(start: int, end: int, state: str) -> dict[str, str]:
    return {
        "startTimeFromMidnight": _timespan(start),
        "endTimeFromMidnight": _timespan(end),
        "state": state,
    }
