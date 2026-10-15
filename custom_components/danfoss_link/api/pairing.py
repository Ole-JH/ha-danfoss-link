"""Pair a new identity with a CC using the code shown on the CC's display.

This is the app's "remote pairing" (`pair-remote otp=...`), rebuilt from its
Lua sources (`pairing_procedure`, `pace`, `mdg_pairing_pb`):

1. Create our own key and self-signed certificate (brainpoolP320r1).
2. On the MDG control connection send `place_pairing_call { pair_id }` (tag 32).
   The reply (tag 33) routes us to the CC like a normal `place_call`.
3. Open the end-to-end TLS session through the router as the TLS client,
   presenting our certificate.
4. Inside TLS, frames are `[u16 BE length][u8 tag][protobuf]`. Run PACE
   (password-authenticated key exchange on brainpoolP320r1) with the last
   five characters of the code as the password, bound to both TLS public keys:

       client_hello (1) ->     <- server_hello (2) {salt, iv, z}
       xy (3)           ->     <- xy (3)
       pk (4)           ->     <- pk (4)
       proof (5)        ->     <- proof (5)
       contact_info (6) ->     <- contact_info (6)

5. The CC stores our certificate; we store the CC's peer ID (the Subject Key
   Identifier of its TLS certificate) and whitelist it on the control connection.

Codes starting with "0000" are LAN pairing codes, which the app does without
the relay; those are not supported.
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import hmac
import logging
import secrets
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from ecdsa.curves import BRAINPOOLP320r1
from ecdsa.ellipticcurve import INFINITY, PointJacobi

from . import protobuf as pb
from .client import (
    DEFAULT_TIMEOUT,
    MDG_HOSTS,
    MDG_PORT,
    MT_PLACE_PAIRING_CALL,
    MT_PLACE_PAIRING_CALL_REPLY,
    PLACE_CALL_RESULTS,
    DanfossLinkClient,
    SocketFactory,
    _ControlConnection,
    _default_socket_factory,
)
from .credentials import (
    AppIdentity,
    generate_certificate,
    public_key_pem,
    subject_key_identifier,
)
from .errors import (
    CannotConnect,
    InvalidPairingCode,
    LanPairingNotSupported,
    PairingCodeExpired,
    PairingTimeout,
    ProtocolError,
    RoutingError,
    WrongPairingCode,
)
from .tls import TLS_ECDHE_ECDSA_AES256_GCM_SHA384, TLSClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Home Assistant"
PASSWORD_LENGTH = 5
LAN_CODE_PREFIX = "0000"
CODE_SEPARATORS = "- .,"

PACE_MAGIC = 0xF09D8CA7
PACE_VERSION = (1, 0)
PBKDF2_ITERATIONS = 1024

# Pairing message tags (mdg_pairing_pb)
PT_CLIENT_HELLO = 1
PT_SERVER_HELLO = 2
PT_XY = 3
PT_PK = 4
PT_PROOF = 5
PT_CONTACT_INFO = 6

ROUTING_PEER_TIMEOUT = 4

CURVE = BRAINPOOLP320r1
FIELD_LEN = 40  # bytes per coordinate on brainpoolP320r1
_ZERO_IV = b"\x00" * 16


# ------------------------------------------------------------------ code


def parse_pairing_code(code: str) -> tuple[str, str]:
    """Split a code as typed into (pair_id, password).

    Separators the user may type ('-', ' ', '.', ',') are dropped. The last
    five characters are the PACE password; the rest (leading zeros included)
    is the pair ID the relay knows the CC's pairing channel by.
    """
    cleaned = "".join(ch for ch in code.strip() if ch not in CODE_SEPARATORS)
    if len(cleaned) <= PASSWORD_LENGTH or not cleaned.isascii() or not cleaned.isalnum():
        raise InvalidPairingCode("That does not look like a pairing code from the CC")
    if cleaned.startswith(LAN_CODE_PREFIX):
        raise LanPairingNotSupported(
            "Codes starting with 0000 are for pairing on the local network, which is not "
            "supported. Choose remote pairing on the CC to get a code without 0000."
        )
    return cleaned[:-PASSWORD_LENGTH], cleaned[-PASSWORD_LENGTH:]


# ------------------------------------------------------------------ PACE


def encode_point(point: PointJacobi) -> bytes:
    """Uncompressed SEC1 encoding: 0x04 || X || Y (81 bytes)."""
    if point == INFINITY:
        raise ProtocolError("PACE produced the point at infinity")
    return b"\x04" + point.x().to_bytes(FIELD_LEN, "big") + point.y().to_bytes(FIELD_LEN, "big")


def decode_point(data: bytes) -> PointJacobi:
    """Decode a peer's point; anything not on the curve means a misbehaving peer."""
    if len(data) != 1 + 2 * FIELD_LEN or data[0] != 4:
        raise ProtocolError("PACE peer sent a malformed point")
    x = int.from_bytes(data[1 : 1 + FIELD_LEN], "big")
    y = int.from_bytes(data[1 + FIELD_LEN :], "big")
    p = CURVE.curve.p()
    if not (0 <= x < p and 0 <= y < p and CURVE.curve.contains_point(x, y)):
        raise ProtocolError("PACE peer sent a point that is not on the curve")
    return PointJacobi(CURVE.curve, x, y, 1, CURVE.order)


def random_scalar() -> int:
    return secrets.randbelow(CURVE.order - 1) + 1


def calculate_pi(password: str, server_pub_pem: bytes, client_pub_pem: bytes) -> bytes:
    """The PACE secret: each item as u16 BE length + bytes, then 0xFF."""
    out = b""
    for item in (password.encode("ascii"), server_pub_pem, client_pub_pem):
        out += struct.pack(">H", len(item)) + item
    return out + b"\xff"


def calculate_kpi(pi: bytes, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 (1024 rounds); the first 16 bytes are the nonce key."""
    return hashlib.pbkdf2_hmac("sha256", pi, salt, PBKDF2_ITERATIONS, 32)[:16]


def _nonce_cipher(kpi: bytes) -> Cipher:
    # The app's AES-256-CBC uses the 16-byte key zero padded to 32 bytes and a zero IV.
    return Cipher(algorithms.AES(kpi.ljust(32, b"\x00")), modes.CBC(_ZERO_IV))


def decode_nonce(kpi: bytes, z: bytes) -> int:
    if not z or len(z) % 16:
        raise ProtocolError("PACE server_hello has a malformed nonce")
    decryptor = _nonce_cipher(kpi).decryptor()
    plain = decryptor.update(z) + decryptor.finalize()
    return int.from_bytes(plain[:FIELD_LEN], "big")


def encode_nonce(kpi: bytes, nonce: int) -> bytes:
    """The CC's side of decode_nonce (used by the test fake)."""
    plain = nonce.to_bytes(FIELD_LEN, "big") + secrets.token_bytes(24)
    encryptor = _nonce_cipher(kpi).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


def proof_token(shared: PointJacobi, pk: PointJacobi) -> bytes:
    """HMAC-SHA256(key=SHA-256(K), msg=encoded pk) as lowercase hex ASCII."""
    kmac = hashlib.sha256(encode_point(shared)).digest()
    return hmac.new(kmac, encode_point(pk), hashlib.sha256).hexdigest().encode("ascii")


class PaceExchange:
    """One side of the PACE mapping/key-agreement steps after the nonce is known.

    Both sides run the same steps: anonymous ECDH to H, map G' = s*G + H,
    ECDH on G', exchange proofs over each other's public key.
    """

    def __init__(self, nonce: int) -> None:
        self.nonce = nonce
        self._a = random_scalar()
        self._b = random_scalar()
        self._my_pk: PointJacobi | None = None
        self._peer_pk: PointJacobi | None = None
        self._shared: PointJacobi | None = None

    def xy(self) -> bytes:
        return encode_point(CURVE.generator * self._a)

    def pk(self, peer_xy: bytes) -> bytes:
        h = decode_point(peer_xy) * self._a
        mapped = CURVE.generator * self.nonce + h
        if mapped == INFINITY:
            raise ProtocolError("PACE mapping produced the point at infinity")
        self._my_pk = mapped * self._b
        return encode_point(self._my_pk)

    def proof(self, peer_pk: bytes) -> bytes:
        assert self._my_pk is not None
        self._peer_pk = decode_point(peer_pk)
        self._shared = self._peer_pk * self._b
        return proof_token(self._shared, self._peer_pk)

    def verify(self, peer_proof: bytes) -> bool:
        assert self._shared is not None and self._my_pk is not None
        return hmac.compare_digest(peer_proof, proof_token(self._shared, self._my_pk))


# ---------------------------------------------------------------- client


class _PairingClient(DanfossLinkClient):
    """A control connection for an identity that is not paired with anything yet."""

    def _whitelist(self) -> list[str]:
        return []

    def place_pairing_call(self, pair_id: str) -> TLSClient:
        """Ask the relay to connect us to the CC waiting with `pair_id`."""
        if self._control is None:
            raise CannotConnect("No control connection")
        req_id = self._req_id()
        self._control.send(
            MT_PLACE_PAIRING_CALL, pb.varint_field(1, req_id) + pb.bytes_field(2, pair_id)
        )
        reply = self._await_control(MT_PLACE_PAIRING_CALL_REPLY, req_id)
        result = reply.int(2, 0)
        if result != 0:
            text = reply.str(4) or PLACE_CALL_RESULTS.get(result or 0, str(result))
            if result == 1:
                raise PairingCodeExpired(
                    f"No CC is waiting for this code ({text}): it has expired or was mistyped"
                )
            raise CannotConnect(f"place_pairing_call failed: {text}")
        routing = reply.message(3)
        routers = [] if routing is None else routing.messages(4)
        if routing is None or not routers or not routers[0].str(1) or not routers[0].int(2):
            raise ProtocolError("place_pairing_call reply without a router")
        # Like the app, only the first router is used.
        host, port = routers[0].str(1), routers[0].int(2)
        try:
            tls = self._connect_router(host, port, routing.bytes(5) or b"")  # type: ignore[arg-type]
        except RoutingError as err:
            if err.code == ROUTING_PEER_TIMEOUT:
                raise PairingCodeExpired("The CC did not pick up the pairing call") from err
            raise
        if tls.cipher != TLS_ECDHE_ECDSA_AES256_GCM_SHA384:
            _LOGGER.debug("Pairing session negotiated cipher 0x%04X", tls.cipher or 0)
        return tls


def _run_pace(stream: _ControlConnection, password: str, cc_cert: bytes, own_cert: bytes) -> None:
    """Client side of PACE over an established pairing stream."""

    def expect(tag: int) -> pb.Message:
        got, msg = stream.recv()
        if got != tag:
            raise ProtocolError(f"Expected pairing message {tag}, got {got}")
        return msg

    stream.send(
        PT_CLIENT_HELLO,
        pb.fixed32_field(1, PACE_MAGIC)
        + pb.varint_field(2, PACE_VERSION[0])
        + pb.varint_field(3, PACE_VERSION[1]),
    )
    hello = expect(PT_SERVER_HELLO)
    if (hello.int(1), hello.int(2)) != PACE_VERSION:
        raise ProtocolError(f"Unsupported PACE version {hello.int(1)}.{hello.int(2)}")
    pi = calculate_pi(password, public_key_pem(cc_cert), public_key_pem(own_cert))
    kpi = calculate_kpi(pi, hello.bytes(3) or b"")
    exchange = PaceExchange(decode_nonce(kpi, hello.bytes(5) or b""))  # iv is unused

    stream.send(PT_XY, pb.bytes_field(1, exchange.xy()))
    pk = exchange.pk(expect(PT_XY).bytes(1) or b"")
    stream.send(PT_PK, pb.bytes_field(1, pk))
    proof = exchange.proof(expect(PT_PK).bytes(1) or b"")
    stream.send(PT_PROOF, pb.bytes_field(1, proof))
    try:
        peer_proof = expect(PT_PROOF).bytes(1) or b""
    except CannotConnect as err:
        raise WrongPairingCode("The CC rejected the code") from err
    if not exchange.verify(peer_proof):
        raise WrongPairingCode("The code does not match the one shown on the CC")


def pair_with_code(
    code: str,
    *,
    name: str = DEFAULT_NAME,
    hosts: Sequence[str] = MDG_HOSTS,
    port: int = MDG_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    socket_factory: SocketFactory = _default_socket_factory,
) -> AppIdentity:
    """Pair a new identity with the CC showing `code` and return it.

    Blocking. The returned identity has `cert_pem`, `key_pem` and the CC's
    `peer_id`, ready for `DanfossLinkClient`.
    """
    pair_id, password = parse_pairing_code(code)
    cert_pem, key_pem = generate_certificate(name)
    # The CC's peer ID is not known yet; the placeholder is never sent.
    unpaired = AppIdentity.from_pem(cert_pem, key_pem, "00" * 20)
    client = _PairingClient(
        unpaired, hosts=hosts, port=port, timeout=timeout, socket_factory=socket_factory
    )
    tls: TLSClient | None = None
    try:
        client.connect_control()
        _LOGGER.debug("Placing pairing call for pair ID %s", pair_id)
        tls = client.place_pairing_call(pair_id)
        cc_cert = tls.server_certs[0] if tls.server_certs else b""
        if not cc_cert:
            raise ProtocolError("The CC did not present a certificate")
        stream = _ControlConnection(tls)
        _run_pace(stream, password, cc_cert, unpaired.cert_der)
        _LOGGER.debug("PACE succeeded, exchanging contact info")
        stream.send(PT_CONTACT_INFO, pb.bytes_field(1, name))
        got, info = stream.recv()
        if got != PT_CONTACT_INFO:
            raise ProtocolError(f"Expected contact_info, got pairing message {got}")
        cc_peer_id = subject_key_identifier(cc_cert)
        _LOGGER.debug("Paired with CC %s (%s)", cc_peer_id, info.str(1))
        stream.close()
        tls = None
        client.set_whitelist([cc_peer_id])
    except TimeoutError as err:
        raise PairingTimeout("The relay or the CC stopped answering") from err
    except OSError as err:
        raise CannotConnect(f"Pairing connection failed: {err}") from err
    except ValueError as err:
        raise ProtocolError(f"Malformed pairing message: {err}") from err
    finally:
        if tls is not None:
            tls.close()
        client.close()
    return AppIdentity.from_pem(cert_pem, key_pem, cc_peer_id)
