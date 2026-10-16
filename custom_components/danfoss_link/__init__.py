"""The Danfoss Link integration."""

from __future__ import annotations

from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .alarms import AlarmNotifier
from .api import AppIdentity, DanfossLinkClient, InvalidCredentials
from .const import (
    CONF_ALARM_NOTIFICATIONS,
    CONF_CERT_PEM,
    CONF_KEY_PEM,
    CONF_PEER_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> bool:
    """Set up Danfoss Link from a config entry."""
    try:
        identity = await hass.async_add_executor_job(
            AppIdentity.from_pem,
            entry.data[CONF_CERT_PEM],
            entry.data[CONF_KEY_PEM],
            entry.data[CONF_PEER_ID],
        )
    except InvalidCredentials as err:
        raise ConfigEntryError(f"Stored Danfoss Link credentials are invalid: {err}") from err

    coordinator = DanfossLinkCoordinator(
        hass,
        entry,
        DanfossLinkClient(identity),
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await coordinator.async_close()
        raise

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    if entry.options.get(CONF_ALARM_NOTIFICATIONS, True):
        notifier = AlarmNotifier(hass, coordinator)
        notifier.async_start()
        entry.async_on_unload(notifier.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> bool:
    """Unload a config entry and drop the cloud connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
