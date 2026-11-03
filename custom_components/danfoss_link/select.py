"""Selects: the CC's house mode and each room's schedule/manual mode."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DOMAIN,
    MODE_ACTIVE,
    MODE_AT_HOME,
    MODE_ENDPOINTS,
    MODE_INACTIVE,
    MODE_PAUSE,
    MODE_VACATION,
    REGULATION_MANUAL,
    REGULATION_SCHEDULE,
)
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity, DanfossLinkRoomEntity

PARALLEL_UPDATES = 1

# CC mode type -> option key (translated in strings.json)
_OPTIONS = {"AtHome": "at_home", "Vacation": "vacation", "Pause": "pause", "Boost": "boost"}
_TYPES = {v: k for k, v in _OPTIONS.items()}
# No mode active: the CC follows its weekly schedule (the app's normal state).
_SCHEDULE = "schedule"

# Room regulation type -> option key; same setting as the climate entity's auto/heat.
_REGULATION_OPTIONS = {REGULATION_SCHEDULE: "schedule", REGULATION_MANUAL: "manual"}
_REGULATION_TYPES = {v: k for k, v in _REGULATION_OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [
        DanfossLinkRoomMode(coordinator, room_id) for room_id in coordinator.data.rooms
    ]
    if any(mode.get("type") in _OPTIONS for mode in coordinator.data.modes):
        entities.append(DanfossLinkHouseMode(coordinator))
    async_add_entities(entities)


class DanfossLinkHouseMode(DanfossLinkEntity, SelectEntity):
    """Which house mode the CC is in."""

    _attr_translation_key = "house_mode"

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "house_mode")

    @property
    def options(self) -> list[str]:
        types = {mode.get("type") for mode in self.coordinator.data.modes}
        return [_SCHEDULE] + [
            option for mode_type, option in _OPTIONS.items() if mode_type in types
        ]

    def _active_type(self) -> str | None:
        active = [
            mode.get("type")
            for mode in self.coordinator.data.modes
            if mode.get("state") == MODE_ACTIVE and mode.get("type") in _OPTIONS
        ]
        # A temporary mode (vacation, pause, boost) overrides AtHome.
        for mode_type in active:
            if mode_type != MODE_AT_HOME:
                return mode_type
        return active[0] if active else None

    @property
    def current_option(self) -> str:
        active = self._active_type()
        return _OPTIONS[active] if active else _SCHEDULE

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs: dict[str, Any] = {}
        if (at_home := self.coordinator.active_at_home()) is not None:
            rooms = self.coordinator.data.rooms
            # The rooms the "home now" override covers; rooms switched to Manual
            # drop out of it on the CC.
            attrs["at_home_rooms"] = [
                rooms[room_id].get("name") or f"Room {room_id}"
                for room_id in at_home.get("rooms") or []
                if room_id in rooms
            ]
        data = self.coordinator.data
        settings = data.temperature_settings
        # For the pause and vacation card: each mode's state (active, planned
        # or None), start and end, and the temperature the rooms then hold.
        for mode_type, key in ((MODE_PAUSE, "pause"), (MODE_VACATION, "vacation")):
            if not any(m.get("type") == mode_type for m in data.modes):
                continue
            state, start_time, end_time = self.coordinator.mode_info(mode_type)
            attrs[f"{key}_state"] = state
            attrs[f"{key}_start"] = start_time.isoformat() if start_time else None
            attrs[f"{key}_end"] = end_time.isoformat() if end_time else None
            temperature = (settings.get(f"{key}Temperature") or {}).get("setpoint")
            if temperature is not None:
                attrs[f"{key}_temperature"] = temperature
        return attrs or None

    async def async_select_option(self, option: str) -> None:
        await self._async_switch(_TYPES.get(option))

    def _require_pause(self) -> None:
        if not any(mode.get("type") == MODE_PAUSE for mode in self.coordinator.data.modes):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_pause")

    async def async_pause(self, start: datetime | None, end: datetime | None) -> None:
        """Pause heating from ``start`` (None: now) until ``end`` (None: until resumed).

        Pausing again while paused moves the end time. The CC cannot plan a
        pause, so a later start is kept and started by Home Assistant; it
        replaces an earlier plan.
        """
        self._require_pause()
        if start is not None:
            await self.coordinator.planned_pause.async_plan(start, end)
            return
        await self.coordinator.planned_pause.async_cancel()
        await self.coordinator.async_start_pause(end)

    async def async_cancel_pause(self) -> None:
        """End a running pause and cancel a planned one."""
        self._require_pause()
        await self.coordinator.planned_pause.async_cancel()
        if self._active_type() == MODE_PAUSE:
            await self.coordinator.async_write(
                "PUT",
                f"/api/modes/{MODE_ENDPOINTS[MODE_PAUSE]}",
                {"type": MODE_PAUSE, "state": MODE_INACTIVE},
            )

    async def _async_switch(self, target: str | None) -> None:
        current = self._active_type()
        all_rooms = sorted(self.coordinator.data.rooms)
        if current == target:
            at_home = self.coordinator.active_at_home()
            if (
                target == MODE_AT_HOME
                and at_home is not None
                and not set(at_home.get("rooms") or []).issuperset(all_rooms)
            ):
                # Selecting At home again brings every room back in.
                await self.coordinator.async_start_at_home(all_rooms, restart=True)
                await self.coordinator.async_refresh()
            return
        # AtHome is an override too ("home now"); only switching to another
        # mode replaces it, going back to the schedule has to end it.
        if current is not None and (current != MODE_AT_HOME or target is None):
            await self.coordinator.async_write(
                "PUT",
                f"/api/modes/{MODE_ENDPOINTS[current]}",
                {"type": current, "state": MODE_INACTIVE},
            )
        if target is None:
            return
        if target == MODE_AT_HOME:
            await self.coordinator.async_start_at_home(all_rooms)
            await self.coordinator.async_refresh()
            return
        await self.coordinator.async_write(
            "PUT", f"/api/modes/{MODE_ENDPOINTS[target]}", {"type": target, "state": MODE_ACTIVE}
        )


class DanfossLinkRoomMode(DanfossLinkRoomEntity, SelectEntity):
    """Whether a room follows its weekly schedule or holds a manual setpoint."""

    _attr_translation_key = "room_mode"
    _attr_options = list(_REGULATION_TYPES)

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "room_mode")

    @property
    def current_option(self) -> str | None:
        regulation = (self.room.get("regulation") or {}).get("type")
        return _REGULATION_OPTIONS.get(regulation)

    async def async_select_option(self, option: str) -> None:
        await self.async_set_regulation_type(_REGULATION_TYPES[option])
