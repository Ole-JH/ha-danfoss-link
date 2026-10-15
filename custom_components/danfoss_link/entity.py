"""Base entities for Danfoss Link."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, ZONE_LIVING
from .coordinator import DanfossLinkCoordinator


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.peer_id}_room_{room_id}")},
            name=self.room.get("name") or f"Room {room_id}",
            manufacturer=MANUFACTURER,
            model="Link room",
            via_device=(DOMAIN, coordinator.peer_id),
        )

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
        if self.room.get("zone") == ZONE_LIVING:
            # The CC rejects this on a single living-zone room (HTTP 400).
            await self.coordinator.async_write(
                "PATCH",
                "/api/zones/living",
                {"name": ZONE_LIVING, "regulationType": regulation_type},
            )
            return
        await self.coordinator.async_write(
            "PATCH", self.room_uri, {"id": self.room_id, "regulation": {"type": regulation_type}}
        )
