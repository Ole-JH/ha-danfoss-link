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
from .cc_time import clock_drift, format_cc_time, parse_cc_time
from .const import (
    DOMAIN,
    MODE_ACTIVE,
    MODE_AT_HOME,
    MODE_ENDPOINTS,
    MODE_INACTIVE,
    MODE_PAUSE,
    MODE_PLANNED,
    ZONE_LIVING,
)
from .planned_pause import PlannedPause

_LOGGER = logging.getLogger(__name__)

# The CC drops off the Danfoss relay for a minute or two every few hours
# (PEER_NOT_AVAILABLE). Keep showing the last known state for this long, and
# poll more often meanwhile, before marking the entities unavailable.
OUTAGE_GRACE_PERIOD = timedelta(minutes=5)
OUTAGE_RETRY_INTERVAL = timedelta(seconds=15)
# GET api/rooms leaves out each room's weekly schedule; the app reads it from
# GET api/rooms/{id}. Schedules rarely change, so fetch them this often.
SCHEDULE_REFRESH_INTERVAL = timedelta(hours=1)

# Endpoints that may legitimately be missing on some firmware versions.
OPTIONAL_ENDPOINTS = {
    "status": "/api/system/status",
    "modes": "/api/modes",
    "onoffdevices": "/api/onoffdevices",
    "alarms": "/api/alarms",
    "temperature_settings": "/api/settings/temperature",
}


def mode_state(mode: dict[str, Any]) -> str | None:
    """ "active", "planned" or None for a mode from api/modes."""
    state = mode.get("state")
    if state == MODE_PLANNED:
        return "planned"
    if state == MODE_ACTIVE:
        return "active"
    return None


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
    # How far the CC's clock is ahead of Home Assistant's.
    clock_drift: timedelta = timedelta(0)

    def parse_time(self, mode: dict[str, Any], key: str) -> datetime | None:
        """A mode's start or end time, on Home Assistant's clock."""
        return parse_cc_time(mode, key, self.info, self.clock_drift)

    def format_time(self, when: datetime) -> str:
        """A time to send to the CC, on the CC's clock."""
        return format_cc_time(when, self.info, self.clock_drift)


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
        # room id -> weekly schedule from api/rooms/{id}, and when it was read.
        self._schedules: dict[int, dict[str, Any]] = {}
        self._schedules_fetched: datetime | None = None
        # The CC cannot plan a pause, so Home Assistant does.
        self.planned_pause = PlannedPause(
            hass, entry.entry_id, self.async_start_pause, self.async_update_listeners
        )

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
        drift = clock_drift(info, dt_util.utcnow())
        rooms = client.get("/api/rooms") or []
        data = DanfossLinkData(
            info=info,
            rooms={room["id"]: room for room in rooms if "id" in room},
            clock_drift=drift,
        )
        self._add_schedules(data.rooms)
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

    def _add_schedules(self, rooms: dict[int, dict[str, Any]]) -> None:
        """Add each room's weekly schedule, read from api/rooms/{id} hourly."""
        now = dt_util.utcnow()
        if (
            self._schedules_fetched is None
            or now - self._schedules_fetched >= SCHEDULE_REFRESH_INTERVAL
        ):
            schedules: dict[int, dict[str, Any]] = {}
            for room_id, room in rooms.items():
                if isinstance(room.get("schedule"), dict):
                    continue
                try:
                    detail = self.client.get(f"/api/rooms/{room_id}")
                except RequestError as err:
                    _LOGGER.debug("No schedule for room %s: %s", room_id, err)
                    continue
                schedule = detail.get("schedule") if isinstance(detail, dict) else None
                if isinstance(schedule, dict):
                    schedules[room_id] = schedule
                else:
                    _LOGGER.debug(
                        "Room %s detail has no schedule, keys: %s",
                        room_id,
                        sorted(detail) if isinstance(detail, dict) else type(detail).__name__,
                    )
            self._schedules = schedules
            self._schedules_fetched = now
        for room_id, schedule in self._schedules.items():
            if room_id in rooms and not isinstance(rooms[room_id].get("schedule"), dict):
                rooms[room_id]["schedule"] = schedule

    async def async_write_schedule(
        self,
        uri: str,
        base: dict[str, Any],
        days: dict[str, list[dict[str, str]]],
        room_ids: list[int],
    ) -> None:
        """Send weekly schedule days and show them on these rooms right away.

        Every round trip through the relay is slow, so all days go in one
        PATCH. Should the CC refuse that, fall back to one PATCH per day,
        as the app's "copy to days" does.
        """
        content = {**base, "schedule": days}
        _LOGGER.debug("PATCH %s %s", uri, content)
        try:
            await self._call(self.client.request, "PATCH", uri, content)
        except RequestError as err:
            if len(days) == 1:
                raise HomeAssistantError(
                    f"The Danfoss Link CC rejected the change (HTTP {err.status}): {err.content}"
                ) from err
            _LOGGER.debug("CC refused several days at once (%s), sending one per day", err)
            for day, periods in days.items():
                await self._send("PATCH", uri, {**base, "schedule": {day: periods}})
        except CannotConnect as err:
            raise HomeAssistantError(f"Cannot reach the Danfoss Link CC: {err}") from err
        except DanfossLinkError as err:
            raise HomeAssistantError(str(err)) from err
        # The CC took it: update the known schedules instead of reading every
        # room again (the hourly schedule refresh picks up anything else).
        for room_id in room_ids:
            schedule = dict(self._schedules.get(room_id, {}))
            schedule.update(days)
            self._schedules[room_id] = schedule
            if room_id in self.data.rooms:
                self.data.rooms[room_id]["schedule"] = schedule
        self.async_set_updated_data(self.data)

    async def async_write(self, method: str, uri: str, content: dict[str, Any]) -> None:
        """Send a change to the CC, then refresh."""
        await self.async_send(method, uri, content)
        # Not debounced: the UI should show what the CC accepted right away.
        await self.async_refresh()

    def living_zone_rooms(self) -> list[int]:
        """Ids of the rooms in the living zone, in the CC's order."""
        return [
            room_id for room_id, room in self.data.rooms.items() if room.get("zone") == ZONE_LIVING
        ]

    async def async_set_living_zone(self, room_id: int, member: bool) -> None:
        """Move a room into or out of the living zone.

        The app cannot do this (it sends users to the CC's display), and the
        CC's schemas for PATCH api/rooms/{id} only allow id, setpoint,
        regulation and schedule. Try the zone itself with its new room list,
        as the AtHome mode lists rooms, and check that it changed.
        """
        rooms = [r for r in self.living_zone_rooms() if r != room_id]
        if member:
            rooms.append(room_id)
        content = {"name": ZONE_LIVING, "rooms": sorted(rooms)}
        _LOGGER.debug("PATCH /api/zones/living %s", content)
        name = self.data.rooms.get(room_id, {}).get("name") or str(room_id)
        try:
            await self._call(self.client.request, "PATCH", "/api/zones/living", content)
        except RequestError as err:
            # The CC answers with its JSON schemas: keep them for the docs.
            _LOGGER.warning(
                "The Danfoss Link CC refused to change the living zone (HTTP %s): %s",
                err.status,
                err.content,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="living_zone_unchanged",
                translation_placeholders={"room": name},
            ) from err
        except CannotConnect as err:
            raise HomeAssistantError(f"Cannot reach the Danfoss Link CC: {err}") from err
        except DanfossLinkError as err:
            raise HomeAssistantError(str(err)) from err
        # A room that joins takes the zone's schedule: read schedules again.
        self._schedules_fetched = None
        await self.async_refresh()
        zone = self.data.rooms.get(room_id, {}).get("zone")
        if (zone == ZONE_LIVING) != member:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="living_zone_unchanged",
                translation_placeholders={"room": name},
            )

    def active_at_home(self) -> dict[str, Any] | None:
        """The active "home now" (AtHome) override, if any."""
        return next(
            (
                mode
                for mode in self.data.modes
                if mode.get("type") == MODE_AT_HOME and mode.get("state") == MODE_ACTIVE
            ),
            None,
        )

    async def async_start_at_home(self, rooms: list[int], *, restart: bool = False) -> None:
        """Turn on the AtHome override for these rooms (without refreshing).

        The CC drops a room from an active AtHome override when the room is
        switched to Manual, and does not add it back when it returns to
        Schedule. ``restart`` ends the override first so it can be sent again
        with the full room list.
        """
        uri = f"/api/modes/{MODE_ENDPOINTS[MODE_AT_HOME]}"
        if restart:
            await self._send("PUT", uri, {"type": MODE_AT_HOME, "state": MODE_INACTIVE})
        await self._send(
            "PUT",
            uri,
            # The CC requires the list of rooms the mode applies to.
            {"type": MODE_AT_HOME, "state": MODE_ACTIVE, "rooms": sorted(rooms)},
        )

    def mode_info(self, mode_type: str) -> tuple[str | None, datetime | None, datetime | None]:
        """("active" / "planned" / None, start, end) of a vacation or pause."""
        data = self.data
        for mode in data.modes:
            if mode.get("type") == mode_type and (state := mode_state(mode)):
                return state, data.parse_time(mode, "startTime"), data.parse_time(mode, "endTime")
        if mode_type == MODE_PAUSE and self.planned_pause.start is not None:
            return "planned", self.planned_pause.start, self.planned_pause.end
        return None, None, None

    async def async_start_pause(self, end: datetime | None) -> None:
        """Pause heating now, until ``end`` (None: until resumed).

        The CC ends AtHome by itself when a pause starts; another temporary
        mode (vacation, boost) is ended first, as the house mode select does.
        """
        for mode in self.data.modes:
            mode_type = mode.get("type")
            if (
                mode.get("state") == MODE_ACTIVE
                and mode_type in MODE_ENDPOINTS
                and mode_type not in (MODE_AT_HOME, MODE_PAUSE)
            ):
                await self._send(
                    "PUT",
                    f"/api/modes/{MODE_ENDPOINTS[mode_type]}",
                    {"type": mode_type, "state": MODE_INACTIVE},
                )
        content: dict[str, Any] = {"type": MODE_PAUSE, "state": MODE_ACTIVE}
        if end is not None:
            content["endTime"] = self.data.format_time(end)
        await self.async_write("PUT", f"/api/modes/{MODE_ENDPOINTS[MODE_PAUSE]}", content)

    async def async_mark_alarms_read(self, alarm_ids: list[int]) -> None:
        """Mark alarms as read, as the app does when one is opened."""
        for alarm_id in alarm_ids:
            await self._send("PATCH", f"/api/alarms/{alarm_id}", {"isRead": True, "id": alarm_id})
        await self.async_refresh()

    async def async_send(self, method: str, uri: str, content: dict[str, Any]) -> None:
        """Send a change to the CC without refreshing."""
        await self._send(method, uri, content)

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
        self.planned_pause.async_stop()
        await self._call(self.client.close)
