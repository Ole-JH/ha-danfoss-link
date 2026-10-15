"""Config flow for Danfoss Link."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
import voluptuous as vol

from .api import (
    AppIdentity,
    CannotConnect,
    DanfossLinkClient,
    DanfossLinkError,
    InvalidCredentials,
    PeerNotAvailable,
)
from .const import (
    CONF_CERT_PEM,
    CONF_KEY_PEM,
    CONF_PEER_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import DanfossLinkConfigEntry

_LOGGER = logging.getLogger(__name__)

_MULTILINE = TextSelector(TextSelectorConfig(multiline=True))


def _credentials_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CERT_PEM, default=defaults.get(CONF_CERT_PEM, "")): _MULTILINE,
            vol.Required(CONF_KEY_PEM, default=defaults.get(CONF_KEY_PEM, "")): _MULTILINE,
            vol.Required(CONF_PEER_ID, default=defaults.get(CONF_PEER_ID, "")): str,
        }
    )


def _read_pem(value: str, config_dir: str) -> str:
    """Return PEM text, reading it from a file if a path was given instead."""
    value = value.strip()
    if "-----BEGIN" in value:
        return value + "\n"
    path = Path(value)
    if not path.is_absolute():
        path = Path(config_dir) / path
    try:
        return path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as err:
        raise InvalidCredentials(f"Not PEM text and not a readable file: {value}") from err


def _probe(cert_pem: str, key_pem: str, peer_id: str) -> tuple[AppIdentity, dict[str, Any]]:
    identity = AppIdentity.from_pem(cert_pem, key_pem, peer_id)
    client = DanfossLinkClient(identity)
    try:
        info = client.get("/api/system/info") or {}
    finally:
        client.close()
    return identity, info


async def _validate(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str], str]:
    """Return (entry data, errors, title)."""

    def _load_and_probe() -> tuple[dict[str, str], str]:
        cert_pem = _read_pem(user_input[CONF_CERT_PEM], hass.config.config_dir)
        key_pem = _read_pem(user_input[CONF_KEY_PEM], hass.config.config_dir)
        identity, info = _probe(cert_pem, key_pem, user_input[CONF_PEER_ID])
        data = {CONF_CERT_PEM: cert_pem, CONF_KEY_PEM: key_pem, CONF_PEER_ID: identity.peer_id}
        return data, info.get("name") or "Danfoss Link"

    try:
        data, title = await hass.async_add_executor_job(_load_and_probe)
    except InvalidCredentials as err:
        _LOGGER.warning("Invalid Danfoss Link credentials: %s", err)
        return {}, {"base": "invalid_credentials"}, ""
    except PeerNotAvailable:
        return {}, {"base": "cc_offline"}, ""
    except (CannotConnect, OSError) as err:
        _LOGGER.warning("Cannot connect to the Danfoss Link CC: %s", err)
        return {}, {"base": "cannot_connect"}, ""
    except DanfossLinkError:
        _LOGGER.exception("Unexpected answer from the Danfoss Link CC")
        return {}, {"base": "unknown"}, ""
    return data, {}, title


class DanfossLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Danfoss Link CC using credentials exported from the app."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors, title = await _validate(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(data[CONF_PEER_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the credentials, e.g. after re-pairing the app."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors, _ = await _validate(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(data[CONF_PEER_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_cc")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(user_input or {CONF_PEER_ID: entry.data[CONF_PEER_ID]}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: DanfossLinkConfigEntry) -> OptionsFlow:
        return DanfossLinkOptionsFlow()


class DanfossLinkOptionsFlow(OptionsFlow):
    """Polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=3600,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )
