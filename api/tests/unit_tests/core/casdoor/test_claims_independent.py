"""Independent boundary checks using in-memory RS256 keys and pinned certificates."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from casdoor import CasdoorSDK
from core.casdoor.claims import (
    MAX_PAYLOAD_BYTES,
    ClaimsError,
    ClaimsValidator,
    NativeTokenContract,
    NativeTokenSchema,
)
from core.casdoor.crypto import CertificateTrustStore, TrustedCertificate
from core.casdoor.errors import CasdoorErrorCode
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

ISSUER = "https://issuer.independent.invalid"
CLIENT = "independent-client"
ORG = "independent-org"
APP = "independent-app"
SUB = "independent-subject-7"
NONCE = "transaction-nonce-4f9a"
NOW = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
START = NOW - timedelta(seconds=30)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pin(key, kid, *, end=NOW + timedelta(days=2)):
    start = NOW - timedelta(days=2)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "independent.invalid")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    return TrustedCertificate(
        pem=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        kid=kid,
        not_before=start,
        accept_until=end,
    )


@pytest.fixture(scope="module")
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(keypair):
    return ClaimsValidator(
        trust_store=CertificateTrustStore([_pin(keypair, "independent-kid")]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )


def _jwt(key, claims, *, kid="independent-kid", header_extra=None, raw_payload=None):
    header = {"alg": "RS256", "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    header.update(header_extra or {})
    header_bytes = json.dumps(header, separators=(",", ":")).encode()
    payload_bytes = raw_payload if raw_payload is not None else json.dumps(claims, allow_nan=True).encode()
    signing_input = f"{_b64(header_bytes)}.{_b64(payload_bytes)}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64(signature)


def _id(**overrides):
    return {
        "iss": ISSUER,
        "sub": SUB,
        "aud": CLIENT,
        "exp": NOW.timestamp() + 240,
        "iat": NOW.timestamp(),
        "nonce": NONCE,
    } | overrides


def _native(**overrides):
    return {
        "iss": ISSUER,
        "aud": CLIENT,
        "exp": NOW.timestamp() + 240,
        "iat": NOW.timestamp(),
        "owner": ORG,
        "id": SUB,
    } | overrides


def _verify_id(verifier, token, nonce=NONCE):
    return verifier.verify_id_token(token, expected_nonce=nonce, auth_started_at=START, now=NOW)


def _contract(**changes):
    return replace(
        NativeTokenContract(NativeTokenSchema.FLAT_USER_V1, ISSUER, ORG, APP, CLIENT, "a" * 64, "b" * 64),
        **changes,
    )


def test_strict_standard_claim_types_and_audience_edges(verifier, keypair):
    for changes in (
        {"exp": True},
        {"iat": "1799999999"},
        {"exp": None},
        {"iat": float("nan")},
        {"exp": float("inf")},
        {"sub": True},
        {"nonce": 1},
        {"aud": [CLIENT, "other"]},
        {"aud": [CLIENT, "other"], "azp": "wrong"},
        {"aud": CLIENT, "azp": "wrong"},
        {"aud": [CLIENT, CLIENT], "azp": CLIENT},
        {"aud": [CLIENT, []], "azp": CLIENT},
    ):
        with pytest.raises(ClaimsError):
            _verify_id(verifier, _jwt(keypair, _id(**changes)))
    assert _verify_id(verifier, _jwt(keypair, _id(aud=[CLIENT, "other"], azp=CLIENT))).subject == SUB


def test_auth_attempt_clock_and_temporal_checks_are_strict(verifier, keypair):
    for changes in (
        {"iat": START.timestamp() - 61},
        {"iat": NOW.timestamp() + 61},
        {"exp": NOW.timestamp() - 61},
        {"nbf": NOW.timestamp() + 61},
        {"nbf": NOW.timestamp() + 241},
        {"nbf": NOW.timestamp() + 300, "exp": NOW.timestamp() + 200},
    ):
        with pytest.raises(ClaimsError):
            _verify_id(verifier, _jwt(keypair, _id(**changes)))
    accepted = _verify_id(verifier, _jwt(keypair, _id(iat=START.timestamp() - 60, nbf=NOW.timestamp() + 60)))
    assert accepted.subject == SUB


@pytest.mark.parametrize(
    "field",
    ["iss", "sub", "aud", "exp", "iat", "nonce"],
)
def test_each_required_claim_is_missing_rejected(verifier, keypair, field):
    claims = _id()
    del claims[field]
    with pytest.raises(ClaimsError):
        _verify_id(verifier, _jwt(keypair, claims))


def test_sdk_signature_issuer_audience_and_required_contract_is_preserved(verifier, keypair, monkeypatch):
    calls = []
    original = CasdoorSDK.parse_jwt_token

    def capture(sdk, token, **kwargs):
        calls.append((sdk, kwargs))
        return original(sdk, token, **kwargs)

    monkeypatch.setattr(CasdoorSDK, "parse_jwt_token", capture)
    good = _jwt(keypair, _id())
    assert _verify_id(verifier, good).subject == SUB
    sdk, kwargs = calls[-1]
    assert sdk.client_id == CLIENT and sdk.client_secret == ""
    assert kwargs["issuer"] == ISSUER
    assert "audience" not in kwargs
    assert kwargs["options"]["require"] == ["iss", "sub", "aud", "exp", "iat", "nonce"]
    assert kwargs["options"]["verify_exp"] is False
    assert kwargs["options"]["verify_iat"] is False
    assert kwargs["options"]["verify_nbf"] is False
    # Expiry is owned by strict UTC checks after the real SDK signature/issuer/audience parse.
    with pytest.raises(ClaimsError):
        _verify_id(verifier, _jwt(keypair, _id(exp=NOW.timestamp() - 100)))
    with pytest.raises(ClaimsError):
        _verify_id(verifier, _jwt(keypair, _id(iss=ISSUER + "/wrong")))


def test_two_pins_select_signature_with_or_without_kid(keypair):
    second = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pins = CertificateTrustStore([_pin(keypair, "first-kid"), _pin(second, "second-kid")])
    check = ClaimsValidator(
        trust_store=pins,
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )
    for kid in ("second-kid", None):
        assert _verify_id(check, _jwt(second, _id(), kid=kid)).subject == SUB
    with pytest.raises(ClaimsError):
        _verify_id(check, _jwt(second, _id(), kid="first-kid"))


def test_two_pins_with_absent_kid_and_ambiguous_same_public_key_fail_closed(keypair):
    # Distinct certificates can contain one public key. Without a kid this is ambiguous.
    ambiguous = CertificateTrustStore([_pin(keypair, None), _pin(keypair, None)])
    check = ClaimsValidator(
        trust_store=ambiguous,
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )
    with pytest.raises(ClaimsError):
        _verify_id(check, _jwt(keypair, _id(), kid=None))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"iss":"x","iss":"y"}',
        b'{"nested":{"x":1,"x":2}}',
        b'{"exp":NaN}',
        b'{"exp":1e9999}',
        b"\xff",
        b"[]",
    ],
)
def test_signed_duplicate_or_nonfinite_json_rejected(verifier, keypair, raw):
    with pytest.raises(ClaimsError):
        _verify_id(verifier, _jwt(keypair, {}, raw_payload=raw))


def test_marker_claims_do_not_choose_token_purpose_and_slots_are_fixed(verifier, keypair):
    from core.casdoor.gateway import RawTokens

    identity = _jwt(keypair, _id(owner=ORG, id=SUB, roles=["admin"], application=APP))
    native = _jwt(keypair, _native(nonce=NONCE, roles=["admin"], isAdmin=True))
    contract = _contract()
    args = {
        "expected_nonce": NONCE,
        "auth_started_at": START,
        "contract": contract,
        "now": NOW,
    }
    result = verifier.verify_token_bundle(RawTokens({"id_token": identity, "access_token": native}), **args)
    assert result.identity.subject == result.native_access.subject == SUB
    for token_fields in (
        {"access_token": identity},
        {"id_token": native, "access_token": native},
        {"id_token": identity, "access_token": "opaque-token"},
    ):
        with pytest.raises(ClaimsError):
            verifier.verify_token_bundle(RawTokens(token_fields), **args)
    with pytest.raises(ClaimsError) as error:
        verifier.verify_native_access_token(
            native, identity=_verify_id(verifier, identity), auth_started_at=START, now=NOW
        )
    assert error.value.code == CasdoorErrorCode.CONFIG_CONFLICT
    # Signed custom owner/id fields can make one token satisfy both layouts;
    # purpose comes from the response slot plus both validations, never a marker.
    dual_layout = verifier.verify_token_bundle(RawTokens({"id_token": identity, "access_token": identity}), **args)
    assert dual_layout.identity.subject == dual_layout.native_access.subject == SUB


def test_native_contract_and_subject_org_binding_are_exact(verifier, keypair):
    id_token = _verify_id(verifier, _jwt(keypair, _id()))
    good = _jwt(keypair, _native(sub=SUB, application=APP, roles=["admin"]))
    verified = verifier.verify_native_access_token(
        good, identity=id_token, auth_started_at=START, contract=_contract(), now=NOW
    )
    assert verified.subject == SUB and verified.organization == ORG
    for claims in (
        _native(owner="another-org"),
        _native(id="another-subject"),
        _native(sub="another-subject"),
        _native(application="another-app"),
    ):
        with pytest.raises(ClaimsError):
            verifier.verify_native_access_token(
                _jwt(keypair, claims), identity=id_token, auth_started_at=START, contract=_contract(), now=NOW
            )
    roles_are_ignored = verifier.verify_native_access_token(
        _jwt(keypair, _native(roles="admin", isAdmin=True)),
        identity=id_token,
        auth_started_at=START,
        contract=_contract(),
        now=NOW,
    )
    assert not hasattr(roles_are_ignored, "roles")


def test_userinfo_requires_exact_subject_and_original_boolean(verifier, keypair):
    from core.casdoor.claims import VerifiedIDToken

    identity = _verify_id(verifier, _jwt(keypair, _id()))
    profile = verifier.verify_userinfo(
        {"sub": SUB, "email": "person@example.invalid", "email_verified": True, "roles": ["admin"]},
        identity=identity,
    )
    assert profile.eligible_for_first_admission()
    assert not hasattr(profile, "roles")
    for value in ("true", 1, None, [], {}):
        with pytest.raises(ClaimsError):
            verifier.verify_userinfo({"sub": SUB, "email_verified": value}, identity=identity)
    with pytest.raises(ClaimsError):
        verifier.verify_userinfo({"sub": "another-subject"}, identity=identity)
    unknown = verifier.verify_userinfo({"sub": SUB, "email": "person@example.invalid"}, identity=identity)
    assert unknown.email_verified is None and not unknown.eligible_for_first_admission()
    assert isinstance(identity, VerifiedIDToken)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "other-org"),
        ("id", "other-id"),
        ("isForbidden", "false"),
        ("isForbidden", 0),
        ("isDeleted", "false"),
        ("isDeleted", 1),
        ("isDeleted", None),
    ],
)
def test_online_user_requires_org_stable_id_name_and_real_boolean_states(verifier, keypair, field, value):
    identity = _verify_id(verifier, _jwt(keypair, _id()))
    raw = {
        "owner": ORG,
        "id": SUB,
        "name": "directory-login-name",
        "isForbidden": False,
        "isDeleted": False,
        "roles": ["admin"],
    }
    raw[field] = value
    with pytest.raises(ClaimsError):
        verifier.verify_online_user(raw, identity=identity)
    assert (
        verifier.verify_online_user(
            {**raw, field: False if field in ("isForbidden", "isDeleted") else (ORG if field == "owner" else SUB)},
            identity=identity,
        )
        if field in ("owner", "id", "isForbidden", "isDeleted")
        else True
    )


def test_online_user_missing_flags_are_unknown_and_true_flags_are_disabled(verifier, keypair):
    identity = _verify_id(verifier, _jwt(keypair, _id()))
    base = {"owner": ORG, "id": SUB, "name": "directory-name", "isForbidden": False, "isDeleted": False}
    for key in ("owner", "id", "name", "isForbidden", "isDeleted"):
        with pytest.raises(ClaimsError) as error:
            verifier.verify_online_user({k: v for k, v in base.items() if k != key}, identity=identity)
        assert error.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    for key in ("isForbidden", "isDeleted"):
        with pytest.raises(ClaimsError) as error:
            verifier.verify_online_user({**base, key: True}, identity=identity)
        assert error.value.code == CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED


def test_raw_dict_and_bytes_bounds_duplicate_keys_and_unknown_email_are_strict(verifier, keypair):
    identity = _verify_id(verifier, _jwt(keypair, _id()))
    assert not verifier.verify_userinfo(
        {"sub": SUB, "email_verified": True}, identity=identity
    ).eligible_for_first_admission()
    for raw in (b'{"sub":"x","sub":"y"}', b'{"sub":"x","v":NaN}', b"[]", b"x" * (MAX_PAYLOAD_BYTES * 5)):
        with pytest.raises(ClaimsError):
            verifier.verify_userinfo(raw, identity=identity)
    with pytest.raises(ClaimsError):
        verifier.verify_userinfo({"sub": SUB, "email": "bad\nemail", "email_verified": True}, identity=identity)
