"""Binary sensors: per-room battery and radio problems, read from the CC's alarms.

The CC does not report battery level or radio signal per thermostat; the app
only learns about them through alarms (``titleTextId`` is the app's string
resource name). These sensors are diagnostic and disabled by default.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkRoomEntity

PARALLEL_UPDATES = 0

ALARM_BATTERY_LOW = "IDS_UA1_ALERTVIEWER_BATTERY_LOW_ALERT"
ALARM_BATTERY_CRITICAL = "IDS_UA1_ALERTVIEWER_BATTERY_CRITICAL_ALERT"
ALARM_NODE_DEAD = "IDS_UA1_ALERTVIEWER_NODE_DEAD_ALERT"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for room_id in coordinator.data.rooms:
        entities.append(DanfossLinkRoomBattery(coordinator, room_id))
        entities.append(DanfossLinkRoomConnectivity(coordinator, room_id))
    async_add_entities(entities)


class _RoomAlarmSensor(DanfossLinkRoomEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def _alarms(self, *titles: str) -> list[dict[str, Any]]:
        # The CC sends the room id as a number, the app reads it as a string.
        room = str(self.room_id)
        return [
            alarm
            for alarm in self.coordinator.data.alarms
            if str(alarm.get("room")) == room and alarm.get("titleTextId") in titles
        ]


class DanfossLinkRoomBattery(_RoomAlarmSensor):
    """On when a thermostat in the room reports a low or critical battery."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "battery_low")

    @property
    def is_on(self) -> bool:
        return bool(self._alarms(ALARM_BATTERY_LOW, ALARM_BATTERY_CRITICAL))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        critical = bool(self._alarms(ALARM_BATTERY_CRITICAL))
        low = bool(self._alarms(ALARM_BATTERY_LOW))
        return {"battery_state": "critical" if critical else "low" if low else "ok"}


class DanfossLinkRoomConnectivity(_RoomAlarmSensor):
    """Off when the CC has lost radio contact with a device in the room."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "radio"

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "radio")

    @property
    def is_on(self) -> bool:
        return not self._alarms(ALARM_NODE_DEAD)
