"""Bounded shared key caching, source fencing and atomic rotation with fake time."""

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from core.casdoor.crypto import CryptoError
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import GatewayError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from services.casdoor_signing_key_service_extend import (
    MAX_AGE_SECONDS,
    RedisSigningKeyProvider,
)

NOW = datetime(2026, 10, 6, tzinfo=UTC)
MESSAGE = b"synthetic.token"
SOURCE = "https://casdoor.invalid/.well-known/jwks"


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Redis:
    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.lock = threading.RLock()
        self.fail = False

    def get(self, key):
        with self.lock:
            if self.fail:
                raise OSError("synthetic unavailable")
            value = self.values.get(key)
            if value is not None and value[1] <= self.clock():
                self.values.pop(key)
                return None
            return value[0] if value else None

    def set(self, key, value, *, nx=False, ex):
        with self.lock:
            if nx and self.get(key) is not None:
                return False
            if self.fail:
                raise OSError("synthetic unavailable")
            self.values[key] = (value, self.clock() + ex)
            return True

    def delete(self, key):
        with self.lock:
            if self.fail:
                raise OSError("synthetic unavailable")
            return int(self.values.pop(key, None) is not None)

    def eval(self, script, count, key, token):
        with self.lock:
            if self.get(key) == token:
                return self.delete(key)
            return 0


class Remote:
    def __init__(self, raw):
        self.raw = raw
        self.source = SOURCE
        self.profile = "global"
        self.calls = []
        self.error = None
        self.block = None
        self.started = threading.Event()

    def fetch(self, profile):
        self.calls.append(profile)
        self.started.set()
        if self.block:
            assert self.block.wait(2)
        if self.error:
            raise self.error
        return self.source, self.raw, self.profile


class Operation:
    def __init__(self, remote, config):
        self.remote = remote
        self.config = config
        self.deadline = time.monotonic() + 3

    def remaining_seconds(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError(CasdoorErrorCode.PROVIDER_UNAVAILABLE, "deadline")
        return remaining

    def discover_signing_keys(self, profile=None):
        return self.remote.fetch(profile)


def jwk(key, kid):
    numbers = key.public_key().public_numbers()

    def encode(number):
        raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"kty": "RSA", "n": encode(numbers.n), "e": encode(numbers.e), "kid": kid}


@pytest.fixture(scope="module")
def keys():
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))


@pytest.fixture
def setup(keys):
    clock = Clock()
    redis = Redis(clock)
    remote = Remote({"keys": [jwk(keys[0], "old")]})
    config = SimpleNamespace(
        backend_api_url="https://casdoor.invalid",
        expected_issuer="https://casdoor.invalid",
        application="app",
        client_id="client",
        config_digest=lambda: "d" * 64,
    )
    namespace, revision = uuid4(), uuid4()

    def provider(**kwargs):
        return RedisSigningKeyProvider(
            Operation(remote, kwargs.pop("config", config)),
            kwargs.pop("namespace_id", namespace),
            kwargs.pop("revision_id", revision),
            redis_client=redis,
            clock=clock,
            **kwargs,
        )

    return SimpleNamespace(
        clock=clock,
        redis=redis,
        remote=remote,
        provider=provider,
        config=config,
        namespace=namespace,
        revision=revision,
    )


def sign(key):
    return key.sign(MESSAGE, padding.PKCS1v15(), hashes.SHA256())


def verify(provider, key, kid):
    return provider.verify_rs256(MESSAGE, sign(key), kid=kid, now=NOW)


def test_cold_fresh_normal_refresh_and_metadata(setup):
    s = setup
    first = s.provider().snapshot()
    assert (first.namespace_id, first.revision_id, first.config_digest) == (
        s.namespace,
        s.revision,
        s.config.config_digest(),
    )
    assert (first.source_url, first.profile, first.fetched_at) == (SOURCE, "global", s.clock())
    assert s.provider().snapshot().fingerprints == first.fingerprints
    assert len(s.remote.calls) == 1
    s.clock.advance(300)
    assert s.provider().snapshot().fetched_at == s.clock()
    assert len(s.remote.calls) == 2


@pytest.mark.parametrize("reason", ["signing_keys_transport", "signing_keys_temporary"])
def test_transient_failure_uses_bounded_stale_snapshot_then_expiry_refuses(setup, keys, reason):
    s = setup
    first = s.provider().snapshot()
    s.clock.advance(301)
    s.remote.error = GatewayError(CasdoorErrorCode.PROVIDER_UNAVAILABLE, reason)
    stale = s.provider()
    assert stale.snapshot().fetched_at == first.fetched_at
    assert verify(stale, keys[0], "old")
    s.clock.advance(MAX_AGE_SECONDS - 301)
    with pytest.raises(GatewayError):
        stale.snapshot()
    with pytest.raises(GatewayError):
        s.provider().snapshot()


@pytest.mark.parametrize(
    "reason", ["issuer_mismatch", "json_invalid", "response_bounds", "deadline", "termination_unconfirmed"]
)
def test_nontransient_gateway_failure_removes_old_cache_and_never_falls_back(setup, reason):
    s = setup
    s.provider().snapshot()
    s.clock.advance(301)
    s.remote.error = GatewayError(CasdoorErrorCode.PROVIDER_UNAVAILABLE, reason)
    with pytest.raises(GatewayError, match=reason):
        s.provider().snapshot()
    with pytest.raises(GatewayError, match="refresh_cooldown"):
        s.provider().snapshot()
    assert len(s.remote.calls) == 2


def test_successful_invalid_jwks_cannot_hide_behind_previously_valid_keys(setup, keys):
    s = setup
    provider = s.provider()
    provider.snapshot()
    s.clock.advance(31)
    s.remote.raw = {"keys": []}
    with pytest.raises(GatewayError, match="signing_keys_invalid"):
        provider.refresh_once()
    with pytest.raises(GatewayError, match="refresh_cooldown"):
        verify(provider, keys[0], "old")
    assert s.redis.get(provider._cache_key) is None


def test_cache_corruption_rebuild_is_bounded_by_shared_cooldown(setup):
    s = setup
    provider = s.provider()
    provider.snapshot()
    s.redis.set(provider._cache_key, b"corrupted", ex=600)
    with pytest.raises(GatewayError, match="refresh_cooldown"):
        s.provider().snapshot()
    assert len(s.remote.calls) == 1
    s.clock.advance(31)
    assert s.provider().snapshot()
    assert len(s.remote.calls) == 2


def test_source_namespace_revision_and_config_isolation(setup):
    s = setup
    s.provider().snapshot()
    s.provider(namespace_id=uuid4()).snapshot()
    s.provider(revision_id=uuid4()).snapshot()
    new_config = SimpleNamespace(**vars(s.config))
    new_config.application = "other"
    s.provider(config=new_config).snapshot()
    s.provider(source_url=SOURCE, profile="global").snapshot()
    assert len(s.remote.calls) == 5


def test_persisted_source_profile_fencing_and_no_implicit_source_change(setup):
    s = setup
    provider = s.provider(source_url=SOURCE, profile="global")
    provider.snapshot()
    assert s.remote.calls == ["global"]
    s.clock.advance(31)
    s.remote.source = "https://casdoor.invalid/.well-known/app/jwks"
    with pytest.raises(GatewayError, match="source_changed"):
        provider.refresh_once()
    assert s.redis.get(provider._cache_key) is None


def test_rotation_replaces_snapshot_and_callback_remains_fixed_until_single_retry(setup, keys):
    s = setup
    provider = s.provider()
    old = provider.snapshot()
    s.remote.raw = {"keys": [jwk(keys[0], "old"), jwk(keys[1], "new")]}
    s.clock.advance(31)
    with pytest.raises(CryptoError):
        verify(provider, keys[1], "new")
    refreshed = provider.refresh_once()
    assert len(refreshed.fingerprints) == 2
    assert refreshed.fingerprints != old.fingerprints
    assert verify(provider, keys[1], "new")
    with pytest.raises(CryptoError):
        provider.refresh_once()
    s.clock.advance(31)
    s.remote.raw = {"keys": [jwk(keys[1], "new")]}
    current = s.provider()
    current.snapshot(force_refresh=True)
    with pytest.raises(CryptoError):
        verify(current, keys[0], "old")
    assert verify(current, keys[1], "new")
    # Fixed old callback state never observes another callback's generation.
    assert verify(provider, keys[0], "old")


def test_same_kid_key_replacement_and_unknown_kid_cooldown(setup, keys):
    s = setup
    provider = s.provider()
    provider.snapshot()
    s.remote.raw = {"keys": [jwk(keys[1], "old")]}
    s.clock.advance(31)
    provider.refresh_once()
    assert verify(provider, keys[1], "old")
    with pytest.raises(CryptoError):
        verify(provider, keys[0], "old")
    for _ in range(10):
        attack = s.provider()
        with pytest.raises(CryptoError):
            verify(attack, keys[1], "unknown")
        attack.refresh_once()
        with pytest.raises(CryptoError):
            verify(attack, keys[1], "unknown")
    assert len(s.remote.calls) == 2


def test_parallel_refresh_singleflight_remote_call_count(setup, keys):
    s = setup
    s.provider().snapshot()
    s.clock.advance(31)
    s.remote.raw = {"keys": [jwk(keys[1], "new")]}
    s.remote.block = threading.Event()
    s.remote.started.clear()
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(s.provider().snapshot, force_refresh=True)
        assert s.remote.started.wait(1)
        others = [pool.submit(s.provider().snapshot, force_refresh=True) for _ in range(7)]
        s.remote.block.set()
        results = [first.result()] + [job.result() for job in others]
    assert len(s.remote.calls) == 2
    assert len({result.fingerprints for result in results}) == 1


def test_redis_failure_fails_closed_before_any_remote_fetch(setup):
    s = setup
    s.redis.fail = True
    with pytest.raises(GatewayError, match="cache_unavailable"):
        s.provider().snapshot()
    assert not s.remote.calls


def test_lock_wait_charged_to_operation_deadline(setup):
    s = setup
    provider = s.provider()
    provider._operation.deadline = time.monotonic() + 0.02
    s.redis.set(provider._lock_key, "other-worker", ex=45)
    with pytest.raises(GatewayError, match="deadline"):
        provider.snapshot()
    assert not s.remote.calls


def test_future_cache_timestamp_and_false_metadata_not_usable(setup):
    s = setup
    provider = s.provider()
    provider.snapshot()
    value = json.loads(s.redis.get(provider._cache_key))
    value["fetched_at"] = s.clock() + 1
    s.redis.set(provider._cache_key, json.dumps(value), ex=600)
    with pytest.raises(GatewayError, match="refresh_cooldown"):
        s.provider().snapshot()


def test_cold_start_concurrent_callers_share_one_remote_fetch(setup):
    s = setup
    s.remote.block = threading.Event()
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(s.provider().snapshot)
        assert s.remote.started.wait(1)
        rest = [pool.submit(s.provider().snapshot) for _ in range(7)]
        s.remote.block.set()
        results = [first.result()] + [job.result() for job in rest]
    assert len(s.remote.calls) == 1
    assert len({item.fingerprints for item in results}) == 1


def test_transient_failure_cooldown_blocks_repeated_remote_refresh(setup):
    s = setup
    s.provider().snapshot()
    s.clock.advance(301)
    s.remote.error = GatewayError(CasdoorErrorCode.PROVIDER_UNAVAILABLE, "signing_keys_transport")
    for _ in range(10):
        assert s.provider().snapshot().fetched_at == 1000
    assert len(s.remote.calls) == 2


def test_successful_remote_fetch_with_failed_redis_write_does_not_yield_uncached_keys(setup):
    s = setup
    original_fetch = s.remote.fetch

    def fetched_but_cache_failed(profile):
        result = original_fetch(profile)
        s.redis.fail = True
        return result

    s.remote.fetch = fetched_but_cache_failed
    with pytest.raises(GatewayError, match="cache_unavailable"):
        s.provider().snapshot()
    with pytest.raises(GatewayError, match="cache_unavailable"):
        s.provider().snapshot()
    assert len(s.remote.calls) == 1
