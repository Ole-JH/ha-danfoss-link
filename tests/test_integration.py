"""Home Assistant tests: config flow, setup and entities against the fake relay."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import functools
import json
from pathlib import Path
import shutil
from typing import Any
from unittest.mock import PropertyMock, patch

from homeassistant.components import persistent_notification
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN, SERVICE_SET_VALUE
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, ConfigFlowResult
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_SCAN_INTERVAL,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
import voluptuous as vol

from custom_components.danfoss_link import coordinator as coordinator_module
from custom_components.danfoss_link.api import (
    AppIdentity,
    DanfossLinkClient,
    pair_with_code,
    pairing as pairing_module,
)
from custom_components.danfoss_link.cc_time import clock_drift, parse_cc_time
from custom_components.danfoss_link.const import (
    CONF_CERT_PEM,
    CONF_CODE,
    CONF_KEY_PEM,
    CONF_PEER_ID,
    DOMAIN,
)
from custom_components.danfoss_link.diagnostics import async_get_config_entry_diagnostics

from .fake_mdg import FIXTURES, PAIR_ID, PAIR_PASSWORD, PAIRING_CODE, FakeMDG

PEER_ID = "e7:af:e3:77:61:90:0d:b5:1f:87:d9:7c:ea:da:10:1e:25:26:9b:a1"
CERT = (FIXTURES / "app_p256_cert.pem").read_text()
KEY = (FIXTURES / "app_p256_key.pem").read_text()
USER_INPUT = {CONF_CERT_PEM: CERT, CONF_KEY_PEM: KEY, CONF_PEER_ID: PEER_ID.replace(":", "_")}


@pytest.fixture
def fake(socket_enabled: None) -> Iterator[FakeMDG]:
    server = FakeMDG()
    client = functools.partial(
        DanfossLinkClient, hosts=("127.0.0.1",), port=server.control_port, timeout=5
    )
    with (
        patch("custom_components.danfoss_link.DanfossLinkClient", client),
        patch("custom_components.danfoss_link.config_flow.DanfossLinkClient", client),
    ):
        yield server
    server.close()


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="My home",
        unique_id=PEER_ID,
        data={CONF_CERT_PEM: CERT, CONF_KEY_PEM: KEY, CONF_PEER_ID: PEER_ID},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --------------------------------------------------------------- config flow


async def _start_credentials_flow(hass: HomeAssistant) -> ConfigFlowResult:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["pair", "credentials"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )


async def test_user_flow(hass: HomeAssistant, fake: FakeMDG) -> None:
    result = await _start_credentials_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My home"
    assert result["data"][CONF_PEER_ID] == PEER_ID
    assert result["result"].unique_id == PEER_ID
    assert result["result"].options == {"alarm_notifications": True}
    await hass.async_block_till_done()


async def test_user_flow_reads_files(hass: HomeAssistant, fake: FakeMDG, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "creds").mkdir()
    shutil.copy(FIXTURES / "app_p256_cert.pem", tmp_path / "creds" / "app_cert.pem")
    shutil.copy(FIXTURES / "app_p256_key.pem", tmp_path / "creds" / "app_key.pem")
    result = await _start_credentials_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CERT_PEM: "creds/app_cert.pem",
            CONF_KEY_PEM: str(tmp_path / "creds" / "app_key.pem"),
            CONF_PEER_ID: PEER_ID,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CERT_PEM].startswith("-----BEGIN CERTIFICATE-----")
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({CONF_KEY_PEM: (FIXTURES / "server_key.pem").read_text()}, "invalid_credentials"),
        ({CONF_PEER_ID: "e7:af"}, "invalid_credentials"),
        ({CONF_CERT_PEM: "/does/not/exist.pem"}, "invalid_credentials"),
    ],
)
async def test_user_flow_invalid(
    hass: HomeAssistant, fake: FakeMDG, change: dict, error: str
) -> None:
    result = await _start_credentials_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, **change}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_user_flow_cc_offline(hass: HomeAssistant, fake: FakeMDG) -> None:
    fake.place_call_result = 1
    result = await _start_credentials_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cc_offline"}

    # Recovers once the CC is back.
    fake.place_call_result = 0
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_user_flow_cannot_connect(hass: HomeAssistant, socket_enabled: None) -> None:
    client = functools.partial(DanfossLinkClient, hosts=("127.0.0.1",), port=1, timeout=1)
    with patch("custom_components.danfoss_link.config_flow.DanfossLinkClient", client):
        result = await _start_credentials_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_already_configured(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    result = await _start_credentials_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    other = {**USER_INPUT, CONF_PEER_ID: "00" * 20}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], other)
    assert result["reason"] == "wrong_cc"

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


async def test_options_flow(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120, "alarm_notifications": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_SCAN_INTERVAL: 120, "alarm_notifications": True}
    assert entry.runtime_data.update_interval.total_seconds() == 120


# ------------------------------------------------------------------- setup


async def test_setup_creates_entities(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    living = hass.states.get("climate.living_room")
    assert living.state == HVACMode.AUTO
    assert living.attributes["current_temperature"] == 20.6
    assert living.attributes[ATTR_TEMPERATURE] == 21.0
    assert living.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert living.attributes["max_temp"] == 28.0
    bathroom = hass.states.get("climate.bathroom")
    assert bathroom.state == HVACMode.HEAT
    assert bathroom.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    # Weekly schedule: the at home periods; away (setback) fills the rest.
    week = living.attributes["schedule"]
    assert week["monday"] == ["06:00-08:00", "16:00-22:30"]
    assert week["sunday"] == ["07:30-24:00"]
    assert living.attributes["schedule_today"] in (week["monday"], week["sunday"])
    assert "schedule" not in bathroom.attributes
    # Schedules come from api/rooms/{id}, read once and then hourly, not every poll.
    await entry.runtime_data.async_refresh()
    assert [r for r in fake.requests if r[1] == "/api/rooms/0"] == [("GET", "/api/rooms/0", None)]

    assert hass.states.get("sensor.living_room_temperature").state == "20.6"
    alarms = hass.states.get("sensor.my_home_unread_alarms")
    assert alarms.state == "1"
    assert alarms.attributes["alarms"][0]["room"] == "Bathroom"
    house_mode = hass.states.get("select.my_home_house_mode")
    assert house_mode.state == "schedule"
    assert house_mode.attributes["options"] == ["schedule", "at_home", "vacation", "pause"]
    assert hass.states.get("sensor.my_home_house_state").state == "at_home"
    assert hass.states.get("select.living_room_mode").state == "schedule"
    assert hass.states.get("select.bathroom_mode").state == "manual"
    assert hass.states.get("switch.my_home_towel_rail").state == "off"
    assert hass.states.get("number.living_room_at_home_temperature").state == "21.0"
    assert hass.states.get("number.bathroom_away_temperature").state == "19.0"
    assert hass.states.get("number.my_home_vacation_temperature").state == "15.0"

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    cc = next(d for d in devices if d.identifiers == {(DOMAIN, PEER_ID)})
    assert cc.sw_version == "4.2.1797"
    assert len(devices) == 3  # the CC and two rooms
    for room in (d for d in devices if d.id != cc.id):
        assert room.via_device_id == cc.id

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostic_entities(hass: HomeAssistant, fake: FakeMDG) -> None:
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        PropertyMock(return_value=True),
    ):
        await _setup(hass)
    assert hass.states.get("binary_sensor.bathroom_battery").state == "on"
    assert hass.states.get("binary_sensor.bathroom_battery").attributes["battery_state"] == "low"
    assert hass.states.get("binary_sensor.living_room_battery").state == "off"
    assert hass.states.get("binary_sensor.bathroom_radio_connection").state == "on"
    assert hass.states.get("sensor.my_home_wi_fi_signal").state == "-61"


async def test_living_zone_sensor(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    assert hass.states.get("binary_sensor.living_room_living_zone").state == "on"
    assert hass.states.get("binary_sensor.bathroom_living_zone").state == "off"


LIVING_ZONE = "sensor.my_home_living_zone"


async def test_living_zone(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    zone = hass.states.get(LIVING_ZONE)
    assert zone.state == "1"
    assert zone.attributes["unit_of_measurement"] == "rooms"
    assert zone.attributes["rooms"] == ["Living room"]
    assert zone.attributes["entity_id"] == ["climate.living_room"]
    assert zone.attributes["other_entity_id"] == ["climate.bathroom"]
    assert zone.attributes["hvac_mode"] == "auto"
    assert zone.attributes["schedule"]["monday"] == ["06:00-08:00", "16:00-22:30"]

    # Switching a member to Manual switches the zone, and the sensor follows.
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: "climate.living_room", ATTR_HVAC_MODE: HVACMode.HEAT},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(LIVING_ZONE).attributes["hvac_mode"] == "heat"

    # Membership is set on the CC's display; the sensor follows the next poll.
    fake.state["/api/rooms"][1]["zone"] = "Living"
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.async_refresh()
    await hass.async_block_till_done()
    zone = hass.states.get(LIVING_ZONE)
    assert zone.state == "2"
    assert zone.attributes["entity_id"] == ["climate.living_room", "climate.bathroom"]
    assert zone.attributes["other_entity_id"] == []


async def test_diagnostic_entities_disabled_by_default(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    registry = er.async_get(hass)
    for entity_id in (
        "binary_sensor.bathroom_battery",
        "binary_sensor.bathroom_radio_connection",
        "sensor.my_home_wi_fi_signal",
    ):
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_retries_when_cc_offline(hass: HomeAssistant, fake: FakeMDG) -> None:
    fake.place_call_result = 1
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PEER_ID,
        data={CONF_CERT_PEM: CERT, CONF_KEY_PEM: KEY, CONF_PEER_ID: PEER_ID},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_short_relay_outage_keeps_entities_available(
    hass: HomeAssistant, fake: FakeMDG
) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    start = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    fake.place_call_result = 1  # the CC dropped off the relay
    fake.drop_connections()
    with patch.object(coordinator_module.dt_util, "utcnow", return_value=start):
        await coordinator.async_refresh()
    assert hass.states.get("climate.living_room").state == HVACMode.AUTO
    assert coordinator.update_interval == coordinator_module.OUTAGE_RETRY_INTERVAL

    later = start + coordinator_module.OUTAGE_GRACE_PERIOD
    with patch.object(coordinator_module.dt_util, "utcnow", return_value=later):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("climate.living_room").state == "unavailable"
    assert coordinator.update_interval.total_seconds() == 60

    fake.place_call_result = 0
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("climate.living_room").state == HVACMode.AUTO
    assert coordinator.update_interval.total_seconds() == 60


async def test_setup_with_broken_credentials(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PEER_ID,
        data={CONF_CERT_PEM: "junk", CONF_KEY_PEM: KEY, CONF_PEER_ID: PEER_ID},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


# ---------------------------------------------------------------- control


async def test_set_temperature(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: "climate.living_room", ATTR_TEMPERATURE: 19.5},
        blocking=True,
    )
    assert ("PATCH", "/api/rooms/0", {"id": 0, "setpoint": {"temperature": 19.5}}) in (
        fake.requests
    )
    await hass.async_block_till_done()
    assert hass.states.get("climate.living_room").attributes[ATTR_TEMPERATURE] == 19.5


def _setpoints_sent(fake: FakeMDG, room_id: int) -> list[float]:
    return [
        content["setpoint"]["temperature"]
        for method, uri, content in fake.requests
        if method == "PATCH" and uri == f"/api/rooms/{room_id}" and "setpoint" in content
    ]


async def test_rapid_setpoint_presses(hass: HomeAssistant, fake: FakeMDG) -> None:
    """+/- pressed quickly: each press shows at once, and only the last waits to be sent."""
    await _setup(hass)
    fake.write_delay = 0.3
    entity = "climate.living_room"
    shown: list[float] = []

    @callback
    def record(event: Any) -> None:
        state = event.data["new_state"]
        if event.data["entity_id"] == entity and state is not None:
            shown.append(state.attributes.get(ATTR_TEMPERATURE))

    unsub = hass.bus.async_listen("state_changed", record)
    calls = []
    for temperature in (21.5, 22.0, 22.5, 23.0):
        calls.append(
            hass.async_create_task(
                hass.services.async_call(
                    CLIMATE_DOMAIN,
                    SERVICE_SET_TEMPERATURE,
                    {ATTR_ENTITY_ID: entity, ATTR_TEMPERATURE: temperature},
                    blocking=True,
                )
            )
        )
        await asyncio.sleep(0.05)
        assert hass.states.get(entity).attributes[ATTR_TEMPERATURE] == temperature
    await asyncio.gather(*calls)
    # The first press went out at once; the two in between were replaced
    # before they were sent.
    assert _setpoints_sent(fake, 0) == [21.5, 23.0]
    # The CC is read back once, after the presses, and the display never moved back.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity).attributes[ATTR_TEMPERATURE] == 23.0
    unsub()
    assert shown == sorted(shown)
    assert fake.state["/api/rooms"][0]["setpoint"]["temperature"] == 23.0


async def test_rapid_at_home_temperature_presses(hass: HomeAssistant, fake: FakeMDG) -> None:
    """The same for the at-home temperature number (a tile card with +/- buttons)."""
    await _setup(hass)
    fake.write_delay = 0.3
    entity = "number.living_room_at_home_temperature"
    calls = []
    for value in (22.5, 23.0, 23.5):
        calls.append(
            hass.async_create_task(
                hass.services.async_call(
                    NUMBER_DOMAIN,
                    SERVICE_SET_VALUE,
                    {ATTR_ENTITY_ID: entity, "value": value},
                    blocking=True,
                )
            )
        )
        await asyncio.sleep(0.05)
        assert float(hass.states.get(entity).state) == value
    await asyncio.gather(*calls)
    sent = [
        content["regulation"]["atHomeSetpoint"]
        for method, uri, content in fake.requests
        if method == "PATCH" and uri == "/api/rooms/0" and "regulation" in content
    ]
    assert sent == [22.5, 23.5]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    assert float(hass.states.get(entity).state) == 23.5
    assert fake.state["/api/rooms"][0]["regulation"]["atHomeSetpoint"] == 23.5


async def test_stale_poll_keeps_new_setpoint(hass: HomeAssistant, fake: FakeMDG, freezer) -> None:
    """A poll that still has the old setpoint does not move the display back, for a while."""
    entry = await _setup(hass)
    fake.ignore_setpoints = True
    entity = "climate.living_room"
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity, ATTR_TEMPERATURE: 19.5},
        blocking=True,
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity).attributes[ATTR_TEMPERATURE] == 19.5
    # The CC still reports 21 half a minute later: show what it has.
    freezer.tick(timedelta(seconds=31))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity).attributes[ATTR_TEMPERATURE] == 21.0


async def test_rejected_setpoint_shows_cc_value(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    fake.reject_writes = True
    entity = "climate.living_room"
    with pytest.raises(HomeAssistantError, match="HTTP 400"):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: entity, ATTR_TEMPERATURE: 19.5},
            blocking=True,
        )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity).attributes[ATTR_TEMPERATURE] == 21.0


async def test_set_hvac_mode(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: "climate.living_room", ATTR_HVAC_MODE: HVACMode.HEAT},
        blocking=True,
    )
    # The living room is in the living zone, so the change goes to the zone.
    assert (
        "PATCH",
        "/api/zones/living",
        {"name": "Living", "regulationType": "Manual"},
    ) in fake.requests
    await hass.async_block_till_done()
    assert hass.states.get("climate.living_room").state == HVACMode.HEAT


async def test_room_mode_select(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    entity = "select.bathroom_mode"
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: entity, "option": "schedule"},
        blocking=True,
    )  # fmt: skip
    assert ("PATCH", "/api/rooms/1", {"id": 1, "regulation": {"type": "Schedule"}}) in (
        fake.requests
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "schedule"
    assert hass.states.get("climate.bathroom").state == HVACMode.AUTO


async def test_rejected_write_raises(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    fake.reject_writes = True
    with pytest.raises(HomeAssistantError, match="HTTP 400"):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: "number.my_home_vacation_temperature", "value": 14},
            blocking=True,
        )


async def test_house_mode(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    entity = "select.my_home_house_mode"
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: entity, "option": "vacation"},
        blocking=True,
    )  # fmt: skip
    assert ("PUT", "/api/modes/vacation", {"type": "Vacation", "state": "Active"}) in (
        fake.requests
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "vacation"

    fake.requests.clear()
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: entity, "option": "at_home"},
        blocking=True,
    )  # fmt: skip
    writes = [r for r in fake.requests if r[0] != "GET"]
    assert writes == [
        ("PUT", "/api/modes/vacation", {"type": "Vacation", "state": "Inactive"}),
        ("PUT", "/api/modes/athome", {"type": "AtHome", "state": "Active", "rooms": [0, 1]}),
    ]
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "at_home"

    fake.requests.clear()
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: entity, "option": "schedule"},
        blocking=True,
    )  # fmt: skip
    writes = [r for r in fake.requests if r[0] != "GET"]
    assert writes == [("PUT", "/api/modes/athome", {"type": "AtHome", "state": "Inactive"})]
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "schedule"


async def test_room_back_on_schedule_rejoins_at_home(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: house, "option": "at_home"},
        blocking=True,
    )  # fmt: skip
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["at_home_rooms"] == ["Living room", "Bathroom"]

    # Manual takes the living zone out of the override on the CC ...
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: "climate.living_room", ATTR_HVAC_MODE: HVACMode.HEAT}, blocking=True,
    )  # fmt: skip
    await hass.async_block_till_done()
    assert hass.states.get(house).state == "at_home"
    assert hass.states.get(house).attributes["at_home_rooms"] == ["Bathroom"]

    # ... and Schedule puts it back.
    fake.requests.clear()
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: "climate.living_room", ATTR_HVAC_MODE: HVACMode.AUTO}, blocking=True,
    )  # fmt: skip
    writes = [r for r in fake.requests if r[0] != "GET"]
    assert writes == [
        ("PATCH", "/api/zones/living", {"name": "Living", "regulationType": "Schedule"}),
        ("PUT", "/api/modes/athome", {"type": "AtHome", "state": "Inactive"}),
        ("PUT", "/api/modes/athome", {"type": "AtHome", "state": "Active", "rooms": [0, 1]}),
    ]
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["at_home_rooms"] == ["Living room", "Bathroom"]


async def test_schedule_without_at_home_sends_no_mode(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: "select.bathroom_mode", "option": "schedule"}, blocking=True,
    )  # fmt: skip
    assert not [r for r in fake.requests if r[0] != "GET" and r[1].startswith("/api/modes")]


async def test_reselecting_at_home_covers_all_rooms(hass: HomeAssistant, fake: FakeMDG) -> None:
    fake.state["/api/modes"][0] = {"type": "AtHome", "state": "Active", "rooms": [1]}
    await _setup(hass)
    house = "select.my_home_house_mode"
    assert hass.states.get(house).attributes["at_home_rooms"] == ["Bathroom"]
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: house, "option": "at_home"},
        blocking=True,
    )  # fmt: skip
    writes = [r for r in fake.requests if r[0] != "GET"]
    assert writes == [
        ("PUT", "/api/modes/athome", {"type": "AtHome", "state": "Inactive"}),
        ("PUT", "/api/modes/athome", {"type": "AtHome", "state": "Active", "rooms": [0, 1]}),
    ]

    # Already covering every room: nothing to send.
    fake.requests.clear()
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: house, "option": "at_home"},
        blocking=True,
    )  # fmt: skip
    assert not [r for r in fake.requests if r[0] != "GET"]


async def test_house_state_manual(hass: HomeAssistant, fake: FakeMDG) -> None:
    fake.state["/api/system/status"] = {"currentState": "Manual"}
    await _setup(hass)
    assert hass.states.get("sensor.my_home_house_state").state == "manual"


async def test_outdoor_temperature(hass: HomeAssistant, fake: FakeMDG) -> None:
    fake.state["/api/system/status"] = {"currentState": "AtHome", "outdoorTemperature": 8.9}
    entry = await _setup(hass)
    state = hass.states.get("sensor.my_home_outdoor_temperature")
    assert state.state == "8.9"
    assert state.attributes["device_class"] == "temperature"
    assert state.attributes["unit_of_measurement"] == "°C"

    fake.state["/api/system/status"]["outdoorTemperature"] = -3.5
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.my_home_outdoor_temperature").state == "-3.5"

    # A sensor that stops answering shows as unknown, not as a stale value.
    fake.state["/api/system/status"]["outdoorTemperature"] = None
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.my_home_outdoor_temperature").state == "unknown"


@pytest.mark.parametrize(
    "status",
    [{"currentState": "AtHome"}, {"currentState": "AtHome", "outdoorTemperature": None}],
)
async def test_no_outdoor_temperature(
    hass: HomeAssistant, fake: FakeMDG, status: dict[str, Any]
) -> None:
    """No outdoor sensor on the CC: no outdoor temperature entity."""
    fake.state["/api/system/status"] = status
    await _setup(hass)
    assert hass.states.get("sensor.my_home_outdoor_temperature") is None
    assert hass.states.get("sensor.my_home_house_state") is not None


async def test_switch_unavailable_while_node_dead(hass: HomeAssistant, fake: FakeMDG) -> None:
    fake.state["/api/alarms"].append(
        {"id": 9, "level": "Warning", "node": "FT-016|Towel rail", "isRead": True,
         "titleTextId": "IDS_UA1_ALERTVIEWER_NODE_DEAD_ALERT",
         "createdDate": "2026-05-21T03:21:16"}
    )  # fmt: skip
    entry = await _setup(hass)
    assert hass.states.get("switch.my_home_towel_rail").state == "unavailable"

    # The CC removes the alarm when the unit answers again.
    fake.state["/api/alarms"].pop()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.my_home_towel_rail").state == "off"


async def test_switch_and_numbers(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.my_home_towel_rail"},
        blocking=True,
    )  # fmt: skip
    assert ("PATCH", "/api/onoffdevices/0", {"id": 0, "state": "On"}) in fake.requests

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: "number.living_room_away_temperature", "value": 16.5},
        blocking=True,
    )
    assert ("PATCH", "/api/rooms/0", {"id": 0, "regulation": {"awaySetpoint": 16.5}}) in (
        fake.requests
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.my_home_towel_rail").state == "on"
    assert hass.states.get("number.living_room_away_temperature").state == "16.5"


def _alarm_notifications(hass: HomeAssistant) -> dict[str, dict]:
    return {
        key: value
        for key, value in persistent_notification._async_get_or_create_notifications(hass).items()
        if key.startswith("danfoss_link_")
    }


def _alarm_writes(fake: FakeMDG) -> list:
    return [r for r in fake.requests if r[0] == "PATCH" and r[1].startswith("/api/alarms/")]


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_alarm_notification(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    notifications = _alarm_notifications(hass)
    assert list(notifications) == [f"danfoss_link_{entry.entry_id}_alarm_3"]
    notification = notifications[f"danfoss_link_{entry.entry_id}_alarm_3"]
    assert notification["title"] == "My home: Low battery level"
    assert "Room: Bathroom" in notification["message"]
    assert "Device: 7" in notification["message"]

    # A new alarm shows up; the same one is not repeated.
    fake.state["/api/alarms"].append(
        {"id": 4, "level": "Error", "room": "0", "isRead": False,
         "titleTextId": "IDS_UA1_ALERTVIEWER_NODE_DEAD_ALERT"}
    )  # fmt: skip
    await _refresh(hass, entry)
    notifications = _alarm_notifications(hass)
    assert len(notifications) == 2
    assert "Room: Living room" in notifications[f"danfoss_link_{entry.entry_id}_alarm_4"]["message"]

    # Read in the app: the notification goes away without writing to the CC.
    fake.state["/api/alarms"][0]["isRead"] = True
    await _refresh(hass, entry)
    assert list(_alarm_notifications(hass)) == [f"danfoss_link_{entry.entry_id}_alarm_4"]
    assert not _alarm_writes(fake)

    # Marked unread in the app: it comes back.
    fake.state["/api/alarms"][0]["isRead"] = False
    await _refresh(hass, entry)
    assert len(_alarm_notifications(hass)) == 2

    await hass.config_entries.async_unload(entry.entry_id)
    assert not _alarm_notifications(hass)
    assert not _alarm_writes(fake)


async def test_alarm_notifications_can_be_turned_off(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    assert len(_alarm_notifications(hass)) == 1
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, "alarm_notifications": False}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert not _alarm_notifications(hass)
    fake.state["/api/alarms"].append({"id": 4, "isRead": False})
    await _refresh(hass, entry)
    assert not _alarm_notifications(hass)
    # The sensor and button still work.
    assert hass.states.get("sensor.my_home_unread_alarms").state == "2"
    assert not _alarm_writes(fake)


async def test_alarm_notification_danish(hass: HomeAssistant, fake: FakeMDG) -> None:
    hass.config.language = "da"
    entry = await _setup(hass)
    notification = _alarm_notifications(hass)[f"danfoss_link_{entry.entry_id}_alarm_3"]
    assert notification["title"] == "My home: Lavt batteriniveau"
    assert "Rum: Bathroom" in notification["message"]


async def test_dismissing_notification_marks_alarm_read(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    persistent_notification.async_dismiss(hass, f"danfoss_link_{entry.entry_id}_alarm_3")
    await hass.async_block_till_done()
    assert _alarm_writes(fake) == [("PATCH", "/api/alarms/3", {"isRead": True, "id": 3})]
    assert fake.state["/api/alarms"][0]["isRead"] is True
    assert hass.states.get("sensor.my_home_unread_alarms").state == "0"
    assert not _alarm_notifications(hass)


async def test_dismiss_all_notifications_marks_alarms_read(
    hass: HomeAssistant, fake: FakeMDG
) -> None:
    await _setup(hass)
    persistent_notification.async_create(hass, "Not ours", "Other", "other")
    persistent_notification.async_dismiss_all(hass)
    await hass.async_block_till_done()
    assert _alarm_writes(fake) == [("PATCH", "/api/alarms/3", {"isRead": True, "id": 3})]


async def test_failed_mark_read_shows_alarm_again(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    fake.reject_writes = True
    persistent_notification.async_dismiss(hass, f"danfoss_link_{entry.entry_id}_alarm_3")
    await hass.async_block_till_done()
    assert not _alarm_notifications(hass)
    await _refresh(hass, entry)
    assert len(_alarm_notifications(hass)) == 1


async def test_mark_alarms_read_button(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: "button.my_home_mark_alarms_as_read"},
        blocking=True,
    )  # fmt: skip
    await hass.async_block_till_done()
    assert _alarm_writes(fake) == [("PATCH", "/api/alarms/3", {"isRead": True, "id": 3})]
    assert hass.states.get("sensor.my_home_unread_alarms").state == "0"
    assert not _alarm_notifications(hass)

    # Nothing unread: nothing to send.
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: "button.my_home_mark_alarms_as_read"},
        blocking=True,
    )  # fmt: skip
    assert len(_alarm_writes(fake)) == 1


def _vacation_writes(fake: FakeMDG) -> list:
    return [r for r in fake.requests if r[0] == "PUT" and r[1] == "/api/modes/vacation"]


async def test_plan_vacation(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.my_home_vacation_start").state == "unknown"
    # Naive times from the datetime selector are HA local time (US/Pacific in tests).
    await hass.services.async_call(
        DOMAIN,
        "plan_vacation",
        {"start": "2030-07-01 12:00:00", "end": "2030-07-08 18:30:45"},
        blocking=True,
    )
    assert _vacation_writes(fake) == [
        (
            "PUT",
            "/api/modes/vacation",
            {
                "type": "Vacation",
                "state": "Active",
                "startTime": "2030-07-01T19:00:00Z",
                "endTime": "2030-07-09T01:30:00Z",
            },
        )
    ]
    await hass.async_block_till_done()
    # Planned, not active yet: the house keeps following its schedule.
    assert hass.states.get("select.my_home_house_mode").state == "schedule"
    assert hass.states.get("sensor.my_home_vacation_start").state == "2030-07-01T19:00:00+00:00"
    assert hass.states.get("sensor.my_home_vacation_end").state == "2030-07-09T01:30:00+00:00"

    fake.requests.clear()
    await hass.services.async_call(DOMAIN, "cancel_vacation", {}, blocking=True)
    assert _vacation_writes(fake) == [
        ("PUT", "/api/modes/vacation", {"type": "Vacation", "state": "Inactive"})
    ]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.my_home_vacation_start").state == "unknown"
    assert hass.states.get("sensor.my_home_vacation_end").state == "unknown"


async def test_plan_vacation_cc_local_time(hass: HomeAssistant, fake: FakeMDG) -> None:
    # A CC that reports its offset expects local time without a suffix.
    fake.state["/api/system/info"]["systemTime"] = "2026-10-16T07:00:00+02:00"
    await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "plan_vacation",
        {"start": "2030-07-01T10:00:00+00:00", "end": "2030-07-02T10:00:00+00:00"},
        blocking=True,
    )
    content = _vacation_writes(fake)[0][2]
    assert content["startTime"] == "2030-07-01T12:00:00"
    assert content["endTime"] == "2030-07-02T12:00:00"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.my_home_vacation_start").state == "2030-07-01T10:00:00+00:00"


def _mode_writes(fake: FakeMDG) -> list:
    return [r for r in fake.requests if r[0] == "PUT" and r[1].startswith("/api/modes/")]


async def test_pause_for_a_while(hass: HomeAssistant, fake: FakeMDG, freezer) -> None:
    freezer.move_to("2030-07-01T12:00:20+00:00")
    await _setup(hass)
    house = "select.my_home_house_mode"
    assert hass.states.get(house).attributes["pause_end"] is None
    assert hass.states.get(house).attributes["pause_temperature"] == 6.0
    assert hass.states.get("sensor.my_home_pause_end").state == "unknown"

    await hass.services.async_call(
        DOMAIN, "pause", {"duration": {"hours": 2}}, target={ATTR_ENTITY_ID: house}, blocking=True
    )
    assert _mode_writes(fake) == [
        (
            "PUT",
            "/api/modes/pause",
            {"type": "Pause", "state": "Active", "endTime": "2030-07-01T14:00:00Z"},
        )
    ]
    await hass.async_block_till_done()
    state = hass.states.get(house)
    assert state.state == "pause"
    assert state.attributes["pause_end"] == "2030-07-01T14:00:00+00:00"
    assert hass.states.get("sensor.my_home_pause_end").state == "2030-07-01T14:00:00+00:00"

    # Pausing again while paused only moves the end.
    fake.requests.clear()
    await hass.services.async_call(
        DOMAIN,
        "pause",
        {"end": "2030-07-01T16:30:00+00:00"},
        target={ATTR_ENTITY_ID: house},
        blocking=True,
    )
    assert _mode_writes(fake) == [
        (
            "PUT",
            "/api/modes/pause",
            {"type": "Pause", "state": "Active", "endTime": "2030-07-01T16:30:00Z"},
        )
    ]
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["pause_end"] == "2030-07-01T16:30:00+00:00"

    # Resuming is going back to the schedule.
    fake.requests.clear()
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: house, "option": "schedule"},
        blocking=True,
    )  # fmt: skip
    assert _mode_writes(fake) == [
        ("PUT", "/api/modes/pause", {"type": "Pause", "state": "Inactive"})
    ]
    await hass.async_block_till_done()
    assert hass.states.get(house).state == "schedule"
    assert hass.states.get(house).attributes["pause_end"] is None
    assert hass.states.get("sensor.my_home_pause_end").state == "unknown"


async def test_pause_until_resumed(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: house, "option": "vacation"},
        blocking=True,
    )  # fmt: skip
    fake.requests.clear()
    await hass.services.async_call(
        DOMAIN, "pause", {}, target={ATTR_ENTITY_ID: house}, blocking=True
    )
    # Another temporary mode is ended first; no end time means until resumed.
    assert _mode_writes(fake) == [
        ("PUT", "/api/modes/vacation", {"type": "Vacation", "state": "Inactive"}),
        ("PUT", "/api/modes/pause", {"type": "Pause", "state": "Active"}),
    ]
    await hass.async_block_till_done()
    assert hass.states.get(house).state == "pause"
    assert hass.states.get(house).attributes["pause_end"] is None


async def test_pause_replaces_at_home(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: house, "option": "at_home"},
        blocking=True,
    )  # fmt: skip
    fake.requests.clear()
    await hass.services.async_call(
        DOMAIN, "pause", {"duration": "00:30:00"}, target={ATTR_ENTITY_ID: house}, blocking=True
    )
    # The CC ends AtHome by itself when a pause starts (seen live).
    writes = _mode_writes(fake)
    assert [w[1] for w in writes] == ["/api/modes/pause"]
    assert "endTime" in writes[0][2]


async def test_pause_cc_local_time(hass: HomeAssistant, fake: FakeMDG) -> None:
    fake.state["/api/system/info"]["systemTime"] = "2026-10-16T07:00:00+01:00"
    await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        DOMAIN,
        "pause",
        {"end": "2030-01-05T10:00:00+00:00"},
        target={ATTR_ENTITY_ID: house},
        blocking=True,
    )
    assert _mode_writes(fake)[0][2]["endTime"] == "2030-01-05T11:00:00"
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["pause_end"] == "2030-01-05T11:00:00+01:00"


async def test_planned_pause(hass: HomeAssistant, fake: FakeMDG, freezer) -> None:
    freezer.move_to("2030-07-01T12:00:00+00:00")
    await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: house, "option": "vacation"},
        blocking=True,
    )  # fmt: skip
    fake.requests.clear()
    await hass.services.async_call(
        DOMAIN,
        "pause",
        {"start": "2030-07-01T18:00:00+00:00", "duration": {"hours": 3}},
        target={ATTR_ENTITY_ID: house},
        blocking=True,
    )
    # The CC cannot plan a pause: Home Assistant keeps it until the start.
    assert _mode_writes(fake) == []
    await hass.async_block_till_done()
    state = hass.states.get(house)
    assert state.state == "vacation"
    assert state.attributes["pause_state"] == "planned"
    assert state.attributes["pause_start"] == "2030-07-01T18:00:00+00:00"
    assert state.attributes["pause_end"] == "2030-07-01T21:00:00+00:00"
    assert hass.states.get("sensor.my_home_pause_start").state == "2030-07-01T18:00:00+00:00"

    freezer.move_to("2030-07-01T18:00:01+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _mode_writes(fake) == [
        ("PUT", "/api/modes/vacation", {"type": "Vacation", "state": "Inactive"}),
        (
            "PUT",
            "/api/modes/pause",
            {"type": "Pause", "state": "Active", "endTime": "2030-07-01T21:00:00Z"},
        ),
    ]
    state = hass.states.get(house)
    assert state.state == "pause"
    assert state.attributes["pause_state"] == "active"
    assert state.attributes["pause_end"] == "2030-07-01T21:00:00+00:00"


async def test_planned_pause_survives_restart(hass: HomeAssistant, fake: FakeMDG, freezer) -> None:
    freezer.move_to("2030-07-01T12:00:00+00:00")
    entry = await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        DOMAIN, "pause", {"start": "2030-07-01T18:00:00+00:00"}, target={ATTR_ENTITY_ID: house},
        blocking=True,
    )  # fmt: skip
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["pause_state"] == "planned"

    # Cancelling a plan sends nothing to the CC.
    await hass.services.async_call(
        DOMAIN, "cancel_pause", {}, target={ATTR_ENTITY_ID: house}, blocking=True
    )
    assert _mode_writes(fake) == []
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["pause_state"] is None
    assert hass.states.get("sensor.my_home_pause_start").state == "unknown"
    freezer.move_to("2030-07-01T19:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _mode_writes(fake) == []


async def test_planned_pause_retries_when_unreachable(
    hass: HomeAssistant, fake: FakeMDG, freezer
) -> None:
    freezer.move_to("2030-07-01T12:00:00+00:00")
    await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        DOMAIN, "pause", {"start": "2030-07-01T12:10:00+00:00"}, target={ATTR_ENTITY_ID: house},
        blocking=True,
    )  # fmt: skip
    fake.reject_writes = True
    freezer.move_to("2030-07-01T12:10:01+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["pause_state"] == "planned"
    fake.reject_writes = False
    freezer.move_to("2030-07-01T12:11:05+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(house).state == "pause"
    assert hass.states.get(house).attributes["pause_end"] is None


async def test_pause_start_in_past_pauses_now(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "pause",
        {"start": "2020-01-01 10:00:00"},
        target={ATTR_ENTITY_ID: "select.my_home_house_mode"},
        blocking=True,
    )
    assert _mode_writes(fake) == [("PUT", "/api/modes/pause", {"type": "Pause", "state": "Active"})]


async def test_mode_times_follow_cc_clock(hass: HomeAssistant, fake: FakeMDG, freezer) -> None:
    # Ole's CC ran 18 minutes fast: send and show times on its clock.
    freezer.move_to("2030-07-01T12:00:00+00:00")
    fake.state["/api/system/info"]["systemTime"] = "2030-07-01T14:18:30+02:00"
    await _setup(hass)
    house = "select.my_home_house_mode"
    await hass.services.async_call(
        DOMAIN, "pause", {"duration": {"minutes": 20}}, target={ATTR_ENTITY_ID: house},
        blocking=True,
    )  # fmt: skip
    assert _mode_writes(fake)[0][2]["endTime"] == "2030-07-01T14:38:00"
    await hass.async_block_till_done()
    assert hass.states.get(house).attributes["pause_end"] == "2030-07-01T14:20:00+02:00"
    await hass.services.async_call(
        DOMAIN,
        "plan_vacation",
        {"start": "2030-07-10T10:00:00+00:00", "end": "2030-07-12T10:00:00+00:00"},
        blocking=True,
    )
    assert _vacation_writes(fake)[-1][2]["startTime"] == "2030-07-10T12:18:00"
    await hass.async_block_till_done()
    attrs = hass.states.get(house).attributes
    assert attrs["vacation_state"] == "planned"
    assert attrs["vacation_start"] == "2030-07-10T12:00:00+02:00"
    assert attrs["vacation_temperature"] == 15.0


def test_clock_drift() -> None:
    now = datetime(2030, 7, 1, 12, 0, tzinfo=UTC)
    assert clock_drift({"systemTime": "2030-07-01T12:18:30Z"}, now) == timedelta(minutes=18)
    assert clock_drift({"systemTime": "2030-07-01T13:55:00+02:00"}, now) == timedelta(minutes=-5)
    # Hours off is a wrong clock, not drift; and no systemTime at all.
    assert clock_drift({"systemTime": "2030-07-01T15:00:00Z"}, now) == timedelta(0)
    assert clock_drift({}, now) == timedelta(0)


@pytest.mark.parametrize(
    ("entity", "data", "error"),
    [
        ("select.my_home_house_mode", {"end": "2020-01-01 12:00:00"}, "pause_end_in_past"),
        ("select.living_room_mode", {}, "not_house_mode"),
    ],
)
async def test_pause_invalid(
    hass: HomeAssistant, fake: FakeMDG, entity: str, data: dict, error: str
) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "pause", data, target={ATTR_ENTITY_ID: entity}, blocking=True
        )
    assert err.value.translation_key == error
    assert _mode_writes(fake) == []


async def test_pause_duration_and_end_exclusive(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "pause",
            {"duration": "01:00:00", "end": "2030-01-01 12:00:00"},
            target={ATTR_ENTITY_ID: "select.my_home_house_mode"},
            blocking=True,
        )


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        ("2030-07-08 12:00:00", "2030-07-01 12:00:00", "end_before_start"),
        ("2020-01-01 12:00:00", "2020-01-02 12:00:00", "end_in_past"),
    ],
)
async def test_plan_vacation_invalid(
    hass: HomeAssistant, fake: FakeMDG, start: str, end: str, error: str
) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "plan_vacation", {"start": start, "end": end}, blocking=True
        )
    assert err.value.translation_key == error
    assert _vacation_writes(fake) == []


def test_parse_cc_time() -> None:
    info = {"systemTime": "2026-10-16T05:00:00Z"}
    # UTC CCs: the app reads the "...Utc" keys first.
    mode = {"startTime": "2030-07-01T12:00:00", "startTimeUtc": "2030-07-01T10:00:00Z"}
    assert parse_cc_time(mode, "startTime", info) == datetime(2030, 7, 1, 10, tzinfo=UTC)
    local = {"systemTime": "2026-10-16T07:00:00+02:00"}
    assert parse_cc_time(mode, "startTime", local) == datetime(2030, 7, 1, 10, tzinfo=UTC)
    assert parse_cc_time({"endTime": "2100-01-01T00:00:00Z"}, "endTime", info) is None
    assert parse_cc_time({}, "endTime", info) is None


async def test_diagnostics_redacts_secrets(hass: HomeAssistant, fake: FakeMDG) -> None:
    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "BEGIN" not in text
    assert PEER_ID not in text
    assert "123456" not in text
    assert diag["connection"]["connected"] is True
    assert diag["data"]["rooms"][0]["name"] == "Living room"


# ------------------------------------------------------------ pairing flow


@pytest.fixture
def pairing(fake: FakeMDG) -> Iterator[FakeMDG]:
    pair = functools.partial(
        pair_with_code, hosts=("127.0.0.1",), port=fake.control_port, timeout=5
    )
    with (
        patch("custom_components.danfoss_link.config_flow.pair_with_code", pair),
        patch.object(pairing_module, "generate_certificate", fake.generate_trusted_certificate),
    ):
        yield fake


async def _start_pair_flow(hass: HomeAssistant) -> ConfigFlowResult:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pair"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    return result


async def test_pair_flow(hass: HomeAssistant, pairing: FakeMDG) -> None:
    result = await _start_pair_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: PAIRING_CODE, "alarm_notifications": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My home"
    assert result["result"].options == {"alarm_notifications": False}
    data = result["data"]
    assert data[CONF_PEER_ID] == pairing.cc_peer_id
    assert result["result"].unique_id == pairing.cc_peer_id
    identity = AppIdentity.from_pem(data[CONF_CERT_PEM], data[CONF_KEY_PEM], data[CONF_PEER_ID])
    pairing.wait_for_pairing()
    assert pairing.paired == [("Home Assistant", identity.cert_der)]
    assert ("GET", "/api/rooms", None) in pairing.requests
    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED
    assert hass.states.get("climate.living_room") is not None


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("0000-1234-5678", "lan_code"),
        ("123", "invalid_code"),
        ("99" + PAIR_ID + PAIR_PASSWORD, "code_expired"),
        (PAIR_ID + "00000", "wrong_code"),
    ],
)
async def test_pair_flow_errors(
    hass: HomeAssistant, pairing: FakeMDG, code: str, error: str
) -> None:
    result = await _start_pair_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: code})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert result["errors"] == {"base": error}


async def test_pair_flow_retries_without_repairing(hass: HomeAssistant, pairing: FakeMDG) -> None:
    pairing.place_call_result = 1
    with patch("custom_components.danfoss_link.config_flow.PAIRED_PROBE_DELAY", 0):
        result = await _start_pair_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CODE: PAIRING_CODE}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verify"
    assert result["errors"] == {"base": "cc_offline"}

    pairing.place_call_result = 0
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(pairing.paired) == 1
    await hass.async_block_till_done()


async def test_pair_flow_already_configured(hass: HomeAssistant, pairing: FakeMDG) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=pairing.cc_peer_id, data={})
    entry.add_to_hass(hass)
    result = await _start_pair_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: PAIRING_CODE}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def test_english_translation_matches_strings() -> None:
    base = Path(__file__).parent.parent / "custom_components" / "danfoss_link"
    strings = json.loads((base / "strings.json").read_text())
    assert json.loads((base / "translations" / "en.json").read_text()) == strings
    assert strings["entity"]["binary_sensor"]["living_zone"]["state"] == {"off": "No", "on": "Yes"}


def _schedule_writes(fake: FakeMDG) -> list[tuple[str, str, dict]]:
    return [r for r in fake.requests if r[0] == "PATCH" and "schedule" in (r[2] or {})]


async def test_set_schedule_room(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"days": ["tuesday", "monday"], "at_home": ["06:00-08:00", "16:00-22:30"]},
        target={ATTR_ENTITY_ID: "climate.bathroom"},
        blocking=True,
    )
    day = [
        {"startTimeFromMidnight": "00:00:00", "endTimeFromMidnight": "06:00:00", "state": "Away"},
        {"startTimeFromMidnight": "06:00:00", "endTimeFromMidnight": "08:00:00", "state": "AtHome"},
        {"startTimeFromMidnight": "08:00:00", "endTimeFromMidnight": "16:00:00", "state": "Away"},
        {"startTimeFromMidnight": "16:00:00", "endTimeFromMidnight": "22:30:00", "state": "AtHome"},
        {"startTimeFromMidnight": "22:30:00", "endTimeFromMidnight": "1.00:00:00", "state": "Away"},
    ]
    # All days in one PATCH: each round trip through the relay is slow.
    assert _schedule_writes(fake) == [
        ("PATCH", "/api/rooms/1", {"id": 1, "schedule": {"monday": day, "tuesday": day}}),
    ]
    # Shown without reading the rooms again.
    assert [r for r in fake.requests if r[:2] == ("GET", "/api/rooms/1")] == [
        ("GET", "/api/rooms/1", None)  # only the one at setup
    ]
    # Read back right away, not after the hourly schedule refresh.
    week = hass.states.get("climate.bathroom").attributes["schedule"]
    assert week["monday"] == week["tuesday"] == ["06:00-08:00", "16:00-22:30"]


async def test_set_schedule_living_zone(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"days": "saturday"},
        target={ATTR_ENTITY_ID: "climate.living_room"},
        blocking=True,
    )
    assert _schedule_writes(fake) == [
        (
            "PATCH",
            "/api/zones/living",
            {
                "name": "Living",
                "schedule": {
                    "saturday": [
                        {
                            "startTimeFromMidnight": "00:00:00",
                            "endTimeFromMidnight": "1.00:00:00",
                            "state": "Away",
                        }
                    ]
                },
            },
        )
    ]
    assert hass.states.get("climate.living_room").attributes["schedule"]["saturday"] == []


async def test_set_schedule_invalid_period(hass: HomeAssistant, fake: FakeMDG) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {"days": ["monday"], "at_home": ["06:10-08:00"]},
            target={ATTR_ENTITY_ID: "climate.bathroom"},
            blocking=True,
        )
    assert err.value.translation_key == "invalid_period"
    assert _schedule_writes(fake) == []


async def test_cards_registered(hass: HomeAssistant) -> None:
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    from custom_components.danfoss_link.frontend import (  # noqa: PLC0415
        CARD_URLS,
        async_register_card,
    )

    assert CARD_URLS == (
        "/danfoss_link/danfoss-link-schedule-card.js",
        "/danfoss_link/danfoss-link-away-card.js",
    )
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await async_register_card(hass)
    (paths,) = hass.http.async_register_static_paths.call_args.args
    assert [path.url_path for path in paths] == list(CARD_URLS)
    loaded = [call.args[1] for call in add_js.call_args_list]
    assert [url.split("?v=")[0] for url in loaded] == list(CARD_URLS)


async def test_set_schedule_falls_back_to_one_day_per_patch(
    hass: HomeAssistant, fake: FakeMDG
) -> None:
    await _setup(hass)
    original = fake._handle

    def handle(method, uri, content):
        if method == "PATCH" and len((content or {}).get("schedule", {})) > 1:
            fake.requests.append((method, uri, content))
            return 400, {"errorMessages": ["JSON content does not conform to JSON Schema."]}
        return original(method, uri, content)

    fake._handle = handle
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"days": ["monday", "sunday"], "at_home": ["06:00-08:00"]},
        target={ATTR_ENTITY_ID: "climate.living_room"},
        blocking=True,
    )
    assert [list(r[2]["schedule"]) for r in _schedule_writes(fake)] == [
        ["monday", "sunday"],
        ["monday"],
        ["sunday"],
    ]
    week = hass.states.get("climate.living_room").attributes["schedule"]
    assert week["monday"] == week["sunday"] == ["06:00-08:00"]
