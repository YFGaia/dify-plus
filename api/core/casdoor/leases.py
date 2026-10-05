"""Bounded Casdoor leases; not remote fencing, authentication or a transaction owner.

The service resolves the complete scope from DB state before acquisition, then
rechecks it under all locks. A changed scope requires releasing the complete set
and restarting; never expand a partially held set. Check ``ensure_owned()`` before
and after each business step/external I/O and immediately before DB commit. Use
the same monotonic deadline as the callback gateway (normally <= 45 seconds).

Redis I/O timeouts/retries are owned by the injected client. Calls cannot interrupt
a blocked socket; the post-call deadline guard rejects late results before any
further business work. No background heartbeat or blocking lock wait is used.
"""

import hashlib
import math
import secrets
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from redis.exceptions import LockNotOwnedError
from redis.lock import Lock
from services.account_email import normalize_email

from core.casdoor.errors import CasdoorErrorCode

_PREFIX = "casdoor:lease:v1"
_MAX_TTL_SECONDS = 60.0

# One key per invocation also works with Redis Cluster. Compare and renew are
# atomic; a missing/persistent/replaced key is never treated as a valid lease.
_CHECK_OR_RENEW = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return -1 end
local ttl = redis.call('pttl', KEYS[1])
if ttl <= 0 then return -1 end
local renewal = tonumber(ARGV[2])
if renewal > 0 then
    redis.call('pexpire', KEYS[1], renewal)
    return redis.call('pttl', KEYS[1])
end
return ttl
"""


class CasdoorLeaseError(Exception):
    """Stable internal reasons only; no identity, key, token or Redis error text."""

    code = CasdoorErrorCode.AUTHORIZATION_PENDING

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"casdoor_lease_{reason}")


def _uuid(value: UUID) -> str:
    if not isinstance(value, UUID):
        raise CasdoorLeaseError("invalid_scope")
    return str(value)


def _digest(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CasdoorLeaseError("invalid_scope")
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        raise CasdoorLeaseError("invalid_scope") from None
    if len(raw) > 255:
        raise CasdoorLeaseError("invalid_scope")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class WorkspaceMemberScope:
    """Trusted DB-reconstructed member scope, including RBAC membership mutations."""

    workspace_id: UUID
    account_id: UUID

    def __post_init__(self) -> None:
        _uuid(self.workspace_id)
        _uuid(self.account_id)


@dataclass(frozen=True, repr=False)
class CasdoorLeaseScope:
    """Immutable complete scope; raw identity values never enter Redis keys/repr.

    Email/account/member locks intentionally span Casdoor namespaces. Legacy
    provider registration paths do not share these locks; this is no guarantee
    of deployment-wide email uniqueness. Email normalization reuses its owner
    unchanged, including Gmail aliases; email validity is the admission owner's
    responsibility. Subjects use exact, untrimmed UTF-8, matching identity storage.
    """

    namespace_id: UUID
    subject: str
    emails: tuple[str, ...] = ()
    account_ids: tuple[UUID, ...] = ()
    members: tuple[WorkspaceMemberScope, ...] = ()

    def __post_init__(self) -> None:
        _uuid(self.namespace_id)
        _digest(self.subject)
        if any(type(values) is not tuple for values in (self.emails, self.account_ids, self.members)):
            raise CasdoorLeaseError("invalid_scope")
        for email in self.emails:
            _digest(email)
            _digest(normalize_email(email))
        for account_id in self.account_ids:
            _uuid(account_id)
        if any(not isinstance(member, WorkspaceMemberScope) for member in self.members):
            raise CasdoorLeaseError("invalid_scope")

    @property
    def canonical_keys(self) -> tuple[str, ...]:
        keys = {f"{_PREFIX}:subject:{self.namespace_id}:{_digest(self.subject)}"}
        keys.update(f"{_PREFIX}:email:{_digest(normalize_email(email))}" for email in self.emails)
        accounts = set(self.account_ids) | {member.account_id for member in self.members}
        keys.update(f"{_PREFIX}:account:{account_id}" for account_id in accounts)
        keys.update(f"{_PREFIX}:member:{member.workspace_id}:{member.account_id}" for member in self.members)
        return tuple(sorted(keys))


class RedisLeaseClient(Protocol):
    """The existing prefix-aware Redis wrapper or a compatible injected owner."""

    def lock(self, name: str, *, timeout: float, blocking: bool, thread_local: bool) -> Lock: ...


class CasdoorLeases:
    """Single-use sorted lease set, with explicit caller-thread renewal.

    ``ttl_seconds`` is at most 60; actual acquire/renew TTL is capped by remaining
    deadline. Call ``ensure_owned(renew=True)`` before a long bounded step; normal
    checks always read real Redis ownership + TTL, with no local freshness cache.
    Renewal failure permanently stops this instance. Never renew an expired lease.
    Always call ``release()`` in finally, including after uncertain Redis failures.
    Its boolean result reports cleanup transport failures without masking the
    business exception; expired/replaced locks are safe cleanup successes.
    """

    def __init__(
        self,
        redis_client: RedisLeaseClient,
        scope: CasdoorLeaseScope,
        *,
        deadline: float,
        ttl_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(scope, CasdoorLeaseScope):
            raise CasdoorLeaseError("invalid_scope")
        if (
            type(deadline) not in (float, int)
            or not math.isfinite(deadline)
            or type(ttl_seconds) not in (float, int)
            or not math.isfinite(ttl_seconds)
            or not 0.001 <= ttl_seconds <= _MAX_TTL_SECONDS
        ):
            raise CasdoorLeaseError("invalid_budget")
        self._client = redis_client
        self._keys = scope.canonical_keys
        self._deadline = float(deadline)
        self._ttl_seconds = float(ttl_seconds)
        self._monotonic = monotonic
        self._token = secrets.token_hex(32).encode("ascii")
        self._attempted: list[Lock] = []
        self._held: list[Lock] = []
        self._started = False
        self._stopped = False

    @property
    def canonical_keys(self) -> tuple[str, ...]:
        return self._keys

    def _fail(self, reason: str) -> None:
        self._stopped = True
        raise CasdoorLeaseError(reason) from None

    def _remaining(self) -> float:
        if self._stopped:
            self._fail("stopped")
        remaining = self._deadline - self._monotonic()
        if remaining < 0.001:
            self._fail("deadline")
        return remaining

    def _ttl_ms(self) -> int:
        return math.floor(min(self._ttl_seconds, self._remaining()) * 1000)

    def _check_held(self, *, renew: bool) -> None:
        for lock in self._held:
            ttl_ms = self._ttl_ms() if renew else 0
            self._remaining()
            try:
                # lock.name is already serialized by RedisClientWrapper.lock;
                # raw wrapper.eval would bypass its key prefix handling.
                ttl = lock.redis.eval(_CHECK_OR_RENEW, 1, lock.name, self._token, ttl_ms)
            except Exception:
                self._fail("redis_unavailable")
            self._remaining()
            if type(ttl) is not int or ttl <= 0:
                self._fail("ownership_lost")

    def acquire(self) -> None:
        """Try each canonical key once, nonblocking; failed partial sets clean up."""
        if self._started:
            self._fail("already_started")
        self._started = True
        try:
            for key in self._keys:
                self._remaining()
                self._check_held(renew=False)
                ttl_ms = self._ttl_ms()
                lock = self._client.lock(key, timeout=ttl_ms / 1000, blocking=False, thread_local=False)
                self._remaining()
                # Track before SET so timeout-after-write can still compare-delete
                # our owner token, even if redis-py never recorded its local token.
                self._attempted.append(lock)
                acquired = lock.acquire(blocking=False, token=self._token)
                self._remaining()
                if not acquired:
                    self._fail("busy")
                self._held.append(lock)
            self.ensure_owned()
        except CasdoorLeaseError:
            self.release()
            raise
        except Exception:
            self.release()
            self._fail("redis_unavailable")

    def ensure_owned(self, *, renew: bool = False) -> None:
        """Fail closed before/after I/O and immediately before caller DB commit."""
        self._remaining()
        if not self._started or len(self._held) != len(self._keys):
            self._fail("not_acquired")
        self._check_held(renew=renew)
        self._remaining()

    def release(self) -> bool:
        """Reverse compare-delete; never delete a winner or extend any deadline."""
        self._stopped = True
        failed: list[Lock] = []
        for lock in reversed(self._attempted):
            try:
                cleanup_scope = getattr(self._client, "_casdoor_cleanup_scope", nullcontext)
                with cleanup_scope():
                    lock.do_release(self._token)
            except LockNotOwnedError:
                pass
            except Exception:
                failed.append(lock)
        self._attempted = list(reversed(failed))
        self._held = []
        return not failed

    @classmethod
    def for_scopes(cls, redis_client, scopes, *, deadline, ttl_seconds=15.0):
        """Fix the complete bounded union before acquisition; no late expansion."""
        if type(scopes) is not tuple or not 1 <= len(scopes) <= 100:
            raise CasdoorLeaseError("invalid_scope")
        if any(type(scope) is not CasdoorLeaseScope for scope in scopes):
            raise CasdoorLeaseError("invalid_scope")
        owner = cls(redis_client, scopes[0], deadline=deadline, ttl_seconds=ttl_seconds)
        owner._keys = tuple(sorted({key for scope in scopes for key in scope.canonical_keys}))
        return owner
