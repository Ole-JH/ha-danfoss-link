"""Serve the dashboard cards (schedule, pause) and load them in the frontend."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_FOLDER = Path(__file__).parent
CARD_FILES = (
    "danfoss-link-schedule-card.js",
    "danfoss-link-away-card.js",
)
URL_BASE = "/danfoss_link"
CARD_URLS = tuple(f"{URL_BASE}/{name}" for name in CARD_FILES)


async def async_register_card(hass: HomeAssistant) -> None:
    """Make the cards available on every dashboard, without a separate install."""
    if hass.http is None:  # e.g. in tests without the http component
        return
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    def _read() -> list[bytes]:
        return [(_FOLDER / name).read_bytes() for name in CARD_FILES]

    contents = await hass.async_add_executor_job(_read)
    try:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(url, str(_FOLDER / name), cache_headers=False)
                for url, name in zip(CARD_URLS, CARD_FILES, strict=True)
            ]
        )
    except RuntimeError:
        # Already registered (the integration was reloaded).
        return
    for url, content in zip(CARD_URLS, contents, strict=True):
        # Bust the browser cache whenever a card changes.
        version = hashlib.sha256(content).hexdigest()[:8]
        add_extra_js_url(hass, f"{url}?v={version}")
    _LOGGER.debug("Cards served at %s", ", ".join(CARD_URLS))
