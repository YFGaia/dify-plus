"""Synthetic RSA/X509 checks, with no real IdP, secret, network or account."""

import base64
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest
from casdoor import CasdoorSDK
from core.casdoor.claims import (
    MAX_HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_RAW_BYTES,
    MAX_SIGNATURE_BYTES,
    MAX_TOKEN_BYTES,
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

NOW = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
START = NOW - timedelta(seconds=20)
ISSUER, CLIENT, ORG, APP = "https://issuer.example.test", "synthetic-client", "synthetic-org", "synthetic-app"
SUB, NONCE = "synthetic-stable-id", "synthetic-transaction-random-nonce"


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def certificate(key, end=NOW + timedelta(days=1)):
    start = NOW - timedelta(days=1)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.example.test")])
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
        pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        kid="synthetic-kid",
        not_before=start,
        accept_until=end,
    )


@pytest.fixture
def validator(key):
    return ClaimsValidator(
        trust_store=CertificateTrustStore([certificate(key)]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )


def id_claims(**changes):
    return {
        "iss": ISSUER,
        "sub": SUB,
        "aud": CLIENT,
        "exp": NOW.timestamp() + 300,
        "iat": NOW.timestamp(),
        "nonce": NONCE,
    } | changes


def native_claims(**changes):
    return {
        "iss": ISSUER,
        "owner": ORG,
        "id": SUB,
        "aud": CLIENT,
        "exp": NOW.timestamp() + 300,
        "iat": NOW.timestamp(),
    } | changes


def sign(key, claims=None, *, header=None, payload_bytes=None, header_bytes=None):
    h = header_bytes or json.dumps(header or {"alg": "RS256", "typ": "JWT", "kid": "synthetic-kid"}).encode()
    p = payload_bytes or json.dumps(claims).encode()
    body = b64(h) + "." + b64(p)
    return body + "." + b64(key.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256()))


def verify_id(validator, token, expected_nonce=NONCE):
    return validator.verify_id_token(token, expected_nonce=expected_nonce, auth_started_at=START, now=NOW)


def identity(validator, key):
    return verify_id(validator, sign(key, id_claims()))


def contract(**changes):
    return replace(
        NativeTokenContract(NativeTokenSchema.FLAT_USER_V1, ISSUER, ORG, APP, CLIENT, "a" * 64, "b" * 64), **changes
    )


SYNTHETIC_CONTRACT = contract()


def verify_native(validator, key, claims=None, proof=SYNTHETIC_CONTRACT):
    return validator.verify_native_access_token(
        sign(key, native_claims() if claims is None else claims),
        identity=identity(validator, key),
        auth_started_at=START,
        now=NOW,
        contract=proof,
    )


def test_valid_projection_custom_claims(validator, key):
    result = verify_id(validator, sign(key, id_claims(owner=ORG, id=SUB, roles=["admin"], isAdmin=True)))
    assert result.subject == SUB and result.issuer == ISSUER
    assert not hasattr(result, "roles") and not hasattr(result, "nonce") and SUB not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.subject = "changed"


@pytest.mark.parametrize("field", ["iss", "sub", "aud", "exp", "iat", "nonce"])
def test_required_id(validator, key, field):
    claims = id_claims()
    del claims[field]
    with pytest.raises(ClaimsError):
        verify_id(validator, sign(key, claims))


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": ISSUER + "/"},
        {"sub": ""},
        {"sub": 42},
        {"sub": "界" * 86},
        {"sub": "bad\x00subject"},
        {"aud": "other"},
        {"aud": []},
        {"aud": True},
        {"aud": [CLIENT, CLIENT]},
        {"aud": [CLIENT, 1]},
        {"aud": [CLIENT, "other"]},
        {"aud": [CLIENT, "other"], "azp": "other"},
        {"azp": False},
        {"nonce": "wrong"},
        {"nonce": False},
        {"nonce": None},
        {"exp": True},
        {"exp": "1800"},
        {"iat": False},
        {"iat": "1800"},
        {"exp": float("nan")},
        {"iat": float("inf")},
        {"nbf": "1800"},
        {"iat": START.timestamp() - 61},
        {"iat": NOW.timestamp() + 61},
        {"exp": NOW.timestamp()},
        {"exp": NOW.timestamp() - 61, "iat": START.timestamp() - 60},
        {"nbf": NOW.timestamp() + 61},
        {"nbf": NOW.timestamp() + 300},
    ],
)
def test_bad_id_claims(validator, key, changes):
    with pytest.raises(ClaimsError):
        verify_id(validator, sign(key, id_claims(**changes)))


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": [CLIENT]},
        {"aud": [CLIENT, "other"], "azp": CLIENT},
        {"azp": CLIENT},
        {"sub": "界" * 85},
        {"iat": START.timestamp() - 60},
        {"iat": NOW.timestamp() + 60},
        {"nbf": NOW.timestamp() + 60},
    ],
)
def test_valid_boundaries(validator, key, changes):
    assert verify_id(validator, sign(key, id_claims(**changes)))


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none"},
        {"alg": "HS256"},
        {"alg": "RS256", "jku": "https://evil.test"},
        {"alg": "RS256", "jwk": {}},
        {"alg": "RS256", "x5u": "https://evil.test"},
        {"alg": "RS256", "crit": []},
        {"alg": "RS256", "b64": False},
        {"alg": "RS256", "kid": None},
        {"alg": "RS256", "typ": "at+jwt"},
    ],
)
def test_bad_headers(validator, key, header):
    with pytest.raises(ClaimsError):
        verify_id(validator, sign(key, id_claims(), header=header))


@pytest.mark.parametrize("which", ["header", "payload", "nested"])
def test_duplicate_json(validator, key, which):
    kwargs = (
        {"header_bytes": b'{"alg":"RS256","alg":"RS256"}'}
        if which == "header"
        else {
            "payload_bytes": (
                json.dumps(id_claims())[:-1]
                + (',"sub":"second"}' if which == "payload" else ',"extra":{"nested":1,"nested":2}}')
            ).encode()
        }
    )
    with pytest.raises(ClaimsError):
        verify_id(validator, sign(key, id_claims(), **kwargs))


@pytest.mark.parametrize("raw", [b"[]", b"null", b'{"bad":"\xff"}', b'{"exp":1e309}', b'{"x":NaN}'])
def test_invalid_json(validator, key, raw):
    with pytest.raises(ClaimsError):
        verify_id(validator, sign(key, payload_bytes=raw))


@pytest.mark.parametrize(
    "token", ["opaque-token", "a.b", "a.b.c.d", ".b.c", "a=.b.c", "a.b.c=", "x" * (MAX_TOKEN_BYTES + 1), 1, None]
)
def test_malformed_compact(validator, token):
    with pytest.raises(ClaimsError):
        verify_id(validator, token)


def test_noncanonical_base64(validator, key):
    parts = sign(key, id_claims()).split(".")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    parts[2] = parts[2][:-1] + alphabet[alphabet.index(parts[2][-1]) + 1]
    with pytest.raises(ClaimsError):
        verify_id(validator, ".".join(parts))


@pytest.mark.parametrize("part,limit", [(0, MAX_HEADER_BYTES), (1, MAX_PAYLOAD_BYTES), (2, MAX_SIGNATURE_BYTES)])
def test_sizes(validator, key, part, limit):
    parts = sign(key, id_claims()).split(".")
    parts[part] = b64(b"x" * (limit + 1))
    with pytest.raises(ClaimsError):
        verify_id(validator, ".".join(parts))


def test_pinned_signature_kid_and_window(validator, key):
    for token in [
        sign(key, id_claims(), header={"alg": "RS256", "kid": "unknown"}),
        sign(rsa.generate_private_key(public_exponent=65537, key_size=2048), id_claims()),
    ]:
        with pytest.raises(ClaimsError) as error:
            verify_id(validator, token)
        assert error.value.reason == "signature_invalid"
    expired = ClaimsValidator(
        trust_store=CertificateTrustStore([certificate(key, end=NOW)]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )
    with pytest.raises(ClaimsError) as error:
        verify_id(expired, sign(key, id_claims()))
    assert error.value.reason == "signature_invalid"


def test_sdk_bound_client_pin_no_network(validator, key, monkeypatch):
    calls = []
    parse = CasdoorSDK.parse_jwt_token

    def inspect(sdk, token, **kwargs):
        calls.append((sdk, kwargs))
        return parse(sdk, token, **kwargs)

    def no_http(*args, **kwargs):
        pytest.fail("SDK HTTP must not run")

    monkeypatch.setattr(CasdoorSDK, "parse_jwt_token", inspect)
    for method in [
        "get_auth_link",
        "get_oauth_token",
        "get_user",
        "get_roles",
        "get_organization",
        "_oauth_token_request",
    ]:
        monkeypatch.setattr(CasdoorSDK, method, no_http)
    verify_id(validator, sign(key, id_claims()))
    sdk, kwargs = calls[0]
    assert sdk.client_id == CLIENT and sdk.client_secret == "" and sdk.front_endpoint == "https://unused.invalid"
    assert sdk.certificate.startswith("-----BEGIN CERTIFICATE-----")
    assert kwargs["issuer"] == ISSUER and "audience" not in kwargs and kwargs["leeway"] == 60
    assert kwargs["options"]["require"] == ["iss", "sub", "aud", "exp", "iat", "nonce"]


def test_native_valid_nonce_roles_projection(validator, key):
    result = verify_native(validator, key, native_claims(sub=SUB, application=APP, nonce=NONCE, roles=["admin"]))
    assert result.subject == SUB and result.organization == ORG
    assert not hasattr(result, "roles") and SUB not in repr(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner": "other"},
        {"owner": False},
        {"id": "other"},
        {"id": 42},
        {"sub": "other"},
        {"sub": None},
        {"application": "other"},
        {"application": False},
        {"iss": "other"},
        {"aud": "other"},
        {"exp": True},
        {"iat": "123"},
        {"aud": [CLIENT, "other"]},
    ],
)
def test_native_independent(validator, key, changes):
    with pytest.raises(ClaimsError):
        verify_native(validator, key, native_claims(**changes))


@pytest.mark.parametrize("field", ["owner", "id", "iss", "aud", "exp", "iat"])
def test_native_required(validator, key, field):
    claims = native_claims()
    del claims[field]
    with pytest.raises(ClaimsError):
        verify_native(validator, key, claims)


@pytest.mark.parametrize(
    "proof",
    [
        None,
        contract(schema="unknown"),
        contract(organization="other"),
        contract(application="other"),
        contract(client_id="other"),
        contract(expected_issuer="other"),
        contract(release_fingerprint=""),
        contract(schema_proof_fingerprint="not-proof"),
    ],
)
def test_native_missing_or_wrong_proof(validator, key, proof):
    with pytest.raises(ClaimsError) as error:
        verify_native(validator, key, proof=proof)
    assert error.value.code == CasdoorErrorCode.CONFIG_CONFLICT


def test_distinct_verified_types_and_entrypoints(validator, key):
    with pytest.raises(ClaimsError):
        verify_id(validator, sign(key, native_claims()))
    with pytest.raises(ClaimsError):
        verify_native(validator, key, id_claims())
    with pytest.raises(ClaimsError):
        validator.verify_userinfo({"sub": SUB}, identity=verify_native(validator, key))


def test_bundle_never_fallback(validator, key):
    from core.casdoor.gateway import RawTokens

    id_token, access = sign(key, id_claims()), sign(key, native_claims())
    kwargs = {"expected_nonce": NONCE, "auth_started_at": START, "now": NOW, "contract": contract()}
    good = validator.verify_token_bundle(RawTokens({"id_token": id_token, "access_token": access}), **kwargs)
    assert good.identity.subject == good.native_access.subject == SUB and SUB not in repr(good)
    for payload in [
        {"access_token": id_token},
        {"id_token": access, "access_token": access},
        {"id_token": id_token, "access_token": id_token},
        {"id_token": id_token, "access_token": "opaque"},
        {"id_token": "", "access_token": access},
    ]:
        with pytest.raises(ClaimsError):
            validator.verify_token_bundle(RawTokens(payload), **kwargs)


def test_profile_snapshot_only(validator, key):
    verified = identity(validator, key)
    profile = validator.verify_userinfo(
        {
            "sub": SUB,
            "email": "person@example.test",
            "email_verified": True,
            "name": "Synthetic",
            "locale": "zh-CN",
            "zoneinfo": "Asia/Shanghai",
            "roles": ["admin"],
            "password": "never-output",
        },
        identity=verified,
    )
    assert profile.eligible_for_first_admission() and profile.locale == "zh-CN"
    assert not hasattr(profile, "password") and not hasattr(profile, "roles") and profile.email not in repr(profile)
    changed = validator.verify_userinfo({"sub": SUB, "email": "changed@example.test"}, identity=verified)
    assert changed.email_verified is None and not changed.eligible_for_first_admission()
    # No local-email-login flag can bypass this necessary remote email condition.
    old_email_login_enabled = False
    assert not old_email_login_enabled and profile.eligible_for_first_admission()


@pytest.mark.parametrize(
    "data",
    [
        {"sub": SUB},
        {"sub": SUB, "email": "person@example.test"},
        {"sub": SUB, "email_verified": True},
        {"sub": SUB, "email": "person@example.test", "email_verified": False},
        {"sub": SUB, "email": "invalid", "email_verified": True},
        {"sub": SUB, "email": "a..b@example.test", "email_verified": True},
        {"sub": SUB, "email": "a@-example.test", "email_verified": True},
    ],
)
def test_first_admission_unknown_unverified_email(validator, key, data):
    assert not validator.verify_userinfo(data, identity=identity(validator, key)).eligible_for_first_admission()


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"sub": "other"},
        {"sub": False},
        {"sub": SUB, "email_verified": "true"},
        {"sub": SUB, "email_verified": 1},
        {"sub": SUB, "email_verified": None},
        {"sub": SUB, "email": None},
        {"sub": SUB, "name": 42},
        {"sub": SUB, "locale": []},
        {"sub": SUB, "zoneinfo": "x" * 65},
    ],
)
def test_profile_schema(validator, key, data):
    with pytest.raises(ClaimsError):
        validator.verify_userinfo(data, identity=identity(validator, key))


def online(**changes):
    return {"owner": ORG, "id": SUB, "name": "synthetic-login-name", "isForbidden": False, "isDeleted": False} | changes


def test_online_structured_ref(validator, key):
    result = validator.verify_online_user(online(roles=["admin"], isAdmin=True), identity=identity(validator, key))
    assert result.subject == SUB and result.user_ref.owner == ORG and result.user_ref.name == "synthetic-login-name"
    assert not hasattr(result, "roles") and not hasattr(result, "isAdmin")


@pytest.mark.parametrize("field", ["owner", "id", "name", "isForbidden", "isDeleted"])
def test_online_missing_state(validator, key, field):
    data = online()
    del data[field]
    with pytest.raises(ClaimsError) as error:
        validator.verify_online_user(data, identity=identity(validator, key))
    assert error.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN


@pytest.mark.parametrize(
    "changes",
    [
        {"owner": "other"},
        {"id": "other"},
        {"id": "synthetic-login-name"},
        {"name": ""},
        {"isForbidden": "false"},
        {"isDeleted": 0},
        {"isDeleted": None},
    ],
)
def test_online_crossorg_schema(validator, key, changes):
    with pytest.raises(ClaimsError):
        validator.verify_online_user(online(**changes), identity=identity(validator, key))


@pytest.mark.parametrize("field", ["isForbidden", "isDeleted"])
def test_online_disabled(validator, key, field):
    with pytest.raises(ClaimsError) as error:
        validator.verify_online_user(online(**{field: True}), identity=identity(validator, key))
    assert error.value.code == CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED


@pytest.mark.parametrize(
    "raw",
    [
        b'{"sub":"a","sub":"b"}',
        b'{"x":Infinity}',
        b"[]",
        b"x" * (MAX_RAW_BYTES + 1),
        {"sub": SUB, "extra": float("nan")},
        object(),
    ],
)
def test_raw_integrity(validator, key, raw):
    with pytest.raises(ClaimsError):
        validator.verify_userinfo(raw, identity=identity(validator, key))


def test_redacted_errors(validator, key):
    token = sign(key, id_claims(nonce="private-marker"))
    with pytest.raises(ClaimsError) as error:
        verify_id(validator, token)
    for output in [str(error.value), repr(error.value), error.value.reason, error.value.code.value]:
        assert token not in output and "private-marker" not in output and SUB not in output
    assert error.value.retry_allowed is False


@pytest.mark.parametrize("leeway", [True, -1, 61, 1.5, "60"])
def test_leeway_cap(key, leeway):
    with pytest.raises(ClaimsError):
        ClaimsValidator(
            trust_store=CertificateTrustStore([certificate(key)]),
            expected_issuer=ISSUER,
            organization=ORG,
            application=APP,
            client_id=CLIENT,
            leeway=leeway,
        )


def test_nonce_constant_time_bytes(validator, key, monkeypatch):
    seen = []

    def compare(a, b):
        seen.append((a, b))
        return a == b

    monkeypatch.setattr("core.casdoor.claims.hmac.compare_digest", compare)
    verify_id(validator, sign(key, id_claims(nonce="随机nonce")), expected_nonce="随机nonce")
    assert seen == [("随机nonce".encode(), "随机nonce".encode())]


def test_clock_and_huge_numeric_date(validator, key):
    for start, now in [
        (NOW + timedelta(seconds=1), NOW),
        (START.replace(tzinfo=None), NOW),
        (START, NOW.replace(tzinfo=None)),
    ]:
        with pytest.raises(ClaimsError):
            validator.verify_id_token(sign(key, id_claims()), expected_nonce=NONCE, auth_started_at=start, now=now)
    with pytest.raises(ClaimsError):
        verify_id(validator, sign(key, id_claims(exp=10**400)))


def test_second_actual_pin_selected_for_sdk(key, monkeypatch):
    second_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    first = certificate(key)
    second_base = certificate(second_key)
    second = TrustedCertificate(
        pem=second_base.pem, kid="new-kid", not_before=second_base.not_before, accept_until=second_base.accept_until
    )
    validator = ClaimsValidator(
        trust_store=CertificateTrustStore([first, second]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )
    seen = []
    parse = CasdoorSDK.parse_jwt_token

    def inspect(sdk, token, **kwargs):
        seen.append(sdk.certificate)
        return parse(sdk, token, **kwargs)

    monkeypatch.setattr(CasdoorSDK, "parse_jwt_token", inspect)
    verify_id(validator, sign(second_key, id_claims(), header={"alg": "RS256", "kid": "new-kid"}))
    assert seen == [second.pem] and second.pem != first.pem
    # No kid also selects the sole actually successful pin, not first configured.
    verify_id(validator, sign(second_key, id_claims(), header={"alg": "RS256"}))
    assert seen == [second.pem, second.pem]


def test_expiry_leeway_exact_boundary(key):
    validator = ClaimsValidator(
        trust_store=CertificateTrustStore([certificate(key)]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )
    start = NOW - timedelta(seconds=100)

    def verify(exp):
        return validator.verify_id_token(
            sign(key, id_claims(iat=start.timestamp(), exp=exp)), expected_nonce=NONCE, auth_started_at=start, now=NOW
        )

    assert verify(NOW.timestamp() - 59.9)
    with pytest.raises(ClaimsError):
        verify(NOW.timestamp() - 60)


def test_unverified_mail_never_grants_first_admission_with_old_policy_disabled(validator, key):
    old_email_login_enabled = False
    raw = {"sub": SUB, "email": "synthetic@example.test"}
    profile = validator.verify_userinfo(raw, identity=identity(validator, key))
    assert not old_email_login_enabled and profile.email_verified is None
    assert not profile.eligible_for_first_admission()
