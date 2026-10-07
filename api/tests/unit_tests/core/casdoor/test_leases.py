"""Offline fault injection using redis-py Lock and a synthetic Lua contract model.

This fake models Redis atomic command outcomes; it does not execute Lua or prove
script/ACL/cluster support in a deployed Redis, nor real DB concurrency.
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
from redis.exceptions import ConnectionError
from redis.lock import Lock

NS = UUID("10000000-0000-4000-8000-000000000001")
OTHER_NS = UUID("10000000-0000-4000-8000-000000000002")
ACCOUNT = UUID("20000000-0000-4000-8000-000000000001")
OTHER_ACCOUNT = UUID("20000000-0000-4000-8000-000000000002")
WORKSPACE = UUID("30000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE = UUID("30000000-0000-4000-8000-000000000002")


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


class SyntheticLock(Lock):
    # Avoid touching redis-py's production class-level registered script objects.
    lua_release = None
    lua_extend = None
    lua_reacquire = None


class FakeRedisLua:
    def __init__(self, clock: Clock, *, prefix: str = "") -> None:
        self.clock = clock
        self.prefix = prefix
        self.data: dict[str, tuple[bytes, float | None]] = {}
        self.sets: list[tuple[str, bytes, int]] = []
        self.evals: list[tuple[str, int]] = []
        self.releases: list[str] = []
        self.hook: Callable[[str, str], None] = lambda command, key: None

    def live(self, key: str) -> tuple[bytes, float | None] | None:
        value = self.data.get(key)
        if value and value[1] is not None and value[1] <= self.clock():
            del self.data[key]
            return None
        return value

    def lock(self, name: str, **kwargs: Any) -> Lock:
        assert kwargs["blocking"] is False
        assert kwargs["thread_local"] is False
        return SyntheticLock(self, self.prefix + name, **kwargs)

    def get_encoder(self) -> "FakeRedisLua":
        return self

    def encode(self, value: bytes) -> bytes:
        assert isinstance(value, bytes)
        return value

    def set(self, name: str, token: bytes, *, nx: bool, px: int) -> bool:
        assert nx is True
        assert 0 < px <= 60_000
        self.sets.append((name, token, px))
        self.hook("before_set", name)
        if self.live(name):
            return False
        self.data[name] = (token, self.clock() + px / 1000)
        self.hook("after_set", name)
        return True

    def eval(self, script: str, count: int, key: str, token: bytes, renewal: int) -> int:
        assert script == _CHECK_OR_RENEW
        assert count == 1
        self.evals.append((key, renewal))
        self.hook("before_eval", key)
        current = self.live(key)
        if current is None or current[0] != token or current[1] is None:
            return -1
        ttl = math.floor((current[1] - self.clock()) * 1000)
        if ttl <= 0:
            return -1
        if renewal > 0:
            self.data[key] = (token, self.clock() + renewal / 1000)
            ttl = renewal
        self.hook("after_eval", key)
        return ttl

    def register_script(self, source: str) -> Callable[..., int]:
        def invoke(*, keys: list[str], args: list[bytes], client: "FakeRedisLua") -> int:
            assert source == Lock.LUA_RELEASE_SCRIPT
            key = keys[0]
            client.releases.append(key)
            client.hook("before_release", key)
            current = client.live(key)
            if current is None or current[0] != args[0]:
                return 0
            del client.data[key]
            return 1

        return partial(invoke)


def scope(**kwargs: Any) -> CasdoorLeaseScope:
    return CasdoorLeaseScope(
        kwargs.pop("namespace_id", NS),
        kwargs.pop("subject", "synthetic-private-subject"),
        **kwargs,
    )


def lease(client: FakeRedisLua, target: CasdoorLeaseScope | None = None, **kwargs: Any) -> CasdoorLeases:
    return CasdoorLeases(
        client,
        target or scope(emails=("Synthetic.Private+alias@gmail.com",), account_ids=(ACCOUNT,)),
        deadline=kwargs.pop("deadline", client.clock() + 45),
        monotonic=client.clock,
        **kwargs,
    )


def test_two_callers_deduplicate_sort_complete_set_and_reverse_release() -> None:
    clock = Clock()
    redis = FakeRedisLua(clock)
    one = scope(
        emails=("User.Name+one@googlemail.com", "username@gmail.com"),
        account_ids=(OTHER_ACCOUNT, ACCOUNT, ACCOUNT),
        members=(WorkspaceMemberScope(WORKSPACE, ACCOUNT), WorkspaceMemberScope(OTHER_WORKSPACE, OTHER_ACCOUNT)),
    )
    two = scope(
        emails=("username@gmail.com", "User.Name+two@gmail.com"),
        account_ids=(ACCOUNT, OTHER_ACCOUNT),
        members=tuple(reversed(one.members)),
    )
    first, second = lease(redis, one), lease(redis, two)
    assert first.canonical_keys == second.canonical_keys == tuple(sorted(set(one.canonical_keys)))
    first.acquire()
    assert [value[0] for value in redis.sets] == list(one.canonical_keys)
    first_tokens = {value[1] for value in redis.sets}
    assert len(first_tokens) == 1
    with pytest.raises(CasdoorLeaseError, match="busy"):
        second.acquire()
    assert all(value[0] in first_tokens for value in redis.data.values())
    assert first.release() is True
    assert redis.releases[-len(one.canonical_keys) :] == list(reversed(one.canonical_keys))
    third = lease(redis, two)
    third.acquire()
    assert [value[0] for value in redis.sets[-len(two.canonical_keys) :]] == list(two.canonical_keys)
    assert redis.sets[-len(two.canonical_keys) :][0][1] not in first_tokens
    assert third.release() is True
    assert redis.data == {}


def test_scope_keys_are_private_precise_and_cross_namespace_shared() -> None:
    first = scope(emails=("Private@Example.Test",), members=(WorkspaceMemberScope(WORKSPACE, ACCOUNT),))
    other_ns = scope(
        namespace_id=OTHER_NS, emails=("private@example.test",), members=(WorkspaceMemberScope(WORKSPACE, ACCOUNT),)
    )
    assert len(set(first.canonical_keys) & set(other_ns.canonical_keys)) == 3
    assert all(first.subject not in key and "private" not in key.lower() for key in first.canonical_keys)
    assert first.subject not in repr(first)
    assert "Private@Example.Test" not in repr(first)
    assert any(key.endswith(f":member:{WORKSPACE}:{ACCOUNT}") for key in first.canonical_keys)
    assert any(key.endswith(f":account:{ACCOUNT}") for key in first.canonical_keys)
    for member in (WorkspaceMemberScope(OTHER_WORKSPACE, ACCOUNT), WorkspaceMemberScope(WORKSPACE, OTHER_ACCOUNT)):
        alternate = scope(members=(member,))
        assert {key for key in first.canonical_keys if ":member:" in key}.isdisjoint(alternate.canonical_keys)
    assert scope(subject="Case").canonical_keys != scope(subject="case").canonical_keys
    assert len(scope(subject="界" * 85).canonical_keys) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"namespace_id": str(NS)},
        {"subject": ""},
        {"subject": "界" * 86},
        {"subject": "\ud800"},
        {"emails": ("x" * 256,)},
        {"emails": (None,)},
        {"account_ids": (str(ACCOUNT),)},
        {"members": ((WORKSPACE, ACCOUNT),)},
        {"emails": ["a@example.test"]},
    ],
)
def test_invalid_scope_rejected_before_redis(values: dict[str, Any]) -> None:
    with pytest.raises(CasdoorLeaseError, match="invalid_scope"):
        scope(**values)


def test_prefix_is_applied_exactly_once_for_acquire_check_renew_release() -> None:
    redis = FakeRedisLua(Clock(), prefix="synthetic-instance:")
    target = lease(redis)
    target.acquire()
    target.ensure_owned(renew=True)
    assert target.release() is True
    expected = {redis.prefix + key for key in target.canonical_keys}
    assert {value[0] for value in redis.sets} == expected
    assert {value[0] for value in redis.evals} == expected
    assert set(redis.releases) == expected


@pytest.mark.parametrize("failure", ["busy", "before_write", "after_write", "deadline_after_write"])
def test_mid_acquisition_cleanup_including_uncertain_set(failure: str) -> None:
    clock = Clock()
    redis = FakeRedisLua(clock)
    target = lease(redis)
    failed_key = target.canonical_keys[1]
    if failure == "busy":
        redis.data[failed_key] = (b"winner", clock() + 60)

    def hook(command: str, key: str) -> None:
        if key != failed_key:
            return
        if (failure, command) in (("before_write", "before_set"), ("after_write", "after_set")):
            raise ConnectionError("synthetic-private-error")
        if failure == "deadline_after_write" and command == "after_set":
            clock.now += 46

    redis.hook = hook
    with pytest.raises(CasdoorLeaseError) as error:
        target.acquire()
    assert "synthetic-private" not in str(error.value)
    assert len(redis.sets) == 2
    assert redis.releases == list(reversed(target.canonical_keys[:2]))
    assert redis.data == ({failed_key: (b"winner", 160.0)} if failure == "busy" else {})
    with pytest.raises(CasdoorLeaseError, match="stopped"):
        target.ensure_owned(renew=True)


@pytest.mark.parametrize("loss", ["expired", "replaced", "persistent", "renew_failure"])
def test_loss_or_failed_renewal_latches_entire_set_without_deleting_winner(loss: str) -> None:
    clock = Clock()
    redis = FakeRedisLua(clock)
    target = lease(redis)
    target.acquire()
    key = target.canonical_keys[1]
    if loss == "expired":
        clock.now += 16
    elif loss == "replaced":
        redis.data[key] = (b"winner", clock() + 30)
    elif loss == "persistent":
        redis.data[key] = (redis.data[key][0], None)
    else:

        def fail_renew(command: str, name: str) -> None:
            if command == "before_eval" and name == key:
                raise ConnectionError("synthetic-failed-renew")

        redis.hook = fail_renew
    with pytest.raises(CasdoorLeaseError):
        target.ensure_owned(renew=True)
    evaluated = len(redis.evals)
    with pytest.raises(CasdoorLeaseError, match="stopped"):
        target.ensure_owned()
    assert len(redis.evals) == evaluated
    assert target.release() is True
    assert redis.data == ({key: (b"winner", 130.0)} if loss == "replaced" else {})


def test_replacement_immediately_before_atomic_release_is_preserved() -> None:
    redis = FakeRedisLua(Clock())
    target = lease(redis)
    target.acquire()
    key = target.canonical_keys[0]

    def replace(command: str, name: str) -> None:
        if command == "before_release" and name == key:
            redis.data[key] = (b"new-owner", redis.clock() + 20)

    redis.hook = replace
    assert target.release() is True
    assert redis.data == {key: (b"new-owner", 120.0)}


def test_expired_callback_never_acquires_and_late_check_cannot_continue() -> None:
    clock = Clock()
    redis = FakeRedisLua(clock)
    expired = lease(redis, deadline=clock())
    with pytest.raises(CasdoorLeaseError, match="deadline"):
        expired.acquire()
    assert redis.sets == redis.evals == redis.releases == []
    target = lease(redis)
    target.acquire()

    def late(command: str, key: str) -> None:
        if command == "after_eval":
            clock.now += 46

    redis.hook = late
    evaluated = len(redis.evals)
    with pytest.raises(CasdoorLeaseError, match="deadline"):
        target.ensure_owned()
    assert len(redis.evals) == evaluated + 1
    with pytest.raises(CasdoorLeaseError, match="stopped"):
        target.ensure_owned(renew=True)
    target.release()


def test_partial_lease_expiry_prevents_any_later_acquisition() -> None:
    clock = Clock()
    redis = FakeRedisLua(clock)
    target = lease(redis)
    first_key = target.canonical_keys[0]

    def expire(command: str, key: str) -> None:
        if command == "after_set" and key == first_key:
            clock.now += 16

    redis.hook = expire
    with pytest.raises(CasdoorLeaseError, match="ownership_lost"):
        target.acquire()
    assert [value[0] for value in redis.sets] == [first_key]
    assert redis.releases == [first_key]
    assert redis.data == {}


def test_reentrant_acquire_is_rejected_and_requires_finally_cleanup() -> None:
    redis = FakeRedisLua(Clock())
    target = lease(redis)
    target.acquire()
    commands = len(redis.sets)
    with pytest.raises(CasdoorLeaseError, match="already_started"):
        target.acquire()
    with pytest.raises(CasdoorLeaseError, match="stopped"):
        target.ensure_owned()
    assert len(redis.sets) == commands
    assert target.release() is True
    assert redis.data == {}


def test_renewal_is_explicit_capped_by_remaining_budget_and_has_real_checks() -> None:
    clock = Clock()
    redis = FakeRedisLua(clock)
    target = lease(redis, ttl_seconds=60, deadline=clock() + 45)
    target.acquire()
    assert all(0 < value[2] <= 45_000 for value in redis.sets)
    clock.now += 10
    target.ensure_owned()
    assert all(value[1] == 0 for value in redis.evals)
    checks = len(redis.evals)
    target.ensure_owned(renew=True)
    assert len(redis.evals) == checks + len(target.canonical_keys)
    assert all(0 < value[1] <= 35_000 for value in redis.evals[checks:])
    assert all(value[1] <= 145 for value in redis.data.values() if value[1] is not None)
    target.release()


def test_release_failure_is_reported_and_retry_only_cleans_owned_token() -> None:
    redis = FakeRedisLua(Clock())
    target = lease(redis)
    target.acquire()
    key = target.canonical_keys[0]

    def fail(command: str, name: str) -> None:
        if command == "before_release" and name == key:
            raise ConnectionError("synthetic-release-error")

    redis.hook = fail
    assert target.release() is False
    assert set(redis.data) == {key}
    redis.data[key] = (b"winner", 130.0)
    redis.hook = lambda command, name: None
    assert target.release() is True
    assert redis.data == {key: (b"winner", 130.0)}


@pytest.mark.parametrize("values", [{"ttl_seconds": 61}, {"ttl_seconds": 0}, {"deadline": float("nan")}])
def test_invalid_budgets_fail_before_redis(values: dict[str, Any]) -> None:
    redis = FakeRedisLua(Clock())
    with pytest.raises(CasdoorLeaseError, match="invalid_budget"):
        lease(redis, **values)
    assert redis.sets == []
