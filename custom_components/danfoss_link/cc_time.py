"""Timestamps in the CC's format (mode startTime/endTime).

The app copies the CC: if /api/system/info systemTime ends in "Z", timestamps
are UTC with a "Z" suffix (read from the "...Utc" keys); if it ends in an
offset such as "+02:00", they are CC local time without a suffix.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
import re
from typing import Any

_FORMAT = "%Y-%m-%dT%H:%M:%S"
_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})$")


def cc_timezone(info: dict[str, Any]) -> tzinfo | None:
    """The CC's fixed offset, or None when it talks UTC (the app's default)."""
    match = _OFFSET.search(info.get("systemTime") or "")
    if not match:
        return None
    sign, hours, minutes = match.groups()
    offset = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-offset if sign == "-" else offset)


def format_cc_time(when: datetime, info: dict[str, Any]) -> str:
    """Format an aware datetime the way the app sends it to this CC."""
    tz = cc_timezone(info)
    if tz is None:
        return when.astimezone(UTC).strftime(_FORMAT) + "Z"
    return when.astimezone(tz).strftime(_FORMAT)


def parse_cc_time(mode: dict[str, Any], key: str, info: dict[str, Any]) -> datetime | None:
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
    return parsed
