"""Actions: plan and cancel a vacation (as in the app's vacation calendar), pause, set schedules."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, service
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .cc_time import format_cc_time
from .const import DOMAIN, MODE_ACTIVE, MODE_ENDPOINTS, MODE_INACTIVE, MODE_VACATION
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .schedule import WEEKDAYS, parse_period

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START = "start"
ATTR_END = "end"

SERVICE_PLAN_VACATION = "plan_vacation"
SERVICE_CANCEL_VACATION = "cancel_vacation"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_PAUSE = "pause"
ATTR_DURATION = "duration"
ATTR_DAYS = "days"
ATTR_AT_HOME = "at_home"

_ENTRY_SCHEMA = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
PLAN_VACATION_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required(ATTR_START): cv.datetime,
        vol.Required(ATTR_END): cv.datetime,
    }
)
CANCEL_VACATION_SCHEMA = vol.Schema(_ENTRY_SCHEMA)
SET_SCHEDULE_SCHEMA = {
    vol.Required(ATTR_DAYS): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)], vol.Length(min=1)),
    # Empty (or left out): away all day.
    vol.Optional(ATTR_AT_HOME, default=list): vol.All(cv.ensure_list, [cv.string]),
}
PAUSE_SCHEMA = {
    vol.Exclusive(ATTR_DURATION, "pause_end"): cv.positive_time_period,
    vol.Exclusive(ATTR_END, "pause_end"): cv.datetime,
}


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


async def _set_schedule(entity, call: ServiceCall) -> None:
    periods = []
    for text in call.data[ATTR_AT_HOME]:
        try:
            periods.append(parse_period(text))
        except ValueError:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_period",
                translation_placeholders={"period": str(text)},
            ) from None
    # Keep the week's order and send each day once.
    days = [day for day in WEEKDAYS if day in call.data[ATTR_DAYS]]
    await entity.async_set_schedule(days, periods)


async def _pause(entity, call: ServiceCall) -> None:
    # Only the house mode select can pause; the action targets it so the card
    # and automations name the CC by its entity.
    if not hasattr(entity, "async_pause"):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_house_mode")
    end: datetime | None = None
    if ATTR_DURATION in call.data:
        end = dt_util.now() + call.data[ATTR_DURATION]
    elif ATTR_END in call.data:
        end = _aware(call.data[ATTR_END])
        if end <= dt_util.now():
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="pause_end_in_past"
            )
    if end is not None:
        end = end.replace(second=0, microsecond=0)
    await entity.async_pause(end)


def async_setup_services(hass: HomeAssistant) -> None:
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_SCHEDULE,
        entity_domain=CLIMATE_DOMAIN,
        func=_set_schedule,
        schema=SET_SCHEDULE_SCHEMA,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_PAUSE,
        entity_domain=SELECT_DOMAIN,
        func=_pause,
        schema=PAUSE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PLAN_VACATION, _plan_vacation, schema=PLAN_VACATION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_VACATION, _cancel_vacation, schema=CANCEL_VACATION_SCHEMA
    )
