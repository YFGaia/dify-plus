"""Name-only parser changes and distinct audit whitelist, with real old owners."""

from uuid import uuid4

import pytest

from core.casdoor.claims import ClaimsError
from core.casdoor.request_safety import (
    RATE_LIMITS,
    LocalReferences,
    ProfileAuditAction,
    ProfileAuditEvent,
    ProfileAuditResult,
    ProfileAuditSummary,
    ProfileNameReason,
    RequestAction,
    RequestSafetyError,
    record_safety_event,
)
from tests.unit_tests.core.casdoor import test_claims as claims

key = claims.key
validator = claims.validator


@pytest.mark.parametrize("name", [None, "", " ", "  \u2003  "])
def test_userinfo_empty_name_only_normalizes(validator, key, name):
    assert (
        validator.verify_userinfo({"sub": claims.SUB, "name": name}, identity=claims.identity(validator, key)).name
        is None
    )


@pytest.mark.parametrize(
    "name",
    [True, False, 42, [], {}, "\t", "\n", "x\x7f", " " * 256, "\ud800", "汉" * 86],
)
def test_userinfo_hostile_name_stays_strict(validator, key, name):
    with pytest.raises(ClaimsError, match="casdoor_claims_profile_schema_invalid"):
        validator.verify_userinfo({"sub": claims.SUB, "name": name}, identity=claims.identity(validator, key))


def test_userinfo_preserves_exact_valid_bytes_and_missing(validator, key):
    verified = claims.identity(validator, key)
    assert validator.verify_userinfo({"sub": claims.SUB}, identity=verified).name is None
    name = "  汉 AbC  "
    assert validator.verify_userinfo({"sub": claims.SUB, "name": name}, identity=verified).name == name


def event():
    return ProfileAuditEvent(
        ProfileAuditAction.PROFILE_SYNC,
        ProfileAuditResult.APPLIED,
        uuid4(),
        LocalReferences(),
        ProfileAuditSummary(1, ProfileAuditResult.APPLIED, ProfileNameReason.FILLED_EMPTY, False, True),
    )


def test_distinct_profile_event_never_request_action_or_log():
    assert len(RequestAction) == len(RATE_LIMITS) == 5
    with pytest.raises(RequestSafetyError):
        record_safety_event(event())
    assert event().values()["action"] == "profile_sync"


@pytest.mark.parametrize("generation", [True, -1, 2**63, 1.5])
def test_profile_audit_strict_generation(generation):
    with pytest.raises(RequestSafetyError):
        ProfileAuditSummary(
            generation,
            ProfileAuditResult.APPLIED,
            ProfileNameReason.FILLED_EMPTY,
            False,
            True,
        ).values()
