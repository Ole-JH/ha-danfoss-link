"""Home Assistant tests: config flow, setup and entities against the fake relay."""

from __future__ import annotations

from collections.abc import Iterator
import functools
import shutil
from unittest.mock import PropertyMock, patch

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
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.danfoss_link.api import (
    AppIdentity,
    DanfossLinkClient,
    pair_with_code,
    pairing as pairing_module,
)
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
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_SCAN_INTERVAL: 120}
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
        result["flow_id"], {CONF_CODE: PAIRING_CODE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My home"
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
