"""Pure Casdoor credential protection and pinned RS256 certificate selection.

Deployment SECRET_KEY is injected by the caller. This module never reads config,
storage or the environment, and never migrates legacy provider ciphertext. Token
claims, certificate chain discovery and persistence belong to their own owners.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENVELOPE_VERSION = 1
MAX_PLAINTEXT_BYTES = 64 * 1024
MAX_ENVELOPE_BYTES = 90 * 1024
MAX_CERTIFICATE_BYTES = 16 * 1024
_KEY_VERSION = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z")


class CryptoError(ValueError):
    """Stable, non-sensitive domain error; no raw input is included in messages."""

    def __init__(self, code: str = "casdoor_crypto_invalid") -> None:
        self.code = code
        super().__init__(code)


class EncryptionPurpose(StrEnum):
    CONFIG_SECRET = "config_secret"
    AUTH_VERIFIER = "auth_verifier"
    LOGOUT_ID_TOKEN = "logout_id_token"
    AVATAR_URL = "avatar_url"


@dataclass(frozen=True)
class EncryptionContext:
    """Trusted DB/transaction identifiers, reconstructed by the calling owner.

    Config secrets bind to an immutable revision. Other short-lived values also
    bind to their individual record. UUID normalization prevents alternate textual
    representations from accidentally producing different authenticated contexts.
    """

    purpose: EncryptionPurpose
    namespace_id: UUID
    revision_id: UUID
    record_id: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "purpose", EncryptionPurpose(self.purpose))
            object.__setattr__(self, "namespace_id", UUID(str(self.namespace_id)))
            object.__setattr__(self, "revision_id", UUID(str(self.revision_id)))
        except (ValueError, TypeError, AttributeError):
            raise CryptoError() from None
        if self.purpose == EncryptionPurpose.CONFIG_SECRET:
            if self.record_id is not None:
                raise CryptoError()
        elif not _bounded_text(self.record_id, 128):
            raise CryptoError()

    def aad(self, key_version: str) -> bytes:
        return _canonical_json(
            {
                "envelope_version": ENVELOPE_VERSION,
                "key_version": key_version,
                "namespace_id": str(self.namespace_id),
                "purpose": self.purpose.value,
                "record_id": self.record_id,
                "revision_id": str(self.revision_id),
            }
        ).encode("utf-8")


def _bounded_text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: object) -> bytes:
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        raise CryptoError()
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise CryptoError() from None
    if _encode(result) != value:
        raise CryptoError()
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CryptoError()
        result[key] = value
    return result


class CasdoorCrypto:
    """AES-256-GCM with independently derived purpose keys and explicit rotation.

    Only the injected key/version can decrypt. To rotate, keep the old decryptor
    briefly in the trusted maintenance owner and call reencrypt with a new target
    decryptor before replacing SECRET_KEY. There is no automatic old-key fallback.
    """

    def __init__(self, *, secret_key: str | bytes, key_version: str) -> None:
        if not isinstance(key_version, str) or not _KEY_VERSION.fullmatch(key_version):
            raise CryptoError()
        try:
            material = secret_key.encode("utf-8") if isinstance(secret_key, str) else secret_key
        except UnicodeEncodeError:
            raise CryptoError() from None
        if not isinstance(material, bytes) or not material:
            raise CryptoError()
        self._material = material
        self.key_version = key_version

    def _derive_key(self, purpose: str) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"dify-plus:casdoor:hkdf:v1",
            info=_canonical_json({"key_version": self.key_version, "purpose": purpose}).encode("ascii"),
        ).derive(self._material)

    def encrypt(self, plaintext: str, *, context: EncryptionContext) -> str:
        if not isinstance(plaintext, str) or not isinstance(context, EncryptionContext):
            raise CryptoError()
        try:
            data = plaintext.encode("utf-8")
        except UnicodeEncodeError:
            raise CryptoError() from None
        if not data or len(data) > MAX_PLAINTEXT_BYTES:
            raise CryptoError()
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._derive_key(context.purpose.value)).encrypt(nonce, data, context.aad(self.key_version))
        return _canonical_json(
            {
                "envelope_version": ENVELOPE_VERSION,
                "key_version": self.key_version,
                "nonce": _encode(nonce),
                "ciphertext": _encode(ciphertext),
            }
        )

    def decrypt(self, envelope: str, *, context: EncryptionContext) -> str:
        if not isinstance(context, EncryptionContext) or not _bounded_text(envelope, MAX_ENVELOPE_BYTES):
            raise CryptoError()
        try:
            parsed = json.loads(envelope, object_pairs_hook=_unique_object)
        except (ValueError, TypeError, RecursionError):
            raise CryptoError() from None
        if not isinstance(parsed, dict) or set(parsed) != {"envelope_version", "key_version", "nonce", "ciphertext"}:
            raise CryptoError()
        if type(parsed["envelope_version"]) is not int or parsed["envelope_version"] != ENVELOPE_VERSION:
            raise CryptoError("casdoor_crypto_version_unsupported")
        if parsed["key_version"] != self.key_version:
            raise CryptoError("casdoor_crypto_key_version_unknown")
        nonce, ciphertext = _decode(parsed["nonce"]), _decode(parsed["ciphertext"])
        if len(nonce) != 12 or not 16 < len(ciphertext) <= MAX_PLAINTEXT_BYTES + 16:
            raise CryptoError()
        try:
            return (
                AESGCM(self._derive_key(context.purpose.value))
                .decrypt(nonce, ciphertext, context.aad(self.key_version))
                .decode("utf-8")
            )
        except (InvalidTag, UnicodeDecodeError):
            raise CryptoError("casdoor_crypto_decryption_failed") from None

    def reencrypt(
        self,
        envelope: str,
        *,
        source_context: EncryptionContext,
        target_context: EncryptionContext,
        target_crypto: "CasdoorCrypto | None" = None,
    ) -> str:
        """Preserve a blank replacement through decrypt/encrypt, never ciphertext copy."""
        plaintext = self.decrypt(envelope, context=source_context)
        return (target_crypto or self).encrypt(plaintext, context=target_context)

    def refresh_source_digest(self, refresh_token: str) -> str:
        """HMAC provenance only; this digest is never proof of a valid session."""
        if not _bounded_text(refresh_token, MAX_PLAINTEXT_BYTES):
            raise CryptoError()
        key = self._derive_key("refresh_source_hmac")
        return hmac.new(key, refresh_token.encode("utf-8"), hashlib.sha256).hexdigest()

    def matches_refresh_source(self, refresh_token: str, digest: str) -> bool:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
        return hmac.compare_digest(self.refresh_source_digest(refresh_token), digest)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise CryptoError("casdoor_certificate_invalid")
    return value


@dataclass(frozen=True, init=False)
class TrustedCertificate:
    """One explicitly pinned public X.509 certificate and half-open trust window."""

    pem: str
    kid: str | None
    fingerprint: str
    not_before: datetime
    accept_until: datetime
    _public_key: rsa.RSAPublicKey = field(repr=False, compare=False)

    def __init__(self, *, pem: str, not_before: datetime, accept_until: datetime, kid: str | None = None) -> None:
        if not _bounded_text(pem, MAX_CERTIFICATE_BYTES) or (kid is not None and not _bounded_text(kid, 128)):
            raise CryptoError("casdoor_certificate_invalid")
        # Reject private keys, bundles, and unrelated PEM data rather than letting
        # a permissive PEM parser silently select the first certificate.
        stripped = pem.strip()
        if not re.fullmatch(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----", stripped):
            raise CryptoError("casdoor_certificate_invalid")
        try:
            certificate = x509.load_pem_x509_certificate(stripped.encode("ascii"))
            public_key = certificate.public_key()
        except (ValueError, UnicodeEncodeError, UnsupportedAlgorithm):
            raise CryptoError("casdoor_certificate_invalid") from None
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
            raise CryptoError("casdoor_certificate_invalid")
        start, end = _utc(not_before), _utc(accept_until)
        if start < certificate.not_valid_before_utc or end > certificate.not_valid_after_utc or start >= end:
            raise CryptoError("casdoor_certificate_invalid")
        object.__setattr__(self, "pem", pem)
        object.__setattr__(self, "kid", kid)
        object.__setattr__(self, "fingerprint", certificate.fingerprint(hashes.SHA256()).hex())
        object.__setattr__(self, "not_before", start)
        object.__setattr__(self, "accept_until", end)
        object.__setattr__(self, "_public_key", public_key)

    def public_key_pem(self) -> bytes:
        """Public-only SDK input; does not bypass this owner's selection policy."""
        return self._public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


class CertificateTrustStore:
    """At most two pins; select only a uniquely successful RS256 signature.

    The caller supplies the exact JWS signing input/signature and protected kid.
    This is not an OIDC claims validator and must not itself authorize a login.
    """

    def __init__(self, certificates: Sequence[TrustedCertificate]) -> None:
        pins = tuple(certificates)
        if not 1 <= len(pins) <= 2 or any(not isinstance(pin, TrustedCertificate) for pin in pins):
            raise CryptoError("casdoor_certificate_invalid")
        kids = [pin.kid for pin in pins if pin.kid is not None]
        if len(set(kids)) != len(kids) or len({pin.fingerprint for pin in pins}) != len(pins):
            raise CryptoError("casdoor_certificate_invalid")
        self._certificates = pins

    def verify_rs256(
        self,
        signing_input: bytes,
        signature: bytes,
        *,
        kid: str | None,
        now: datetime,
        algorithm: str = "RS256",
    ) -> TrustedCertificate:
        if algorithm != "RS256" or not isinstance(signing_input, bytes) or not isinstance(signature, bytes):
            raise CryptoError("casdoor_signature_invalid")
        if kid is not None and not _bounded_text(kid, 128):
            raise CryptoError("casdoor_signature_invalid")
        checked_at = _utc(now)
        candidates = [
            pin
            for pin in self._certificates
            if pin.not_before <= checked_at < pin.accept_until and (kid is None or pin.kid == kid)
        ]
        verified = []
        for pin in candidates:
            try:
                pin._public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            except InvalidSignature:
                continue
            verified.append(pin)
        if len(verified) != 1:
            raise CryptoError("casdoor_signature_invalid")
        return verified[0]
