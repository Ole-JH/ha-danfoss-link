"""Base entities for Danfoss Link."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, REGULATION_SCHEDULE, ZONE_LIVING
from .coordinator import DanfossLinkCoordinator

# HA 2026.10 replaced `via_device` (identifier tuple) with `via_device_id`
# (device registry id); `via_device` stops working in 2027.8.
_HAS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def controller_device_info(coordinator: DanfossLinkCoordinator) -> DeviceInfo:
    info = coordinator.data.info
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.peer_id)},
        name=info.get("name") or "Danfoss Link CC",
        manufacturer=MANUFACTURER,
        model="Link CC",
        sw_version=info.get("version"),
        serial_number=info.get("serialNo"),
    )


class DanfossLinkEntity(CoordinatorEntity[DanfossLinkCoordinator]):
    """An entity that belongs to the central controller."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DanfossLinkCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.peer_id}_{key}"
        self._attr_device_info = controller_device_info(coordinator)


class DanfossLinkRoomEntity(CoordinatorEntity[DanfossLinkCoordinator]):
    """An entity that belongs to one room; each room is its own device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int, key: str) -> None:
        super().__init__(coordinator)
        self.room_id = room_id
        self._attr_unique_id = f"{coordinator.peer_id}_room_{room_id}_{key}"
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.peer_id}_room_{room_id}")},
            name=self.room.get("name") or f"Room {room_id}",
            manufacturer=MANUFACTURER,
            model="Link room",
        )
        if _HAS_VIA_DEVICE_ID:
            if coordinator.controller_device_id is not None:
                device_info["via_device_id"] = coordinator.controller_device_id
        else:
            device_info["via_device"] = (DOMAIN, coordinator.peer_id)
        self._attr_device_info = device_info

    @property
    def room(self) -> dict[str, Any]:
        return self.coordinator.data.rooms.get(self.room_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.room_id in self.coordinator.data.rooms

    @property
    def room_uri(self) -> str:
        return f"/api/rooms/{self.room_id}"

    async def async_set_regulation_type(self, regulation_type: str) -> None:
        """Switch between Schedule and Manual; living-zone rooms switch as a zone."""
        coordinator = self.coordinator
        if self.room.get("zone") == ZONE_LIVING:
            # The CC rejects this on a single living-zone room (HTTP 400).
            rooms = coordinator.living_zone_rooms()
            await coordinator.async_send(
                "PATCH",
                "/api/zones/living",
                {"name": ZONE_LIVING, "regulationType": regulation_type},
            )
        else:
            rooms = [self.room_id]
            await coordinator.async_send(
                "PATCH",
                self.room_uri,
                {"id": self.room_id, "regulation": {"type": regulation_type}},
            )
        # Switching to Manual drops the rooms from an active "home now"
        # override, and the CC does not add them back on Schedule: do that
        # here, so the rooms follow the house mode again. Send every room, as
        # the House mode select does: with only some rooms the CC ends the
        # override at the next schedule period instead of keeping it.
        at_home = coordinator.active_at_home()
        if (
            regulation_type == REGULATION_SCHEDULE
            and at_home is not None
            and not set(at_home.get("rooms") or []).issuperset(rooms)
        ):
            await coordinator.async_start_at_home(sorted(coordinator.data.rooms), restart=True)
        coordinator.forget_setpoints()
        await coordinator.async_refresh()
