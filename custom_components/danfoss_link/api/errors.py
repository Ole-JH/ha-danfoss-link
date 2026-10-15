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


class RequestError(DanfossLinkError):
    """The CC answered a request with an HTTP error status."""

    def __init__(self, method: str, uri: str, status: int, content: Any) -> None:
        super().__init__(f"{method} {uri} failed with HTTP {status}: {content!r}")
        self.method = method
        self.uri = uri
        self.status = status
        self.content = content
