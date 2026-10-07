"""Public JWK parsing and signature selection independent of transport or claims."""

import base64
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from core.casdoor.crypto import CryptoError
from core.casdoor.signing_keys import (
    MAX_JWKS_BYTES,
    MAX_SIGNING_KEYS,
    SigningKeyTrustStore,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

NOW = datetime(2026, 10, 6, tzinfo=UTC)
MESSAGE = b"synthetic.jws-message"


@pytest.fixture(scope="module")
def keys():
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))


def encoded(number):
    return base64.urlsafe_b64encode(number.to_bytes((number.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def jwk(key, kid="current", **fields):
    numbers = key.public_key().public_numbers()
    value = {"kty": "RSA", "n": encoded(numbers.n), "e": encoded(numbers.e)}
    if kid is not None:
        value["kid"] = kid
    return value | fields


def signature(key):
    return key.sign(MESSAGE, padding.PKCS1v15(), hashes.SHA256())


def store(*values):
    return SigningKeyTrustStore.from_jwks({"keys": list(values)})


def test_no_x5c_public_jwk_selects_by_kid_and_thumbprint(keys):
    trusted = store(jwk(keys[0]))
    selected = trusted.verify_rs256(MESSAGE, signature(keys[0]), kid="current", now=NOW)
    assert selected.fingerprint == trusted.fingerprints[0]
    assert len(selected.fingerprint) == 43
    assert "PRIVATE" not in selected.public_key_pem().decode()
    assert selected.public_key_pem() == keys[0].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert store(jwk(keys[0], kid="renamed")).fingerprints == trusted.fingerprints


def test_no_kid_requires_unique_success_unknown_kid_does_not_use_unlabelled_key(keys):
    trusted = store(jwk(keys[0], None), jwk(keys[1], "new"))
    assert trusted.verify_rs256(MESSAGE, signature(keys[0]), kid=None, now=NOW).kid is None
    assert trusted.verify_rs256(MESSAGE, signature(keys[1]), kid=None, now=NOW).kid == "new"
    for kid in ("unknown", "new", ""):
        with pytest.raises(CryptoError, match="casdoor_signature_invalid"):
            trusted.verify_rs256(MESSAGE, signature(keys[0]), kid=kid, now=NOW)
    with pytest.raises(CryptoError, match="casdoor_signature_invalid"):
        trusted.verify_rs256(MESSAGE + b"tampered", signature(keys[0]), kid=None, now=NOW)


@pytest.mark.parametrize("algorithm", ["none", "HS256", "PS256", "RS512"])
def test_non_rs256_rejected(keys, algorithm):
    with pytest.raises(CryptoError):
        store(jwk(keys[0])).verify_rs256(MESSAGE, signature(keys[0]), kid=None, now=NOW, algorithm=algorithm)


@pytest.mark.parametrize("field", ["d", "p", "q", "dp", "dq", "qi", "oth", "k"])
def test_private_parameters_reject_entire_set_even_for_ignored_keys(keys, field):
    with pytest.raises(CryptoError, match="casdoor_signing_keys_invalid"):
        store(jwk(keys[0]), {"kty": "EC", field: "redacted"})


@pytest.mark.parametrize("fields", [{"alg": "RS512"}, {"use": "enc"}, {"key_ops": ["encrypt"]}, {"kty": "EC"}])
def test_irrelevant_keys_ignored_and_empty_usable_set_fails(keys, fields):
    irrelevant = jwk(keys[1], "irrelevant", **fields)
    assert store(jwk(keys[0]), irrelevant).fingerprints == store(jwk(keys[0])).fingerprints
    with pytest.raises(CryptoError):
        store(irrelevant)


@pytest.mark.parametrize(
    "fields",
    [
        {"alg": None},
        {"use": []},
        {"key_ops": None},
        {"key_ops": []},
        {"key_ops": [1]},
        {"key_ops": ["verify", "verify"]},
    ],
)
def test_bad_key_metadata_rejects(keys, fields):
    with pytest.raises(CryptoError):
        store(jwk(keys[0], **fields))


def test_supported_verify_metadata_and_key_ambiguity(keys):
    assert store(jwk(keys[0], alg="RS256", use="sig", key_ops=["verify"]))
    for values in (
        (jwk(keys[0]), jwk(keys[1])),
        (jwk(keys[0], "one"), jwk(keys[0], "two")),
        (jwk(keys[0], None), jwk(keys[0], "two")),
    ):
        with pytest.raises(CryptoError):
            store(*values)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("n", "AA"), ("n", "invalid="), ("n", "!!!"), ("n", ""), ("e", "Ag"), ("e", "AAEAAQ"), ("e", True)],
)
def test_malformed_rsa_parameters_fail(keys, field, bad):
    value = jwk(keys[0])
    value[field] = bad
    with pytest.raises(CryptoError):
        store(value)


def test_rsa_size_bounds_without_slow_large_key_generation(keys):
    for bits in (1024, 8193):
        value = jwk(keys[0]) | {"n": encoded((1 << (bits - 1)) + 1)}
        with pytest.raises(CryptoError):
            store(value)


def test_collection_and_raw_response_bounds(keys):
    for raw in (
        b"",
        b"[]",
        b'{"keys":[],"keys":[]}',
        b'{"keys":NaN}',
        {"keys": []},
        {"keys": [jwk(keys[0])] * (MAX_SIGNING_KEYS + 1)},
        {"keys": [jwk(keys[0])], "extra": "x" * MAX_JWKS_BYTES},
    ):
        with pytest.raises(CryptoError):
            SigningKeyTrustStore.from_jwks(raw)
    raw = json.dumps({"keys": [jwk(keys[0])]}).encode()
    assert SigningKeyTrustStore.from_jwks(raw).fingerprints == store(jwk(keys[0])).fingerprints
    with pytest.raises(CryptoError):
        store(jwk(keys[0])).verify_rs256(MESSAGE, signature(keys[0]), kid=None, now=NOW.replace(tzinfo=None))


def certificate(key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic public description")])
    result = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(NOW - timedelta(days=100))
        .not_valid_after(NOW - timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(result.public_bytes(serialization.Encoding.DER)).decode()


def test_x5c_optional_description_must_match_n_e_but_is_not_certificate_trust_window(keys):
    value = jwk(keys[0], x5c=[certificate(keys[0])], x5u="https://untrusted.invalid/never-requested")
    assert store(value).verify_rs256(MESSAGE, signature(keys[0]), kid="current", now=NOW)
    for chain in ([], [certificate(keys[1])], ["invalid"], [True], "invalid"):
        invalid = copy.deepcopy(value)
        invalid["x5c"] = chain
        with pytest.raises(CryptoError):
            store(invalid)
