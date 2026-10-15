"""An in-process fake of the Danfoss MDG relay and a Link CC, for end-to-end tests.

It uses Python's `ssl` on the server side, so the client's TLS stack is
exercised against an independent implementation (OpenSSL).
"""

from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path
import socket
import ssl
import struct
import threading
from typing import Any

from custom_components.danfoss_link.api import protobuf as pb
from custom_components.danfoss_link.api.client import (
    CONTROL_MAGIC,
    MT_HANDSHAKE,
    MT_PING,
    MT_PING_REPLY,
    MT_PLACE_CALL,
    MT_PLACE_CALL_REPLY,
    MT_SET_PEER_WHITELIST,
    MT_SET_PEER_WHITELIST_REPLY,
    RMT_GO_AHEAD,
    RMT_PLEASE_HOLD,
    ROUTING_MAGIC,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = b"routing-token-123"

ROOMS = [
    {
        "id": 0,
        "name": "Living room",
        "ambientTemperature": 20.6,
        "setpoint": {"temperature": 21.0, "type": "Adjustable"},
        "regulation": {
            "type": "Schedule",
            "atHomeSetpoint": 21.0,
            "awaySetpoint": 17.0,
            "manualSetpoint": 22.0,
        },
        "minSetpoint": 5.0,
        "maxSetpoint": 28.0,
        "regulationState": "Heating",
        "state": "Normal",
        "zone": "Living",
        "hasTemperatureSensor": True,
    },
    {
        "id": 1,
        "name": "Bathroom",
        "ambientTemperature": 23.1,
        "setpoint": {"temperature": 24.0, "type": "Adjustable"},
        "regulation": {"type": "Manual", "atHomeSetpoint": 24.0, "awaySetpoint": 19.0},
        "minSetpoint": 5.0,
        "maxSetpoint": 35.0,
        "regulationState": "Idle",
        "state": "Normal",
        "zone": "Living",
    },
]


def initial_state() -> dict[str, Any]:
    return {
        "/api/system/info": {"name": "My home", "serialNo": "123456", "version": "4.2.1797"},
        "/api/system/status": {"currentState": "AtHome"},
        "/api/rooms": copy.deepcopy(ROOMS),
        "/api/modes": [
            {"type": "AtHome", "state": "Active"},
            {"type": "Vacation", "state": "Inactive"},
            {"type": "Pause", "state": "Inactive"},
        ],
        "/api/onoffdevices": [{"id": 0, "name": "Towel rail", "state": "Off"}],
        "/api/alarms": [
            {"id": 3, "level": "Warning", "room": 1, "node": 7, "isRead": False,
             "titleTextId": "LowBattery", "createdDate": "2026-10-01T10:00:00"},
        ],
        "/api/settings/temperature": {"vacationTemperature": {"setpoint": 15.0}},
    }  # fmt: skip


def _recv_exact(sock: Any, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        data = sock.recv(n - len(buf))
        if not data:
            raise EOFError
        buf += data
    return buf


def _read_frame(sock: Any) -> tuple[int, bytes]:
    length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    frame = _recv_exact(sock, length)
    return frame[0], frame[1:]


def _send_frame(sock: Any, type_code: int, payload: bytes) -> None:
    sock.sendall(struct.pack(">H", len(payload) + 1) + bytes([type_code]) + payload)


class FakeMDG:
    """Fake control server, router and CC on localhost."""

    def __init__(self, client_cert: str = "app_p256_cert.pem") -> None:
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.ctx.load_cert_chain(FIXTURES / "server_cert.pem", FIXTURES / "server_key.pem")
        self.ctx.verify_mode = ssl.CERT_REQUIRED
        self.ctx.load_verify_locations(FIXTURES / client_cert)
        self.state = initial_state()
        self.requests: list[tuple[str, str, Any]] = []
        self.whitelist: list[bytes] = []
        self.ping_replies: list[int] = []
        self.place_call_result = 0
        self.send_ping = False
        self.reject_writes = False
        self.send_unrelated_message = False
        self.tunnels_opened = 0
        self.control_connections = 0
        self.drop_tunnel_after: int | None = None
        self._live: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.control_sock = self._listen(self._serve_control)
        self.router_sock = self._listen(self._serve_router)
        self.control_port = self.control_sock.getsockname()[1]
        self.router_port = self.router_sock.getsockname()[1]

    def _listen(self, handler: Any) -> socket.socket:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        sock.settimeout(0.1)
        self._start(self._accept_loop, sock, handler)
        return sock

    def _start(self, target: Any, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _accept_loop(self, sock: socket.socket, handler: Any) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(10)
            self._live.append(conn)
            self._start(self._guard, handler, conn)

    @staticmethod
    def _guard(handler: Any, conn: socket.socket) -> None:
        try:
            handler(conn)
        except (EOFError, OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()
        for sock in [self.control_sock, self.router_sock]:
            sock.close()
        self.drop_connections()
        for thread in self._threads:
            thread.join(timeout=5)

    def drop_connections(self) -> None:
        for sock in self._live:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    # ----------------------------------------------------------- control

    def _serve_control(self, conn: socket.socket) -> None:
        tls = self.ctx.wrap_socket(conn, server_side=True)
        self._live.append(tls)
        self.control_connections += 1
        msg_type, payload = _read_frame(tls)
        assert msg_type == MT_HANDSHAKE
        assert pb.Message(payload).int(1) == CONTROL_MAGIC
        while True:
            msg_type, payload = _read_frame(tls)
            msg = pb.Message(payload)
            req_id = msg.int(1) or 0
            if msg_type == MT_SET_PEER_WHITELIST:
                self.whitelist = [v for v in msg.fields.get(2, []) if isinstance(v, bytes)]
                if self.send_ping:
                    _send_frame(tls, MT_PING, pb.varint_field(1, 77))
                _send_frame(tls, MT_SET_PEER_WHITELIST_REPLY, pb.varint_field(1, req_id))
            elif msg_type == MT_PLACE_CALL:
                assert msg.bytes(2) in self.whitelist
                assert msg.str(3) == "cc_json"
                router = pb.bytes_field(1, "127.0.0.1") + pb.varint_field(2, self.router_port)
                routing = (
                    pb.bytes_field(3, msg.bytes(2) or b"")
                    + pb.bytes_field(4, router)
                    + pb.bytes_field(5, TOKEN)
                )
                reply = pb.varint_field(1, req_id) + pb.varint_field(2, self.place_call_result)
                if self.place_call_result == 0:
                    reply += pb.bytes_field(3, routing)
                else:
                    reply += pb.bytes_field(4, "PEER_NOT_AVAILABLE")
                _send_frame(tls, MT_PLACE_CALL_REPLY, reply)
            elif msg_type == MT_PING_REPLY:
                self.ping_replies.append(req_id)

    # ---------------------------------------------------- router + CC

    def _serve_router(self, conn: socket.socket) -> None:
        msg_type, payload = _read_frame(conn)
        msg = pb.Message(payload)
        assert msg_type == 0
        assert msg.int(1) == ROUTING_MAGIC
        assert msg.bytes(4) == TOKEN
        assert msg.str(5) == "tlsv1_2"
        _send_frame(conn, RMT_PLEASE_HOLD, b"")
        _send_frame(conn, RMT_GO_AHEAD, pb.bytes_field(1, "tlsv1_2"))
        tls = self.ctx.wrap_socket(conn, server_side=True)
        self._live.append(tls)
        self.tunnels_opened += 1
        served = 0
        while True:
            length = struct.unpack(">H", _recv_exact(tls, 2))[0]
            request = json.loads(_recv_exact(tls, length))
            if self.drop_tunnel_after is not None and served >= self.drop_tunnel_after:
                self.drop_tunnel_after = None
                return
            served += 1
            inner = request["data"]["data"]
            assert inner["port"] == 8000
            status, content = self._handle(inner["method"], inner["uri"], inner.get("content"))
            if self.send_unrelated_message:
                self._send_json(tls, {"id": 999999, "data": {"type": "Event", "data": {}}})
            self._send_json(
                tls,
                {
                    "id": request["id"],
                    "cc_time": 1791893185000,
                    "data": {
                        "type": "JsonWebResponse",
                        "data": {"headers": [], "status": status, "content": content},
                    },
                },
            )

    @staticmethod
    def _send_json(tls: Any, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode()
        tls.sendall(struct.pack(">H", len(body)) + body)

    def _handle(self, method: str, uri: str, content: Any) -> tuple[int, Any]:
        self.requests.append((method, uri, content))
        if method != "GET" and self.reject_writes:
            return 400, {"errorMessages": ["JSON content does not conform to JSON Schema."]}
        if method == "GET":
            if uri in self.state:
                return 200, self.state[uri]
            return 404, {"errorMessages": ["Not found"]}
        if method == "PATCH" and uri.startswith("/api/rooms/"):
            if not isinstance(content, dict) or "id" not in content:
                return 400, {"errorMessages": ["JSON content does not conform to JSON Schema."]}
            for room in self.state["/api/rooms"]:
                if room["id"] == content["id"]:
                    for key, value in content.items():
                        if isinstance(value, dict):
                            room.setdefault(key, {}).update(value)
                        else:
                            room[key] = value
            return 200, None
        if method == "PUT" and uri.startswith("/api/modes/"):
            for mode in self.state["/api/modes"]:
                if mode["type"] == content["type"]:
                    mode["state"] = content["state"]
                elif content["state"] == "Active":
                    mode["state"] = "Inactive"
            return 200, None
        if method == "PATCH" and uri.startswith("/api/onoffdevices/"):
            for device in self.state["/api/onoffdevices"]:
                if device["id"] == content["id"]:
                    device.update(content)
            return 200, None
        if method == "PATCH" and uri == "/api/settings/temperature":
            self.state[uri].update(content)
            return 200, None
        return 400, {"errorMessages": [f"Unsupported {method} {uri}"]}
