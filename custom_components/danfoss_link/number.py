"""Number entities for the comfort/away setpoints that the schedule switches between."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity, DanfossLinkRoomEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class RoomSetpointDescription(NumberEntityDescription):
    regulation_key: str


ROOM_SETPOINTS = (
    RoomSetpointDescription(
        key="at_home_setpoint",
        translation_key="at_home_setpoint",
        regulation_key="atHomeSetpoint",
    ),
    RoomSetpointDescription(
        key="away_setpoint",
        translation_key="away_setpoint",
        regulation_key="awaySetpoint",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        DanfossLinkRoomSetpoint(coordinator, room_id, description)
        for room_id, room in coordinator.data.rooms.items()
        for description in ROOM_SETPOINTS
        if description.regulation_key in (room.get("regulation") or {})
    ]
    if _vacation_setpoint(coordinator) is not None:
        entities.append(DanfossLinkVacationTemperature(coordinator))
    async_add_entities(entities)


class _SetpointNumber(NumberEntity):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG


class DanfossLinkRoomSetpoint(DanfossLinkRoomEntity, _SetpointNumber):
    """The at-home or away temperature of a room."""

    entity_description: RoomSetpointDescription

    def __init__(
        self,
        coordinator: DanfossLinkCoordinator,
        room_id: int,
        description: RoomSetpointDescription,
    ) -> None:
        super().__init__(coordinator, room_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return (self.room.get("regulation") or {}).get(self.entity_description.regulation_key)

    @property
    def native_min_value(self) -> float:
        return self.room.get("minSetpoint") or 5.0

    @property
    def native_max_value(self) -> float:
        return self.room.get("maxSetpoint") or 35.0

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(
            "PATCH",
            self.room_uri,
            {"id": self.room_id, "regulation": {self.entity_description.regulation_key: value}},
        )


def _vacation_setpoint(coordinator: DanfossLinkCoordinator) -> float | None:
    vacation: dict[str, Any] = (
        coordinator.data.temperature_settings.get("vacationTemperature") or {}
    )
    return vacation.get("setpoint")


class DanfossLinkVacationTemperature(DanfossLinkEntity, _SetpointNumber):
    """The temperature every room uses while vacation mode is active."""

    _attr_translation_key = "vacation_temperature"
    _attr_native_min_value = 5.0
    _attr_native_max_value = 35.0

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "vacation_temperature")

    @property
    def native_value(self) -> float | None:
        return _vacation_setpoint(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(
            "PATCH", "/api/settings/temperature", {"vacationTemperature": {"setpoint": value}}
        )
