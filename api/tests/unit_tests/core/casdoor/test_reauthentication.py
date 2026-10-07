"""Offline RS256/X509 observation checks; no proof, session, route or account."""

import json
import math
from dataclasses import FrozenInstanceError, fields
from datetime import timedelta, timezone

import pytest
from core.casdoor.claims import ClaimsError, ClaimsValidator
from core.casdoor.crypto import CertificateTrustStore
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.reauthentication import RecentAuthTimeObservation, verify_recent_auth_time
from cryptography.hazmat.primitives.asymmetric import rsa
from test_claims import APP, CLIENT, ISSUER, NONCE, NOW, ORG, START, certificate, id_claims, sign

PRIVATE = "private-reauth-claim-marker"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def validator(signing_key):
    return ClaimsValidator(
        trust_store=CertificateTrustStore([certificate(signing_key)]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )


def observe(validator, token, *, start=START, now=NOW, nonce=NONCE):
    return verify_recent_auth_time(validator, token, expected_nonce=nonce, auth_started_at=start, now=now)


def assert_private_failure(validator, token, reason, **kwargs):
    with pytest.raises(ClaimsError) as caught:
        observe(validator, token, **kwargs)
    error = caught.value
    assert error.reason == reason and str(error) == "casdoor_claims_" + reason
    assert error.code is CasdoorErrorCode.INVALID_TRANSACTION and error.retry_allowed is False
    assert error.__cause__ is None and error.__context__ is None
    projection = repr((error, error.args, vars(error), error.__cause__, error.__context__))
    assert token not in projection and PRIVATE not in projection and NONCE not in projection


@pytest.mark.parametrize("start_ago", [20, 600])
@pytest.mark.parametrize("position", ["lower-int", "lower-float", "within", "now"])
def test_inclusive_window_and_exact_numeric_types(validator, signing_key, start_ago, position):
    start = NOW - timedelta(seconds=start_ago)
    lower = max(start.timestamp() - 60, NOW.timestamp() - 300)
    value = {
        "lower-int": int(lower),
        "lower-float": lower,
        "within": lower + 0.125,
        "now": NOW.timestamp(),
    }[position]
    token = sign(signing_key, id_claims(auth_time=value, private=PRIVATE))
    result = observe(validator, token, start=start)
    assert type(result) is RecentAuthTimeObservation
    assert type(result.auth_time) is type(value) and result.auth_time == value


@pytest.mark.parametrize("start_ago", [20, 600])
def test_just_before_each_active_lower_bound_rejects(validator, signing_key, start_ago):
    start = NOW - timedelta(seconds=start_ago)
    lower = max(start.timestamp() - 60, NOW.timestamp() - 300)
    value = math.nextafter(lower, -math.inf)
    assert value < lower
    token = sign(signing_key, id_claims(auth_time=value, private=PRIVATE))
    assert_private_failure(validator, token, "auth_time_invalid", start=start)


@pytest.mark.parametrize("future", [math.nextafter(NOW.timestamp(), math.inf), NOW.timestamp() + 1])
def test_future_authentication_is_not_an_observation(validator, signing_key, future):
    token = sign(signing_key, id_claims(auth_time=future, private=PRIVATE))
    assert_private_failure(validator, token, "auth_time_invalid")


def test_new_iat_does_not_replace_stale_auth_time(validator, signing_key):
    token = sign(signing_key, id_claims(iat=NOW.timestamp(), auth_time=NOW.timestamp() - 301, private=PRIVATE))
    assert validator.verify_id_token(token, expected_nonce=NONCE, auth_started_at=START, now=NOW)
    assert_private_failure(validator, token, "auth_time_invalid")


def test_missing_auth_time_preserves_old_api_success(validator, signing_key):
    token = sign(signing_key, id_claims(iat=NOW.timestamp(), private=PRIVATE))
    identity = validator.verify_id_token(token, expected_nonce=NONCE, auth_started_at=START, now=NOW)
    assert identity.issued_at == NOW.timestamp() and not hasattr(identity, "auth_time")
    assert_private_failure(validator, token, "token_invalid")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (True, "auth_time_invalid"),
        (False, "auth_time_invalid"),
        (str(int(NOW.timestamp())), "auth_time_invalid"),
        (None, "token_invalid"),
        ([], "auth_time_invalid"),
        ({}, "auth_time_invalid"),
        (float("nan"), "json_invalid"),
        (float("inf"), "json_invalid"),
        (-float("inf"), "json_invalid"),
        (1e308, "auth_time_invalid"),
        (-1e308, "auth_time_invalid"),
        (10**400, "auth_time_invalid"),
        (-(10**400), "auth_time_invalid"),
    ],
    ids=[
        "bool-true",
        "bool-false",
        "string",
        "null",
        "list",
        "object",
        "nan",
        "inf",
        "neg-inf",
        "huge-float",
        "negative-float",
        "overflow-int",
        "negative-overflow-int",
    ],
)
def test_invalid_numeric_dates_have_no_private_context(validator, signing_key, value, reason):
    token = sign(signing_key, id_claims(auth_time=value, private=PRIVATE))
    assert_private_failure(validator, token, reason)


@pytest.mark.parametrize(
    "extra",
    [
        '"auth_time":1e309',
        '"auth_time":-1e309',
        '"auth_time":' + "9" * 5000,
        '"auth_time":1,"auth_time":2',
        '"auth_time":1,"private":{"duplicate":1,"duplicate":2}',
    ],
    ids=[
        "overflow-json-float",
        "negative-overflow-json-float",
        "overflow-json-int",
        "duplicate-auth-time",
        "duplicate-nested",
    ],
)
def test_strict_signed_json_parser_is_preserved(validator, signing_key, extra):
    payload = (json.dumps(id_claims(private=PRIVATE))[:-1] + "," + extra + "}").encode()
    token = sign(signing_key, payload_bytes=payload)
    assert_private_failure(validator, token, "json_invalid")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"iss": PRIVATE}, "token_invalid"),
        ({"aud": PRIVATE}, "token_invalid"),
        ({"aud": [CLIENT, PRIVATE]}, "authorized_party_invalid"),
        ({"aud": [CLIENT, PRIVATE], "azp": PRIVATE}, "authorized_party_invalid"),
        ({"nonce": PRIVATE}, "nonce_invalid"),
        ({"exp": NOW.timestamp() - 61}, "time_invalid"),
        ({"iat": NOW.timestamp() + 61}, "time_invalid"),
        ({"iat": START.timestamp() - 61}, "time_invalid"),
    ],
    ids=["issuer", "audience", "missing-azp", "wrong-azp", "nonce", "expired", "future-iat", "old-iat"],
)
def test_original_verified_token_contract_still_rejects(validator, signing_key, changes, reason):
    token = sign(signing_key, id_claims(auth_time=NOW.timestamp(), private=PRIVATE) | changes)
    assert_private_failure(validator, token, reason)


def test_wrong_signature_does_not_expose_crypto_context(validator):
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = sign(wrong_key, id_claims(auth_time=NOW.timestamp(), private=PRIVATE))
    assert_private_failure(validator, token, "signature_invalid")


@pytest.mark.parametrize(
    ("start", "now"),
    [
        (START.replace(tzinfo=None), NOW),
        (START, NOW.replace(tzinfo=None)),
        (START.astimezone(timezone(timedelta(hours=8))), NOW),
        (START, NOW.astimezone(timezone(timedelta(hours=8)))),
        (NOW + timedelta(microseconds=1), NOW),
        (START, None),
        (None, NOW),
    ],
    ids=["naive-start", "naive-now", "offset-start", "offset-now", "future-start", "missing-now", "missing-start"],
)
def test_existing_utc_clock_contract_is_required(validator, signing_key, start, now):
    token = sign(signing_key, id_claims(auth_time=NOW.timestamp(), private=PRIVATE))
    assert_private_failure(validator, token, "clock_invalid", start=start, now=now)


def test_observation_is_immutable_redacted_and_contains_only_timestamp(validator, signing_key):
    token = sign(signing_key, id_claims(auth_time=NOW.timestamp(), private=PRIVATE))
    result = observe(validator, token)
    assert [field.name for field in fields(result)] == ["auth_time"]
    assert repr(result) == "RecentAuthTimeObservation(<redacted>)"
    assert token not in repr(result) and PRIVATE not in repr(result) and str(result.auth_time) not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.auth_time = 0
