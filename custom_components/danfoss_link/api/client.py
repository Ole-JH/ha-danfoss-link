"""Blocking client that talks to a Danfoss Link CC the way the phone app does.

    client ──mTLS──► MDG control server   (who am I, connect me to peer X)
    client ────────► MDG routing server   (token), then mTLS end-to-end with the CC
    client ═cc_json═► CC's internal REST API on 127.0.0.1:8000

All methods block, so Home Assistant runs them in the executor. One instance
holds at most one control connection and one tunnel; calls must not overlap.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import logging
import select
import socket
import struct
from typing import Any

from . import protobuf as pb
from .credentials import AppIdentity
from .errors import CannotConnect, DanfossLinkError, PeerNotAvailable, ProtocolError, RequestError
from .tls import PROFILE_APP, PROFILE_CONTROL, TLSClient

_LOGGER = logging.getLogger(__name__)

MDG_HOSTS = ("77.66.11.90", "77.66.11.92", "5.179.92.180", "5.179.92.182")
MDG_PORT = 443
CONTROL_SERVER_NAME = "mdg-danfoss.hosted.trifork.com"
CC_PROTOCOL = "cc_json"
CC_HTTP_PORT = 8000

CONTROL_MAGIC = 0xF09D8CA8
ROUTING_MAGIC = 0xF09D8C95

# Control message type codes
MT_HANDSHAKE = 1
MT_BYE = 2
MT_RECONNECT = 3
MT_PING = 4
MT_PING_REPLY = 5
MT_PLACE_CALL = 10
MT_PLACE_CALL_REPLY = 11
MT_INCOMING_CALL = 12
MT_INCOMING_CALL_REPLY = 13
MT_SET_PEER_WHITELIST = 20
MT_SET_PEER_WHITELIST_REPLY = 21

# Routing message type codes
RMT_HANDSHAKE = 0
RMT_PLEASE_HOLD = 1
RMT_GO_AHEAD = 2
RMT_ROUTING_ERROR = 3

PLACE_CALL_RESULTS = {0: "OK", 1: "PEER_NOT_AVAILABLE", 2: "SERVER_ERROR"}

DEFAULT_TIMEOUT = 20.0
MAX_ROUTING_HOLDS = 30

SocketFactory = Callable[[str, int, float], socket.socket]


def _default_socket_factory(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def _frame(type_code: int, payload: bytes) -> bytes:
    return struct.pack(">H", len(payload) + 1) + bytes([type_code]) + payload


class _ControlConnection:
    """The long-lived connection to the MDG control server."""

    def __init__(self, tls: TLSClient) -> None:
        self.tls = tls
        self._buf = b""
        self.closed = False

    def send(self, type_code: int, payload: bytes) -> None:
        self.tls.send(_frame(type_code, payload))

    def _frame_ready(self) -> bool:
        return len(self._buf) >= 2 and len(self._buf) >= 2 + struct.unpack(">H", self._buf[:2])[0]

    def recv(self) -> tuple[int, pb.Message]:
        while not self._frame_ready():
            self._buf += self.tls.recv()
        length = struct.unpack(">H", self._buf[:2])[0]
        frame, self._buf = self._buf[2 : 2 + length], self._buf[2 + length :]
        if not frame:
            raise ProtocolError("Empty control frame")
        return frame[0], pb.Message(frame[1:])

    def readable(self) -> bool:
        """True if a message can be read without blocking for long."""
        if self._frame_ready() or self.tls.has_buffered_data():
            return True
        readable, _, _ = select.select([self.tls.sock], [], [], 0)
        return bool(readable)

    def close(self) -> None:
        self.closed = True
        self.tls.close()


class DanfossLinkClient:
    """Connects to one CC through the Danfoss MDG cloud relay."""

    def __init__(
        self,
        identity: AppIdentity,
        *,
        hosts: Sequence[str] = MDG_HOSTS,
        port: int = MDG_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        socket_factory: SocketFactory = _default_socket_factory,
    ) -> None:
        self.identity = identity
        self._hosts = list(hosts)
        self._port = port
        self._timeout = timeout
        self._socket_factory = socket_factory
        self._control: _ControlConnection | None = None
        self._tunnel: TLSClient | None = None
        self._tunnel_buf = b""
        self._next_id = 0
        self.cc_certificate: bytes | None = None
        self.connected_host: str | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        return self._tunnel is not None

    def close(self) -> None:
        """Drop the tunnel and the control connection."""
        self._close_tunnel()
        if self._control is not None:
            self._control.close()
            self._control = None

    def _close_tunnel(self) -> None:
        if self._tunnel is not None:
            self._tunnel.close()
            self._tunnel = None
        self._tunnel_buf = b""

    def _req_id(self) -> int:
        self._next_id = self._next_id % 0x7FFFFFFF + 1
        return self._next_id

    def connect(self) -> None:
        """Open the control connection and a tunnel to the CC."""
        self.close()
        errors: list[str] = []
        hosts = list(self._hosts)
        while hosts:
            host = hosts.pop(0)
            try:
                self._connect_control(host)
                self._open_tunnel()
                return
            except PeerNotAvailable:
                self.close()
                raise
            except (OSError, DanfossLinkError, ValueError) as err:
                _LOGGER.debug("MDG host %s failed: %s", host, err)
                errors.append(f"{host}: {err}")
                self.close()
        raise CannotConnect("Could not reach the CC via any MDG host (" + "; ".join(errors) + ")")

    def _connect_control(self, host: str) -> None:
        sock = self._socket_factory(host, self._port, self._timeout)
        tls = TLSClient(
            sock,
            self.identity.cert_der,
            self.identity.signing_key,
            profile=PROFILE_CONTROL,
            server_name=CONTROL_SERVER_NAME,
        )
        control = _ControlConnection(tls)
        try:
            tls.handshake()
            control.send(
                MT_HANDSHAKE,
                pb.fixed32_field(1, CONTROL_MAGIC) + pb.varint_field(2, 1) + pb.varint_field(3, 0),
            )
            req_id = self._req_id()
            control.send(
                MT_SET_PEER_WHITELIST,
                pb.varint_field(1, req_id) + pb.bytes_field(2, self.identity.peer_id),
            )
            self._control = control
            self._await_control(MT_SET_PEER_WHITELIST_REPLY, req_id)
        except BaseException:
            control.close()
            self._control = None
            raise
        self.connected_host = host
        _LOGGER.debug("Connected to MDG control server %s", host)

    def _handle_control(self, type_code: int, msg: pb.Message) -> None:
        """React to unsolicited control messages."""
        assert self._control is not None
        if type_code == MT_PING:
            self._control.send(MT_PING_REPLY, pb.varint_field(1, msg.int(1, 0) or 0))
        elif type_code == MT_INCOMING_CALL:
            # We never accept calls; say so instead of leaving the server waiting.
            self._control.send(
                MT_INCOMING_CALL_REPLY,
                pb.varint_field(1, msg.int(1, 0) or 0) + pb.varint_field(2, 0),
            )
        elif type_code in (MT_BYE, MT_RECONNECT):
            _LOGGER.debug("MDG control server ended the session (type %s)", type_code)
            self._control.close()
            self._control = None
            raise CannotConnect("MDG control server closed the session")
        elif type_code == MT_HANDSHAKE:
            pass
        else:
            _LOGGER.debug("Ignoring control message type %s", type_code)

    def _await_control(self, want_type: int, req_id: int) -> pb.Message:
        if self._control is None:
            raise CannotConnect("No control connection")
        for _ in range(100):
            type_code, msg = self._control.recv()
            if type_code == want_type and msg.int(1) in (req_id, None):
                return msg
            self._handle_control(type_code, msg)
        raise ProtocolError(f"No reply of type {want_type} from the MDG control server")

    def service_control(self) -> None:
        """Answer pending pings so the relay keeps the session alive."""
        control = self._control
        if control is None:
            return
        try:
            while control.readable():
                type_code, msg = control.recv()
                self._handle_control(type_code, msg)
        except (OSError, DanfossLinkError) as err:
            _LOGGER.debug("Control connection lost: %s", err)
            if self._control is not None:
                self._control.close()
                self._control = None

    def _open_tunnel(self) -> None:
        if self._control is None:
            raise CannotConnect("No control connection")
        self._close_tunnel()
        req_id = self._req_id()
        self._control.send(
            MT_PLACE_CALL,
            pb.varint_field(1, req_id)
            + pb.bytes_field(2, self.identity.peer_id)
            + pb.bytes_field(3, CC_PROTOCOL),
        )
        reply = self._await_control(MT_PLACE_CALL_REPLY, req_id)
        result = reply.int(2, 0)
        if result != 0:
            text = reply.str(4) or PLACE_CALL_RESULTS.get(result or 0, str(result))
            if result == 1:
                raise PeerNotAvailable(f"The CC is not reachable through the relay: {text}")
            raise CannotConnect(f"place_call failed: {text}")
        routing = reply.message(3)
        if routing is None:
            raise ProtocolError("place_call reply without routing information")
        token = routing.bytes(5) or b""
        routers = [(r.str(1), r.int(2)) for r in routing.messages(4) if r.str(1) and r.int(2)]
        if not routers:
            raise ProtocolError("place_call reply without a router")

        last_error: Exception | None = None
        for host, port in routers:
            try:
                self._tunnel = self._connect_router(host, port, token)  # type: ignore[arg-type]
                _LOGGER.debug("Tunnel to the CC open via %s:%s", host, port)
                return
            except (OSError, DanfossLinkError) as err:
                last_error = err
        raise CannotConnect(f"Could not open the tunnel: {last_error}")

    def _connect_router(self, host: str, port: int, token: bytes) -> TLSClient:
        sock = self._socket_factory(host, port, self._timeout)
        try:
            sock.sendall(
                _frame(
                    RMT_HANDSHAKE,
                    pb.fixed32_field(1, ROUTING_MAGIC)
                    + pb.varint_field(2, 1)
                    + pb.varint_field(3, 0)
                    + pb.bytes_field(4, token)
                    + pb.bytes_field(5, "tlsv1_2"),
                )
            )
            buf = b""
            for _ in range(MAX_ROUTING_HOLDS):
                while len(buf) < 2 or len(buf) < 2 + struct.unpack(">H", buf[:2])[0]:
                    data = sock.recv(4096)
                    if not data:
                        raise CannotConnect("Router closed the connection")
                    buf += data
                length = struct.unpack(">H", buf[:2])[0]
                frame, buf = buf[2 : 2 + length], buf[2 + length :]
                if not frame:
                    raise ProtocolError("Empty routing frame")
                if frame[0] == RMT_GO_AHEAD:
                    break
                if frame[0] == RMT_ROUTING_ERROR:
                    raise CannotConnect(f"Routing error {pb.Message(frame[1:]).int(1)}")
                # RMT_PLEASE_HOLD: the CC has not picked up yet.
            else:
                raise CannotConnect("The CC did not answer the call")
            tls = TLSClient(
                sock,
                self.identity.cert_der,
                self.identity.signing_key,
                profile=PROFILE_APP,
                initial_data=buf,
            )
            tls.handshake()
        except BaseException:
            sock.close()
            raise
        self.cc_certificate = tls.server_certs[0] if tls.server_certs else None
        return tls

    # -------------------------------------------------------------- cc_json

    def request(self, method: str, uri: str, content: Any = None) -> Any:
        """Send one REST request to the CC and return the decoded JSON body.

        Reconnects once if the tunnel has gone away. Raises RequestError for
        HTTP error statuses.
        """
        for attempt in (1, 2):
            if self._tunnel is None:
                if self._control is None:
                    self.connect()
                else:
                    try:
                        self._open_tunnel()
                    except PeerNotAvailable:
                        raise
                    except (OSError, DanfossLinkError):
                        self.connect()
            else:
                self.service_control()
            try:
                status, body = self._exchange(method, uri, content)
            except (OSError, CannotConnect, ValueError) as err:
                _LOGGER.debug("Request %s %s failed (attempt %s): %s", method, uri, attempt, err)
                self._close_tunnel()
                if attempt == 2:
                    raise CannotConnect(f"{method} {uri}: {err}") from err
                continue
            if status is None or status >= 400:
                raise RequestError(method, uri, status or 0, body)
            return body
        raise AssertionError("unreachable")

    def _exchange(self, method: str, uri: str, content: Any) -> tuple[int | None, Any]:
        assert self._tunnel is not None
        req_id = self._req_id()
        inner: dict[str, Any] = {
            "uri": uri,
            "method": method,
            "headers": ["Accept: application/json; charset=utf-8"],
            "port": CC_HTTP_PORT,
        }
        if content is not None:
            inner["content"] = content
        body = json.dumps(
            {"id": req_id, "data": {"type": "JsonWebRequest", "data": inner}},
            separators=(",", ":"),
        ).encode()
        if len(body) > 0xFFFF:
            raise ValueError("Request too large")
        self._tunnel.send(struct.pack(">H", len(body)) + body)

        for _ in range(50):
            message = self._read_tunnel_message()
            if message.get("id") != req_id:
                _LOGGER.debug("Ignoring unrelated cc_json message: %.200s", message)
                continue
            data = message.get("data") or {}
            if data.get("type") != "JsonWebResponse":
                raise ProtocolError(f"Unexpected cc_json reply type {data.get('type')!r}")
            response = data.get("data") or {}
            return response.get("status"), response.get("content")
        raise ProtocolError("No matching cc_json response")

    def _read_tunnel_message(self) -> dict[str, Any]:
        assert self._tunnel is not None
        while len(self._tunnel_buf) < 2:
            self._tunnel_buf += self._tunnel.recv()
        length = struct.unpack(">H", self._tunnel_buf[:2])[0]
        while len(self._tunnel_buf) < 2 + length:
            self._tunnel_buf += self._tunnel.recv()
        raw = self._tunnel_buf[2 : 2 + length]
        self._tunnel_buf = self._tunnel_buf[2 + length :]
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ProtocolError("cc_json message is not an object")
        return message

    def get(self, uri: str) -> Any:
        return self.request("GET", uri)

    def patch(self, uri: str, content: Any) -> Any:
        return self.request("PATCH", uri, content)

    def put(self, uri: str, content: Any) -> Any:
        return self.request("PUT", uri, content)
