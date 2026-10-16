"""Unread CC alarms as Home Assistant notifications, kept in sync both ways.

Each unread alarm becomes a persistent notification. When the alarm is read
elsewhere (the app, the button) or goes away, the notification is dismissed.
When someone dismisses the notification in Home Assistant, the alarm is marked
as read on the CC, as the app does when an alarm is opened.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .alarm_texts import ALARM_TEXTS
from .coordinator import DanfossLinkCoordinator

_LOGGER = logging.getLogger(__name__)

# Labels the app puts in front of the room and device (system_status.*).
_LABELS = {
    "en": {"room": "Room:", "node": "Device:", "dismiss": "Dismiss to mark it as read."},
    "da": {"room": "Rum:", "node": "Enhed:", "dismiss": "Afvis for at markere den som læst."},
}


def unread_alarms(alarms: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [alarm for alarm in alarms if not alarm.get("isRead") and "id" in alarm]


def room_name(rooms: dict[int, dict[str, Any]], room: Any) -> Any:
    """The room's name; ``room`` may arrive as a number or a string."""
    try:
        return rooms.get(int(room), {}).get("name", room)
    except (TypeError, ValueError):
        return room


def alarm_text(language: str, text_id: str | None) -> str | None:
    if not text_id:
        return None
    texts = ALARM_TEXTS.get(language, ALARM_TEXTS["en"])
    return texts.get(text_id) or ALARM_TEXTS["en"].get(text_id) or text_id


class AlarmNotifier:
    """Mirrors the CC's unread alarms as persistent notifications."""

    def __init__(self, hass: HomeAssistant, coordinator: DanfossLinkCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._prefix = f"danfoss_link_{coordinator.config_entry.entry_id}_alarm_"
        # Alarm ids with a notification on screen.
        self._shown: set[int] = set()
        # Dismissed in HA, not yet read on the CC: don't show them again meanwhile.
        self._marking: set[int] = set()
        self._unsubscribe: list[Any] = []

    def _language(self) -> str:
        return "da" if self.hass.config.language.startswith("da") else "en"

    def _notification_id(self, alarm_id: int) -> str:
        return f"{self._prefix}{alarm_id}"

    @callback
    def async_start(self) -> None:
        self._unsubscribe.append(
            persistent_notification.async_register_callback(self.hass, self._on_notifications)
        )
        self._unsubscribe.append(self.coordinator.async_add_listener(self._on_update))
        self._on_update()

    @callback
    def async_stop(self) -> None:
        """Stop syncing and take our notifications down (they come back on setup)."""
        while self._unsubscribe:
            self._unsubscribe.pop()()
        for alarm_id in self._shown:
            persistent_notification.async_dismiss(self.hass, self._notification_id(alarm_id))
        self._shown.clear()

    @callback
    def _on_update(self) -> None:
        data = self.coordinator.data
        if data is None or not self.coordinator.last_update_success:
            return
        unread = {alarm["id"]: alarm for alarm in unread_alarms(data.alarms)}
        self._marking &= unread.keys()
        for alarm_id in self._shown - unread.keys():
            # Read in the app or cleared on the CC.
            self._shown.discard(alarm_id)
            persistent_notification.async_dismiss(self.hass, self._notification_id(alarm_id))
        for alarm_id, alarm in unread.items():
            if alarm_id in self._shown or alarm_id in self._marking:
                continue
            self._shown.add(alarm_id)
            title, message = self._render(alarm)
            persistent_notification.async_create(
                self.hass, message, title, self._notification_id(alarm_id)
            )

    def _render(self, alarm: dict[str, Any]) -> tuple[str, str]:
        language = self._language()
        labels = _LABELS[language]
        data = self.coordinator.data
        cc_name = data.info.get("name") or "Danfoss Link"
        title = alarm_text(language, alarm.get("titleTextId")) or alarm.get("level") or "Alarm"
        lines = []
        if description := alarm_text(language, alarm.get("descriptionTextId")):
            lines.append(description)
        if alarm.get("room") not in (None, ""):
            lines.append(f"{labels['room']} {room_name(data.rooms, alarm['room'])}")
        if alarm.get("node") not in (None, ""):
            lines.append(f"{labels['node']} {alarm['node']}")
        if created := alarm.get("createdDate"):
            lines.append(str(created).replace("T", " "))
        lines.append(f"_{labels['dismiss']}_")
        return f"{cc_name}: {title.rstrip('.')}", "\n\n".join(lines)

    @callback
    def _on_notifications(
        self,
        update_type: persistent_notification.UpdateType,
        notifications: dict[str, persistent_notification.Notification],
    ) -> None:
        if update_type is not persistent_notification.UpdateType.REMOVED:
            return
        dismissed = []
        for notification_id in notifications:
            if not notification_id.startswith(self._prefix):
                continue
            try:
                alarm_id = int(notification_id.removeprefix(self._prefix))
            except ValueError:
                continue
            # Ignore our own dismissals: those alarms are already read.
            if alarm_id in self._shown:
                self._shown.discard(alarm_id)
                self._marking.add(alarm_id)
                dismissed.append(alarm_id)
        if dismissed:
            self.coordinator.config_entry.async_create_task(
                self.hass, self._mark_read(dismissed), "danfoss_link_mark_alarms_read"
            )

    async def _mark_read(self, alarm_ids: list[int]) -> None:
        try:
            await self.coordinator.async_mark_alarms_read(alarm_ids)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not mark Danfoss Link alarms %s as read: %s", alarm_ids, err)
            # Show them again on the next update.
            self._marking.difference_update(alarm_ids)
