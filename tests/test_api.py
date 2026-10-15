"""Tests for the protocol client against the fake MDG relay."""

from __future__ import annotations

from collections.abc import Iterator
import socket

import pytest

from custom_components.danfoss_link.api import (
    AppIdentity,
    CannotConnect,
    DanfossLinkClient,
    InvalidCredentials,
    PeerNotAvailable,
    RequestError,
    normalize_peer_id,
    protobuf as pb,
)

from .fake_mdg import FIXTURES, FakeMDG

PEER_ID = "e7:af:e3:77:61:90:0d:b5:1f:87:d9:7c:ea:da:10:1e:25:26:9b:a1"


def _identity(kind: str = "p256") -> AppIdentity:
    return AppIdentity.from_pem(
        (FIXTURES / f"app_{kind}_cert.pem").read_text(),
        (FIXTURES / f"app_{kind}_key.pem").read_text(),
        PEER_ID,
    )


@pytest.fixture
def fake(socket_enabled: None) -> Iterator[FakeMDG]:
    server = FakeMDG()
    yield server
    server.close()


def _client(fake: FakeMDG, hosts: tuple[str, ...] = ("127.0.0.1",)) -> DanfossLinkClient:
    return DanfossLinkClient(_identity(), hosts=hosts, port=fake.control_port, timeout=5)


# ------------------------------------------------------------- protobuf


def test_protobuf_roundtrip() -> None:
    inner = pb.bytes_field(1, "host") + pb.varint_field(2, 443)
    data = (
        pb.varint_field(1, 300)
        + pb.fixed32_field(2, 0xF09D8CA8)
        + pb.bytes_field(3, inner)
        + pb.bytes_field(3, inner)
    )
    msg = pb.Message(data)
    assert msg.int(1) == 300
    assert msg.int(2) == 0xF09D8CA8
    assert [m.str(1) for m in msg.messages(3)] == ["host", "host"]
    assert msg.message(3).int(2) == 443
    assert msg.int(9) is None


def test_protobuf_truncated() -> None:
    with pytest.raises(ValueError):
        pb.Message(pb.bytes_field(1, b"abcdef")[:-2])


# ---------------------------------------------------------- credentials


@pytest.mark.parametrize(
    "value",
    [
        PEER_ID,
        PEER_ID.upper(),
        PEER_ID.replace(":", "_"),
        PEER_ID.replace(":", ""),
        f' "{PEER_ID}" ',
    ],
)
def test_normalize_peer_id(value: str) -> None:
    assert normalize_peer_id(value) == PEER_ID


@pytest.mark.parametrize("value", ["", "e7:af", PEER_ID + ":00", "zz" * 20])
def test_normalize_peer_id_invalid(value: str) -> None:
    with pytest.raises(InvalidCredentials):
        normalize_peer_id(value)


def test_brainpool_identity_with_explicit_parameters() -> None:
    """The app's real key format: brainpoolP320r1, explicit curve parameters."""
    assert "BEGIN EC PARAMETERS" not in (FIXTURES / "app_brainpool_key.pem").read_text()
    identity = _identity("brainpool")
    assert identity.signing_key.curve.name == "BRAINPOOLP320r1"
    assert identity.peer_id == PEER_ID


def test_sec1_key_is_accepted() -> None:
    identity = _identity("p256")
    assert identity.signing_key.curve.name == "NIST256p"


def test_key_must_match_certificate() -> None:
    with pytest.raises(InvalidCredentials, match="does not belong"):
        AppIdentity.from_pem(
            (FIXTURES / "app_p256_cert.pem").read_text(),
            (FIXTURES / "server_key.pem").read_text(),
            PEER_ID,
        )


def test_garbage_credentials() -> None:
    with pytest.raises(InvalidCredentials):
        AppIdentity.from_pem("not a cert", "not a key", PEER_ID)


# ------------------------------------------------------------ end to end


def test_get_and_patch(fake: FakeMDG) -> None:
    client = _client(fake)
    try:
        info = client.get("/api/system/info")
        assert info["name"] == "My home"
        assert fake.whitelist == [PEER_ID.encode()]

        client.patch("/api/rooms/0", {"id": 0, "setpoint": {"temperature": 19.5}})
        rooms = client.get("/api/rooms")
        assert rooms[0]["setpoint"]["temperature"] == 19.5
        # Everything went over a single control connection and tunnel.
        assert fake.control_connections == 1
        assert fake.tunnels_opened == 1
        assert client.cc_certificate is not None
    finally:
        client.close()


def test_http_error_raises_request_error(fake: FakeMDG) -> None:
    client = _client(fake)
    try:
        with pytest.raises(RequestError) as err:
            client.patch("/api/rooms/0", {"setpoint": {"temperature": 20}})
        assert err.value.status == 400
        # The tunnel stays usable after an HTTP-level error.
        assert client.get("/api/system/info")["name"] == "My home"
        assert fake.tunnels_opened == 1
    finally:
        client.close()


def test_pings_are_answered(fake: FakeMDG) -> None:
    fake.send_ping = True
    client = _client(fake)
    try:
        client.get("/api/system/info")
        assert fake.ping_replies == [77]
    finally:
        client.close()


def test_unrelated_tunnel_messages_are_skipped(fake: FakeMDG) -> None:
    fake.send_unrelated_message = True
    client = _client(fake)
    try:
        assert client.get("/api/system/info")["name"] == "My home"
        assert client.get("/api/rooms")[1]["name"] == "Bathroom"
    finally:
        client.close()


def test_reconnects_when_tunnel_drops(fake: FakeMDG) -> None:
    client = _client(fake)
    try:
        client.get("/api/system/info")
        fake.drop_tunnel_after = 0
        assert client.get("/api/rooms")[0]["name"] == "Living room"
        assert fake.tunnels_opened == 2
    finally:
        client.close()


def test_reconnects_when_everything_drops(fake: FakeMDG) -> None:
    client = _client(fake)
    try:
        client.get("/api/system/info")
        fake.drop_connections()
        assert client.get("/api/system/info")["name"] == "My home"
        assert fake.control_connections == 2
    finally:
        client.close()


def test_peer_not_available(fake: FakeMDG) -> None:
    fake.place_call_result = 1
    client = _client(fake)
    with pytest.raises(PeerNotAvailable):
        client.get("/api/system/info")
    client.close()


def test_falls_back_to_next_host(fake: FakeMDG) -> None:
    attempted: list[str] = []

    def factory(host: str, port: int, timeout: float) -> socket.socket:
        attempted.append(host)
        if host == "bad-host":
            raise OSError("unreachable")
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)

    client = DanfossLinkClient(
        _identity(), hosts=("bad-host", "good-host"), port=fake.control_port,
        timeout=5, socket_factory=factory,
    )  # fmt: skip
    try:
        assert client.get("/api/system/info")["name"] == "My home"
        assert client.connected_host == "good-host"
        assert attempted[:2] == ["bad-host", "good-host"]
    finally:
        client.close()


def test_all_hosts_down() -> None:
    def factory(host: str, port: int, timeout: float) -> socket.socket:
        raise OSError("unreachable")

    client = DanfossLinkClient(_identity(), hosts=("a", "b"), socket_factory=factory)
    with pytest.raises(CannotConnect, match="any MDG host"):
        client.get("/api/system/info")
