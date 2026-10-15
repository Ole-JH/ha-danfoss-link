"""Sensors: room temperatures and controller alarms."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity, DanfossLinkRoomEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        DanfossLinkRoomTemperature(coordinator, room_id)
        for room_id, room in coordinator.data.rooms.items()
        if "ambientTemperature" in room
    ]
    entities.append(DanfossLinkAlarms(coordinator))
    async_add_entities(entities)


class DanfossLinkRoomTemperature(DanfossLinkRoomEntity, SensorEntity):
    """Measured temperature of a room, for history and statistics."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "temperature")

    @property
    def native_value(self) -> float | None:
        return self.room.get("ambientTemperature")


class DanfossLinkAlarms(DanfossLinkEntity, SensorEntity):
    """Number of unread alarms (low battery, lost device, ...)."""

    _attr_translation_key = "alarms"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "alarms")

    def _unread(self) -> list[dict[str, Any]]:
        return [a for a in self.coordinator.data.alarms if not a.get("isRead")]

    @property
    def native_value(self) -> int:
        return len(self._unread())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rooms = self.coordinator.data.rooms
        return {
            "alarms": [
                {
                    "id": alarm.get("id"),
                    "type": alarm.get("titleTextId"),
                    "level": alarm.get("level"),
                    "room": (rooms.get(alarm.get("room")) or {}).get("name", alarm.get("room")),
                    "node": alarm.get("node"),
                    "created": alarm.get("createdDate"),
                    "read": alarm.get("isRead"),
                }
                for alarm in self.coordinator.data.alarms
            ]
        }
