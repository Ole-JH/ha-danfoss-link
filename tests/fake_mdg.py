"""An in-process fake of the Danfoss MDG relay and a Link CC, for end-to-end tests.

It uses Python's `ssl` on the server side, so the client's TLS stack is
exercised against an independent implementation (OpenSSL).
"""

from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path
import secrets
import socket
import ssl
import struct
import threading
from typing import Any

from ecdsa.curves import NIST256p

from custom_components.danfoss_link.api import pairing as pr, protobuf as pb
from custom_components.danfoss_link.api.client import (
    CONTROL_MAGIC,
    MT_HANDSHAKE,
    MT_PING,
    MT_PING_REPLY,
    MT_PLACE_CALL,
    MT_PLACE_CALL_REPLY,
    MT_PLACE_PAIRING_CALL,
    MT_PLACE_PAIRING_CALL_REPLY,
    MT_SET_PEER_WHITELIST,
    MT_SET_PEER_WHITELIST_REPLY,
    RMT_GO_AHEAD,
    RMT_PLEASE_HOLD,
    RMT_ROUTING_ERROR,
    ROUTING_MAGIC,
)
from custom_components.danfoss_link.api.credentials import (
    generate_certificate,
    public_key_pem,
    subject_key_identifier,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = b"routing-token-123"
PAIRING_TOKEN = b"pairing-token-456"
# The code the fake CC "shows": pair ID 0012345 (leading zeros matter), password 67890.
PAIRING_CODE = "0012-3456-7890"
PAIR_ID = "0012345"
PAIR_PASSWORD = "67890"
CC_NAME = "Danfoss Link CC"

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
        "zone": "None",
    },
]


def initial_state() -> dict[str, Any]:
    return {
        "/api/system/info": {"name": "My home", "serialNo": "123456", "version": "4.2.1797",
                             "wifiSignalStrengthDb": -61},
        "/api/system/status": {"currentState": "AtHome"},
        "/api/rooms": copy.deepcopy(ROOMS),
        # Following the schedule: no mode is active (as on a real CC).
        "/api/modes": [
            {"type": "AtHome", "state": "Inactive"},
            {"type": "Vacation", "state": "Inactive"},
            {"type": "Pause", "state": "Inactive"},
        ],
        "/api/onoffdevices": [{"id": 0, "name": "Towel rail", "state": "Off"}],
        "/api/alarms": [
            {"id": 3, "level": "Warning", "room": 1, "node": 7, "isRead": False,
             "titleTextId": "IDS_UA1_ALERTVIEWER_BATTERY_LOW_ALERT",
             "createdDate": "2026-10-01T10:00:00"},
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
        # Pairing: the CC accepts one pairing call for PAIR_ID while pairing_open.
        self.pairing_open = True
        self.pairing_routing_error: int | None = None
        self.pairing_stall = False
        self._pairing_done = threading.Event()
        self.pairing_results: list[bool] = []
        self.last_cipher: tuple[str, str, int] | None = None
        self.paired: list[tuple[str | None, bytes]] = []
        self.cc_cert_der = ssl.PEM_cert_to_DER_cert((FIXTURES / "server_cert.pem").read_text())
        self.cc_peer_id = subject_key_identifier(self.cc_cert_der)
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

    def wait_for_pairing(self, timeout: float = 5) -> None:
        """Wait until the fake CC has finished its side of the last pairing."""
        self._pairing_done.wait(timeout)

    def trust(self, cert_pem: str) -> None:
        """Accept a client certificate from now on.

        The real relay and CC accept any self-signed certificate on a pairing
        call; Python's ssl cannot do that, so tests register the identity that
        pairing generates.
        """
        self.ctx.load_verify_locations(cadata=cert_pem)

    def generate_trusted_certificate(
        self, common_name: str = "Home Assistant", curve: Any = None
    ) -> tuple[str, str]:
        """Stand-in for generate_certificate in pairing tests.

        Python's ssl (OpenSSL 3) cannot act as the server for a brainpoolP320r1
        client certificate, so the identity is made on P-256 and trusted. Each
        gets a unique subject because OpenSSL finds trusted certificates by name.
        """
        cert_pem, key_pem = generate_certificate(
            f"{common_name} {secrets.token_hex(4)}", curve=NIST256p
        )
        self.trust(cert_pem)
        return cert_pem, key_pem

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
            elif msg_type == MT_PLACE_PAIRING_CALL:
                if self.pairing_open and msg.str(2) == PAIR_ID:
                    router = pb.bytes_field(1, "127.0.0.1") + pb.varint_field(2, self.router_port)
                    routing = (
                        pb.bytes_field(3, self.cc_peer_id)
                        + pb.bytes_field(4, router)
                        + pb.bytes_field(5, PAIRING_TOKEN)
                    )
                    reply = pb.varint_field(1, req_id) + pb.varint_field(2, 0)
                    reply += pb.bytes_field(3, routing)
                else:
                    reply = pb.varint_field(1, req_id) + pb.varint_field(2, 1)
                    reply += pb.bytes_field(4, "PEER_NOT_AVAILABLE")
                _send_frame(tls, MT_PLACE_PAIRING_CALL_REPLY, reply)
            elif msg_type == MT_PING_REPLY:
                self.ping_replies.append(req_id)

    # ---------------------------------------------------- router + CC

    def _serve_router(self, conn: socket.socket) -> None:
        msg_type, payload = _read_frame(conn)
        msg = pb.Message(payload)
        assert msg_type == 0
        assert msg.int(1) == ROUTING_MAGIC
        assert msg.bytes(4) in (TOKEN, PAIRING_TOKEN)
        assert msg.str(5) == "tlsv1_2"
        _send_frame(conn, RMT_PLEASE_HOLD, b"")
        if msg.bytes(4) == PAIRING_TOKEN and self.pairing_routing_error is not None:
            _send_frame(conn, RMT_ROUTING_ERROR, pb.varint_field(1, self.pairing_routing_error))
            return
        _send_frame(conn, RMT_GO_AHEAD, pb.bytes_field(1, "tlsv1_2"))
        tls = self.ctx.wrap_socket(conn, server_side=True)
        self._live.append(tls)
        if msg.bytes(4) == PAIRING_TOKEN:
            try:
                self._serve_pairing(tls)
            finally:
                self._pairing_done.set()
            return
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

    # ------------------------------------------------------------ pairing

    def _serve_pairing(self, tls: ssl.SSLSocket) -> None:
        """The CC's half of PACE (pace.server_part) and the contact_info exchange."""
        self.pairing_open = False  # a pairing code is single use
        self.last_cipher = tls.cipher()

        def recv(tag: int) -> pb.Message:
            got, payload = _read_frame(tls)
            assert got == tag, f"expected pairing message {tag}, got {got}"
            return pb.Message(payload)

        client_cert = tls.getpeercert(binary_form=True)
        assert client_cert
        hello = recv(pr.PT_CLIENT_HELLO)
        if self.pairing_stall:
            self._stop.wait(30)
            return
        assert hello.int(1) == pr.PACE_MAGIC
        assert (hello.int(2), hello.int(3)) == pr.PACE_VERSION
        salt = b"1234"  # a literal constant in the app's pace.lua
        pi = pr.calculate_pi(
            PAIR_PASSWORD, public_key_pem(self.cc_cert_der), public_key_pem(client_cert)
        )
        nonce = pr.random_scalar()
        z = pr.encode_nonce(pr.calculate_kpi(pi, salt), nonce)
        _send_frame(
            tls,
            pr.PT_SERVER_HELLO,
            pb.varint_field(1, 1) + pb.varint_field(2, 0) + pb.bytes_field(3, salt)
            + pb.bytes_field(4, b"\x01\x02") + pb.bytes_field(5, z),
        )  # fmt: skip
        exchange = pr.PaceExchange(nonce)
        _send_frame(tls, pr.PT_XY, pb.bytes_field(1, exchange.xy()))
        pk = exchange.pk(recv(pr.PT_XY).bytes(1) or b"")
        _send_frame(tls, pr.PT_PK, pb.bytes_field(1, pk))
        proof = exchange.proof(recv(pr.PT_PK).bytes(1) or b"")
        _send_frame(tls, pr.PT_PROOF, pb.bytes_field(1, proof))
        ok = exchange.verify(recv(pr.PT_PROOF).bytes(1) or b"")
        self.pairing_results.append(ok)
        if not ok:
            return  # the app's pairing_procedure closes the socket
        _send_frame(tls, pr.PT_CONTACT_INFO, pb.bytes_field(1, CC_NAME))
        contact = recv(pr.PT_CONTACT_INFO)
        self.paired.append((contact.str(1), client_cert))

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
                    if room.get("zone") == "Living" and "type" in content.get("regulation", {}):
                        return 400, {
                            "errorMessages": [
                                "Can't change regulation type of rooms that are part of the"
                                " Living Zone"
                            ]
                        }
                    for key, value in content.items():
                        if isinstance(value, dict):
                            room.setdefault(key, {}).update(value)
                        else:
                            room[key] = value
            return 200, None
        if method == "PATCH" and uri == "/api/zones/living":
            for room in self.state["/api/rooms"]:
                if room.get("zone") == "Living" and "regulationType" in content:
                    room["regulation"]["type"] = content["regulationType"]
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
