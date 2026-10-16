"""Actions: plan and cancel a vacation, as in the app's vacation calendar."""

from __future__ import annotations

from datetime import datetime

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .cc_time import format_cc_time
from .const import DOMAIN, MODE_ACTIVE, MODE_ENDPOINTS, MODE_INACTIVE, MODE_VACATION
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START = "start"
ATTR_END = "end"

SERVICE_PLAN_VACATION = "plan_vacation"
SERVICE_CANCEL_VACATION = "cancel_vacation"

_ENTRY_SCHEMA = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
PLAN_VACATION_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required(ATTR_START): cv.datetime,
        vol.Required(ATTR_END): cv.datetime,
    }
)
CANCEL_VACATION_SCHEMA = vol.Schema(_ENTRY_SCHEMA)


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> DanfossLinkCoordinator:
    entries: list[DanfossLinkConfigEntry] = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    elif len(entries) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="choose_config_entry"
        )
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return entries[0].runtime_data


def _aware(value: datetime) -> datetime:
    # The datetime selector sends local time without a zone.
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return value


async def _plan_vacation(call: ServiceCall) -> None:
    coordinator = _coordinator(call.hass, call)
    start = _aware(call.data[ATTR_START]).replace(second=0, microsecond=0)
    end = _aware(call.data[ATTR_END]).replace(second=0, microsecond=0)
    # The same checks as the app's vacation calendar.
    if start >= end:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="end_before_start")
    if end <= dt_util.now():
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="end_in_past")
    info = coordinator.data.info
    await coordinator.async_write(
        "PUT",
        f"/api/modes/{MODE_ENDPOINTS[MODE_VACATION]}",
        {
            "type": MODE_VACATION,
            # The CC turns this into "Planned" until the start time.
            "state": MODE_ACTIVE,
            "startTime": format_cc_time(start, info),
            "endTime": format_cc_time(end, info),
        },
    )


async def _cancel_vacation(call: ServiceCall) -> None:
    coordinator = _coordinator(call.hass, call)
    await coordinator.async_write(
        "PUT",
        f"/api/modes/{MODE_ENDPOINTS[MODE_VACATION]}",
        {"type": MODE_VACATION, "state": MODE_INACTIVE},
    )


def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN, SERVICE_PLAN_VACATION, _plan_vacation, schema=PLAN_VACATION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_VACATION, _cancel_vacation, schema=CANCEL_VACATION_SCHEMA
    )
