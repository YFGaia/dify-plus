"""Actual RateLimiter with controlled Redis behavior; no sockets or credentials."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
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


class SortedRedis:
    def __init__(self, fail=None):
        self.entries = {}
        self.calls = []
        self.fail = fail
        self.after_call = lambda: None

    def called(self, method, key):
        self.calls.append((method, key))
        self.after_call()
        if method == self.fail:
            raise RuntimeError("synthetic_secret synthetic_code synthetic_token synthetic_profile")

    def zremrangebyscore(self, name, min, max):
        self.called("zremrangebyscore", name)
        entries = self.entries.setdefault(name, {})
        for member, score in list(entries.items()):
            if score <= max:
                del entries[member]
        return 0

    def zcard(self, name):
        self.called("zcard", name)
        return len(self.entries.get(name, {}))

    def zadd(self, name, mapping):
        self.called("zadd", name)
        self.entries.setdefault(name, {}).update(mapping)
        return 1

    def expire(self, name, time):
        self.called("expire", name)
        assert time == 120
        return True


@pytest.mark.parametrize("action", list(RequestAction))
def test_actual_rate_limiter_fixed_threshold_and_sliding_window(action, monkeypatch):
    wall_clock = [1000]
    monkeypatch.setattr("libs.helper.time.time", lambda: wall_clock[0])
    redis = SortedRedis()
    limiter = CasdoorRequestLimiter(redis, clock=lambda: 1)
    assert type(limiter._limits[action]) is RateLimiter
    scope = TrustedRateLimitScope.for_ip("2001:db8::1")
    for _ in range(RATE_LIMITS[action]):
        limiter.check_and_increment(action, scope, deadline=10)
    with pytest.raises(RequestSafetyError) as denied:
        limiter.check_and_increment(action, scope, deadline=10)
    public = format_public_error(denied.value)
    assert (public.status, public.code, public.retry_after_seconds, public.retry_allowed) == (
        429,
        CasdoorErrorCode.INVALID_TRANSACTION,
        60,
        False,
    )
    assert "2001:db8" not in str(redis.calls)
    wall_clock[0] += 60
    limiter.check_and_increment(action, scope, deadline=10)


@pytest.mark.parametrize("method", ["zremrangebyscore", "zcard", "zadd", "expire"])
def test_redis_read_and_increment_faults_fail_closed_without_echo(method):
    redis = SortedRedis(method)
    limiter = CasdoorRequestLimiter(redis, clock=lambda: 1)
    with pytest.raises(RequestSafetyError) as denied:
        limiter.check_and_increment(RequestAction.CALLBACK, TrustedRateLimitScope.for_ip("192.0.2.1"), deadline=10)
    public = format_public_error(denied.value)
    assert public.status == 503
    assert public.code is CasdoorErrorCode.PROVIDER_UNAVAILABLE
    assert denied.value.__suppress_context__
    assert "synthetic" not in repr(public)
    assert sum(name == method for name, _ in redis.calls) == 1


def test_pre_and_post_deadline_guards_reject_late_sync_result():
    monotonic = [10]
    redis = SortedRedis()
    limiter = CasdoorRequestLimiter(redis, clock=lambda: monotonic[0])
    scope = TrustedRateLimitScope.for_ip("192.0.2.1")
    with pytest.raises(RequestSafetyError):
        limiter.check_and_increment(RequestAction.START, scope, deadline=10)
    assert not redis.calls
    monotonic[0] = 1
    redis.after_call = lambda: monotonic.__setitem__(0, 12)
    with pytest.raises(RequestSafetyError) as late:
        limiter.check_and_increment(RequestAction.START, scope, deadline=10)
    assert late.value.failure is SafetyFailure.DEADLINE
    assert "zadd" not in [method for method, _ in redis.calls]
    monotonic[0] = 1
    redis.after_call = lambda: monotonic.__setitem__(0, 12) if redis.calls[-1][0] == "expire" else None
    with pytest.raises(RequestSafetyError):
        limiter.check_and_increment(RequestAction.START, scope, deadline=10)
    assert len(next(iter(redis.entries.values()))) == 1  # Late increment remains charged.


def test_scope_hashes_local_uuid_and_canonical_ip_only():
    account = uuid4()
    assert str(account) not in repr(TrustedRateLimitScope.for_account(account))
    assert TrustedRateLimitScope.for_ip("2001:db8::1") == TrustedRateLimitScope.for_ip("2001:db8:0:0:0:0:0:1")
    assert TrustedRateLimitScope.for_ip("192.0.2.1") != TrustedRateLimitScope.for_ip("192.0.2.2")
    with pytest.raises(RequestSafetyError):
        TrustedRateLimitScope.for_account(str(account))


def test_separate_action_and_local_account_buckets():
    redis = SortedRedis()
    limiter = CasdoorRequestLimiter(redis, clock=lambda: 1)
    first = TrustedRateLimitScope.for_account(uuid4())
    second = TrustedRateLimitScope.for_account(uuid4())
    for _ in range(5):
        limiter.check_and_increment(RequestAction.LINK, first, deadline=10)
    with pytest.raises(RequestSafetyError):
        limiter.check_and_increment(RequestAction.LINK, first, deadline=10)
    limiter.check_and_increment(RequestAction.REAUTH, first, deadline=10)
    limiter.check_and_increment(RequestAction.LINK, second, deadline=10)
    assert len(redis.entries) == 3


def test_existing_check_increment_race_is_explicitly_not_atomic():
    class ConcurrentRedis(SortedRedis):
        barrier = None

        def zcard(self, name):
            count = super().zcard(name)
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            return count

    redis = ConcurrentRedis()
    limiter = CasdoorRequestLimiter(redis, clock=lambda: 1)
    scope = TrustedRateLimitScope.for_account(uuid4())
    for _ in range(4):
        limiter.check_and_increment(RequestAction.DIAGNOSTIC, scope, deadline=10)
    redis.barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(limiter.check_and_increment, RequestAction.DIAGNOSTIC, scope, deadline=10) for _ in range(2)
        ]
        for future in futures:
            future.result(timeout=5)
    assert len(next(iter(redis.entries.values()))) == 6


@pytest.mark.parametrize("bad", ["user@example.test", "192.0.2.1, 192.0.2.2", "fe80::1%eth0", "synthetic_token", None])
def test_untrusted_ip_shapes_rejected(bad):
    with pytest.raises(RequestSafetyError):
        TrustedRateLimitScope.for_ip(bad)


@pytest.mark.parametrize("code", list(CasdoorErrorCode))
def test_public_enum_policy_preserves_known_codes(code):
    correlation = uuid4()
    public = format_public_error(code, correlation_id=correlation)
    assert public.payload() == {"code": code.value, "correlation_id": str(correlation), "retry_allowed": False}
    assert public.status in (400, 403, 409, 503)
    if code in (CasdoorErrorCode.PROVIDER_UNAVAILABLE, CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN):
        assert "new authorization request" in public.message


@pytest.mark.parametrize(
    "crypto_code,public_code",
    [
        ("casdoor_crypto_invalid", CasdoorErrorCode.INVALID_TRANSACTION),
        ("casdoor_crypto_version_unsupported", CasdoorErrorCode.CONFIG_CONFLICT),
        ("casdoor_crypto_key_version_unknown", CasdoorErrorCode.CONFIG_CONFLICT),
        ("casdoor_crypto_decryption_failed", CasdoorErrorCode.INVALID_TRANSACTION),
        ("casdoor_certificate_invalid", CasdoorErrorCode.CONFIG_CONFLICT),
        ("casdoor_signature_invalid", CasdoorErrorCode.INVALID_TRANSACTION),
        ("synthetic_token", CasdoorErrorCode.PROVIDER_UNAVAILABLE),
    ],
)
def test_crypto_string_whitelist(crypto_code, public_code):
    assert format_public_error(CryptoError(crypto_code)).code is public_code


def test_exception_cause_and_pydantic_details_never_enter_logs(caplog):
    from pydantic import BaseModel, ValidationError

    class Model(BaseModel):
        count: int

    secret = "synthetic_secret synthetic_code synthetic_token synthetic_profile"
    try:
        Model(count=secret)
    except ValidationError as cause:
        try:
            raise RuntimeError(secret) from cause
        except RuntimeError as error:
            public = format_public_error(error)
            with caplog.at_level(logging.INFO):
                record_safety_event(SafetyEvent(RequestAction.CALLBACK, public.code, public.correlation_id))
    output = caplog.text + json.dumps(caplog.records[0].casdoor_event) + repr(public)
    for sentinel in secret.split():
        assert sentinel not in output
    assert not caplog.records[0].exc_info
    assert caplog.records[0].stack_info is None
    assert set(caplog.records[0].casdoor_event) == {"action", "result_code", "correlation_id", "references", "summary"}


def test_unknown_exception_attributes_and_string_are_not_read():
    class Dangerous(Exception):
        @property
        def code(self):
            raise AssertionError("must not read")

        def __str__(self):
            raise AssertionError("must not format")

    assert format_public_error(Dangerous()).code is CasdoorErrorCode.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize(
    "field,bad",
    [
        ("action", "callback"),
        ("result_code", "synthetic_token"),
        ("correlation_id", "synthetic_code"),
        ("references", {"email": "synthetic_profile"}),
        ("summary", {"secret": "synthetic_secret"}),
    ],
)
def test_logger_rejects_free_text_and_arbitrary_payload(field, bad, caplog):
    data = {"action": RequestAction.START, "result_code": SafetyResultCode.SUCCESS, "correlation_id": uuid4()}
    data[field] = bad
    with pytest.raises(RequestSafetyError):
        record_safety_event(SafetyEvent(**data))
    assert not caplog.records


@pytest.mark.parametrize("bad", [-1, 20001, True, "synthetic_secret"])
def test_bounded_typed_summary(bad):
    with pytest.raises(RequestSafetyError):
        AuditSummary(role_count=bad).values()


def test_references_only_accept_local_uuid():
    with pytest.raises(RequestSafetyError):
        LocalReferences(account_id="synthetic_profile").values()
