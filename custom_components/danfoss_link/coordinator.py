"""Polling coordinator that owns the tunnel to the Danfoss Link CC."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CannotConnect, DanfossLinkClient, DanfossLinkError, RequestError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

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
        self.client = client
        self.peer_id = client.identity.peer_id
        self._lock = asyncio.Lock()

    async def _call(self, func: Any, *args: Any) -> Any:
        async with self._lock:
            return await self.hass.async_add_executor_job(func, *args)

    async def _async_update_data(self) -> DanfossLinkData:
        try:
            return await self._call(self._fetch)
        except DanfossLinkError as err:
            raise UpdateFailed(f"Error talking to the Danfoss Link CC: {err}") from err

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
        # Not debounced: the UI should show what the CC accepted right away.
        await self.async_refresh()

    async def async_close(self) -> None:
        await self._call(self.client.close)
