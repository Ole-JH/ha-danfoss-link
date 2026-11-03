"""Timestamps in the CC's format (mode startTime/endTime).

The app copies the CC: if /api/system/info systemTime ends in "Z", timestamps
are UTC with a "Z" suffix (read from the "...Utc" keys); if it ends in an
offset such as "+02:00", they are CC local time without a suffix.

Mode times run on the CC's own clock, which can be minutes off (Ole's ran
about 18 minutes fast, so a pause "until 22:12" ended at 21:54). The
coordinator measures that drift from systemTime on every poll and these
helpers correct for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
import re
from typing import Any

_FORMAT = "%Y-%m-%dT%H:%M:%S"
_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})$")
# A bigger difference is not drift but a wrong or stale clock; leave it alone.
MAX_DRIFT = timedelta(hours=1)


def clock_drift(info: dict[str, Any], now: datetime) -> timedelta:
    """How far the CC's clock is ahead of ``now`` (whole minutes; 0 if unknown)."""
    try:
        cc_now = datetime.fromisoformat(info.get("systemTime") or "")
    except ValueError:
        return timedelta(0)
    if cc_now.tzinfo is None:
        cc_now = cc_now.replace(tzinfo=UTC)
    drift = cc_now - now
    if abs(drift) > MAX_DRIFT:
        return timedelta(0)
    return timedelta(minutes=round(drift.total_seconds() / 60))


def cc_timezone(info: dict[str, Any]) -> tzinfo | None:
    """The CC's fixed offset, or None when it talks UTC (the app's default)."""
    match = _OFFSET.search(info.get("systemTime") or "")
    if not match:
        return None
    sign, hours, minutes = match.groups()
    offset = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-offset if sign == "-" else offset)


def format_cc_time(when: datetime, info: dict[str, Any], drift: timedelta = timedelta(0)) -> str:
    """Format an aware datetime the way the app sends it to this CC."""
    when = when + drift
    tz = cc_timezone(info)
    if tz is None:
        return when.astimezone(UTC).strftime(_FORMAT) + "Z"
    return when.astimezone(tz).strftime(_FORMAT)


def parse_cc_time(
    mode: dict[str, Any], key: str, info: dict[str, Any], drift: timedelta = timedelta(0)
) -> datetime | None:
    """Read a mode timestamp ("startTime"/"endTime") as an aware datetime."""
    tz = cc_timezone(info)
    keys = (key, f"{key}Utc") if tz else (f"{key}Utc", key)
    value = next((mode[k] for k in keys if isinstance(mode.get(k), str)), None)
    # Like the app, treat anything outside 1970-2099 as "no date".
    if value is None or not "1970" <= value <= "2099":
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or UTC)
    return parsed - drift
