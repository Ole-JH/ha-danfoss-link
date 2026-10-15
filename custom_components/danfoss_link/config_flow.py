"""Config flow for Danfoss Link."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
    InvalidPairingCode,
    LanPairingNotSupported,
    PairingCodeExpired,
    PairingTimeout,
    PeerNotAvailable,
    WrongPairingCode,
    pair_with_code,
)
from .const import (
    CONF_CERT_PEM,
    CONF_CODE,
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


def _probe(identity: AppIdentity) -> dict[str, Any]:
    """Connect to the CC and read its info and rooms; return the info."""
    client = DanfossLinkClient(identity)
    try:
        info = client.get("/api/system/info") or {}
        client.get("/api/rooms")
    finally:
        client.close()
    return info


# Returns the identity plus the certificate and key PEM text to store.
_Loader = Callable[[], tuple[AppIdentity, str, str]]

# After pairing, the CC needs a moment to tell the relay it accepts our calls.
PAIRED_PROBE_ATTEMPTS = 3
PAIRED_PROBE_DELAY = 5.0


async def _validate(
    hass: HomeAssistant,
    load: _Loader,
    *,
    attempts: int = 1,
) -> tuple[dict[str, str], dict[str, str], str]:
    """Load an identity and probe the CC with it. Return (entry data, errors, title)."""

    def _load_and_probe() -> tuple[dict[str, str], str]:
        identity, cert_pem, key_pem = load()
        info = _probe(identity)
        data = {CONF_CERT_PEM: cert_pem, CONF_KEY_PEM: key_pem, CONF_PEER_ID: identity.peer_id}
        return data, info.get("name") or "Danfoss Link"

    for attempt in range(1, attempts + 1):
        try:
            data, title = await hass.async_add_executor_job(_load_and_probe)
        except InvalidCredentials as err:
            _LOGGER.warning("Invalid Danfoss Link credentials: %s", err)
            return {}, {"base": "invalid_credentials"}, ""
        except PeerNotAvailable:
            if attempt < attempts:
                await asyncio.sleep(PAIRED_PROBE_DELAY)
                continue
            return {}, {"base": "cc_offline"}, ""
        except (CannotConnect, OSError) as err:
            _LOGGER.warning("Cannot connect to the Danfoss Link CC: %s", err)
            return {}, {"base": "cannot_connect"}, ""
        except DanfossLinkError:
            _LOGGER.exception("Unexpected answer from the Danfoss Link CC")
            return {}, {"base": "unknown"}, ""
        return data, {}, title
    raise AssertionError("unreachable")


def _credentials_loader(hass: HomeAssistant, user_input: dict[str, Any]) -> _Loader:
    def load() -> tuple[AppIdentity, str, str]:
        cert_pem = _read_pem(user_input[CONF_CERT_PEM], hass.config.config_dir)
        key_pem = _read_pem(user_input[CONF_KEY_PEM], hass.config.config_dir)
        identity = AppIdentity.from_pem(cert_pem, key_pem, user_input[CONF_PEER_ID])
        # Store the files exactly as exported.
        return identity, cert_pem, key_pem

    return load


_PAIR_ERRORS: dict[type[Exception], str] = {
    InvalidPairingCode: "invalid_code",
    LanPairingNotSupported: "lan_code",
    WrongPairingCode: "wrong_code",
    PairingCodeExpired: "code_expired",
    PairingTimeout: "pairing_timeout",
}


class DanfossLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Danfoss Link CC by pairing with its code, or with exported app credentials."""

    VERSION = 1

    def __init__(self) -> None:
        self._paired: AppIdentity | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["pair", "credentials"])

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pair a new identity using the code shown on the CC."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._paired = await self.hass.async_add_executor_job(
                    pair_with_code, user_input[CONF_CODE]
                )
            except tuple(_PAIR_ERRORS) as err:
                _LOGGER.info("Pairing with the Danfoss Link CC failed: %s", err)
                errors["base"] = _PAIR_ERRORS[type(err)]
            except (CannotConnect, OSError) as err:
                _LOGGER.warning("Cannot connect to the Danfoss cloud for pairing: %s", err)
                errors["base"] = "cannot_connect"
            except DanfossLinkError:
                _LOGGER.exception("Unexpected error while pairing with the Danfoss Link CC")
                errors["base"] = "unknown"
            else:
                return await self.async_step_verify()
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            errors=errors,
        )

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connect with the newly paired identity. Retrying does not pair again."""
        identity = self._paired
        assert identity is not None
        data, errors, title = await _validate(
            self.hass,
            lambda: (identity, identity.cert_pem, identity.key_pem),
            attempts=PAIRED_PROBE_ATTEMPTS,
        )
        if errors:
            return self.async_show_form(step_id="verify", errors=errors)
        await self.async_set_unique_id(data[CONF_PEER_ID])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=data)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use the identity exported from a paired Danfoss Link app."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors, title = await _validate(
                self.hass, _credentials_loader(self.hass, user_input)
            )
            if not errors:
                await self.async_set_unique_id(data[CONF_PEER_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="credentials",
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
            data, errors, _ = await _validate(self.hass, _credentials_loader(self.hass, user_input))
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
