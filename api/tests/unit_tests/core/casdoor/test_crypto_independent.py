"""Independent boundary checks for the Casdoor crypto owner.

These cases target UTF-8 byte accounting and hostile JSON/base64 shapes beyond
the implementation author's focused examples. All values are synthetic.
"""

import base64
import json
from uuid import UUID

import pytest
from core.casdoor.crypto import (
    MAX_PLAINTEXT_BYTES,
    CasdoorCrypto,
    CryptoError,
    EncryptionContext,
    EncryptionPurpose,
)

NAMESPACE = UUID("10000000-0000-4000-8000-000000000001")
REVISION = UUID("20000000-0000-4000-8000-000000000001")
KEY = "independent-synthetic-deployment-key"


def _context() -> EncryptionContext:
    return EncryptionContext(EncryptionPurpose.CONFIG_SECRET, NAMESPACE, REVISION)


def _crypto() -> CasdoorCrypto:
    return CasdoorCrypto(secret_key=KEY, key_version="independent-v1")


def test_plaintext_limit_counts_utf8_bytes_at_the_boundary() -> None:
    protector = _crypto()
    exact = "密" * (MAX_PLAINTEXT_BYTES // 3) + "x"
    assert len(exact.encode("utf-8")) == MAX_PLAINTEXT_BYTES
    envelope = protector.encrypt(exact, context=_context())
    assert protector.decrypt(envelope, context=_context()) == exact

    with pytest.raises(CryptoError):
        protector.encrypt(exact + "x", context=_context())


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        b'{"envelope_version":1}',
        '{"envelope_version":1,"key_version":["independent-v1"],"nonce":"AA","ciphertext":"AA"}',
        '{"envelope_version":1,"key_version":"independent-v1","nonce":"AB","ciphertext":"AA"}',
        '{"envelope_version":1,"key_version":"independent-v1","nonce":"AA","ciphertext":"AA","extra":true}',
    ],
)
def test_untrusted_envelope_shapes_fail_as_domain_errors(envelope: object) -> None:
    with pytest.raises(CryptoError):
        _crypto().decrypt(envelope, context=_context())  # type: ignore[arg-type]


def test_ciphertext_cannot_be_reinterpreted_as_utf8_even_with_valid_gcm_tag() -> None:
    protector = _crypto()
    nonce = b"n" * 12
    ciphertext = protector._derive_key(EncryptionPurpose.CONFIG_SECRET.value)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    sealed = AESGCM(ciphertext).encrypt(nonce, b"\xff", _context().aad(protector.key_version))
    envelope = json.dumps(
        {
            "envelope_version": 1,
            "key_version": protector.key_version,
            "nonce": base64.urlsafe_b64encode(nonce).rstrip(b"=").decode("ascii"),
            "ciphertext": base64.urlsafe_b64encode(sealed).rstrip(b"=").decode("ascii"),
        }
    )
    with pytest.raises(CryptoError, match="casdoor_crypto_decryption_failed"):
        protector.decrypt(envelope, context=_context())
