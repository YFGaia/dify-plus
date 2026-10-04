"""Actual result store with a bottom Redis wire model, never live Redis/Lua proof."""

import copy
import hashlib
import json
import math
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from core.casdoor import auth_transactions as auth
from core.casdoor.errors import CasdoorErrorCode
from extensions.ext_redis import RedisClientWrapper
from extensions.redis_names import serialize_redis_name
from test_auth_transactions import CALLBACK, NAMESPACE, NOW, POLICY, REVISION, FakeRedis

CORRELATION = "30000000-0000-4000-8000-00000000000a"
UNSET = object()
ENVELOPE_FIELDS = {
    "schema_version",
    "owner",
    "scope",
    "context",
    "issued_at",
    "expires_at",
    "data",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ResultRedis(FakeRedis):
    """Only new scripts are modeled here; original auth wire stays inherited."""

    def __init__(self):
        super().__init__()
        self.reply = UNSET
        self.before_fault = False

    def eval(self, script, numkeys, *args):
        if script not in (
            auth.CREATE_RESTRICTED_RESULT_SCRIPT,
            auth.CONSUME_RESTRICTED_RESULT_SCRIPT,
        ):
            return super().eval(script, numkeys, *args)
        assert numkeys == 1
        key, *argv = args
        with self.lock:
            self.calls.append((script, numkeys, args))
            if self.before_fault:
                raise ConnectionError("synthetic private before-IO fault")
            if script == auth.CREATE_RESTRICTED_RESULT_SCRIPT:
                if key in self.records and self.records[key][1] > self.now:
                    result = 0
                else:
                    self.records[key] = (argv[0], self.now + 300)
                    result = 1
            else:
                result = None
                raw, expiry = self.records.get(key, (None, 0))
                if isinstance(raw, str | bytes) and raw and expiry - self.now >= 1:
                    size = len(raw.encode() if isinstance(raw, str) else raw)
                    try:
                        record = json.loads(raw) if size <= 16384 else None
                    except (ValueError, UnicodeError):
                        record = None
                    if type(record) is dict and set(record) == ENVELOPE_FIELDS:
                        issued, expires = record["issued_at"], record["expires_at"]
                        # Lua numbers permit integral floats; Python must reject them
                        # after modeled DEL. cjson also collapses duplicate members.
                        times = all(
                            type(value) in (int, float) and math.isfinite(value) and value == math.floor(value)
                            for value in (issued, expires)
                        )
                        if (
                            type(record["schema_version"]) in (int, float)
                            and record["schema_version"] == 1
                            and all(type(record[field]) is str for field in ("owner", "scope", "context"))
                            and [record[field] for field in ("owner", "scope", "context")] == argv[:3]
                            and times
                            and 0 <= issued < expires <= 253402300799
                            and expires == issued + 300
                            and issued <= int(argv[3]) < expires
                            and type(record["data"]) is str
                            and 0 < len(record["data"].encode()) <= 16384
                        ):
                            del self.records[key]
                            result = raw
        if self.after_eval:
            self.after_eval()
        if self.lose_reply:
            raise TimeoutError("synthetic private lost-reply fault")
        return result if self.reply is UNSET else self.reply


@pytest.fixture
def env():
    raw = ResultRedis()
    wrapper = RedisClientWrapper()
    wrapper.initialize(raw)
    wrapper._get_prefix = lambda: "  result-prefix  "
    crypto = Mock()
    crypto.encrypt.side_effect = crypto.decrypt.side_effect = AssertionError("result called crypto")
    clock = [NOW]
    return SimpleNamespace(
        raw=raw,
        wrapper=wrapper,
        crypto=crypto,
        clock=clock,
        store=auth.AuthTransactionStore(wrapper, crypto, clock=lambda: clock[0]),
        binding=auth.RestrictedResultBinding(
            NAMESPACE,
            REVISION,
            7,
            "a" * 64,
            POLICY.backend_origin,
            "https://web.example.test",
        ),
        payload=auth.RestrictedResultPayload(CasdoorErrorCode.AUTHORIZATION_PENDING, CORRELATION),
        current=[auth.CurrentAuthContext(NAMESPACE, REVISION, auth.AuthMode.LOGIN, CALLBACK, None, allowed=True)],
        scope=secrets.token_urlsafe(32),
    )


def create(env, **changes):
    return env.store.create_restricted_result(
        browser_scope=changes.get("scope", env.scope),
        context_binding=changes.get("binding", env.binding),
        payload=changes.get("payload", env.payload),
        policy=changes.get("policy", POLICY),
        guard=changes.get("guard", lambda: env.current[0]),
    )


def consume(env, created, **changes):
    return env.store.consume_restricted_result(
        changes.get("handoff", created.handoff),
        browser_scope=changes.get("scope", env.scope),
        result_cookie=changes.get("nonce", created.result_cookie.value),
        context_binding=changes.get("binding", env.binding),
        policy=changes.get("policy", POLICY),
        guard=changes.get("guard", lambda: env.current[0]),
    )


def record(env):
    key = env.raw.calls[-1][2][0]
    raw, expiry = env.raw.records[key]
    return key, json.loads(raw), expiry


@pytest.mark.parametrize(
    "code",
    [
        CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN,
        CasdoorErrorCode.WORKSPACE_UNAVAILABLE,
        CasdoorErrorCode.AUTHORIZATION_PENDING,
    ],
)
def test_roundtrip_closed_record_and_only_result_clear(env, code):
    env.payload = replace(env.payload, code=code)
    created = create(env)
    key, envelope, expiry = record(env)
    assert envelope == {
        "schema_version": 1,
        "owner": digest(created.result_cookie.value),
        "scope": digest(env.scope),
        "context": digest(canonical(env.binding.projection())),
        "issued_at": int(NOW.timestamp()),
        "expires_at": int(NOW.timestamp()) + 300,
        "data": canonical({"code": code.value, "correlation_id": CORRELATION, "retry_allowed": False}),
    }
    wire = env.raw.records[key][0]
    assert wire == canonical(envelope) and wire.isascii() and len(wire.encode()) <= 16384
    for value in (created.handoff, created.result_cookie.value, env.scope):
        assert value not in wire + key + repr(created) + repr(created.result_cookie)
    assert expiry == env.raw.now + 300 and not env.raw.slots
    assert created.result_cookie == auth.CookieDirective(
        auth.result_cookie_name(created.handoff),
        created.result_cookie.value,
        300,
        auth.COOKIE_PATH + "/result",
        True,
    )
    assert created.scope_cookie == auth.CookieDirective(auth.SCOPE_COOKIE_NAME, env.scope, 300, auth.COOKIE_PATH, True)
    consumed = consume(env, created)
    assert consumed.payload == env.payload
    assert consumed.payload.public_projection() == json.loads(envelope["data"])
    assert consumed.clear_cookie == replace(created.result_cookie, value="", max_age=0)
    assert key not in env.raw.records and not env.raw.slots
    env.crypto.encrypt.assert_not_called()
    env.crypto.decrypt.assert_not_called()
    with pytest.raises(auth.AuthTransactionError):
        consume(env, created)


@pytest.mark.parametrize("prefix", ["", "  result-prefix  ", "deployment:worker"])
def test_original_wrapper_prefix_once_single_cluster_key(env, prefix, monkeypatch):
    env.wrapper._get_prefix = lambda: prefix
    spy = Mock(wraps=serialize_redis_name)
    monkeypatch.setattr(env.store, "_key_serializer", spy)
    created = create(env)
    logical = f"casdoor:restricted-result:{{{digest(env.scope)}}}:{digest(created.handoff)}"
    key = serialize_redis_name(logical, prefix)
    spy.assert_called_once_with(logical, prefix)
    assert env.raw.calls[-1][2][0] == key
    consume(env, created)
    assert spy.call_count == 2 and all(call[1] == 1 and call[2][0] == key for call in env.raw.calls)


def test_nx_collision_never_overwrites_refreshes_or_retries(env, monkeypatch):
    created = create(env)
    before = copy.deepcopy(env.raw.records)
    env.raw.now += 10
    random = Mock(return_value=created.handoff)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", random)
    with pytest.raises(auth.AuthTransactionError, match="collision"):
        create(env)
    assert env.raw.records == before and len(env.raw.calls) == 2
    assert random.call_args_list == [((32,),), ((32,),)]
    assert consume(env, created).payload == env.payload


@pytest.mark.parametrize("changed", ["handoff", "scope", "nonce", "fence", "digest", "web"])
def test_mismatch_preserves_rightful_record(env, changed):
    created = create(env)
    before = copy.deepcopy(env.raw.records)
    changes = {changed: secrets.token_urlsafe(32)}
    if changed in ("fence", "digest", "web"):
        field, value = {
            "fence": ("fence_epoch", 8),
            "digest": ("config_digest", "b" * 64),
            "web": ("web_origin", "https://other.test"),
        }[changed]
        changes = {"binding": replace(env.binding, **{field: value})}
    with pytest.raises(auth.AuthTransactionError):
        consume(env, created, **changes)
    assert env.raw.records == before
    assert consume(env, created).payload == env.payload


@pytest.mark.parametrize("elapsed", [299.5, 300, 301])
def test_zero_or_expired_redis_ttl_refuses(env, elapsed):
    created = create(env)
    env.raw.now += elapsed
    with pytest.raises(auth.AuthTransactionError):
        consume(env, created)
    assert not env.raw.slots


def test_last_valid_second_then_concurrent_single_consumption(env):
    created = create(env)
    env.raw.now += 299
    env.clock[0] += timedelta(seconds=299)

    def use(_):
        try:
            return consume(env, created).payload
        except auth.AuthTransactionError:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(use, range(6))).count(env.payload) == 1
    assert not env.raw.records and not env.raw.slots


@pytest.mark.parametrize("operation", ["create", "consume"])
@pytest.mark.parametrize("mutation", ["disabled", "namespace", "revision", "callback", "mode"])
def test_pre_and_post_guards_check_original_projection(env, operation, mutation):
    created = create(env) if operation == "consume" else None
    original = env.current[0]
    changes = {
        "disabled": {"allowed": False},
        "namespace": {"namespace_id": REVISION},
        "revision": {"revision_id": NAMESPACE},
        "callback": {"registered_redirect_uri": CALLBACK + "/x"},
        "mode": {"mode": "login"},
    }[mutation]
    changed = replace(original, **changes)
    action = (lambda: consume(env, created)) if created else (lambda: create(env))
    env.current[0] = changed
    calls = len(env.raw.calls)
    with pytest.raises(auth.AuthTransactionError):
        action()
    assert len(env.raw.calls) == calls
    env.current[0] = original
    env.raw.after_eval = lambda: env.current.__setitem__(0, changed)
    with pytest.raises(auth.AuthTransactionError):
        action()
    assert len(env.raw.calls) == calls + 1
    assert bool(env.raw.records) is (operation == "create")


@pytest.mark.parametrize("operation", ["create", "consume"])
def test_rich_service_guard_closure_failure_remains_orphan_or_spent(env, operation):
    created = create(env) if operation == "consume" else None
    fresh = [env.binding.fence_epoch]

    def guard():
        if fresh[0] != env.binding.fence_epoch:
            raise auth.AuthTransactionError("context_changed")
        return env.current[0]

    env.raw.after_eval = lambda: fresh.__setitem__(0, 8)
    with pytest.raises(auth.AuthTransactionError, match="context_changed"):
        consume(env, created, guard=guard) if created else create(env, guard=guard)
    assert bool(env.raw.records) is (operation == "create")


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("namespace_id", str(NAMESPACE)),
        ("revision_id", False),
        ("fence_epoch", True),
        ("fence_epoch", -1),
        ("fence_epoch", 1.0),
        ("config_digest", "A" * 64),
        ("config_digest", "a" * 63),
        ("backend_origin", "https://host.test/private"),
        ("web_origin", "https://user:pass@web.test"),
        ("web_origin", "http://web.test"),
        ("mode", "login"),
        ("mode", auth.AuthMode.LINK),
    ],
)
def test_binding_constructor_and_mutated_frozen_input_rejected_before_io(env, field, bad):
    with pytest.raises(auth.AuthTransactionError):
        replace(env.binding, **{field: bad})
    created = create(env)
    forged = copy.copy(env.binding)
    object.__setattr__(forged, field, bad)
    calls = len(env.raw.calls)
    for action in (
        lambda: create(env, binding=forged),
        lambda: consume(env, created, binding=forged),
    ):
        with pytest.raises(auth.AuthTransactionError):
            action()
        assert len(env.raw.calls) == calls
    assert consume(env, created).payload == env.payload


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("code", CasdoorErrorCode.PROVIDER_UNAVAILABLE),
        ("code", "authorization_pending"),
        ("code", False),
        ("correlation_id", UUID(CORRELATION)),
        ("correlation_id", CORRELATION.upper()),
        ("correlation_id", CORRELATION.replace("-", "")),
        ("correlation_id", "sensitive-not-a-uuid"),
        ("retry_allowed", True),
        ("retry_allowed", 0),
        ("retry_allowed", None),
    ],
)
def test_payload_constructor_and_mutated_frozen_input_rejected_before_io(env, field, bad):
    with pytest.raises(auth.AuthTransactionError):
        replace(env.payload, **{field: bad})
    forged = copy.copy(env.payload)
    object.__setattr__(forged, field, bad)
    with pytest.raises(auth.AuthTransactionError):
        create(env, payload=forged)
    assert not env.raw.calls


@pytest.mark.parametrize("field", ["scope", "handoff", "nonce"])
@pytest.mark.parametrize("bad", [None, False, "", "A" * 42 + "B"])
def test_opaque_inputs_are_canonical_before_io(env, field, bad):
    created = create(env)
    calls = len(env.raw.calls)
    with pytest.raises(auth.AuthTransactionError):
        consume(env, created, **{field: bad})
    if field == "scope":
        with pytest.raises(auth.AuthTransactionError):
            create(env, scope=bad)
    if field == "handoff":
        with pytest.raises(auth.AuthTransactionError):
            auth.result_cookie_name(bad)
    assert len(env.raw.calls) == calls


def test_closed_records_are_immutable_and_do_not_render_payload(env):
    created = create(env)
    consumed = consume(env, created)
    for value in (env.binding, env.payload, created, consumed):
        assert not hasattr(value, "__dict__")
        assert CORRELATION not in repr(value) and created.handoff not in repr(value)
        with pytest.raises((FrozenInstanceError, TypeError)):
            value.extra = "private"
    with pytest.raises(TypeError):
        auth.RestrictedResultPayload(CasdoorErrorCode.AUTHORIZATION_PENDING, CORRELATION, message="private")


@pytest.mark.parametrize("bad", [None, False, {}, POLICY.backend_origin])
def test_wrong_policy_type_rejected_before_io(env, bad):
    with pytest.raises(auth.AuthTransactionError):
        create(env, policy=bad)
    assert not env.raw.calls


def test_policy_origin_and_revalidation_before_io(env):
    created = create(env)
    bad_policy = replace(POLICY)
    object.__setattr__(bad_policy, "allow_loopback_http", 1)
    for policy in (auth.CookiePolicy("https://other.test"), bad_policy):
        calls = len(env.raw.calls)
        for action in (
            lambda policy=policy: create(env, policy=policy),
            lambda policy=policy: consume(env, created, policy=policy),
        ):
            with pytest.raises(auth.AuthTransactionError):
                action()
        assert len(env.raw.calls) == calls


def test_development_policy_applies_to_both_origins_and_cookie_security(env):
    binding = replace(
        env.binding,
        backend_origin="http://127.0.0.1:5001",
        web_origin="http://localhost:3000",
    )
    policy = auth.CookiePolicy(binding.backend_origin, allow_loopback_http=True)
    env.current[0] = replace(
        env.current[0],
        registered_redirect_uri=binding.backend_origin + auth.COOKIE_PATH + "/callback",
    )
    created = create(env, binding=binding, policy=policy)
    assert not created.result_cookie.secure and not created.scope_cookie.secure
    assert not consume(env, created, binding=binding, policy=policy).clear_cookie.secure
    calls = len(env.raw.calls)
    with pytest.raises(auth.AuthTransactionError):
        create(env, binding=replace(env.binding, web_origin="http://localhost:3000"))
    assert len(env.raw.calls) == calls


@pytest.mark.parametrize(
    "bad_clock",
    [None, False, NOW.replace(tzinfo=None), datetime(1960, 1, 1, tzinfo=UTC)],
)
def test_invalid_clock_before_io(env, bad_clock):
    created = create(env)
    calls = len(env.raw.calls)
    env.clock[0] = bad_clock
    for action in (lambda: create(env), lambda: consume(env, created)):
        with pytest.raises(auth.AuthTransactionError, match="clock_invalid"):
            action()
        assert len(env.raw.calls) == calls


@pytest.mark.parametrize("elapsed", [-1, 300])
def test_server_clock_rejects_before_del_and_after_del(env, elapsed):
    created = create(env)
    key, _, _ = record(env)
    env.clock[0] = NOW + timedelta(seconds=elapsed)
    with pytest.raises(auth.AuthTransactionError):
        consume(env, created)
    assert key in env.raw.records
    env.clock[0] = NOW
    env.raw.after_eval = lambda: env.clock.__setitem__(0, NOW + timedelta(seconds=elapsed))
    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        consume(env, created)
    assert key not in env.raw.records


@pytest.mark.parametrize("operation", ["create", "consume"])
@pytest.mark.parametrize("fault", ["before", "lost", "odd"])
def test_storage_faults_never_retry_or_restore(env, operation, fault):
    created = create(env) if operation == "consume" else None
    calls = len(env.raw.calls)
    env.raw.before_fault = fault == "before"
    env.raw.lose_reply = fault == "lost"
    env.raw.reply = True if fault == "odd" else UNSET
    with pytest.raises(auth.AuthTransactionError, match="storage_uncertain") as error:
        consume(env, created) if created else create(env)
    assert "private" not in str(error.value) and len(env.raw.calls) == calls + 1
    assert bool(env.raw.records) is ((operation == "create") != (fault == "before"))


@pytest.mark.parametrize("reply", [None, False, -1, 2, 1.0, "1", b"1"])
def test_create_requires_exact_integer_reply(env, reply):
    env.raw.reply = reply
    with pytest.raises(auth.AuthTransactionError, match="storage_uncertain"):
        create(env)
    assert len(env.raw.records) == len(env.raw.calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "version_float",
        "issued_float",
        "expiry_float",
        "whitespace",
        "duplicate_outer",
        "data_whitespace",
        "data_duplicate",
        "data_extra",
        "retry_zero",
        "retry_true",
        "bad_code",
        "bad_uuid",
        "nonfinite",
    ],
)
def test_lua_accepted_python_rejected_full_envelope_stays_spent(env, mutation):
    created = create(env)
    key, envelope, expiry = record(env)
    data = json.loads(envelope["data"])
    if mutation in ("version_float", "issued_float", "expiry_float"):
        field = {
            "version_float": "schema_version",
            "issued_float": "issued_at",
            "expiry_float": "expires_at",
        }[mutation]
        envelope[field] = float(envelope[field])
    elif mutation == "data_whitespace":
        envelope["data"] = " " + envelope["data"]
    elif mutation == "data_duplicate":
        envelope["data"] = envelope["data"][:-1] + ',"retry_allowed":false}'
    elif mutation == "data_extra":
        data["account_id"] = "synthetic-private"
    elif mutation == "retry_zero":
        data["retry_allowed"] = 0
    elif mutation == "retry_true":
        data["retry_allowed"] = True
    elif mutation == "bad_code":
        data["code"] = "provider_unavailable"
    elif mutation == "bad_uuid":
        data["correlation_id"] = CORRELATION.upper()
    elif mutation == "nonfinite":
        envelope["data"] = '{"code":NaN}'
    if mutation in ("data_extra", "retry_zero", "retry_true", "bad_code", "bad_uuid"):
        envelope["data"] = canonical(data)
    raw = canonical(envelope)
    if mutation == "whitespace":
        raw = " " + raw
    elif mutation == "duplicate_outer":
        raw = raw[:-1] + ',"schema_version":1}'
    env.raw.records[key] = (raw, expiry)
    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        consume(env, created)
    assert key not in env.raw.records and not env.raw.slots
    env.crypto.decrypt.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("owner", "b" * 64),
        ("scope", "c" * 64),
        ("context", "d" * 64),
        ("issued_at", True),
        ("expires_at", False),
        ("issued_at", -1),
        ("expires_at", int(NOW.timestamp()) + 301),
        ("data", ""),
        ("data", {}),
        ("extra", "private"),
    ],
)
def test_lua_rejected_outer_fields_preserve_record(env, field, value):
    created = create(env)
    key, envelope, expiry = record(env)
    envelope[field] = value
    env.raw.records[key] = (canonical(envelope), expiry)
    with pytest.raises(auth.AuthTransactionError):
        consume(env, created)
    assert key in env.raw.records


@pytest.mark.parametrize("raw", [b"\xff", "[1]", "{}", "{", "x" * 16385])
def test_invalid_raw_wire_refuses_without_del(env, raw):
    created = create(env)
    key, _, expiry = record(env)
    env.raw.records[key] = (raw, expiry)
    with pytest.raises(auth.AuthTransactionError):
        consume(env, created)
    assert key in env.raw.records


def test_bytes_full_envelope_supported_and_other_result_remains(env):
    first, second = create(env), create(env)
    for key, (wire, expiry) in list(env.raw.records.items()):
        env.raw.records[key] = (wire.encode(), expiry)
    assert consume(env, first).payload == env.payload
    assert len(env.raw.records) == 1
    assert consume(env, second).payload == env.payload


@pytest.mark.parametrize("fault", ["invalid_utf8", "oversized", "missing", "version_bool", "wrong_digest", "data_type"])
def test_full_returned_envelope_revalidated_after_real_model_del(env, fault):
    created = create(env)
    key, envelope, _ = record(env)
    if fault == "missing":
        del envelope["owner"]
    elif fault == "version_bool":
        envelope["schema_version"] = True
    elif fault == "wrong_digest":
        envelope["scope"] = "not-a-digest"
    elif fault == "data_type":
        envelope["data"] = []
    env.raw.reply = {"invalid_utf8": b"\xff", "oversized": b"x" * 16385}.get(fault, canonical(envelope))
    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        consume(env, created)
    assert key not in env.raw.records


@pytest.mark.parametrize("operation", ["create", "consume"])
def test_baseexception_postguard_keeps_primary_and_no_response(env, operation):
    class Cancelled(BaseException):
        pass

    primary = Cancelled("synthetic cancellation")
    created = create(env) if operation == "consume" else None
    calls = [0]

    def guard():
        calls[0] += 1
        if calls[0] == 2:
            raise primary
        return env.current[0]

    with pytest.raises(Cancelled) as caught:
        consume(env, created, guard=guard) if created else create(env, guard=guard)
    assert caught.value is primary and bool(env.raw.records) is (operation == "create")


@pytest.mark.parametrize("elapsed", [-1, 300])
def test_create_clock_drift_returns_no_directives_only_orphan(env, elapsed):
    env.raw.after_eval = lambda: env.clock.__setitem__(0, NOW + timedelta(seconds=elapsed))
    with pytest.raises(auth.AuthTransactionError, match="clock_invalid"):
        create(env)
    assert len(env.raw.records) == 1 and not env.raw.slots


def test_existing_scope_is_refreshed_without_bootstrap(env, monkeypatch):
    monkeypatch.setattr(auth, "new_browser_scope", Mock(side_effect=AssertionError("unexpected bootstrap")))
    created = create(env)
    assert created.scope_cookie.value == env.scope
    assert consume(env, created).payload == env.payload


def test_lua_contract_does_not_use_authorization_index_or_return_only_data():
    assert "'EX', 300, 'NX'" in auth.CREATE_RESTRICTED_RESULT_SCRIPT
    script = auth.CONSUME_RESTRICTED_RESULT_SCRIPT
    assert "redis.call('TTL'" in script and "return raw" in script and "count ~= 7" in script
    assert "redis.call('DEL'" in script and "return record.data" not in script
    assert "KEYS[2]" not in script and "ZADD" not in auth.CREATE_RESTRICTED_RESULT_SCRIPT
