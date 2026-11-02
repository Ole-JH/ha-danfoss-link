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
