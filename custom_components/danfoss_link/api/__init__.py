"""Client for Danfoss Link central controllers via the Danfoss MDG cloud relay."""

from .client import DanfossLinkClient
from .credentials import AppIdentity, normalize_peer_id
from .errors import (
    CannotConnect,
    DanfossLinkError,
    InvalidCredentials,
    InvalidPairingCode,
    LanPairingNotSupported,
    PairingCodeExpired,
    PairingError,
    PairingTimeout,
    PeerNotAvailable,
    ProtocolError,
    RequestError,
    WrongPairingCode,
)
from .pairing import pair_with_code

__all__ = [
    "AppIdentity",
    "CannotConnect",
    "DanfossLinkClient",
    "DanfossLinkError",
    "InvalidCredentials",
    "InvalidPairingCode",
    "LanPairingNotSupported",
    "PairingCodeExpired",
    "PairingError",
    "PairingTimeout",
    "PeerNotAvailable",
    "ProtocolError",
    "RequestError",
    "WrongPairingCode",
    "normalize_peer_id",
    "pair_with_code",
]
