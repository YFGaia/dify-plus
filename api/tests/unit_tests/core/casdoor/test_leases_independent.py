"""Independent offline checks for I08-B against redis-py and the real wrapper.

The fake client models command outcomes and Lua atomicity; it does not execute
Redis Lua or establish behavior against a running Redis deployment.
"""

import math
from collections.abc import Callable
from functools import partial
from typing import Any
from uuid import UUID

import pytest
from core.casdoor.leases import (
    _CHECK_OR_RENEW,
    CasdoorLeaseError,
    CasdoorLeases,
    CasdoorLeaseScope,
    WorkspaceMemberScope,
)
from extensions import ext_redis
from redis.exceptions import ConnectionError
from redis.lock import Lock

NS = UUID("11111111-2222-4333-8444-555555555555")
OTHER_NS = UUID("11111111-2222-4333-8444-666666666666")
ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
WORKSPACE = UUID("99999999-8888-4777-8666-555555555555")


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


class OfflineRedis:
    """Small response/fault model used behind the production Redis wrapper."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.data: dict[str, tuple[bytes, float | None]] = {}
        self.set_calls: list[tuple[str, bytes, int]] = []
        self.lock_calls: list[tuple[bool, bool]] = []
        self.eval_calls: list[tuple[str, int]] = []
        self.release_calls: list[str] = []
        self.hook: Callable[[str, str], None] = lambda _op, _key: None

    def get_encoder(self) -> "OfflineRedis":
        return self

    @staticmethod
    def encode(value: bytes | str) -> bytes:
        return value.encode() if isinstance(value, str) else value

    def live(self, key: str) -> tuple[bytes, float | None] | None:
        current = self.data.get(key)
        if current and current[1] is not None and current[1] <= self.clock():
            del self.data[key]
            return None
        return current

    def set(self, key: str, token: bytes, *, nx: bool, px: int) -> bool:
        assert nx is True
        assert 0 < px <= 60_000
        self.set_calls.append((key, token, px))
        self.hook("before_set", key)
        if self.live(key):
            return False
        self.data[key] = (token, self.clock() + px / 1000)
        self.hook("after_set", key)
        return True

    def lock(
        self,
        name: str,
        *,
        timeout: float | None = None,
        sleep: float = 0.1,
        blocking: bool = True,
        blocking_timeout: float | None = None,
        thread_local: bool = True,
    ) -> Lock:
        self.lock_calls.append((blocking, thread_local))
        return Lock(
            self,
            name,
            timeout=timeout,
            sleep=sleep,
            blocking=blocking,
            blocking_timeout=blocking_timeout,
            thread_local=thread_local,
        )

    def eval(self, script: str, key_count: int, key: str, token: bytes, renewal_ms: int) -> int:
        assert script == _CHECK_OR_RENEW
        assert key_count == 1
        self.eval_calls.append((key, renewal_ms))
        self.hook("before_eval", key)
        current = self.live(key)
        if current is None or current[0] != token or current[1] is None:
            return -1
        ttl_ms = math.floor((current[1] - self.clock()) * 1000)
        if ttl_ms <= 0:
            return -1
        if renewal_ms > 0:
            self.data[key] = (token, self.clock() + renewal_ms / 1000)
            ttl_ms = renewal_ms
        self.hook("after_eval", key)
        return ttl_ms

    def register_script(self, source: str) -> Callable[..., int]:
        def run(*, keys: list[str], args: list[bytes], client: "OfflineRedis") -> int:
            assert source == Lock.LUA_RELEASE_SCRIPT
            key = keys[0]
            client.release_calls.append(key)
            client.hook("before_release", key)
            current = client.live(key)
            if current is None or current[0] != args[0]:
                return 0
            del client.data[key]
            return 1

        return partial(run)


def scope(*, ns: UUID = NS, subject: str = "private-member-17", **values: Any) -> CasdoorLeaseScope:
    return CasdoorLeaseScope(ns, subject, **values)


def make_lease(redis_wrapper: ext_redis.RedisClientWrapper, clock: Clock, target: CasdoorLeaseScope) -> CasdoorLeases:
    return CasdoorLeases(redis_wrapper, target, deadline=clock() + 30, ttl_seconds=60, monotonic=clock)


@pytest.fixture
def wrapped_redis(monkeypatch: pytest.MonkeyPatch) -> tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock]:
    clock = Clock()
    client = OfflineRedis(clock)
    # Exercise redis-py's actual Lock class and script registration without a server.
    monkeypatch.setattr(Lock, "lua_release", None)
    monkeypatch.setattr(Lock, "lua_extend", None)
    monkeypatch.setattr(Lock, "lua_reacquire", None)
    monkeypatch.setattr(ext_redis.dify_config, "REDIS_KEY_PREFIX", "independent-prefix")
    wrapper = ext_redis.RedisClientWrapper()
    wrapper._client = client  # type: ignore[assignment]
    return wrapper, client, clock


def test_real_lock_and_wrapper_apply_prefix_once_and_use_random_bounded_owner(
    wrapped_redis: tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock],
) -> None:
    wrapper, redis, clock = wrapped_redis
    target = scope(
        emails=("Member@Example.Test",),
        members=(WorkspaceMemberScope(WORKSPACE, ACCOUNT),),
    )
    lease = make_lease(wrapper, clock, target)
    lease.acquire()

    expected = {f"independent-prefix:{key}" for key in target.canonical_keys}
    assert {key for key, _owner, _ttl in redis.set_calls} == expected
    owners = {owner for _key, owner, _ttl in redis.set_calls}
    assert len(owners) == 1
    owner = next(iter(owners))
    assert len(owner) == 64
    assert all(character in "0123456789abcdef" for character in owner.decode("ascii"))
    assert all(0 < ttl_ms <= 30_000 for _key, _owner, ttl_ms in redis.set_calls)
    assert redis.lock_calls == [(False, False)] * len(target.canonical_keys)
    assert {key for key, _renewal in redis.eval_calls} == expected
    assert all(key.startswith("independent-prefix:") for key, _renewal in redis.eval_calls)
    assert lease.release() is True
    assert set(redis.release_calls) == expected
    assert not redis.data


def test_canonical_set_deduplicates_all_input_orders_and_shares_non_subject_scope() -> None:
    member = WorkspaceMemberScope(WORKSPACE, ACCOUNT)
    a = scope(
        emails=("Member+first@gmail.com", "member@gmail.com", "MEMBER@example.test"),
        account_ids=(ACCOUNT, ACCOUNT),
        members=(member, member),
    )
    b = scope(
        ns=OTHER_NS,
        emails=("MEMBER@example.test", "member@gmail.com", "Member+second@gmail.com"),
        account_ids=(ACCOUNT,),
        members=(member,),
    )
    assert a.canonical_keys == tuple(sorted(set(a.canonical_keys)))
    shared = set(a.canonical_keys) & set(b.canonical_keys)
    assert len(shared) == 4  # Gmail aliases normalize together; plus exact email/account/member keys
    assert all(key.startswith("casdoor:lease:v1:") for key in a.canonical_keys)
    assert all("private-member-17" not in key and "member@gmail.com" not in key.lower() for key in a.canonical_keys)
    assert "private-member-17" not in repr(a)
    assert "MEMBER@example.test" not in repr(a)
    assert not hasattr(make_lease(_unconnected_wrapper(), Clock(), a), "add_key")
    assert any(key.endswith(f":member:{WORKSPACE}:{ACCOUNT}") for key in a.canonical_keys)


def _unconnected_wrapper() -> ext_redis.RedisClientWrapper:
    """Lease construction only snapshots scope and does not use this client."""
    return ext_redis.RedisClientWrapper()


def test_owner_loss_latches_whole_set_and_source_script_checks_owner_with_pttl_atomically(
    wrapped_redis: tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock],
) -> None:
    wrapper, redis, clock = wrapped_redis
    target = scope(account_ids=(ACCOUNT,))
    lease = make_lease(wrapper, clock, target)
    lease.acquire()
    first_key = lease.canonical_keys[0]
    redis.data[f"independent-prefix:{first_key}"] = (b"replacement-winner", clock() + 20)

    before = len(redis.eval_calls)
    with pytest.raises(CasdoorLeaseError, match="ownership_lost"):
        lease.ensure_owned(renew=True)
    after_failure = len(redis.eval_calls)
    with pytest.raises(CasdoorLeaseError, match="stopped"):
        lease.ensure_owned(renew=True)
    assert len(redis.eval_calls) == after_failure
    assert after_failure > before
    assert "redis.call('get', KEYS[1])" in _CHECK_OR_RENEW
    assert "redis.call('pttl', KEYS[1])" in _CHECK_OR_RENEW
    assert _CHECK_OR_RENEW.index("redis.call('get'") < _CHECK_OR_RENEW.index("redis.call('pttl'")
    assert "redis.call('pexpire', KEYS[1], renewal)" in _CHECK_OR_RENEW
    assert lease.release() is True
    assert redis.data[f"independent-prefix:{first_key}"][0] == b"replacement-winner"


@pytest.mark.parametrize("failure", ["busy_replacement", "write_then_response_lost", "write_returned_after_deadline"])
def test_partial_acquisition_cleanup_and_late_return_never_start_more_work(
    wrapped_redis: tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock], failure: str
) -> None:
    wrapper, redis, clock = wrapped_redis
    target = scope(emails=("private@example.test",), account_ids=(ACCOUNT,))
    lease = make_lease(wrapper, clock, target)
    failed_key = lease.canonical_keys[1]
    physical = f"independent-prefix:{failed_key}"
    if failure == "busy_replacement":
        redis.data[physical] = (b"winner", clock() + 20)
    elif failure == "write_then_response_lost":

        def lose_response(operation: str, key: str) -> None:
            if operation == "after_set" and key == physical:
                raise ConnectionError("private redis response detail")

        redis.hook = lose_response
    else:

        def late_success(operation: str, key: str) -> None:
            if operation == "after_set" and key == physical:
                clock.now += 31

        redis.hook = late_success

    with pytest.raises(CasdoorLeaseError) as raised:
        lease.acquire()
    assert "private redis response detail" not in str(raised.value)
    assert len(redis.set_calls) == 2
    assert redis.release_calls == [f"independent-prefix:{key}" for key in reversed(target.canonical_keys[:2])]
    if failure == "busy_replacement":
        assert redis.data[physical][0] == b"winner"
    else:
        # The offline compare-delete succeeds. Real transport loss can leave our
        # attempted SET alive until TTL, which release's bool must report honestly.
        assert not redis.data
    with pytest.raises(CasdoorLeaseError, match="stopped"):
        lease.ensure_owned()


def test_cleanup_transport_failure_is_reported_and_retryable_without_masking_scope_data(
    wrapped_redis: tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock],
) -> None:
    wrapper, redis, clock = wrapped_redis
    lease = make_lease(wrapper, clock, scope(subject="sensitive-subject-123"))
    lease.acquire()
    one_key = f"independent-prefix:{lease.canonical_keys[0]}"
    fail_once = True

    def fail_first_release(operation: str, key: str) -> None:
        nonlocal fail_once
        if operation == "before_release" and key == one_key and fail_once:
            fail_once = False
            raise ConnectionError("sensitive Redis failure")

    redis.hook = fail_first_release
    assert lease.release() is False
    assert one_key in redis.data  # cleanup is not falsely reported as confirmed
    assert lease.release() is True
    assert not redis.data


def test_set_response_loss_plus_failed_atomic_cleanup_leaves_only_ttl_bounded_unknown_lease(
    wrapped_redis: tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock],
) -> None:
    wrapper, redis, clock = wrapped_redis
    target = scope(subject="uncertain-private-subject")
    lease = make_lease(wrapper, clock, target)
    target_key = f"independent-prefix:{lease.canonical_keys[0]}"
    fail_cleanup_once = True

    def lose_response_and_cleanup(operation: str, key: str) -> None:
        nonlocal fail_cleanup_once
        if key != target_key:
            return
        if operation == "after_set":
            raise ConnectionError("write response lost")
        if operation == "before_release" and fail_cleanup_once:
            fail_cleanup_once = False
            raise ConnectionError("cleanup response lost")

    redis.hook = lose_response_and_cleanup
    with pytest.raises(CasdoorLeaseError, match="redis_unavailable"):
        lease.acquire()
    # acquire reports failure but cannot claim that cleanup reached Redis.
    assert redis.data[target_key][0].decode("ascii") == redis.set_calls[0][1].decode("ascii")
    assert redis.data[target_key][1] is not None
    assert lease.release() is True
    assert target_key not in redis.data


@pytest.mark.parametrize(
    "arguments",
    [
        {"subject": b"not-text"},
        {"subject": "\ud800"},
        {"ns": "11111111-2222-4333-8444-555555555555"},
        {"account_ids": ("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",)},
        {"emails": ("界" * 86,)},
    ],
)
def test_scope_rejects_wrong_types_and_oversize_utf8_without_leaking_input(arguments: dict[str, Any]) -> None:
    sensitive = repr(arguments)
    with pytest.raises(CasdoorLeaseError) as raised:
        scope(**arguments)
    assert sensitive not in str(raised.value)
    assert "casdoor_lease_invalid_scope" == str(raised.value)


@pytest.mark.parametrize("ttl", [0, -0.001, 0.0009, 60.001, math.inf, math.nan])
def test_invalid_ttl_fails_before_any_client_io(
    wrapped_redis: tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock], ttl: float
) -> None:
    wrapper, redis, clock = wrapped_redis
    with pytest.raises(CasdoorLeaseError, match="invalid_budget"):
        CasdoorLeases(wrapper, scope(), deadline=clock() + 1, ttl_seconds=ttl, monotonic=clock)
    assert not redis.set_calls
    assert not redis.eval_calls


def test_deadline_guard_allows_no_background_renewal_or_late_second_key(
    wrapped_redis: tuple[ext_redis.RedisClientWrapper, OfflineRedis, Clock],
) -> None:
    wrapper, redis, clock = wrapped_redis
    target = scope(emails=("deadline@example.test",))
    lease = CasdoorLeases(wrapper, target, deadline=clock() + 1, ttl_seconds=60, monotonic=clock)
    eval_count = len(redis.eval_calls)
    clock.now += 0.1
    assert len(redis.eval_calls) == eval_count  # no heartbeat runs without a caller guard
    clock.now -= 0.1
    delayed_key = f"independent-prefix:{lease.canonical_keys[0]}"

    def delay_success(operation: str, key: str) -> None:
        if operation == "after_set" and key == delayed_key:
            clock.now += 2

    redis.hook = delay_success
    with pytest.raises(CasdoorLeaseError, match="deadline"):
        lease.acquire()
    assert [key for key, _owner, _ttl in redis.set_calls] == [delayed_key]
    assert not any(renewal_ms > 0 for _key, renewal_ms in redis.eval_calls)
    assert not redis.data  # the synthetic cleanup returned successfully
