"""Sensors: room temperatures, controller alarms and diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .cc_time import parse_cc_time
from .const import MODE_ACTIVE, MODE_PLANNED, MODE_VACATION
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
    if coordinator.data.status.get("currentState"):
        entities.append(DanfossLinkHouseState(coordinator))
    if any(mode.get("type") == MODE_VACATION for mode in coordinator.data.modes):
        entities.extend(
            DanfossLinkVacationTime(coordinator, key) for key in ("startTime", "endTime")
        )
    if "wifiSignalStrengthDb" in coordinator.data.info:
        entities.append(DanfossLinkWifiSignal(coordinator))
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


# CC system state (/api/system/status currentState) -> option key
_HOUSE_STATES = {
    "AtHome": "at_home",
    "Away": "away",
    "Asleep": "asleep",
    "Vacation": "vacation",
    "Pause": "pause",
    "Boost": "boost",
}


class DanfossLinkHouseState(DanfossLinkEntity, SensorEntity):
    """What the CC is doing now, e.g. the schedule's at home / away / asleep period."""

    _attr_translation_key = "house_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_HOUSE_STATES.values())

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "house_state")

    @property
    def native_value(self) -> str | None:
        # The app ignores anything after a comma ("AtHome,..." while overridden).
        state = (self.coordinator.data.status.get("currentState") or "").split(",")[0]
        return _HOUSE_STATES.get(state)


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


class DanfossLinkWifiSignal(DanfossLinkEntity, SensorEntity):
    """Wi-Fi signal strength of the CC (/api/system/info wifiSignalStrengthDb)."""

    _attr_translation_key = "wifi_signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.info.get("wifiSignalStrengthDb")


class DanfossLinkVacationTime(DanfossLinkEntity, SensorEntity):
    """Start or end of a planned or running vacation; unknown when there is none."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DanfossLinkCoordinator, key: str) -> None:
        self._key = key
        self._attr_translation_key = "vacation_start" if key == "startTime" else "vacation_end"
        super().__init__(coordinator, self._attr_translation_key)

    @property
    def native_value(self) -> datetime | None:
        for mode in self.coordinator.data.modes:
            if mode.get("type") == MODE_VACATION and mode.get("state") in (
                MODE_ACTIVE,
                MODE_PLANNED,
            ):
                return parse_cc_time(mode, self._key, self.coordinator.data.info)
        return None
