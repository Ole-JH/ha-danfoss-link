"""Constants for the Danfoss Link integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "danfoss_link"
MANUFACTURER: Final = "Danfoss"

CONF_CERT_PEM: Final = "cert_pem"
CONF_KEY_PEM: Final = "key_pem"
CONF_PEER_ID: Final = "peer_id"
CONF_CODE: Final = "code"

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 20

# Room regulation types used by the CC
REGULATION_SCHEDULE: Final = "Schedule"
REGULATION_MANUAL: Final = "Manual"
# Rooms in this zone share one regulation type and schedule, set on the zone.
ZONE_LIVING: Final = "Living"

# House modes and their REST endpoints (/api/modes/<endpoint>)
MODE_AT_HOME: Final = "AtHome"
MODE_ENDPOINTS: Final = {
    "AtHome": "athome",
    "Vacation": "vacation",
    "Pause": "pause",
    "Boost": "boost",
}
MODE_ACTIVE: Final = "Active"
MODE_INACTIVE: Final = "Inactive"
