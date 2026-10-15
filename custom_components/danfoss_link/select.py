"""House mode select (at home / vacation / pause / boost)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MODE_ACTIVE, MODE_AT_HOME, MODE_ENDPOINTS, MODE_INACTIVE
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity

PARALLEL_UPDATES = 1

# CC mode type -> option key (translated in strings.json)
_OPTIONS = {"AtHome": "at_home", "Vacation": "vacation", "Pause": "pause", "Boost": "boost"}
_TYPES = {v: k for k, v in _OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if any(mode.get("type") in _OPTIONS for mode in coordinator.data.modes):
        async_add_entities([DanfossLinkHouseMode(coordinator)])


class DanfossLinkHouseMode(DanfossLinkEntity, SelectEntity):
    """Which house mode the CC is in."""

    _attr_translation_key = "house_mode"

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "house_mode")

    @property
    def options(self) -> list[str]:
        types = {mode.get("type") for mode in self.coordinator.data.modes}
        return [option for mode_type, option in _OPTIONS.items() if mode_type in types]

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
    def current_option(self) -> str | None:
        active = self._active_type()
        return _OPTIONS[active] if active else None

    async def async_select_option(self, option: str) -> None:
        target = _TYPES[option]
        current = self._active_type()
        if current == target:
            return
        if current is not None and current != MODE_AT_HOME:
            await self.coordinator.async_write(
                "PUT",
                f"/api/modes/{MODE_ENDPOINTS[current]}",
                {"type": current, "state": MODE_INACTIVE},
            )
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
