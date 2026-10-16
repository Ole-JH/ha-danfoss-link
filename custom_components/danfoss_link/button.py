"""Button: mark all unread alarms as read."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .alarms import unread_alarms
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DanfossLinkMarkAlarmsRead(entry.runtime_data)])


class DanfossLinkMarkAlarmsRead(DanfossLinkEntity, ButtonEntity):
    """Marks every unread alarm as read, which also clears their notifications."""

    _attr_translation_key = "mark_alarms_read"

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "mark_alarms_read")

    async def async_press(self) -> None:
        if alarm_ids := [alarm["id"] for alarm in unread_alarms(self.coordinator.data.alarms)]:
            await self.coordinator.async_mark_alarms_read(alarm_ids)
