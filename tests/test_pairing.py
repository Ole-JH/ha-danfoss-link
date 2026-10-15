"""Tests for identity generation and pairing with the code shown on the CC."""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import shutil
import socket
import ssl
import subprocess

from asn1crypto import x509 as asn1_x509
from ecdsa.curves import BRAINPOOLP320r1
from ecdsa.util import sigdecode_der
import pytest

from custom_components.danfoss_link.api import (
    AppIdentity,
    CannotConnect,
    DanfossLinkClient,
    InvalidPairingCode,
    LanPairingNotSupported,
    PairingCodeExpired,
    PairingTimeout,
    WrongPairingCode,
    pairing as pr,
)
from custom_components.danfoss_link.api.credentials import (
    generate_certificate,
    public_key_pem,
    subject_key_identifier,
)

from .fake_mdg import FIXTURES, PAIR_ID, PAIR_PASSWORD, PAIRING_CODE, FakeMDG

CC_PEER_ID = "a1:c3:d7:ea:24:7c:fd:c3:0a:18:36:90:c2:d9:dd:7d:5f:2e:ea:af"


@pytest.fixture
def fake(socket_enabled: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMDG]:
    server = FakeMDG()
    monkeypatch.setattr(pr, "generate_certificate", server.generate_trusted_certificate)
    yield server
    server.close()


def _pair(fake: FakeMDG, code: str = PAIRING_CODE, **kwargs) -> AppIdentity:
    return pr.pair_with_code(
        code, hosts=("127.0.0.1",), port=fake.control_port, timeout=5, **kwargs
    )


# ---------------------------------------------------------------- identity


def test_generated_identity() -> None:
    cert_pem, key_pem = generate_certificate("Home Assistant")
    assert cert_pem.startswith("-----BEGIN CERTIFICATE-----\n")
    identity = AppIdentity.from_pem(cert_pem, key_pem, "00" * 20)
    assert identity.signing_key.curve is BRAINPOOLP320r1

    cert = asn1_x509.Certificate.load(identity.cert_der)
    # Named curve parameters, ecdsa-with-SHA256, self-signed.
    assert cert.public_key["algorithm"]["parameters"].chosen.native == "brainpoolp320r1"
    assert cert["signature_algorithm"]["algorithm"].native == "sha256_ecdsa"
    assert cert.subject.native == {"common_name": "Home Assistant"} == cert.issuer.native
    identity.signing_key.verifying_key.verify(
        cert["signature_value"].native,
        cert["tbs_certificate"].dump(),
        hashfunc=hashlib.sha256,
        sigdecode=sigdecode_der,
    )

    # SKI = SHA-1 of the public key BIT STRING contents, as the CC checks.
    point = identity.signing_key.verifying_key.to_string("uncompressed")
    assert cert.key_identifier == hashlib.sha1(point).digest()
    expected = ":".join(f"{b:02x}" for b in hashlib.sha1(point).digest())
    assert subject_key_identifier(identity.cert_der) == expected == identity.own_peer_id

    # The PEM properties round-trip.
    again = AppIdentity.from_pem(identity.cert_pem, identity.key_pem, "00" * 20)
    assert again.cert_der == identity.cert_der
    assert again.signing_key.to_string() == identity.signing_key.to_string()


def test_generated_identities_differ() -> None:
    assert generate_certificate()[1] != generate_certificate()[1]


def test_subject_key_identifier_without_extension() -> None:
    der = ssl.PEM_cert_to_DER_cert(generate_certificate()[0])
    cert = asn1_x509.Certificate.load(der)
    expected = subject_key_identifier(der)
    tbs = cert["tbs_certificate"].copy()
    del tbs["extensions"]
    stripped = asn1_x509.Certificate(
        {
            "tbs_certificate": tbs,
            "signature_algorithm": cert["signature_algorithm"],
            "signature_value": cert["signature_value"],
        }
    ).dump()
    assert subject_key_identifier(stripped) == expected


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_server_fixture_peer_id_and_public_key() -> None:
    der = ssl.PEM_cert_to_DER_cert((FIXTURES / "server_cert.pem").read_text())
    assert subject_key_identifier(der) == CC_PEER_ID
    # Same text as `openssl x509 -pubkey` (PEM_write_bio_PUBKEY).
    expected = subprocess.run(
        ["openssl", "x509", "-noout", "-pubkey", "-in", str(FIXTURES / "server_cert.pem")],
        capture_output=True,
        check=True,
    ).stdout
    assert public_key_pem(der) == expected


# -------------------------------------------------------------------- code


@pytest.mark.parametrize(
    ("code", "pair_id", "password"),
    [
        ("0012-3456-7890", "0012345", "67890"),
        (" 0012 3456 7890 ", "0012345", "67890"),
        ("0012.3456,7890", "0012345", "67890"),
        ("123456", "1", "23456"),
    ],
)
def test_parse_pairing_code(code: str, pair_id: str, password: str) -> None:
    assert pr.parse_pairing_code(code) == (pair_id, password)


@pytest.mark.parametrize("code", ["", "12345", "12-34-5", "1234/56789", "１２３４５６７"])
def test_parse_pairing_code_invalid(code: str) -> None:
    with pytest.raises(InvalidPairingCode):
        pr.parse_pairing_code(code)


def test_lan_code_rejected_without_network() -> None:
    def factory(host: str, port: int, timeout: float) -> socket.socket:
        raise AssertionError("must not connect")

    with pytest.raises(LanPairingNotSupported):
        pr.pair_with_code("0000-1234-5678", socket_factory=factory)


# -------------------------------------------------------------------- PACE


def test_pace_known_answers() -> None:
    """Values computed with the reference sketch of pace.lua."""
    pi = pr.calculate_pi("12345", b"SRV-PEM", b"CLI-PEM")
    assert pi.hex() == "0005313233343500075352562d50454d0007434c492d50454dff"
    kpi = pr.calculate_kpi(pi, b"1234")
    assert kpi.hex() == "5ae3b58fbcfa674d81183fcff29c332a"
    assert pr.decode_nonce(kpi, bytes(range(64))) == int(
        "2afd982d871608f78eebbbf63949c5749b7f374427ac40cebfc096ab4066915474af9d87e5fc4f34", 16
    )
    g = BRAINPOOLP320r1.generator
    assert pr.proof_token(g * 7, g * 11) == (
        b"47ed1ee5b34bc775ec1dd00b474324a81e363ed196af310397f12ea90f9bccf9"
    )


def test_nonce_roundtrip() -> None:
    kpi = pr.calculate_kpi(pr.calculate_pi("54321", b"a", b"b"), b"1234")
    nonce = pr.random_scalar()
    assert pr.decode_nonce(kpi, pr.encode_nonce(kpi, nonce)) == nonce


@pytest.mark.parametrize("same", [True, False])
def test_pace_exchange(same: bool) -> None:
    nonce = pr.random_scalar()
    client, server = pr.PaceExchange(nonce), pr.PaceExchange(nonce if same else nonce + 1)
    client_xy, server_xy = client.xy(), server.xy()
    client_pk, server_pk = client.pk(server_xy), server.pk(client_xy)
    client_proof, server_proof = client.proof(server_pk), server.proof(client_pk)
    assert client.verify(server_proof) is same
    assert server.verify(client_proof) is same


def test_pace_rejects_bad_points() -> None:
    g = BRAINPOOLP320r1.generator
    good = pr.encode_point(g * 3)
    assert pr.decode_point(good) == g * 3
    off_curve = good[:-1] + bytes([good[-1] ^ 1])
    for bad in (off_curve, good[:-1], b"\x02" + good[1:41]):
        with pytest.raises(CannotConnect):
            pr.decode_point(bad)


# -------------------------------------------------------------- end to end


def test_pair_then_read_rooms(fake: FakeMDG) -> None:
    identity = _pair(fake, name="My Home Assistant")
    fake.wait_for_pairing()
    assert identity.peer_id == CC_PEER_ID
    assert fake.pairing_results == [True]
    # The CC got our name and the certificate we present from now on.
    assert fake.paired == [("My Home Assistant", identity.cert_der)]
    # The routed pairing session used the 256-bit cipher the app requires.
    assert fake.last_cipher is not None
    assert fake.last_cipher[0] == "ECDHE-ECDSA-AES256-GCM-SHA384"
    # The CC was whitelisted on the control connection after pairing.
    assert fake.whitelist == [CC_PEER_ID.encode()]

    # The stored PEMs are enough to connect again.
    stored = AppIdentity.from_pem(identity.cert_pem, identity.key_pem, identity.peer_id)
    client = DanfossLinkClient(stored, hosts=("127.0.0.1",), port=fake.control_port, timeout=5)
    try:
        assert client.get("/api/rooms")[0]["name"] == "Living room"
        assert client.cc_certificate == fake.cc_cert_der
    finally:
        client.close()


def test_wrong_password(fake: FakeMDG) -> None:
    wrong = PAIR_ID + str((int(PAIR_PASSWORD) + 1) % 100000).zfill(5)
    with pytest.raises(WrongPairingCode):
        _pair(fake, wrong)
    fake.wait_for_pairing()
    assert fake.pairing_results == [False]
    assert fake.paired == []


def test_unknown_pair_id(fake: FakeMDG) -> None:
    with pytest.raises(PairingCodeExpired):
        _pair(fake, "99" + PAIR_ID + PAIR_PASSWORD)
    assert fake.pairing_results == []


def test_code_is_single_use(fake: FakeMDG) -> None:
    _pair(fake)
    with pytest.raises(PairingCodeExpired):
        _pair(fake)


def test_cc_does_not_pick_up(fake: FakeMDG) -> None:
    fake.pairing_routing_error = pr.ROUTING_PEER_TIMEOUT
    with pytest.raises(PairingCodeExpired):
        _pair(fake)


def test_cc_stops_answering(fake: FakeMDG) -> None:
    fake.pairing_stall = True
    with pytest.raises(PairingTimeout):
        pr.pair_with_code(PAIRING_CODE, hosts=("127.0.0.1",), port=fake.control_port, timeout=1)


def test_relay_unreachable() -> None:
    def factory(host: str, port: int, timeout: float) -> socket.socket:
        raise OSError("unreachable")

    with pytest.raises(CannotConnect):
        pr.pair_with_code(PAIRING_CODE, hosts=("a", "b"), socket_factory=factory)


def test_tls_group_29_brainpool_fallback() -> None:
    """Curve ID 29 with an 81-byte point is treated as brainpoolP320r1."""
    from ecdsa import BRAINPOOLP320r1, SigningKey

    from custom_components.danfoss_link.api import tls

    server = SigningKey.generate(curve=BRAINPOOLP320r1)
    server_point = server.verifying_key.to_string("uncompressed")
    shared, ours = tls._brainpool320_ecdh(server_point)
    peer = tls.VerifyingKey.from_string(ours, curve=BRAINPOOLP320r1).pubkey.point
    expected = (peer * server.privkey.secret_multiplier).x().to_bytes(40, "big")
    assert shared == expected
    assert len(ours) == 81
