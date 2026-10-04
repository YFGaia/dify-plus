"""I20-A0 offline model checks; no real Redis/Lua, HTTP or browser acceptance."""

import base64
import copy
import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from core.casdoor import auth_transactions as auth
from core.casdoor.crypto import CasdoorCrypto
from extensions.ext_redis import RedisClientWrapper
from extensions.redis_names import serialize_redis_name
from test_auth_transactions import CALLBACK, NAMESPACE, NOW, POLICY, REVISION, FakeRedis


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


UNSET = object()


class InitializationRedis(FakeRedis):
    """Locked wire model with controlled lost/odd replies; never a Lua engine."""

    def __init__(self):
        super().__init__()
        self.reply = UNSET
        self.before_fault = False

    def eval(self, script, numkeys, *args):
        if script not in (auth.CREATE_INITIALIZATION_SCRIPT, auth.CONSUME_INITIALIZATION_SCRIPT):
            return super().eval(script, numkeys, *args)
        assert numkeys == 1
        key, *argv = args
        with self.lock:
            self.calls.append((script, numkeys, args))
            if self.before_fault:
                raise ConnectionError("synthetic sensitive transport sentinel")
            if script == auth.CREATE_INITIALIZATION_SCRIPT:
                if key in self.records and self.records[key][1] > self.now:
                    result = 0
                else:
                    self.records[key] = (argv[0], self.now + 60)
                    result = 1
            else:
                raw, expiry = self.records.get(key, (None, 0))
                result = None
                if raw and expiry - self.now >= 1 and len(raw.encode()) <= 16384:
                    try:
                        envelope = json.loads(raw)
                    except ValueError:
                        envelope = None
                    if (
                        type(envelope) is dict
                        and set(envelope) == {"schema_version", "owner", "context", "data"}
                        and type(envelope["schema_version"]) in (int, float)
                        and envelope["schema_version"] == 1
                        and type(envelope["owner"]) is str
                        and envelope["owner"] == argv[0]
                        and type(envelope["context"]) is str
                        and envelope["context"] == argv[1]
                        and type(envelope["data"]) is str
                        and 0 < len(envelope["data"].encode()) <= 16384
                    ):
                        del self.records[key]
                        result = raw
        if self.after_eval:
            self.after_eval()
        if self.lose_reply:
            raise TimeoutError("synthetic sensitive lost reply sentinel")
        return result if self.reply is UNSET else self.reply


@pytest.fixture
def env(monkeypatch):
    raw = InitializationRedis()
    wrapper = RedisClientWrapper()
    wrapper.initialize(raw)
    wrapper._get_prefix = lambda: "  init-prefix  "
    crypto = CasdoorCrypto(secret_key=secrets.token_bytes(32), key_version="synthetic-init-v1")
    encrypt, decrypt = Mock(wraps=crypto.encrypt), Mock(wraps=crypto.decrypt)
    monkeypatch.setattr(crypto, "encrypt", encrypt)
    monkeypatch.setattr(crypto, "decrypt", decrypt)
    context = auth.TrustedAuthContext(
        NAMESPACE, REVISION, auth.AuthMode.LOGIN, CALLBACK, "/apps/示例", locale="zh-Hans", timezone="Asia/Shanghai"
    )
    current = [auth.CurrentAuthContext(NAMESPACE, REVISION, auth.AuthMode.LOGIN, CALLBACK, None, allowed=True)]
    store = auth.AuthTransactionStore(wrapper, crypto, clock=lambda: NOW)
    return SimpleNamespace(
        raw=raw,
        wrapper=wrapper,
        store=store,
        context=context,
        current=current,
        scope=auth.new_browser_scope(POLICY).value,
        encrypt=encrypt,
        decrypt=decrypt,
    )


def create(env, **changes):
    return env.store.create_initialization(
        changes.get("context", env.context),
        browser_scope=changes.get("scope", env.scope),
        policy=changes.get("policy", POLICY),
        guard=changes.get("guard", lambda: env.current[0]),
    )


def consume(env, handle, **changes):
    return env.store.consume_initialization(
        handle,
        browser_scope=changes.get("scope", env.scope),
        policy=changes.get("policy", POLICY),
        guard=changes.get("guard", lambda: env.current[0]),
    )


def record(env):
    key = env.raw.calls[-1][2][0]
    raw, expiry = env.raw.records[key]
    return key, json.loads(raw), expiry


def replace_record(env, key, value, expiry):
    env.raw.records[key] = (value if isinstance(value, str) else canonical(value), expiry)


def test_roundtrip_full_envelope_then_original_authorization(env):
    handle = create(env)
    key, envelope, expiry = record(env)
    assert len(base64.urlsafe_b64decode(handle + "=")) == 32
    assert envelope == {
        "schema_version": 1,
        "owner": digest(env.scope),
        "context": digest(canonical(env.current[0].projection())),
        "data": canonical(env.context.public_projection()),
    }
    raw = env.raw.records[key][0]
    assert raw == canonical(envelope) and raw.isascii() and len(raw.encode()) <= 16384
    assert handle not in raw + key and env.scope not in raw + key
    assert all(value not in raw for value in ("nonce", "verifier", "invite", "encrypted", "profile", "token"))
    assert expiry == env.raw.now + 60 and not env.raw.slots
    restored = consume(env, handle)
    assert restored == env.context and key not in env.raw.records
    env.encrypt.assert_not_called()
    env.decrypt.assert_not_called()
    authorized = env.store.create(restored, browser_scope=env.scope, policy=POLICY, guard=lambda: env.current[0])
    result = env.store.consume(
        authorized.state,
        transaction_cookie=authorized.cookie.value,
        browser_scope=env.scope,
        policy=POLICY,
        guard=lambda: env.current[0],
    )
    assert result.context == restored and result.nonce == authorized.nonce
    assert authorized.cookie.max_age == 300 and env.encrypt.call_count == env.decrypt.call_count == 1


@pytest.mark.parametrize("prefix", ["", "  init-prefix  ", "deploy:worker"])
def test_actual_wrapper_prefix_once_and_single_cluster_key(env, prefix, monkeypatch):
    env.wrapper._get_prefix = lambda: prefix
    spy = Mock(wraps=serialize_redis_name)
    monkeypatch.setattr(env.store, "_key_serializer", spy)
    handle = create(env)
    key = env.raw.calls[-1][2][0]
    logical = f"casdoor:auth-init:{{{digest(env.scope)}}}:{digest(handle)}"
    assert key == serialize_redis_name(logical, prefix)
    spy.assert_called_once_with(logical, prefix)
    consume(env, handle)
    assert spy.call_count == 2
    assert all(call[1] == 1 and call[2][0] == key for call in env.raw.calls)


def test_initialization_preserves_five_existing_tabs_and_sixth_denial(env):
    tabs = [
        env.store.create(env.context, browser_scope=env.scope, policy=POLICY, guard=lambda: env.current[0])
        for _ in range(5)
    ]
    records, slots = copy.deepcopy(env.raw.records), copy.deepcopy(env.raw.slots)
    calls = env.encrypt.call_count
    assert consume(env, create(env)) == env.context
    assert env.raw.records == records and env.raw.slots == slots and env.encrypt.call_count == calls
    with pytest.raises(auth.AuthTransactionError, match="limit"):
        env.store.create(env.context, browser_scope=env.scope, policy=POLICY, guard=lambda: env.current[0])
    assert env.raw.records == records and env.raw.slots == slots
    for tab in tabs:
        env.store.consume(
            tab.state,
            transaction_cookie=tab.cookie.value,
            browser_scope=env.scope,
            policy=POLICY,
            guard=lambda: env.current[0],
        )


@pytest.mark.parametrize("mismatch", ["scope", "handle"])
def test_wrong_pair_does_not_spend_rightful_initialization(env, mismatch):
    handle = create(env)
    before = copy.deepcopy(env.raw.records)
    with pytest.raises(auth.AuthTransactionError):
        consume(
            env,
            secrets.token_urlsafe(32) if mismatch == "handle" else handle,
            **({"scope": secrets.token_urlsafe(32)} if mismatch == "scope" else {}),
        )
    assert env.raw.records == before
    assert consume(env, handle) == env.context
    with pytest.raises(auth.AuthTransactionError):
        consume(env, handle)


@pytest.mark.parametrize("elapsed", [59.5, 60, 61])
def test_expired_or_zero_ttl_has_no_authorization_effect(env, elapsed):
    handle = create(env)
    env.raw.now += elapsed
    with pytest.raises(auth.AuthTransactionError):
        consume(env, handle)
    assert not env.raw.slots
    env.encrypt.assert_not_called()


def test_concurrent_model_has_exactly_one_consumer(env):
    handle = create(env)

    def use(_):
        try:
            return consume(env, handle)
        except auth.AuthTransactionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(use, range(8)))
    assert outcomes.count(env.context) == 1
    assert not env.raw.records and not env.raw.slots


@pytest.mark.parametrize("operation", ["create", "consume"])
@pytest.mark.parametrize("field", ["scope", "policy"])
@pytest.mark.parametrize("bad", [None, False, 1, "", "!" * 43, "A" * 42 + "B"])
def test_invalid_scope_or_policy_fails_before_io(env, operation, field, bad):
    with pytest.raises(auth.AuthTransactionError):
        create(env, **{field: bad}) if operation == "create" else consume(env, env.scope, **{field: bad})
    assert not env.raw.calls and not env.raw.records


@pytest.mark.parametrize("bad", [None, False, 1, "", "!" * 43, "A" * 42 + "B", "é" * 43])
def test_invalid_handle_fails_before_io(env, bad):
    with pytest.raises(auth.AuthTransactionError):
        consume(env, bad)
    assert not env.raw.calls


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("invite", "synthetic-invite"),
        ("source", False),
        ("identity_id", NAMESPACE),
        ("action", "unlink"),
        ("mode", "login"),
        ("mode", auth.AuthMode.LINK),
        ("mode", auth.AuthMode.DIAGNOSTIC),
        ("mode", auth.AuthMode.REAUTH_UNLINK),
        ("return_path", "//external.test"),
        ("return_path", "/a/../b"),
        ("return_path", "/a%2fb"),
        ("return_path", "/a?b"),
        ("return_path", "/" + "a" * 2048),
        ("locale", False),
        ("timezone", "x" * 65),
        ("namespace_id", str(NAMESPACE)),
        ("registered_redirect_uri", "https://other.example.test" + auth.COOKIE_PATH + "/callback"),
    ],
)
def test_create_revalidates_internal_context_before_io(env, field, bad):
    context = replace(env.context)
    object.__setattr__(context, field, bad)
    with pytest.raises(auth.AuthTransactionError):
        create(env, context=context)
    assert not env.raw.calls
    env.encrypt.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "consume"])
@pytest.mark.parametrize("field", ["allowed", "namespace_id", "revision_id", "registered_redirect_uri", "mode"])
def test_changed_preguard_never_writes_or_spends(env, operation, field):
    handle = create(env) if operation == "consume" else None
    baseline, count = copy.deepcopy(env.raw.records), len(env.raw.calls)
    changes = {
        "allowed": False,
        "namespace_id": UUID(int=5),
        "revision_id": UUID(int=6),
        "registered_redirect_uri": "https://other.example.test" + auth.COOKIE_PATH + "/callback",
        "mode": auth.AuthMode.LINK,
    }
    env.current[0] = replace(env.current[0], **{field: changes[field]})
    with pytest.raises(auth.AuthTransactionError):
        create(env) if operation == "create" else consume(env, handle)
    # Namespace/revision drift is checked atomically against the stored digest.
    expected_calls = 1 if operation == "consume" and field in ("namespace_id", "revision_id") else 0
    assert len(env.raw.calls) == count + expected_calls and env.raw.records == baseline


@pytest.mark.parametrize("operation", ["create", "consume"])
@pytest.mark.parametrize("post", [False, True])
@pytest.mark.parametrize("failure", ["deny", "exception", "bad_type"])
def test_guard_failures_are_bounded_and_postconsume_stays_spent(env, operation, post, failure):
    handle = create(env) if operation == "consume" else None
    count, calls = len(env.raw.calls), 0

    def guard():
        nonlocal calls
        calls += 1
        if post and calls == 1:
            return env.current[0]
        if failure == "exception":
            raise TimeoutError("synthetic guard sensitive sentinel")
        return replace(env.current[0], allowed=False) if failure == "deny" else True

    with pytest.raises(auth.AuthTransactionError) as error:
        create(env, guard=guard) if operation == "create" else consume(env, handle, guard=guard)
    assert "sentinel" not in str(error.value) and error.value.retry_allowed is False
    assert len(env.raw.calls) == count + int(post)
    assert len(env.raw.records) == (int(post) if operation == "create" else int(not post))
    if operation == "consume" and post:
        with pytest.raises(auth.AuthTransactionError):
            consume(env, handle)


def test_nx_collision_does_not_retry_or_replace(env, monkeypatch):
    handle = create(env)
    baseline = copy.deepcopy(env.raw.records)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda size: handle)
    count = len(env.raw.calls)
    with pytest.raises(auth.AuthTransactionError, match="collision"):
        create(env)
    assert len(env.raw.calls) == count + 1 and env.raw.records == baseline


@pytest.mark.parametrize("reply", [True, False, None, "1", b"1", 1.0, -1, 2, [], {}])
def test_unknown_create_reply_is_uncertain_even_if_write_completed(env, reply):
    env.raw.reply = reply
    with pytest.raises(auth.AuthTransactionError, match="storage_uncertain"):
        create(env)
    assert len(env.raw.calls) == 1 and len(env.raw.records) == 1 and not env.raw.slots


@pytest.mark.parametrize("operation", ["create", "consume"])
@pytest.mark.parametrize("lost", [False, True])
def test_storage_faults_are_single_invocation_and_unknown_effects_not_rolled_back(env, operation, lost):
    handle = create(env) if operation == "consume" else None
    count = len(env.raw.calls)
    env.raw.lose_reply, env.raw.before_fault = lost, not lost
    with pytest.raises(auth.AuthTransactionError, match="storage_uncertain") as error:
        create(env) if operation == "create" else consume(env, handle)
    assert "sentinel" not in str(error.value)
    assert len(env.raw.calls) == count + 1
    assert len(env.raw.records) == (int(lost) if operation == "create" else int(not lost))
    env.encrypt.assert_not_called()
    env.decrypt.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extra", None),
        ("schema_version", True),
        ("schema_version", 2),
        ("owner", "wrong"),
        ("context", "wrong"),
        ("data", {}),
        ("data", ""),
    ],
)
def test_lua_model_rejects_wrong_outer_shape_without_spending(env, field, value):
    handle = create(env)
    key, envelope, expiry = record(env)
    envelope[field] = value
    replace_record(env, key, envelope, expiry)
    with pytest.raises(auth.AuthTransactionError):
        consume(env, handle)
    assert key in env.raw.records


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invite", "synthetic-invite"),
        ("source", {}),
        ("source", False),
        ("identity_id", str(NAMESPACE)),
        ("action", "unlink"),
        ("mode", "link"),
        ("mode", True),
        ("return_path", "https://other.test"),
        ("return_path", "/a/../b"),
        ("return_path", "/x#fragment"),
        ("locale", 1),
        ("timezone", False),
        ("namespace_id", str(UUID(int=10))),
        ("namespace_id", str(NAMESPACE).replace("-", "")),
        ("revision_id", str(UUID(int=11))),
        ("extra", "synthetic-code"),
        ("registered_redirect_uri", "https://other.test" + auth.COOKIE_PATH + "/callback"),
    ],
)
def test_closed_inner_context_rejects_tamper_after_delete(env, field, value):
    handle = create(env)
    key, envelope, expiry = record(env)
    inner = json.loads(envelope["data"])
    inner[field] = value
    envelope["data"] = canonical(inner)
    replace_record(env, key, envelope, expiry)
    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        consume(env, handle)
    assert key not in env.raw.records
    env.encrypt.assert_not_called()
    env.decrypt.assert_not_called()


@pytest.mark.parametrize("layer", ["outer", "inner"])
@pytest.mark.parametrize("malformation", ["duplicate", "nonfinite", "whitespace", "missing", "nonobject"])
def test_python_independently_validates_both_canonical_layers(env, layer, malformation):
    handle = create(env)
    key, envelope, expiry = record(env)
    value = envelope if layer == "outer" else json.loads(envelope["data"])
    raw = canonical(value)
    if malformation == "duplicate":
        field = "owner" if layer == "outer" else "return_path"
        raw = raw[:-1] + "," + canonical(field) + ":" + canonical(value[field]) + "}"
    elif malformation == "nonfinite":
        raw = raw[:-1] + ',"extra":NaN}'
    elif malformation == "whitespace":
        raw = " " + raw
    elif malformation == "missing":
        del value["data" if layer == "outer" else "timezone"]
        raw = canonical(value)
    else:
        raw = "[]"
    if layer == "inner":
        envelope["data"] = raw
        replace_record(env, key, envelope, expiry)
    else:
        # Force the transport response after model DEL, independently of Lua.
        env.raw.reply = raw
    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        consume(env, handle)
    assert key not in env.raw.records


@pytest.mark.parametrize("reply", [b"\xff", b"{}", "", "x" * 16385, "é" * 8193, "\ud800", b"x" * 16385])
def test_raw_record_invalid_encoding_or_byte_size_stays_spent(env, reply):
    handle = create(env)
    env.raw.reply = reply
    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        consume(env, handle)
    assert not env.raw.records


@pytest.mark.parametrize("reply", [False, True, 0, 1, 1.0, [], {}, bytearray(b"{}")])
def test_unknown_consume_wire_type_is_uncertain_and_stays_spent(env, reply):
    handle = create(env)
    env.raw.reply = reply
    with pytest.raises(auth.AuthTransactionError, match="storage_uncertain"):
        consume(env, handle)
    assert len(env.raw.calls) == 2 and not env.raw.records


@pytest.mark.parametrize("version", [True, 1.0])
def test_python_rejects_outer_schema_type_alias_independently(env, version):
    handle = create(env)
    _, envelope, _ = record(env)
    envelope["schema_version"] = version
    env.raw.reply = canonical(envelope)
    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        consume(env, handle)
    assert not env.raw.records


def test_canonical_utf8_bytes_reconstruct_successfully(env):
    handle = create(env)
    key, _, _ = record(env)
    env.raw.reply = env.raw.records[key][0].encode("utf-8")
    assert consume(env, handle) == env.context


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_existing_explicit_loopback_policy_roundtrip(env, host):
    policy = auth.CookiePolicy(f"http://{host}:5001", allow_loopback_http=True)
    callback = policy.backend_origin + auth.COOKIE_PATH + "/callback"
    env.context = replace(env.context, registered_redirect_uri=callback)
    env.current[0] = replace(env.current[0], registered_redirect_uri=callback)
    assert consume(env, create(env, policy=policy), policy=policy) == env.context


def test_actual_lua_structure_one_key_ex60_closed_checks_before_delete():
    create_script, consume_script = auth.CREATE_INITIALIZATION_SCRIPT, auth.CONSUME_INITIALIZATION_SCRIPT
    assert "'EX', 60, 'NX'" in create_script
    assert create_script.count("redis.call(") == 1
    position = consume_script.index("redis.call('DEL'")
    for fragment in (
        "redis.call('GET'",
        "redis.call('TTL'",
        "#raw > 16384",
        "pcall(cjson.decode, raw)",
        "type(record) ~= 'table'",
        "pairs(record)",
        "not fields[key]",
        "count ~= 4",
        "type(record.schema_version)",
        "record.schema_version ~= 1",
        "type(record.owner)",
        "record.owner ~= ARGV[1]",
        "type(record.context)",
        "record.context ~= ARGV[2]",
        "type(record.data)",
    ):
        assert consume_script.index(fragment) < position
    assert consume_script.index("return raw") > position
    assert all(word not in create_script + consume_script for word in ("KEYS[2]", "ZADD", "ZREM", "EXPIRE"))
    assert "'EX', 300, 'NX'" in auth.CREATE_SCRIPT and auth.TTL_SECONDS == 300 and auth.MAX_TRANSACTIONS == 5
