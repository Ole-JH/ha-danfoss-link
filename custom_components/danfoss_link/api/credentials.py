"""Loading and checking the app identity (certificate, private key, CC peer ID).

The Danfoss Link app generates a brainpoolP320r1 key whose PEM files use
explicit curve parameters. Neither OpenSSL nor `cryptography` handle that
curve, so the files are parsed with asn1crypto and the curve is matched by its
parameters against the curves known to `ecdsa`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import re

from asn1crypto import keys as asn1_keys, x509 as asn1_x509
from ecdsa import SigningKey, curves as ecdsa_curves
from ecdsa.curves import Curve

from .errors import InvalidCredentials

_PEM_RE = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)
PEER_ID_LENGTH = 20


def _pem_blocks(pem: str) -> list[tuple[str, bytes]]:
    blocks = []
    for label, body in _PEM_RE.findall(pem):
        try:
            blocks.append((label, base64.b64decode("".join(body.split()))))
        except ValueError as err:
            raise InvalidCredentials(f"Malformed base64 in {label} block") from err
    return blocks


def _curve_from_params(params: asn1_keys.ECDomainParameters) -> Curve:
    if params.name == "named":
        oid = params.chosen.dotted
        for curve in ecdsa_curves.curves:
            if ".".join(str(p) for p in curve.oid) == oid:
                return curve
        raise InvalidCredentials(f"Unsupported named curve {params.chosen.native}")
    if params.name == "specified":
        spec = params.chosen
        prime = spec["field_id"]["parameters"].native
        order = spec["order"].native
        a = int.from_bytes(spec["curve"]["a"].native, "big")
        b = int.from_bytes(spec["curve"]["b"].native, "big")
        for curve in ecdsa_curves.curves:
            cfp = curve.curve
            if (cfp.p(), curve.order, cfp.a(), cfp.b()) == (prime, order, a, b):
                return curve
        raise InvalidCredentials("Key uses unknown explicit curve parameters")
    raise InvalidCredentials("Key uses implicit curve parameters")


def normalize_peer_id(value: str) -> str:
    """Return the peer ID as lowercase colon-separated hex (`e7:af:...`).

    Accepts colons, underscores (pairing file names), spaces or no separators.
    """
    hex_digits = re.sub(r"[\s:_\-]", "", value.strip().strip('"')).lower()
    if not re.fullmatch(r"[0-9a-f]+", hex_digits) or len(hex_digits) != PEER_ID_LENGTH * 2:
        raise InvalidCredentials(
            f"Peer ID must be {PEER_ID_LENGTH} bytes of hex, e.g. e7:af:e3:..."
        )
    return ":".join(hex_digits[i : i + 2] for i in range(0, len(hex_digits), 2))


@dataclass(frozen=True)
class AppIdentity:
    """Everything needed to authenticate as the paired Danfoss Link app."""

    cert_der: bytes
    signing_key: SigningKey
    peer_id: str

    @classmethod
    def from_pem(cls, cert_pem: str, key_pem: str, peer_id: str) -> AppIdentity:
        certs = [der for label, der in _pem_blocks(cert_pem) if label == "CERTIFICATE"]
        if not certs:
            raise InvalidCredentials("No CERTIFICATE block found in the app certificate")
        cert_der = certs[0]
        try:
            cert = asn1_x509.Certificate.load(cert_der)
            cert_pub = cert.public_key
            cert_curve = _curve_from_params(cert_pub["algorithm"]["parameters"])
            cert_point = cert_pub["public_key"].native
        except InvalidCredentials:
            raise
        except Exception as err:  # asn1crypto raises assorted errors on bad input
            raise InvalidCredentials(f"Cannot parse app certificate: {err}") from err

        signing_key = cls._load_key(key_pem, cert_curve)
        ours = signing_key.verifying_key.to_string("uncompressed")
        if ours != cert_point:
            raise InvalidCredentials("The private key does not belong to the certificate")
        return cls(cert_der, signing_key, normalize_peer_id(peer_id))

    @staticmethod
    def _load_key(key_pem: str, cert_curve: Curve) -> SigningKey:
        blocks = _pem_blocks(key_pem)
        for label, der in blocks:
            try:
                if label == "PRIVATE KEY":
                    info = asn1_keys.PrivateKeyInfo.load(der)
                    ec_key = info["private_key"].parsed
                    params = info["private_key_algorithm"]["parameters"]
                elif label == "EC PRIVATE KEY":
                    ec_key = asn1_keys.ECPrivateKey.load(der)
                    params = ec_key["parameters"]
                else:
                    continue
                secret = ec_key["private_key"].native
                if isinstance(secret, bytes):
                    secret = int.from_bytes(secret, "big")
                curve = cert_curve
                if params.native is not None:
                    curve = _curve_from_params(params)
            except InvalidCredentials:
                raise
            except Exception as err:
                raise InvalidCredentials(f"Cannot parse app private key: {err}") from err
            if curve is not cert_curve:
                raise InvalidCredentials("Key and certificate use different curves")
            return SigningKey.from_secret_exponent(secret, curve=curve)
        raise InvalidCredentials("No PRIVATE KEY or EC PRIVATE KEY block found")
