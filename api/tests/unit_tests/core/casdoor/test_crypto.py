"""Offline crypto contracts; all secrets are synthetic, all keys generated in memory."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from core.casdoor.crypto import (
    MAX_CERTIFICATE_BYTES,
    MAX_PLAINTEXT_BYTES,
    CasdoorCrypto,
    CertificateTrustStore,
    CryptoError,
    EncryptionContext,
    EncryptionPurpose,
    TrustedCertificate,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

NAMESPACE = UUID("10000000-0000-4000-8000-000000000001")
REVISION = UUID("20000000-0000-4000-8000-000000000001")
NEXT_REVISION = UUID("20000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
SYNTHETIC_DEPLOYMENT_KEY = "synthetic-deployment-key-for-offline-unit-tests-only"
SYNTHETIC_CLIENT_SECRET = "synthetic-client-secret-for-offline-unit-tests-only-密钥"


def crypto() -> CasdoorCrypto:
    return CasdoorCrypto(secret_key=SYNTHETIC_DEPLOYMENT_KEY, key_version="deployment-v1")


def config_context(revision: UUID = REVISION) -> EncryptionContext:
    return EncryptionContext(EncryptionPurpose.CONFIG_SECRET, NAMESPACE, revision)


def record_context(purpose: EncryptionPurpose, record_id: str = "synthetic-record-1") -> EncryptionContext:
    return EncryptionContext(purpose, NAMESPACE, REVISION, record_id)


def issue_certificate(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    *,
    serial: int = 1,
) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic offline certificate")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


@pytest.fixture(scope="module")
def keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def pin(pem: str, kid: str | None = None, **times: datetime) -> TrustedCertificate:
    return TrustedCertificate(
        pem=pem,
        kid=kid,
        not_before=times.get("not_before", NOW - timedelta(hours=1)),
        accept_until=times.get("accept_until", NOW + timedelta(hours=1)),
    )


def sign(key: rsa.RSAPrivateKey, data: bytes = b"synthetic.jws-signing-input") -> bytes:
    return key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def test_roundtrip_is_random_and_has_no_plaintext() -> None:
    protector = crypto()
    first = protector.encrypt(SYNTHETIC_CLIENT_SECRET, context=config_context())
    second = protector.encrypt(SYNTHETIC_CLIENT_SECRET, context=config_context())
    assert first != second
    assert json.loads(first)["nonce"] != json.loads(second)["nonce"]
    assert SYNTHETIC_CLIENT_SECRET not in first
    assert protector.decrypt(first, context=config_context()) == SYNTHETIC_CLIENT_SECRET


@pytest.mark.parametrize("field", ["nonce", "ciphertext"])
def test_tampering_is_authenticated(field: str) -> None:
    protector = crypto()
    envelope = json.loads(protector.encrypt(SYNTHETIC_CLIENT_SECRET, context=config_context()))
    decoded = bytearray(base64.urlsafe_b64decode(envelope[field] + "=" * (-len(envelope[field]) % 4)))
    decoded[0] ^= 1
    envelope[field] = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    with pytest.raises(CryptoError, match="casdoor_crypto_decryption_failed") as error:
        protector.decrypt(json.dumps(envelope), context=config_context())
    assert SYNTHETIC_CLIENT_SECRET not in str(error.value)


@pytest.mark.parametrize(
    "wrong_context",
    [
        config_context(NEXT_REVISION),
        EncryptionContext(EncryptionPurpose.CONFIG_SECRET, NEXT_REVISION, REVISION),
        record_context(EncryptionPurpose.LOGOUT_ID_TOKEN),
    ],
)
def test_namespace_revision_and_purpose_are_authenticated(wrong_context: EncryptionContext) -> None:
    protector = crypto()
    envelope = protector.encrypt(SYNTHETIC_CLIENT_SECRET, context=config_context())
    with pytest.raises(CryptoError, match="casdoor_crypto_decryption_failed"):
        protector.decrypt(envelope, context=wrong_context)


@pytest.mark.parametrize("purpose", list(EncryptionPurpose)[1:])
def test_record_id_is_authenticated(purpose: EncryptionPurpose) -> None:
    protector = crypto()
    envelope = protector.encrypt("synthetic-short-lived-value", context=record_context(purpose))
    assert protector.decrypt(envelope, context=record_context(purpose)) == "synthetic-short-lived-value"
    with pytest.raises(CryptoError):
        protector.decrypt(envelope, context=record_context(purpose, "different-record"))


def test_blank_replacement_preserves_plaintext_with_new_revision_and_nonce() -> None:
    protector = crypto()
    old = protector.encrypt(SYNTHETIC_CLIENT_SECRET, context=config_context())
    new = protector.reencrypt(old, source_context=config_context(), target_context=config_context(NEXT_REVISION))
    assert json.loads(new)["nonce"] != json.loads(old)["nonce"]
    assert protector.decrypt(new, context=config_context(NEXT_REVISION)) == SYNTHETIC_CLIENT_SECRET
    with pytest.raises(CryptoError):
        protector.decrypt(old, context=config_context(NEXT_REVISION))
    with pytest.raises(CryptoError):
        protector.decrypt(new, context=config_context())


def test_rotation_requires_explicit_old_decryptor_and_reencryption() -> None:
    old_crypto = crypto()
    new_crypto = CasdoorCrypto(secret_key="another-synthetic-deployment-key", key_version="deployment-v2")
    old = old_crypto.encrypt(SYNTHETIC_CLIENT_SECRET, context=config_context())
    with pytest.raises(CryptoError, match="casdoor_crypto_key_version_unknown"):
        new_crypto.decrypt(old, context=config_context())
    same_version_changed_key = CasdoorCrypto(secret_key="another-synthetic-key", key_version="deployment-v1")
    with pytest.raises(CryptoError, match="casdoor_crypto_decryption_failed"):
        same_version_changed_key.decrypt(old, context=config_context())
    new = old_crypto.reencrypt(
        old,
        source_context=config_context(),
        target_context=config_context(NEXT_REVISION),
        target_crypto=new_crypto,
    )
    assert new_crypto.decrypt(new, context=config_context(NEXT_REVISION)) == SYNTHETIC_CLIENT_SECRET


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_unknown_envelope_version_is_rejected(version: object) -> None:
    envelope = json.loads(crypto().encrypt(SYNTHETIC_CLIENT_SECRET, context=config_context()))
    envelope["envelope_version"] = version
    with pytest.raises(CryptoError, match="casdoor_crypto_version_unsupported"):
        crypto().decrypt(json.dumps(envelope), context=config_context())


@pytest.mark.parametrize(
    "bad_envelope",
    [
        "not-json",
        "[]",
        "{}",
        '{"envelope_version":1,"envelope_version":1}',
        '{"envelope_version":1,"key_version":"deployment-v1","nonce":"bad=","ciphertext":"x"}',
        '{"envelope_version":1,"key_version":"deployment-v1","nonce":"AA","ciphertext":"AA"}',
        "legacy-blowfish-ciphertext",
    ],
)
def test_malformed_and_legacy_envelopes_are_rejected(bad_envelope: str) -> None:
    with pytest.raises(CryptoError):
        crypto().decrypt(bad_envelope, context=config_context())


@pytest.mark.parametrize("secret_key", ["", b"", None])
def test_deployment_key_is_explicit_and_nonempty(secret_key: str | bytes) -> None:
    with pytest.raises(CryptoError):
        CasdoorCrypto(secret_key=secret_key, key_version="deployment-v1")


@pytest.mark.parametrize("value", ["", "x" * (MAX_PLAINTEXT_BYTES + 1), "\ud800"])
def test_plaintext_bounds(value: str) -> None:
    with pytest.raises(CryptoError):
        crypto().encrypt(value, context=config_context())


def test_purpose_and_record_validation() -> None:
    with pytest.raises(CryptoError):
        EncryptionContext("unknown", NAMESPACE, REVISION)
    with pytest.raises(CryptoError):
        EncryptionContext(EncryptionPurpose.CONFIG_SECRET, "not-uuid", REVISION)
    with pytest.raises(CryptoError):
        EncryptionContext(EncryptionPurpose.CONFIG_SECRET, NAMESPACE, REVISION, "not-allowed")
    with pytest.raises(CryptoError):
        EncryptionContext(EncryptionPurpose.LOGOUT_ID_TOKEN, NAMESPACE, REVISION)


def test_hmac_provenance_is_keyed_separate_and_constant_time_comparable() -> None:
    protector = crypto()
    token = "synthetic-refresh-token"
    digest = protector.refresh_source_digest(token)
    assert digest != hashlib.sha256(token.encode()).hexdigest()
    assert digest != hmac_using_config_key(protector, token)
    assert protector.matches_refresh_source(token, digest)
    assert not protector.matches_refresh_source(token + "tampered", digest)
    assert not protector.matches_refresh_source(token, "malformed")
    assert not CasdoorCrypto(secret_key="synthetic-other-key", key_version="deployment-v1").matches_refresh_source(
        token, digest
    )


def hmac_using_config_key(protector: CasdoorCrypto, value: str) -> str:
    import hmac

    return hmac.new(
        protector._derive_key(EncryptionPurpose.CONFIG_SECRET.value), value.encode(), hashlib.sha256
    ).hexdigest()


def test_public_certificate_fingerprint_and_known_kid(keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]) -> None:
    pem = issue_certificate(keys[0])
    trusted = pin(pem, "old-key")
    der = x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)
    assert trusted.fingerprint == hashlib.sha256(der).hexdigest()
    assert b"PRIVATE" not in trusted.public_key_pem()
    selected = CertificateTrustStore([trusted]).verify_rs256(
        b"synthetic.jws-signing-input", sign(keys[0]), kid="old-key", now=NOW
    )
    assert selected is trusted


def test_unknown_kid_and_kid_key_mismatch(keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]) -> None:
    store = CertificateTrustStore([pin(issue_certificate(keys[0]), "old"), pin(issue_certificate(keys[1]), "new")])
    for kid in ("unknown", "new", ""):
        with pytest.raises(CryptoError):
            store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[0]), kid=kid, now=NOW)


def test_unlabelled_pinned_certificate_verifies_standard_provider_jwt_kid(
    keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    trusted = pin(issue_certificate(keys[0]))
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "cert-built-in"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(b'{"sub":"synthetic-user"}').rstrip(b"=")
    signing_input = header + b"." + payload
    store = CertificateTrustStore([trusted])
    assert store.verify_rs256(signing_input, sign(keys[0], signing_input), kid="cert-built-in", now=NOW) is trusted
    with pytest.raises(CryptoError, match="casdoor_signature_invalid"):
        store.verify_rs256(signing_input, sign(keys[1], signing_input), kid="cert-built-in", now=NOW)


def test_explicit_kid_mismatch_is_not_bypassed_by_an_unlabelled_other_pin(
    keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    labelled = pin(issue_certificate(keys[0]), "configured-key")
    unlabelled = pin(issue_certificate(keys[1]))
    store = CertificateTrustStore([labelled, unlabelled])
    with pytest.raises(CryptoError, match="casdoor_signature_invalid"):
        store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[0]), kid="cert-built-in", now=NOW)
    assert store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[1]), kid="cert-built-in", now=NOW) is unlabelled


@pytest.mark.parametrize("kid", [None, "cert-built-in"])
def test_unlabelled_pins_accept_only_unique_success(
    kid: str | None, keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]
) -> None:
    old, new = pin(issue_certificate(keys[0])), pin(issue_certificate(keys[1]))
    store = CertificateTrustStore([old, new])
    assert store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[0]), kid=kid, now=NOW) is old
    assert store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[1]), kid=kid, now=NOW) is new
    with pytest.raises(CryptoError):
        store.verify_rs256(b"tampered", sign(keys[0]), kid=kid, now=NOW)
    # Distinct certificate DER sharing one public key creates ambiguous success.
    same_key = CertificateTrustStore([old, pin(issue_certificate(keys[0], serial=2))])
    with pytest.raises(CryptoError):
        same_key.verify_rs256(b"synthetic.jws-signing-input", sign(keys[0]), kid=kid, now=NOW)


def test_provider_kid_does_not_extend_unlabelled_pin_trust_window(
    keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    trusted = pin(issue_certificate(keys[0]), accept_until=NOW)
    with pytest.raises(CryptoError, match="casdoor_signature_invalid"):
        CertificateTrustStore([trusted]).verify_rs256(
            b"synthetic.jws-signing-input", sign(keys[0]), kid="cert-built-in", now=NOW
        )


@pytest.mark.parametrize("algorithm", ["HS256", "RS512", "none", "PS256"])
def test_algorithm_is_rs256_only(algorithm: str, keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]) -> None:
    store = CertificateTrustStore([pin(issue_certificate(keys[0]))])
    with pytest.raises(CryptoError):
        store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[0]), kid=None, now=NOW, algorithm=algorithm)


def test_duplicate_kid_fingerprint_and_pin_count_rejected(keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]) -> None:
    first = pin(issue_certificate(keys[0]), "same")
    second = pin(issue_certificate(keys[1]), "same")
    for pins in ([], [first, second], [first, first], [first, second, first]):
        with pytest.raises(CryptoError):
            CertificateTrustStore(pins)


def test_trust_window_boundaries_and_expired_key_rotation(keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]) -> None:
    old = pin(issue_certificate(keys[0]), "old", not_before=NOW - timedelta(hours=1), accept_until=NOW)
    new = pin(issue_certificate(keys[1]), "new", not_before=NOW, accept_until=NOW + timedelta(hours=1))
    store = CertificateTrustStore([old, new])
    with pytest.raises(CryptoError):
        store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[0]), kid="old", now=NOW)
    assert store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[1]), kid="new", now=NOW) is new
    with pytest.raises(CryptoError):
        store.verify_rs256(b"synthetic.jws-signing-input", sign(keys[1]), kid="new", now=NOW - timedelta(seconds=1))


@pytest.mark.parametrize(
    "times",
    [
        {"not_before": NOW.replace(tzinfo=None)},
        {"accept_until": NOW + timedelta(days=2)},
        {"not_before": NOW - timedelta(days=2)},
        {"accept_until": NOW - timedelta(hours=1)},
    ],
)
def test_certificate_time_bounds(times: dict[str, datetime], keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]) -> None:
    with pytest.raises(CryptoError):
        pin(issue_certificate(keys[0]), **times)


def test_public_x509_only_and_utf8_kid_limit(keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]) -> None:
    pem = issue_certificate(keys[0])
    private_pem = (
        keys[0]
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode("ascii")
    )
    for invalid in (private_pem, pem + private_pem, pem + pem, "invalid", "x" * (MAX_CERTIFICATE_BYTES + 1)):
        with pytest.raises(CryptoError, match="casdoor_certificate_invalid") as error:
            pin(invalid)
        assert private_pem not in str(error.value)
    assert pin(pem, "密" * 42).kid == "密" * 42
    with pytest.raises(CryptoError):
        pin(pem, "密" * 43)


def test_ec_and_small_rsa_rejected() -> None:
    for key in (
        ec.generate_private_key(ec.SECP256R1()),
        rsa.generate_private_key(public_exponent=65537, key_size=1024),
    ):
        with pytest.raises(CryptoError):
            pin(issue_certificate(key))
