"""Serve the schedule dashboard card and load it in the frontend."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CARD_FILE = Path(__file__).parent / "danfoss-link-schedule-card.js"
CARD_URL = "/danfoss_link/danfoss-link-schedule-card.js"


async def async_register_card(hass: HomeAssistant) -> None:
    """Make the card available on every dashboard, without a separate install."""
    if hass.http is None:  # e.g. in tests without the http component
        return
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    content = await hass.async_add_executor_job(CARD_FILE.read_bytes)
    # Bust the browser cache whenever the card changes.
    version = hashlib.sha256(content).hexdigest()[:8]
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)]
        )
    except RuntimeError:
        # Already registered (the integration was reloaded).
        return
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    _LOGGER.debug("Schedule card served at %s", CARD_URL)
