"""Diagnostics for Danfoss Link (credentials are redacted)."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_CERT_PEM, CONF_KEY_PEM, CONF_PEER_ID
from .coordinator import DanfossLinkConfigEntry

TO_REDACT = {CONF_CERT_PEM, CONF_KEY_PEM, CONF_PEER_ID, "serialNo"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DanfossLinkConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    client = coordinator.client
    cc_cert = client.cc_certificate
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "connection": {
            "connected": client.connected,
            "mdg_host": client.connected_host,
            "cc_certificate_sha256": hashlib.sha256(cc_cert).hexdigest() if cc_cert else None,
        },
        "data": async_redact_data(asdict(coordinator.data), TO_REDACT)
        if coordinator.data
        else None,
    }
