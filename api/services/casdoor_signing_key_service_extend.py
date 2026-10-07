"""Redis-bounded Casdoor signing-key discovery and stable callback snapshots.

Only the gateway admits remote URLs. Cache/cooldown/locks are shared by the
immutable provider identity and selected source, and no Redis failure creates a
per-request HTTP fallback. Claims and complete bundle retries remain caller-owned.
"""

import hashlib
import json
import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from core.casdoor.crypto import CryptoError
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import GatewayError, GatewayOperation
from core.casdoor.signing_keys import (
    MAX_JWKS_BYTES,
    SigningKeySnapshot,
    SigningKeyTrustStore,
    TrustedSigningKey,
)

FRESH_SECONDS = 5 * 60
MAX_AGE_SECONDS = 10 * 60
REFRESH_COOLDOWN_SECONDS = 30
_LOCK_SECONDS = 45
_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_TRANSIENT_REASONS = frozenset({"signing_keys_transport", "signing_keys_temporary"})


class RedisSigningKeyProvider:
    """One callback provider with at most one explicitly requested retry refresh.

    A call to verify_rs256 always uses the fixed snapshot. To avoid mixing key
    generations, the caller catches signature failure outside the entire token
    bundle, calls refresh_once, and restarts its complete claims validation once.
    """

    def __init__(
        self,
        operation: GatewayOperation,
        namespace_id: UUID,
        revision_id: UUID,
        *,
        source_url: str | None = None,
        profile: str | None = None,
        redis_client: Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if redis_client is None:
            from extensions.ext_redis import redis_client as default_redis_client

            redis_client = default_redis_client
        if (source_url is None) != (profile is None) or (
            profile is not None and profile not in ("application", "global")
        ):
            raise GatewayError(CasdoorErrorCode.CONFIG_CONFLICT, "signing_keys_source_invalid")
        if source_url is not None and (type(source_url) is not str or not source_url or len(source_url) > 2048):
            raise GatewayError(CasdoorErrorCode.CONFIG_CONFLICT, "signing_keys_source_invalid")
        self._operation = operation
        self._namespace_id = UUID(str(namespace_id))
        self._revision_id = UUID(str(revision_id))
        self._config_digest = operation.config.config_digest()
        self._source_url = source_url
        self._profile = profile
        self._redis = redis_client
        self._clock = clock
        self._sleep = sleep
        self._snapshot: SigningKeySnapshot | None = None
        self._retry_refreshed = False
        config = operation.config
        binding = json.dumps(
            {
                "namespace": str(self._namespace_id),
                "revision": str(self._revision_id),
                "backend": config.backend_api_url,
                "issuer": config.expected_issuer,
                "application": config.application,
                "client_id": config.client_id,
                "config_digest": self._config_digest,
                "source": source_url,
                "profile": profile,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._cache_key = "casdoor:signing-keys:v1:" + hashlib.sha256(binding.encode()).hexdigest()
        self._lock_key = self._cache_key + ":lock"
        self._cooldown_key = self._cache_key + ":cooldown"

    def _unavailable(self, reason: str = "signing_keys_cache_unavailable") -> Any:
        raise GatewayError(CasdoorErrorCode.PROVIDER_UNAVAILABLE, reason) from None

    def _redis_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._redis, method)(*args, **kwargs)
        except Exception:
            return self._unavailable()

    def _check_deadline(self) -> float:
        return self._operation.remaining_seconds()

    def _valid_age(self, snapshot: SigningKeySnapshot) -> bool:
        return 0 <= self._clock() - snapshot.fetched_at < MAX_AGE_SECONDS

    def _read(self) -> SigningKeySnapshot | None:
        raw = self._redis_call("get", self._cache_key)
        if raw is None:
            return None
        try:
            if type(raw) not in (str, bytes) or len(raw) > MAX_JWKS_BYTES + 8192:
                raise ValueError
            value = json.loads(raw)
            if type(value) is not dict or set(value) != {"fetched_at", "source_url", "profile", "jwks"}:
                raise ValueError
            fetched_at = value["fetched_at"]
            source, profile = value["source_url"], value["profile"]
            if (
                type(fetched_at) not in (int, float)
                or not math.isfinite(fetched_at)
                or type(source) is not str
                or not source
                or len(source) > 2048
                or profile not in ("application", "global")
                or (self._source_url is not None and (source != self._source_url or profile != self._profile))
            ):
                raise ValueError
            result = self._make_snapshot(value["jwks"], source, profile, float(fetched_at))
            if not self._valid_age(result):
                raise ValueError
            return result
        except (ValueError, TypeError, UnicodeError, RecursionError):
            # A malformed runtime cache is never a signing source; rebuild only
            # through the same shared lock and cooldown as a cold start.
            self._redis_call("delete", self._cache_key)
            return None

    def _make_snapshot(self, raw: dict[str, Any], source: str, profile: str, fetched_at: float) -> SigningKeySnapshot:
        trust_store = SigningKeyTrustStore.from_jwks(raw)
        return SigningKeySnapshot(
            trust_store=trust_store,
            fingerprints=trust_store.fingerprints,
            source_url=source,
            profile=profile,
            fetched_at=fetched_at,
            namespace_id=self._namespace_id,
            revision_id=self._revision_id,
            config_digest=self._config_digest,
        )

    def _refresh(self, cached: SigningKeySnapshot | None) -> SigningKeySnapshot:
        lock_token = uuid4().hex
        while True:
            self._check_deadline()
            if self._redis_call("set", self._lock_key, lock_token, nx=True, ex=_LOCK_SECONDS):
                break
            current = self._read()
            if current is not None and (cached is None or current.fetched_at > cached.fetched_at):
                return current
            self._sleep(min(0.05, self._check_deadline()))
        try:
            # Re-read after lock admission so racing callers use the completed
            # refresh rather than launching a second provider request.
            current = self._read()
            if current is not None and (cached is None or current.fetched_at > cached.fetched_at):
                return current
            if not self._redis_call("set", self._cooldown_key, "1", nx=True, ex=REFRESH_COOLDOWN_SECONDS):
                if current is not None:
                    return current
                return self._unavailable("signing_keys_refresh_cooldown")
            try:
                source, raw, profile = self._operation.discover_signing_keys(profile=self._profile)
                if self._source_url is not None and (source != self._source_url or profile != self._profile):
                    raise GatewayError(CasdoorErrorCode.CONFIG_CONFLICT, "signing_keys_source_changed")
                fresh = self._make_snapshot(raw, source, profile, self._clock())
            except GatewayError as error:
                if error.reason in _TRANSIENT_REASONS and error.termination_confirmed:
                    if current is not None and self._valid_age(current):
                        return current
                    raise
                # A successfully reached incompatible source may not be hidden
                # by previously valid keys, including in other workers.
                self._redis_call("delete", self._cache_key)
                raise
            except CryptoError:
                self._redis_call("delete", self._cache_key)
                return self._unavailable("signing_keys_invalid")
            self._check_deadline()
            encoded = json.dumps(
                {"fetched_at": fresh.fetched_at, "source_url": source, "profile": profile, "jwks": raw},
                separators=(",", ":"),
                allow_nan=False,
            )
            self._redis_call("set", self._cache_key, encoded, ex=MAX_AGE_SECONDS)
            return fresh
        finally:
            self._redis_call("eval", _RELEASE_LOCK, 1, self._lock_key, lock_token)

    def snapshot(self, *, force_refresh: bool = False) -> SigningKeySnapshot:
        self._check_deadline()
        if self._snapshot is not None and not force_refresh:
            if not self._valid_age(self._snapshot):
                return self._unavailable("signing_keys_expired")
            return self._snapshot
        cached = self._read()
        if not force_refresh and cached is not None and self._clock() - cached.fetched_at < FRESH_SECONDS:
            self._snapshot = cached
        else:
            self._snapshot = None
            self._snapshot = self._refresh(cached)
        self._check_deadline()
        return self._snapshot

    def refresh_once(self) -> SigningKeySnapshot:
        if self._retry_refreshed:
            raise CryptoError("casdoor_signature_invalid")
        self._retry_refreshed = True
        return self.snapshot(force_refresh=True)

    def verify_rs256(
        self,
        signing_input: bytes,
        signature: bytes,
        *,
        kid: str | None,
        now: datetime,
        algorithm: str = "RS256",
    ) -> TrustedSigningKey:
        return self.snapshot().trust_store.verify_rs256(signing_input, signature, kid=kid, now=now, algorithm=algorithm)
