"""Climate entities: one thermostat per Danfoss Link room."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import REGULATION_MANUAL, REGULATION_SCHEDULE
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkRoomEntity

PARALLEL_UPDATES = 1

# AUTO follows the room's weekly schedule, HEAT holds a manual setpoint.
_HVAC_TO_REGULATION = {HVACMode.AUTO: REGULATION_SCHEDULE, HVACMode.HEAT: REGULATION_MANUAL}
_REGULATION_TO_HVAC = {v: k for k, v in _HVAC_TO_REGULATION.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        DanfossLinkRoomClimate(coordinator, room_id) for room_id in coordinator.data.rooms
    )


class DanfossLinkRoomClimate(DanfossLinkRoomEntity, ClimateEntity):
    """A Danfoss Link room (radiator thermostats, floor heating, ...)."""

    _attr_name = None
    _attr_translation_key = "room"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "climate")

    @property
    def current_temperature(self) -> float | None:
        return self.room.get("ambientTemperature")

    @property
    def target_temperature(self) -> float | None:
        return (self.room.get("setpoint") or {}).get("temperature")

    @property
    def min_temp(self) -> float:
        return self.room.get("minSetpoint") or 5.0

    @property
    def max_temp(self) -> float:
        return self.room.get("maxSetpoint") or 35.0

    @property
    def hvac_mode(self) -> HVACMode | None:
        regulation = (self.room.get("regulation") or {}).get("type")
        return _REGULATION_TO_HVAC.get(regulation)

    @property
    def hvac_action(self) -> HVACAction | None:
        state = self.room.get("regulationState")
        if state is None:
            return None
        return HVACAction.HEATING if state == "Heating" else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.room
        regulation = room.get("regulation") or {}
        return {
            "regulation_state": room.get("regulationState"),
            "room_state": room.get("state"),
            "setpoint_type": (room.get("setpoint") or {}).get("type"),
            "at_home_setpoint": regulation.get("atHomeSetpoint"),
            "away_setpoint": regulation.get("awaySetpoint"),
            "manual_setpoint": regulation.get("manualSetpoint"),
            "zone": room.get("zone"),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.async_write(
            "PATCH",
            self.room_uri,
            {"id": self.room_id, "setpoint": {"temperature": float(temperature)}},
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.async_set_regulation_type(_HVAC_TO_REGULATION[hvac_mode])
