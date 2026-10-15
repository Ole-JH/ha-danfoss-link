"""Selects: the CC's house mode and each room's schedule/manual mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    MODE_ACTIVE,
    MODE_AT_HOME,
    MODE_ENDPOINTS,
    MODE_INACTIVE,
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

    async def async_select_option(self, option: str) -> None:
        target = _TYPES.get(option)
        current = self._active_type()
        if current == target:
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
            content = {
                "type": MODE_AT_HOME,
                "state": MODE_ACTIVE,
                # The CC requires the list of rooms the mode applies to.
                "rooms": sorted(self.coordinator.data.rooms),
            }
        else:
            content = {"type": target, "state": MODE_ACTIVE}
        await self.coordinator.async_write("PUT", f"/api/modes/{MODE_ENDPOINTS[target]}", content)


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
