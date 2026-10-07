"""Independent request-safety counterexamples using the real RateLimiter API."""

import json
import logging
from uuid import uuid4

import pytest
from core.casdoor.crypto import CryptoError
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.request_safety import (
    RATE_LIMITS,
    AuditSummary,
    CasdoorRequestLimiter,
    LocalReferences,
    RequestAction,
    RequestSafetyError,
    SafetyEvent,
    SafetyFailure,
    SafetyResultCode,
    TrustedRateLimitScope,
    format_public_error,
    record_safety_event,
)
from libs.helper import RateLimiter


class ControlledRedis:
    """In-memory sorted-set subset used by the production RateLimiter."""

    def __init__(self, failure=None):
        self.sorted_sets = {}
        self.calls = []
        self.failure = failure
        self.on_call = lambda _method: None

    def _call(self, method, key):
        self.calls.append((method, key))
        self.on_call(method)
        if method == self.failure:
            raise OSError("syntheticSecret syntheticCode syntheticToken syntheticProfile")

    def zremrangebyscore(self, name, _minimum, maximum):
        self._call("zremrangebyscore", name)
        rows = self.sorted_sets.setdefault(name, {})
        for member, score in list(rows.items()):
            if score <= float(maximum):
                del rows[member]
        return 0

    def zcard(self, name):
        self._call("zcard", name)
        return len(self.sorted_sets.get(name, {}))

    def zadd(self, name, mapping):
        self._call("zadd", name)
        self.sorted_sets.setdefault(name, {}).update(mapping)
        return len(mapping)

    def expire(self, name, seconds):
        self._call("expire", name)
        assert seconds == 120
        return True


def make_limiter(redis, monkeypatch, now=1000):
    wall_time = [now]
    monkeypatch.setattr("libs.helper.time.time", lambda: wall_time[0])
    return CasdoorRequestLimiter(redis, clock=lambda: 1), wall_time


@pytest.mark.parametrize("action", list(RequestAction))
def test_real_rate_limiter_uses_fixed_action_thresholds_and_scoped_keys(action, monkeypatch):
    redis = ControlledRedis()
    limiter, _ = make_limiter(redis, monkeypatch)
    scope = TrustedRateLimitScope.for_ip("2001:db8::1")
    assert isinstance(limiter._limits[action], RateLimiter)
    assert limiter._limits[action].max_attempts == RATE_LIMITS[action]
    assert limiter._limits[action].time_window == 60

    for _ in range(RATE_LIMITS[action]):
        limiter.check_and_increment(action, scope, deadline=20)
    with pytest.raises(RequestSafetyError) as exc_info:
        limiter.check_and_increment(action, scope, deadline=20)

    public = format_public_error(exc_info.value)
    assert (public.status, public.retry_after_seconds, public.retry_allowed) == (429, 60, False)
    assert public.code is CasdoorErrorCode.INVALID_TRANSACTION
    keys = {key for _, key in redis.calls}
    assert len(keys) == 1
    key = next(iter(keys))
    assert action.value in key and "ip:" in key and "2001:db8" not in key


def test_rate_limit_action_and_account_scopes_are_isolated(monkeypatch):
    redis = ControlledRedis()
    limiter, _ = make_limiter(redis, monkeypatch)
    account_a = TrustedRateLimitScope.for_account(uuid4())
    account_b = TrustedRateLimitScope.for_account(uuid4())
    for _ in range(RATE_LIMITS[RequestAction.LINK]):
        limiter.check_and_increment(RequestAction.LINK, account_a, deadline=20)
    with pytest.raises(RequestSafetyError):
        limiter.check_and_increment(RequestAction.LINK, account_a, deadline=20)
    limiter.check_and_increment(RequestAction.REAUTH, account_a, deadline=20)
    limiter.check_and_increment(RequestAction.LINK, account_b, deadline=20)
    assert len(redis.sorted_sets) == 3
    assert all("account:" in key for key in redis.sorted_sets)
    assert str(account_a) not in " ".join(redis.sorted_sets)


@pytest.mark.parametrize("fault", ["zremrangebyscore", "zcard", "zadd", "expire"])
def test_actual_limiter_storage_faults_fail_closed(fault, monkeypatch):
    redis = ControlledRedis(fault)
    limiter, _ = make_limiter(redis, monkeypatch)
    with pytest.raises(RequestSafetyError) as exc_info:
        limiter.check_and_increment(RequestAction.CALLBACK, TrustedRateLimitScope.for_ip("192.0.2.9"), deadline=20)
    public = format_public_error(exc_info.value)
    assert (public.status, public.code, public.retry_allowed) == (
        503,
        CasdoorErrorCode.PROVIDER_UNAVAILABLE,
        False,
    )
    assert sum(method == fault for method, _ in redis.calls) == 1
    assert "synthetic" not in repr(public)


@pytest.mark.parametrize("late_at", ["zcard", "zadd", "expire"])
def test_deadline_checks_reject_late_sync_redis_result(late_at, monkeypatch):
    redis = ControlledRedis()
    limiter, _ = make_limiter(redis, monkeypatch)
    monotonic = [5.0]
    limiter._clock = lambda: monotonic[0]
    redis.on_call = lambda method: monotonic.__setitem__(0, 11.0) if method == late_at else None

    with pytest.raises(RequestSafetyError) as exc_info:
        limiter.check_and_increment(RequestAction.START, TrustedRateLimitScope.for_ip("192.0.2.7"), deadline=10.0)
    assert exc_info.value.failure is SafetyFailure.DEADLINE
    assert format_public_error(exc_info.value).status == 503
    methods = [method for method, _ in redis.calls]
    assert methods[-1] == ("zcard" if late_at == "zcard" else "expire")
    if late_at == "zcard":
        assert "zadd" not in methods  # Never increment after a late read.
    if late_at in ("zadd", "expire"):
        assert len(next(iter(redis.sorted_sets.values()))) == 1  # Do not retry/refund late writes.


def test_unknown_hostile_exception_attributes_and_formatting_are_never_read():
    class HostileError(Exception):
        @property
        def code(self):
            raise AssertionError(".code must not be read")

        def __str__(self):
            raise AssertionError("exception must not be stringified")

    result = format_public_error(HostileError())
    assert result.code is CasdoorErrorCode.PROVIDER_UNAVAILABLE
    assert result.status == 503 and result.retry_allowed is False


def test_error_mapping_logging_and_keys_do_not_reveal_exception_or_identity_data(caplog):
    from pydantic import BaseModel, ValidationError

    marker = "syntheticSecret syntheticCode syntheticToken syntheticProfile"

    class Profile(BaseModel):
        count: int

    try:
        Profile(count=marker)
    except ValidationError as validation_error:
        try:
            raise RuntimeError(marker) from validation_error
        except RuntimeError as error:
            public = format_public_error(error)
            with caplog.at_level(logging.INFO):
                record_safety_event(SafetyEvent(RequestAction.CALLBACK, public.code, public.correlation_id))

    captured = caplog.text + json.dumps(caplog.records[0].casdoor_event) + repr(public)
    assert all(part not in captured for part in marker.split())
    assert caplog.records[0].exc_info is None or caplog.records[0].exc_info is False
    assert caplog.records[0].stack_info is None
    assert caplog.records[0].casdoor_event["result_code"] == CasdoorErrorCode.PROVIDER_UNAVAILABLE.value

    unknown_crypto = format_public_error(CryptoError("syntheticToken"))
    assert unknown_crypto.code is CasdoorErrorCode.PROVIDER_UNAVAILABLE
    assert "syntheticToken" not in repr(unknown_crypto)


@pytest.mark.parametrize(
    "bad_references,bad_summary",
    [
        (LocalReferences(account_id="syntheticCode"), AuditSummary()),
        (LocalReferences(), AuditSummary(role_count=True)),
        (LocalReferences(), AuditSummary(intent_count=-1)),
        (LocalReferences(), AuditSummary(workspace_count=20001)),
    ],
)
def test_invalid_log_references_and_counts_fail_before_record(caplog, bad_references, bad_summary):
    event = SafetyEvent(
        RequestAction.DIAGNOSTIC,
        SafetyResultCode.SUCCESS,
        uuid4(),
        references=bad_references,
        summary=bad_summary,
    )
    with caplog.at_level(logging.INFO), pytest.raises(RequestSafetyError):
        record_safety_event(event)
    assert caplog.records == []
