"""A small TLS 1.2 client (ECDHE-ECDSA with AES-GCM) for the Danfoss MDG network.

Why not `ssl`? The MDG servers, the CC and the app identity all use
brainpoolP320r1 certificates with explicit curve parameters, which OpenSSL
refuses, and the CC only accepts a ClientHello shaped like the one sent by
the app's bundled LibreSSL. Key exchange and bulk encryption use standard
primitives from `cryptography`; brainpool signatures use `ecdsa`.

The handshake logic is derived from the MIT-licensed client in
https://github.com/dfhdsrfjgdf/ha-danfoss-link (see NOTICE).

Limitation: like that client, this does not validate the peer certificate
chain. The ServerKeyExchange signature is checked against the presented
certificate, which protects against a broken handshake but not against an
active man-in-the-middle presenting its own certificate.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import socket
import struct

from asn1crypto import x509 as asn1_x509
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from ecdsa import BRAINPOOLP320r1, SigningKey, VerifyingKey
from ecdsa.util import sigdecode_der, sigencode_der

from .credentials import _curve_from_params
from .errors import CannotConnect, ProtocolError

CT_CHANGE_CIPHER_SPEC = 20
CT_ALERT = 21
CT_HANDSHAKE = 22
CT_APPLICATION_DATA = 23

HS_CLIENT_HELLO = 1
HS_SERVER_HELLO = 2
HS_CERTIFICATE = 11
HS_SERVER_KEY_EXCHANGE = 12
HS_CERTIFICATE_REQUEST = 13
HS_SERVER_HELLO_DONE = 14
HS_CERTIFICATE_VERIFY = 15
HS_CLIENT_KEY_EXCHANGE = 16
HS_FINISHED = 20

TLS_ECDHE_ECDSA_AES128_GCM_SHA256 = 0xC02B
TLS_ECDHE_ECDSA_AES256_GCM_SHA384 = 0xC02C
_CIPHERS = {
    TLS_ECDHE_ECDSA_AES128_GCM_SHA256: (16, "sha256"),
    TLS_ECDHE_ECDSA_AES256_GCM_SHA384: (32, "sha384"),
}

_ECDHE_GROUPS: dict[int, ec.EllipticCurve | None] = {
    0x0017: ec.SECP256R1(),
    0x0018: ec.SECP384R1(),
    0x0019: ec.SECP521R1(),
    0x001D: None,  # x25519
}
_BRAINPOOL320_POINT_LEN = 1 + 2 * 40


def _brainpool320_ecdh(server_point: bytes) -> tuple[bytes, bytes]:
    """ECDH on brainpoolP320r1; returns (shared x-coordinate, our uncompressed point)."""
    peer = VerifyingKey.from_string(server_point, curve=BRAINPOOLP320r1).pubkey.point
    ours = SigningKey.generate(curve=BRAINPOOLP320r1)
    shared = peer * ours.privkey.secret_multiplier
    return shared.x().to_bytes(40, "big"), ours.verifying_key.to_string("uncompressed")


_SIG_HASHES = {2: "sha1", 3: "sha224", 4: "sha256", 5: "sha384", 6: "sha512"}

EXT_SERVER_NAME = 0x0000
EXT_SUPPORTED_GROUPS = 0x000A
EXT_EC_POINT_FORMATS = 0x000B
EXT_SIGNATURE_ALGORITHMS = 0x000D
EXT_HEARTBEAT = 0x000F
EXT_PADDING = 0x0015
EXT_EXTENDED_MASTER_SECRET = 0x0017
EXT_SIGNATURE_ALGORITHMS_CERT = 0x0032
EXT_RENEGOTIATION_INFO = 0xFF01

# The exact ClientHello offer of the Danfoss Link app (LibreSSL). The CC
# rejects other shapes with a handshake_failure alert.
_APP_CIPHERS = [
    0xC030, 0xC02C, 0xC028, 0xC024, 0xC014, 0xC00A, 0x00A5, 0x00A3, 0x00A1,
    0x009F, 0x006B, 0x006A, 0x0069, 0x0068, 0x0039, 0x0038, 0x0037, 0x0036,
    0xC032, 0xC02E, 0xC02A, 0xC026, 0xC00F, 0xC005, 0x009D, 0x003D, 0x0035,
    0xC02F, 0xC02B, 0xC027, 0xC023, 0xC013, 0xC009, 0x00A4, 0x00A2, 0x00A0,
    0x009E, 0x0067, 0x0040, 0x003F, 0x003E, 0x0033, 0x0032, 0x0031, 0x0030,
    0xC031, 0xC02D, 0xC029, 0xC025, 0xC00E, 0xC004, 0x009C, 0x003C, 0x002F,
    0x0007, 0xC012, 0xC008, 0x0016, 0x0013, 0x0010, 0x000D, 0xC00D, 0xC003,
    0x000A, 0x00FF,
]  # fmt: skip
_APP_GROUPS = [
    0x0017, 0x001D, 0x0019, 0x001C, 0x001B, 0x0018, 0x001A,
    0x0016, 0x000E, 0x000D, 0x000B, 0x000C, 0x0009, 0x000A,
]  # fmt: skip
_APP_SIGALGS = [
    0x0601, 0x0602, 0x0603, 0x0501, 0x0502, 0x0503, 0x0401, 0x0402,
    0x0403, 0x0301, 0x0302, 0x0303, 0x0201, 0x0202, 0x0203,
]  # fmt: skip
_SIGALGS = [0x0403, 0x0503, 0x0603, 0x0401, 0x0501, 0x0601]

PROFILE_CONTROL = "control"
PROFILE_APP = "app"


def _u8(v: int) -> bytes:
    return bytes([v])


def _u16(v: int) -> bytes:
    return struct.pack(">H", v)


def _u24(v: int) -> bytes:
    return struct.pack(">I", v)[1:]


def _vec8(b: bytes) -> bytes:
    return _u8(len(b)) + b


def _vec16(b: bytes) -> bytes:
    return _u16(len(b)) + b


def _vec24(b: bytes) -> bytes:
    return _u24(len(b)) + b


def _ext(ext_type: int, body: bytes) -> bytes:
    return _u16(ext_type) + _vec16(body)


def _u16_list(values: list[int]) -> bytes:
    return b"".join(_u16(v) for v in values)


def _handshake_msg(msg_type: int, body: bytes) -> bytes:
    return _u8(msg_type) + _vec24(body)


def prf(secret: bytes, label: bytes, seed: bytes, length: int, hash_name: str) -> bytes:
    """TLS 1.2 PRF (RFC 5246 section 5)."""
    digest = getattr(hashlib, hash_name)
    seed = label + seed
    out = b""
    a = seed
    while len(out) < length:
        a = hmac.new(secret, a, digest).digest()
        out += hmac.new(secret, a + seed, digest).digest()
    return out[:length]


class TLSClient:
    """A blocking TLS 1.2 client over an already connected socket."""

    def __init__(
        self,
        sock: socket.socket,
        cert_der: bytes,
        signing_key: SigningKey,
        *,
        profile: str = PROFILE_CONTROL,
        server_name: str | None = None,
        initial_data: bytes = b"",
    ) -> None:
        self.sock = sock
        self._cert_der = cert_der
        self._signing_key = signing_key
        self._profile = profile
        self._server_name = server_name
        self._rbuf = initial_data  # bytes already read off the socket by the caller
        self._hs_buf = b""
        self._transcript = b""
        self._hash_name = "sha256"
        self._client_key = self._server_key = b""
        self._client_iv = self._server_iv = b""
        self._client_seq = self._server_seq = 0
        self.server_certs: list[bytes] = []
        self.cipher: int | None = None
        self.group: int | None = None

    # ------------------------------------------------------------------ I/O

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rbuf) < n:
            data = self.sock.recv(16384)
            if not data:
                raise CannotConnect("Connection closed by peer")
            self._rbuf += data
        out, self._rbuf = self._rbuf[:n], self._rbuf[n:]
        return out

    def _read_record(self) -> tuple[int, bytes]:
        header = self._recv_exact(5)
        length = struct.unpack(">H", header[3:5])[0]
        if length > 16384 + 2048:
            raise ProtocolError(f"TLS record too large ({length} bytes)")
        return header[0], self._recv_exact(length)

    def _send_record(self, content_type: int, fragment: bytes, version: int = 0x0303) -> None:
        self.sock.sendall(_u8(content_type) + _u16(version) + _vec16(fragment))

    def has_buffered_data(self) -> bool:
        """True if bytes are already read from the socket but not yet returned."""
        return bool(self._rbuf)

    # ------------------------------------------------------------ handshake

    def _read_handshake(self) -> tuple[int, bytes]:
        buf, self._hs_buf = self._hs_buf, b""
        while len(buf) < 4 or len(buf) < 4 + int.from_bytes(buf[1:4], "big"):
            content_type, fragment = self._read_record()
            if content_type == CT_ALERT:
                raise ProtocolError(f"TLS alert during handshake: {fragment.hex()}")
            if content_type != CT_HANDSHAKE:
                raise ProtocolError(f"Expected handshake record, got type {content_type}")
            buf += fragment
        length = int.from_bytes(buf[1:4], "big")
        message = buf[: 4 + length]
        # A peer may pack several handshake messages into one record.
        self._hs_buf = buf[4 + length :]
        self._transcript += message
        return message[0], message[4:]

    def _client_hello(self, client_random: bytes) -> bytes:
        if self._profile == PROFILE_APP:
            ciphers, exts = (
                _APP_CIPHERS,
                (
                    _ext(EXT_EC_POINT_FORMATS, _vec8(b"\x03\x00\x01\x02"))
                    + _ext(EXT_SUPPORTED_GROUPS, _vec16(_u16_list(_APP_GROUPS)))
                    + _ext(EXT_SIGNATURE_ALGORITHMS, _vec16(_u16_list(_APP_SIGALGS)))
                    + _ext(EXT_HEARTBEAT, b"\x01")
                    + _ext(EXT_PADDING, b"\x00" * 250)
                ),
            )
        else:
            ciphers = list(_CIPHERS)[::-1]
            exts = (
                _ext(EXT_SIGNATURE_ALGORITHMS, _vec16(_u16_list(_SIGALGS)))
                + _ext(EXT_SUPPORTED_GROUPS, _vec16(_u16_list([0x0017])))
                + _ext(EXT_EC_POINT_FORMATS, _vec8(b"\x00"))
                + _ext(EXT_RENEGOTIATION_INFO, b"\x00")
                + _ext(EXT_EXTENDED_MASTER_SECRET, b"")
            )
            if self._server_name:
                name = _u8(0) + _vec16(self._server_name.encode())
                exts += _ext(EXT_SERVER_NAME, _vec16(name))
            exts += _ext(EXT_SIGNATURE_ALGORITHMS_CERT, _vec16(_u16_list(_SIGALGS)))
        body = (
            _u16(0x0303)
            + client_random
            + _vec8(b"")
            + _vec16(_u16_list(ciphers))
            + _vec8(b"\x00")
            + _vec16(exts)
        )
        return _handshake_msg(HS_CLIENT_HELLO, body)

    def handshake(self) -> None:
        client_random = os.urandom(32)
        hello = self._client_hello(client_random)
        self._transcript += hello
        self._send_record(CT_HANDSHAKE, hello, 0x0301 if self._profile == PROFILE_APP else 0x0303)

        server_random = b""
        ske = None
        cert_requested = False
        ems = False
        while True:
            msg_type, body = self._read_handshake()
            if msg_type == HS_SERVER_HELLO:
                server_random, ems = self._parse_server_hello(body)
            elif msg_type == HS_CERTIFICATE:
                self._parse_certificates(body)
            elif msg_type == HS_SERVER_KEY_EXCHANGE:
                ske = body
            elif msg_type == HS_CERTIFICATE_REQUEST:
                cert_requested = True
            elif msg_type == HS_SERVER_HELLO_DONE:
                break
        if not server_random or ske is None or not self.server_certs:
            raise ProtocolError("Incomplete TLS server handshake")

        server_point = self._verify_server_key_exchange(client_random, server_random, ske)
        premaster, our_point = self._ecdhe(server_point)

        flight = b""
        if cert_requested:
            flight += _handshake_msg(HS_CERTIFICATE, _vec24(_vec24(self._cert_der)))
        flight += _handshake_msg(HS_CLIENT_KEY_EXCHANGE, _vec8(our_point))

        if ems:
            session_hash = getattr(hashlib, self._hash_name)(self._transcript + flight).digest()
            master = prf(premaster, b"extended master secret", session_hash, 48, self._hash_name)
        else:
            master = prf(
                premaster, b"master secret", client_random + server_random, 48, self._hash_name
            )
        key_len, _ = _CIPHERS[self.cipher]  # type: ignore[index]
        block = prf(
            master, b"key expansion", server_random + client_random,
            2 * (key_len + 4), self._hash_name,
        )  # fmt: skip
        self._client_key = block[:key_len]
        self._server_key = block[key_len : 2 * key_len]
        self._client_iv = block[2 * key_len : 2 * key_len + 4]
        self._server_iv = block[2 * key_len + 4 : 2 * key_len + 8]

        if cert_requested:
            digest = hashlib.sha256(self._transcript + flight).digest()
            signature = self._signing_key.sign_digest(digest, sigencode=sigencode_der)
            flight += _handshake_msg(HS_CERTIFICATE_VERIFY, _u16(0x0403) + _vec16(signature))

        self._transcript += flight
        self._send_record(CT_HANDSHAKE, flight)
        self._send_record(CT_CHANGE_CIPHER_SPEC, b"\x01")
        verify = prf(master, b"client finished", self._transcript_hash(), 12, self._hash_name)
        finished = _handshake_msg(HS_FINISHED, verify)
        self._send_encrypted(CT_HANDSHAKE, finished)
        self._transcript += finished

        while True:
            content_type, fragment = self._read_record()
            if content_type == CT_CHANGE_CIPHER_SPEC:
                break
            if content_type == CT_ALERT:
                raise ProtocolError(f"TLS alert before ChangeCipherSpec: {fragment.hex()}")
        expected = prf(master, b"server finished", self._transcript_hash(), 12, self._hash_name)
        content_type, plaintext = self._read_encrypted()
        if (
            content_type != CT_HANDSHAKE
            or plaintext[:1] != bytes([HS_FINISHED])
            or not hmac.compare_digest(plaintext[4:16], expected)
        ):
            raise ProtocolError("TLS server Finished did not verify")

    def _transcript_hash(self) -> bytes:
        return getattr(hashlib, self._hash_name)(self._transcript).digest()

    def _parse_server_hello(self, body: bytes) -> tuple[bytes, bool]:
        if body[:2] != b"\x03\x03":
            raise ProtocolError(f"Server chose TLS version {body[:2].hex()}")
        server_random = body[2:34]
        pos = 35 + body[34]
        self.cipher = struct.unpack_from(">H", body, pos)[0]
        if self.cipher not in _CIPHERS:
            raise ProtocolError(f"Server chose unsupported cipher 0x{self.cipher:04X}")
        self._hash_name = _CIPHERS[self.cipher][1]
        pos += 3
        ems = False
        if pos + 2 <= len(body):
            end = pos + 2 + struct.unpack_from(">H", body, pos)[0]
            pos += 2
            while pos + 4 <= end:
                ext_type, ext_len = struct.unpack_from(">HH", body, pos)
                if ext_type == EXT_EXTENDED_MASTER_SECRET:
                    ems = True
                pos += 4 + ext_len
        return server_random, ems

    def _parse_certificates(self, body: bytes) -> None:
        total = int.from_bytes(body[:3], "big")
        pos = 3
        while pos < 3 + total:
            length = int.from_bytes(body[pos : pos + 3], "big")
            pos += 3
            self.server_certs.append(body[pos : pos + length])
            pos += length

    def _verify_server_key_exchange(
        self, client_random: bytes, server_random: bytes, ske: bytes
    ) -> bytes:
        """Check the ServerKeyExchange signature and return the server's ECDHE point."""
        if ske[0] != 3:
            raise ProtocolError("ServerKeyExchange does not use a named curve")
        self.group = struct.unpack_from(">H", ske, 1)[0]
        point_len = ske[3]
        params = ske[: 4 + point_len]
        server_point = ske[4 : 4 + point_len]
        sig_alg, sig_len = struct.unpack_from(">HH", ske, 4 + point_len)
        signature = ske[8 + point_len : 8 + point_len + sig_len]
        if sig_alg & 0xFF != 3 or (sig_alg >> 8) not in _SIG_HASHES:
            raise ProtocolError(f"Unsupported ServerKeyExchange signature 0x{sig_alg:04X}")

        try:
            key_info = asn1_x509.Certificate.load(self.server_certs[0]).public_key
            curve = _curve_from_params(key_info["algorithm"]["parameters"])
            verifying_key = VerifyingKey.from_string(key_info["public_key"].native, curve=curve)
        except Exception as err:
            raise ProtocolError(f"Cannot use the server certificate key: {err}") from err
        digest = getattr(hashlib, _SIG_HASHES[sig_alg >> 8])(
            client_random + server_random + params
        ).digest()
        try:
            verifying_key.verify_digest(
                signature, digest, sigdecode=sigdecode_der, allow_truncate=True
            )
        except Exception as err:
            raise ProtocolError("ServerKeyExchange signature is invalid") from err
        return server_point

    def _ecdhe(self, server_point: bytes) -> tuple[bytes, bytes]:
        if self.group not in _ECDHE_GROUPS:
            raise ProtocolError(f"Server chose unsupported ECDHE group 0x{self.group:04X}")
        curve = _ECDHE_GROUPS[self.group]
        try:
            if self.group == 0x001D and len(server_point) == _BRAINPOOL320_POINT_LEN:
                # The app's patched OpenSSL uses ID 29 for brainpoolP320r1, not x25519.
                return _brainpool320_ecdh(server_point)
            if curve is None:
                private = x25519.X25519PrivateKey.generate()
                peer = x25519.X25519PublicKey.from_public_bytes(server_point)
                ours = private.public_key().public_bytes_raw()
                return private.exchange(peer), ours
            ec_private = ec.generate_private_key(curve)
            ec_peer = ec.EllipticCurvePublicKey.from_encoded_point(curve, server_point)
            ours = ec_private.public_key().public_bytes(
                Encoding.X962, PublicFormat.UncompressedPoint
            )
            return ec_private.exchange(ec.ECDH(), ec_peer), ours
        except ValueError as err:
            raise ProtocolError(f"Invalid ECDHE public key from server: {err}") from err

    # --------------------------------------------------------- record layer

    def _send_encrypted(self, content_type: int, plaintext: bytes) -> None:
        explicit = self._client_seq.to_bytes(8, "big")
        aad = explicit + _u8(content_type) + _u16(0x0303) + _u16(len(plaintext))
        ciphertext = AESGCM(self._client_key).encrypt(self._client_iv + explicit, plaintext, aad)
        self._client_seq += 1
        self._send_record(content_type, explicit + ciphertext)

    def _read_encrypted(self) -> tuple[int, bytes]:
        content_type, fragment = self._read_record()
        if len(fragment) < 24:
            raise ProtocolError("Encrypted TLS record too short")
        explicit, body = fragment[:8], fragment[8:]
        aad = (
            self._server_seq.to_bytes(8, "big")
            + _u8(content_type)
            + _u16(0x0303)
            + _u16(len(body) - 16)
        )
        try:
            plaintext = AESGCM(self._server_key).decrypt(self._server_iv + explicit, body, aad)
        except Exception as err:
            raise ProtocolError("TLS record failed authentication") from err
        self._server_seq += 1
        return content_type, plaintext

    def send(self, data: bytes) -> None:
        for i in range(0, len(data), 16384):
            self._send_encrypted(CT_APPLICATION_DATA, data[i : i + 16384])

    def recv(self) -> bytes:
        """Return the plaintext of the next application data record."""
        while True:
            content_type, plaintext = self._read_encrypted()
            if content_type == CT_APPLICATION_DATA:
                return plaintext
            if content_type == CT_ALERT:
                if plaintext[1:2] == b"\x00":
                    raise CannotConnect("Peer closed the TLS session")
                raise ProtocolError(f"TLS alert: {plaintext.hex()}")
            # Ignore post-handshake handshake messages such as HelloRequest.

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()
