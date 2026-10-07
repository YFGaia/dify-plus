"""Pure bounded public JWKS parsing and RS256 signature selection.

The caller admits the remote source and owns caching. This owner never reads
configuration, performs I/O, follows key URLs, or treats a signature as identity.
"""

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.casdoor.crypto import CryptoError

MAX_JWKS_BYTES = 256 * 1024
MAX_SIGNING_KEYS = 16
MIN_RSA_BITS = 2048
MAX_RSA_BITS = 8192
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_PRIVATE_PARAMETERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})


def _invalid() -> Any:
    raise CryptoError("casdoor_signing_keys_invalid") from None


def _text(value: object, limit: int) -> bool:
    try:
        return (
            type(value) is str
            and bool(value)
            and len(value.encode("utf-8")) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except UnicodeError:
        return False


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _bad_constant(value: str) -> Any:
    return _invalid()


def _raw(raw: dict[str, Any] | bytes) -> dict[str, Any]:
    try:
        if type(raw) is dict:
            data = json.dumps(raw, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        elif type(raw) is bytes:
            data = raw
        else:
            return _invalid()
        if not data or len(data) > MAX_JWKS_BYTES:
            return _invalid()
        parsed = json.loads(data, object_pairs_hook=_pairs, parse_constant=_bad_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return _invalid()
    if type(parsed) is not dict:
        return _invalid()
    return parsed


def _integer(value: object, max_bytes: int) -> int:
    if type(value) is not str or len(value) > ((max_bytes + 2) // 3) * 4 or not _BASE64URL.fullmatch(value):
        return _invalid()
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return _invalid()
    if not raw or len(raw) > max_bytes or raw[0] == 0:
        return _invalid()
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        return _invalid()
    return int.from_bytes(raw, "big")


@dataclass(frozen=True)
class TrustedSigningKey:
    """One admitted public RSA JWK; fingerprint is the RFC 7638 thumbprint."""

    kid: str | None
    fingerprint: str
    _public_key: rsa.RSAPublicKey = field(repr=False, compare=False)

    def public_key_pem(self) -> bytes:
        return self._public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


class SigningKeyTrustStore:
    """Fixed immutable key snapshot with unique successful signature selection."""

    def __init__(self, keys: tuple[TrustedSigningKey, ...]) -> None:
        if not 1 <= len(keys) <= MAX_SIGNING_KEYS or any(not isinstance(key, TrustedSigningKey) for key in keys):
            _invalid()
        kids = [key.kid for key in keys if key.kid is not None]
        # Identical RSA material under different labels is ambiguous without kid.
        if len(set(kids)) != len(kids) or len({key.fingerprint for key in keys}) != len(keys):
            _invalid()
        self._keys = keys

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(key.fingerprint for key in self._keys)

    @classmethod
    def from_jwks(cls, raw: dict[str, Any] | bytes) -> "SigningKeyTrustStore":
        parsed = _raw(raw)
        values = parsed.get("keys")
        if type(values) is not list or not 1 <= len(values) <= MAX_SIGNING_KEYS:
            _invalid()
        keys: list[TrustedSigningKey] = []
        seen_kids: set[str] = set()
        for value in values:
            if type(value) is not dict or _PRIVATE_PARAMETERS.intersection(value):
                _invalid()
            kid = value.get("kid")
            if "kid" in value:
                if not _text(kid, 128) or kid in seen_kids:
                    _invalid()
                seen_kids.add(kid)
            for label in ("kty", "alg", "use"):
                if label in value and not _text(value[label], 32):
                    _invalid()
            key_ops = value.get("key_ops")
            if "key_ops" in value and (
                type(key_ops) is not list
                or not key_ops
                or any(not _text(op, 32) for op in key_ops)
                or len(set(key_ops)) != len(key_ops)
            ):
                _invalid()
            if (
                value.get("kty") != "RSA"
                or value.get("alg", "RS256") != "RS256"
                or value.get("use", "sig") != "sig"
                or (key_ops is not None and key_ops != ["verify"])
            ):
                continue
            modulus = _integer(value.get("n"), MAX_RSA_BITS // 8)
            exponent = _integer(value.get("e"), 8)
            if (
                not MIN_RSA_BITS <= modulus.bit_length() <= MAX_RSA_BITS
                or modulus % 2 != 1
                or not 3 <= exponent <= 2**32 - 1
            ):
                _invalid()
            try:
                public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            except (ValueError, UnsupportedAlgorithm):
                _invalid()
            if "x5c" in value:
                chain = value["x5c"]
                if type(chain) is not list or not 1 <= len(chain) <= 8:
                    _invalid()
                for index, encoded in enumerate(chain):
                    if type(encoded) is not str or not encoded or len(encoded) > 24 * 1024:
                        _invalid()
                    try:
                        certificate = x509.load_der_x509_certificate(base64.b64decode(encoded, validate=True))
                        described_key = certificate.public_key()
                    except (ValueError, binascii.Error, UnsupportedAlgorithm):
                        _invalid()
                    if index == 0 and (
                        not isinstance(described_key, rsa.RSAPublicKey)
                        or described_key.public_numbers() != public_key.public_numbers()
                    ):
                        _invalid()
            canonical = json.dumps(
                {"e": value["e"], "kty": "RSA", "n": value["n"]}, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
            fingerprint = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode("ascii")
            keys.append(TrustedSigningKey(kid=kid, fingerprint=fingerprint, _public_key=public_key))
        return cls(tuple(keys))

    def verify_rs256(
        self,
        signing_input: bytes,
        signature: bytes,
        *,
        kid: str | None,
        now: datetime,
        algorithm: str = "RS256",
    ) -> TrustedSigningKey:
        if (
            algorithm != "RS256"
            or type(signing_input) is not bytes
            or type(signature) is not bytes
            or not isinstance(now, datetime)
            or now.utcoffset() != timedelta(0)
            or (kid is not None and not _text(kid, 128))
        ):
            raise CryptoError("casdoor_signature_invalid")
        verified: list[TrustedSigningKey] = []
        for key in self._keys:
            if kid is not None and key.kid != kid:
                continue
            try:
                key._public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            except (InvalidSignature, ValueError):
                continue
            verified.append(key)
        if len(verified) != 1:
            raise CryptoError("casdoor_signature_invalid")
        return verified[0]


@dataclass(frozen=True)
class SigningKeySnapshot:
    """Trusted current set; metadata is suitable for revision-bound diagnosis."""

    trust_store: SigningKeyTrustStore
    fingerprints: tuple[str, ...]
    source_url: str
    profile: str
    fetched_at: float
    namespace_id: UUID
    revision_id: UUID
    config_digest: str
