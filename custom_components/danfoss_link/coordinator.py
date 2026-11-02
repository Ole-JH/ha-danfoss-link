"""Polling coordinator that owns the tunnel to the Danfoss Link CC."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import CannotConnect, DanfossLinkClient, DanfossLinkError, RequestError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The CC drops off the Danfoss relay for a minute or two every few hours
# (PEER_NOT_AVAILABLE). Keep showing the last known state for this long, and
# poll more often meanwhile, before marking the entities unavailable.
OUTAGE_GRACE_PERIOD = timedelta(minutes=5)
OUTAGE_RETRY_INTERVAL = timedelta(seconds=15)

# Endpoints that may legitimately be missing on some firmware versions.
OPTIONAL_ENDPOINTS = {
    "status": "/api/system/status",
    "modes": "/api/modes",
    "onoffdevices": "/api/onoffdevices",
    "alarms": "/api/alarms",
    "temperature_settings": "/api/settings/temperature",
}


@dataclass
class DanfossLinkData:
    """A snapshot of the CC state."""

    info: dict[str, Any]
    rooms: dict[int, dict[str, Any]]
    status: dict[str, Any] = field(default_factory=dict)
    modes: list[dict[str, Any]] = field(default_factory=list)
    onoffdevices: dict[int, dict[str, Any]] = field(default_factory=dict)
    alarms: list[dict[str, Any]] = field(default_factory=list)
    temperature_settings: dict[str, Any] = field(default_factory=dict)


type DanfossLinkConfigEntry = ConfigEntry[DanfossLinkCoordinator]


class DanfossLinkCoordinator(DataUpdateCoordinator[DanfossLinkData]):
    """Serialises all traffic over the single tunnel to the CC."""

    config_entry: DanfossLinkConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DanfossLinkConfigEntry,
        client: DanfossLinkClient,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._normal_interval = timedelta(seconds=scan_interval)
        self._outage_started: datetime | None = None
        self.client = client
        self.peer_id = client.identity.peer_id
        # Device registry id of the CC, set at setup so rooms can link to it.
        self.controller_device_id: str | None = None
        self._lock = asyncio.Lock()

    async def _call(self, func: Any, *args: Any) -> Any:
        async with self._lock:
            return await self.hass.async_add_executor_job(func, *args)

    async def _async_update_data(self) -> DanfossLinkData:
        try:
            data = await self._call(self._fetch)
        except CannotConnect as err:
            return self._handle_outage(err)
        except DanfossLinkError as err:
            raise UpdateFailed(f"Error talking to the Danfoss Link CC: {err}") from err
        if self._outage_started is not None:
            _LOGGER.info(
                "Danfoss Link CC is reachable again after %s",
                dt_util.utcnow() - self._outage_started,
            )
            self._outage_started = None
            self.update_interval = self._normal_interval
        return data

    def _handle_outage(self, err: CannotConnect) -> DanfossLinkData:
        """Ride out a short outage on the last known data, or give up."""
        now = dt_util.utcnow()
        if self._outage_started is None:
            self._outage_started = now
        if self.data is None or now - self._outage_started >= OUTAGE_GRACE_PERIOD:
            self.update_interval = self._normal_interval
            raise UpdateFailed(f"Error talking to the Danfoss Link CC: {err}") from err
        _LOGGER.debug("Danfoss Link CC not reachable, keeping last known state: %s", err)
        self.update_interval = min(OUTAGE_RETRY_INTERVAL, self._normal_interval)
        return self.data

    def _fetch(self) -> DanfossLinkData:
        client = self.client
        info = client.get("/api/system/info") or {}
        rooms = client.get("/api/rooms") or []
        data = DanfossLinkData(
            info=info,
            rooms={room["id"]: room for room in rooms if "id" in room},
        )
        for key, uri in OPTIONAL_ENDPOINTS.items():
            try:
                value = client.get(uri)
            except RequestError as err:
                _LOGGER.debug("Skipping %s: %s", uri, err)
                continue
            if value is None:
                continue
            if key == "onoffdevices":
                value = {dev["id"]: dev for dev in value if "id" in dev}
            setattr(data, key, value)
        return data

    async def async_write(self, method: str, uri: str, content: dict[str, Any]) -> None:
        """Send a change to the CC, then refresh."""
        await self._send(method, uri, content)
        # Not debounced: the UI should show what the CC accepted right away.
        await self.async_refresh()

    async def async_mark_alarms_read(self, alarm_ids: list[int]) -> None:
        """Mark alarms as read, as the app does when one is opened."""
        for alarm_id in alarm_ids:
            await self._send("PATCH", f"/api/alarms/{alarm_id}", {"isRead": True, "id": alarm_id})
        await self.async_refresh()

    async def _send(self, method: str, uri: str, content: dict[str, Any]) -> None:
        _LOGGER.debug("%s %s %s", method, uri, content)
        try:
            await self._call(self.client.request, method, uri, content)
        except RequestError as err:
            raise HomeAssistantError(
                f"The Danfoss Link CC rejected the change (HTTP {err.status}): {err.content}"
            ) from err
        except CannotConnect as err:
            raise HomeAssistantError(f"Cannot reach the Danfoss Link CC: {err}") from err
        except DanfossLinkError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_close(self) -> None:
        await self._call(self.client.close)
