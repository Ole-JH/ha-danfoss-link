"""Switches for Danfoss Link on/off devices (e.g. Link plugs and relays)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        DanfossLinkOnOffDevice(coordinator, device_id)
        for device_id in coordinator.data.onoffdevices
    )


class DanfossLinkOnOffDevice(DanfossLinkEntity, SwitchEntity):
    """An on/off output controlled by the CC."""

    def __init__(self, coordinator: DanfossLinkCoordinator, device_id: int) -> None:
        super().__init__(coordinator, f"onoff_{device_id}")
        self.device_id = device_id
        self._attr_name = self._device.get("name") or f"On/off device {device_id}"

    @property
    def _device(self) -> dict[str, Any]:
        return self.coordinator.data.onoffdevices.get(self.device_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.device_id in self.coordinator.data.onoffdevices

    @property
    def is_on(self) -> bool | None:
        state = self._device.get("state")
        return None if state is None else state == "On"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self._device
        return {
            "regulation": device.get("regulation"),
            "activity_simulation": device.get("activitySimulationEnabled"),
        }

    async def _set(self, state: str) -> None:
        await self.coordinator.async_write(
            "PATCH",
            f"/api/onoffdevices/{self.device_id}",
            {"id": self.device_id, "state": state},
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set("On")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set("Off")
