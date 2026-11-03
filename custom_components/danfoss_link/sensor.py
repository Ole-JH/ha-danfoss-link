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
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, MODE_PAUSE, MODE_VACATION, REGULATION_MANUAL, REGULATION_SCHEDULE
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity, DanfossLinkRoomEntity
from .schedule import schedule_attributes

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
    # Only CCs with an outdoor sensor (e.g. via an ECL controller) report it.
    if isinstance(coordinator.data.status.get("outdoorTemperature"), (int, float)):
        entities.append(DanfossLinkOutdoorTemperature(coordinator))
    for mode_type in (MODE_VACATION, MODE_PAUSE):
        if any(mode.get("type") == mode_type for mode in coordinator.data.modes):
            entities.extend(
                DanfossLinkModeTime(coordinator, mode_type, key) for key in ("startTime", "endTime")
            )
    if any("zone" in room for room in coordinator.data.rooms.values()):
        entities.append(DanfossLinkLivingZone(coordinator))
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


class DanfossLinkOutdoorTemperature(DanfossLinkEntity, SensorEntity):
    """Outdoor temperature (/api/system/status outdoorTemperature)."""

    _attr_translation_key = "outdoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "outdoor_temperature")

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.status.get("outdoorTemperature")
        return value if isinstance(value, (int, float)) else None


# CC system state (/api/system/status currentState) -> option key
_HOUSE_STATES = {
    "AtHome": "at_home",
    "Away": "away",
    "Asleep": "asleep",
    "Vacation": "vacation",
    "Pause": "pause",
    "Boost": "boost",
    # Seen live when the living zone is on Manual.
    "Manual": "manual",
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


class DanfossLinkModeTime(DanfossLinkEntity, SensorEntity):
    """Start or end of a planned or running vacation or pause; unknown when there is none."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DanfossLinkCoordinator, mode_type: str, key: str) -> None:
        self._mode_type = mode_type
        self._key = key
        # vacation_start, vacation_end, pause_start, pause_end
        self._attr_translation_key = (
            f"{mode_type.lower()}_{'start' if key == 'startTime' else 'end'}"
        )
        super().__init__(coordinator, self._attr_translation_key)

    @property
    def native_value(self) -> datetime | None:
        _, start, end = self.coordinator.mode_info(self._mode_type)
        return start if self._key == "startTime" else end


class DanfossLinkLivingZone(DanfossLinkEntity, SensorEntity):
    """Number of rooms in the living zone; the rooms, mode and schedule as attributes.

    The living zone's rooms share one Schedule/Manual setting and one weekly
    schedule. Which rooms are in it can only be changed on the CC's display:
    the CC's PATCH schemas for rooms and for the zone have no such field
    (tried live, see docs/protocol.md).
    """

    _attr_translation_key = "living_zone"

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "living_zone")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # The climate entities may be registered after this sensor (or
        # renamed): show their entity ids once they are known.
        @callback
        def _climate_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
            if event.data["entity_id"].startswith("climate."):
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _climate_changed)
        )

    @property
    def native_value(self) -> int:
        return len(self.coordinator.living_zone_rooms())

    def _climate_entity_id(self, room_id: int) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            "climate", DOMAIN, f"{self.coordinator.peer_id}_room_{room_id}_climate"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rooms = self.coordinator.data.rooms
        members = self.coordinator.living_zone_rooms()
        others = [room_id for room_id in rooms if room_id not in members]
        first = rooms[members[0]] if members else {}
        regulation = (first.get("regulation") or {}).get("type")
        return {
            "rooms": [rooms[room_id].get("name") for room_id in members],
            "entity_id": [self._climate_entity_id(room_id) for room_id in members],
            "other_entity_id": [self._climate_entity_id(room_id) for room_id in others],
            # The climate entities' hvac mode: auto follows the schedule.
            "hvac_mode": {REGULATION_SCHEDULE: "auto", REGULATION_MANUAL: "heat"}.get(regulation),
            **schedule_attributes(first, self.coordinator.data.info),
        }
