"""A pause planned for later, kept and started by Home Assistant.

The CC cannot plan a pause: PUT api/modes/pause accepts only type, state
(Active) and endTime, and refuses startTime (seen live). So Home Assistant
keeps the planned start and end, survives restarts by storing them, and
starts the pause on the CC at the start time, as if the app's Pause button
had been pressed then.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# When the CC cannot be reached at the start time, try again this often.
RETRY_INTERVAL = timedelta(minutes=1)
MAX_RETRIES = 60


class PlannedPause:
    """The (at most one) pause planned for later on one CC."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        start_pause: Callable[[datetime | None], Awaitable[None]],
        on_change: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.planned_pause"
        )
        self._start_pause = start_pause
        self._on_change = on_change
        self.start: datetime | None = None
        self.end: datetime | None = None
        self._unsub: CALLBACK_TYPE | None = None
        self._retries = 0

    async def async_load(self) -> None:
        """Pick up a plan made before Home Assistant restarted."""
        stored = await self._store.async_load()
        if not stored or not stored.get("start"):
            return
        self.start = dt_util.parse_datetime(stored["start"])
        self.end = dt_util.parse_datetime(stored["end"]) if stored.get("end") else None
        self._arm()

    async def async_plan(self, start: datetime, end: datetime | None) -> None:
        """Plan a pause from ``start`` to ``end`` (None: until resumed)."""
        self.start, self.end = start, end
        await self._save()
        self._arm()
        self._on_change()

    async def async_cancel(self) -> None:
        """Drop the plan, if there is one."""
        if self.start is None:
            return
        self._clear()
        await self._save()
        self._on_change()

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _clear(self) -> None:
        self.async_stop()
        self.start = self.end = None
        self._retries = 0

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None,
            }
        )

    @callback
    def _arm(self, when: datetime | None = None) -> None:
        self.async_stop()
        if self.start is None:
            return
        now = dt_util.utcnow()
        if self.end is not None and self.end <= now:
            # Missed altogether (Home Assistant was down the whole time).
            _LOGGER.info("Planned pause ended before it could start; dropping it")
            self._clear()
            self._hass.async_create_task(self._save())
            return
        self._unsub = async_track_point_in_time(self._hass, self._fire, when or self.start)

    async def _fire(self, _now: datetime) -> None:
        self._unsub = None
        end = self.end
        try:
            await self._start_pause(end)
        except HomeAssistantError as err:
            self._retries += 1
            if self._retries < MAX_RETRIES:
                _LOGGER.warning("Could not start the planned pause, retrying: %s", err)
                self._arm(dt_util.utcnow() + RETRY_INTERVAL)
                return
            _LOGGER.error("Giving up on the planned pause: %s", err)
        self._clear()
        await self._save()
        self._on_change()
