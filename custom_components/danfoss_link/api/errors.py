"""Exceptions raised by the Danfoss Link client."""

from __future__ import annotations

from typing import Any


class DanfossLinkError(Exception):
    """Base class for all Danfoss Link client errors."""


class InvalidCredentials(DanfossLinkError):
    """The certificate, private key or peer ID cannot be used."""


class CannotConnect(DanfossLinkError):
    """The cloud relay or the CC could not be reached."""


class PeerNotAvailable(CannotConnect):
    """The relay accepted us, but the CC is offline or refused the call."""


class ProtocolError(CannotConnect):
    """The other side sent something we did not expect."""


class RoutingError(CannotConnect):
    """The MDG router refused to connect us to the peer."""

    def __init__(self, code: int | None) -> None:
        super().__init__(f"Routing error {code}")
        self.code = code


class PairingError(DanfossLinkError):
    """Pairing with the code shown on the CC failed."""


class InvalidPairingCode(PairingError):
    """The text entered is not a pairing code."""


class LanPairingNotSupported(PairingError):
    """The code is a LAN pairing code (starts with 0000), which is not supported."""


class PairingCodeExpired(PairingError):
    """No CC is waiting for this code: it has expired or was mistyped."""


class WrongPairingCode(PairingError):
    """A CC answered, but the password part of the code did not match."""


class PairingTimeout(PairingError):
    """The relay or the CC stopped answering during pairing."""


class RequestError(DanfossLinkError):
    """The CC answered a request with an HTTP error status."""

    def __init__(self, method: str, uri: str, status: int, content: Any) -> None:
        super().__init__(f"{method} {uri} failed with HTTP {status}: {content!r}")
        self.method = method
        self.uri = uri
        self.status = status
        self.content = content
