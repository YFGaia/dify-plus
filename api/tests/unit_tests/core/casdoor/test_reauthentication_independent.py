"""Independent signed-auth-time checks using the accepted validator and trust pins."""

import math
from dataclasses import FrozenInstanceError, fields
from datetime import timedelta

import pytest
from core.casdoor.claims import ClaimsError, ClaimsValidator
from core.casdoor.crypto import CertificateTrustStore
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.reauthentication import RecentAuthTimeObservation, verify_recent_auth_time
from cryptography.hazmat.primitives.asymmetric import rsa
from test_claims import APP, CLIENT, ISSUER, NONCE, NOW, ORG, START, certificate, id_claims, sign

PRIVATE = "independent-auth-time-claim-marker"


@pytest.fixture(scope="module")
def independent_signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def independent_validator(independent_signing_key):
    return ClaimsValidator(
        trust_store=CertificateTrustStore([certificate(independent_signing_key)]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )


def signed_time(key, *, auth_time, **changes):
    return sign(key, id_claims(auth_time=auth_time, private=PRIVATE) | changes)


def assert_fixed_error(validator, token, reason, *, start=START, now=NOW):
    with pytest.raises(ClaimsError) as caught:
        verify_recent_auth_time(
            validator,
            token,
            expected_nonce=NONCE,
            auth_started_at=start,
            now=now,
        )
    error = caught.value
    assert error.reason == reason
    assert str(error) == f"casdoor_claims_{reason}"
    assert error.code is CasdoorErrorCode.INVALID_TRANSACTION and error.retry_allowed is False
    assert error.__cause__ is None and error.__context__ is None
    assert token not in repr((error, error.args, vars(error), error.__cause__, error.__context__))
    assert PRIVATE not in repr((error, error.args, vars(error), error.__cause__, error.__context__))
    assert NONCE not in repr((error, error.args, vars(error), error.__cause__, error.__context__))


@pytest.mark.parametrize("start_age", [30, 480], ids=["transaction-bound", "five-minute-bound"])
def test_independent_inclusive_lower_and_upper_edges(independent_validator, independent_signing_key, start_age):
    now = NOW
    start = now - timedelta(seconds=start_age)
    lower = max(start.timestamp() - 60, now.timestamp() - 300)
    for value in (int(lower), lower, now.timestamp()):
        token = signed_time(independent_signing_key, auth_time=value)
        observed = verify_recent_auth_time(
            independent_validator,
            token,
            expected_nonce=NONCE,
            auth_started_at=start,
            now=now,
        )
        assert type(observed) is RecentAuthTimeObservation
        assert type(observed.auth_time) is type(value) and observed.auth_time == value

    just_before = math.nextafter(lower, -math.inf)
    assert just_before < lower
    assert_fixed_error(
        independent_validator,
        signed_time(independent_signing_key, auth_time=just_before),
        "auth_time_invalid",
        start=start,
        now=now,
    )


@pytest.mark.parametrize(
    ("case", "auth_time", "reason"),
    [
        ("fresh-iat-stale-auth-time", NOW.timestamp() - 300.25, "auth_time_invalid"),
        ("future-auth-time", NOW.timestamp() + 0.25, "auth_time_invalid"),
        ("boolean-auth-time", True, "auth_time_invalid"),
        ("string-auth-time", str(int(NOW.timestamp())), "auth_time_invalid"),
        ("null-auth-time", None, "token_invalid"),
    ],
    ids=["fresh-iat-stale", "future", "bool", "string", "null"],
)
def test_independent_stale_future_and_wrong_type_rejections(
    independent_validator, independent_signing_key, case, auth_time, reason
):
    claims = id_claims(private=PRIVATE)
    claims["auth_time"] = auth_time
    token = sign(independent_signing_key, claims)
    if case == "fresh-iat-stale-auth-time":
        assert independent_validator.verify_id_token(
            token,
            expected_nonce=NONCE,
            auth_started_at=START,
            now=NOW,
        )
    assert_fixed_error(independent_validator, token, reason)


def test_missing_auth_time_remains_optional_only_for_ordinary_validation(
    independent_validator, independent_signing_key
):
    token = sign(independent_signing_key, id_claims(private=PRIVATE))
    identity = independent_validator.verify_id_token(
        token,
        expected_nonce=NONCE,
        auth_started_at=START,
        now=NOW,
    )
    assert identity.issued_at == NOW.timestamp() and not hasattr(identity, "auth_time")
    assert_fixed_error(independent_validator, token, "token_invalid")


@pytest.mark.parametrize("case", ["wrong-signature", "wrong-nonce"])
def test_independent_signature_and_nonce_checks_remain_owned_by_validator(
    independent_validator, independent_signing_key, case
):
    if case == "wrong-signature":
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = signed_time(wrong_key, auth_time=NOW.timestamp())
        reason = "signature_invalid"
    else:
        token = signed_time(independent_signing_key, auth_time=NOW.timestamp(), nonce=PRIVATE)
        reason = "nonce_invalid"
    assert_fixed_error(independent_validator, token, reason)


def test_observation_has_only_an_immutable_redacted_timestamp(independent_validator, independent_signing_key):
    token = signed_time(independent_signing_key, auth_time=NOW.timestamp(), private=PRIVATE)
    observed = verify_recent_auth_time(
        independent_validator,
        token,
        expected_nonce=NONCE,
        auth_started_at=START,
        now=NOW,
    )
    assert [field.name for field in fields(observed)] == ["auth_time"]
    assert repr(observed) == "RecentAuthTimeObservation(<redacted>)"
    assert token not in repr(observed) and PRIVATE not in repr(observed) and NONCE not in repr(observed)
    with pytest.raises(FrozenInstanceError):
        observed.auth_time = 0


def test_future_transaction_start_uses_fixed_clock_error(independent_validator, independent_signing_key):
    token = signed_time(independent_signing_key, auth_time=NOW.timestamp())
    assert_fixed_error(
        independent_validator,
        token,
        "clock_invalid",
        start=NOW + timedelta(microseconds=1),
        now=NOW,
    )
