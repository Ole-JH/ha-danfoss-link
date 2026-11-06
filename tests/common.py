"""Test helpers that work with both Home Assistant test packages.

Home Assistant 2026.11 ships its own ``pytest-homeassistant`` package. Older
releases are tested with ``pytest-homeassistant-custom-component``. Both expose
the same helpers, only under a different module name.
"""

try:
    from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
except ImportError:  # Home Assistant before 2026.11
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed,
    )

__all__ = ["MockConfigEntry", "async_fire_time_changed"]
