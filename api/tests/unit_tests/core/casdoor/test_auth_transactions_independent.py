"""Independent boundary checks for the I07-A auth transaction foundation.

The in-memory evaluator models the documented Lua effects. It is not a Redis or
Lua interpreter; assertions over Lua text are kept separate from model outcomes.
"""

import base64
import hashlib
import inspect
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from controllers.console.casdoor_schemas_extend import CasdoorCallbackQuery
from core.casdoor.auth_transactions import (
    CONSUME_SCRIPT,
    COOKIE_PATH,
    CREATE_SCRIPT,
    AuthMode,
    AuthTransactionError,
    AuthTransactionStore,
    CookiePolicy,
    CurrentAuthContext,
    SourceSessionContext,
    TrustedAuthContext,
    new_browser_scope,
    transaction_cookie_name,
)
from core.casdoor.crypto import CasdoorCrypto, EncryptionContext, EncryptionPurpose
from extensions.ext_redis import RedisClientWrapper
from extensions.redis_names import serialize_redis_name
from pydantic import ValidationError

NS = UUID("10000000-0000-4000-8000-000000000001")
REV = UUID("20000000-0000-4000-8000-000000000001")
ACCOUNT = UUID("30000000-0000-4000-8000-000000000001")
ORIGIN = "https://console.example.test"
CALLBACK = ORIGIN + COOKIE_PATH + "/callback"
POLICY = CookiePolicy(ORIGIN)
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


class ModelRedis:
    """Thread-safe, minimal behavioral model for the two production scripts."""

    def __init__(self) -> None:
        self.now = NOW.timestamp()
        self.values: dict[str, tuple[str, float]] = {}
        self.indexes: dict[str, dict[str, float]] = {}
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.lock = threading.Lock()
        self.after_eval = None
        self.drop_reply = False

    def eval(self, script: str, nkeys: int, *items: str):
        assert nkeys == 2
        record_key, index_key, *args = items
        self.calls.append((script, nkeys, tuple(items)))
        with self.lock:
            index = self.indexes.setdefault(index_key, {})
            if script == CREATE_SCRIPT:
                for digest, expiry in tuple(index.items()):
                    if expiry <= self.now:
                        del index[digest]
                current = self.values.get(record_key)
                if len(index) >= 5:
                    result = 0
                elif current and current[1] > self.now:
                    result = -1
                else:
                    self.values[record_key] = (args[0], self.now + 300)
                    index[args[1]] = self.now + 300
                    result = 1
            else:
                assert script == CONSUME_SCRIPT
                entry = self.values.get(record_key)
                record = json.loads(entry[0]) if entry else None
                if (
                    entry is None
                    or entry[1] - self.now < 1
                    or record["owner"] != args[0]
                    or record["context"] != args[1]
                    or args[2] not in index
                ):
                    result = None
                else:
                    del self.values[record_key]
                    del index[args[2]]
                    result = record["data"]
        if self.after_eval:
            self.after_eval()
        if self.drop_reply:
            raise TimeoutError("synthetic evaluator reply loss")
        return result


@pytest.fixture
def env():
    raw = ModelRedis()
    wrapper = RedisClientWrapper()
    wrapper.initialize(raw)
    wrapper._get_prefix = lambda: "  deploy-prefix  "
    crypto = CasdoorCrypto(secret_key=secrets.token_bytes(32), key_version="independent-test")
    clock = [NOW]
    # No serializer override: exercise the production default owner.
    store = AuthTransactionStore(wrapper, crypto, clock=lambda: clock[0])
    trusted = TrustedAuthContext(NS, REV, AuthMode.LOGIN, CALLBACK, "/apps/sample")
    current = [CurrentAuthContext(NS, REV, AuthMode.LOGIN, CALLBACK, None, allowed=True)]
    scope = new_browser_scope(POLICY).value
    return raw, wrapper, crypto, clock, store, trusted, current, scope


def create(env, *, context=None, scope=None, policy=POLICY, guard=None):
    return env[4].create(
        context or env[5],
        browser_scope=scope or env[7],
        policy=policy,
        guard=guard or (lambda: env[6][0]),
    )


def consume(env, authorization, *, state=None, cookie=None, scope=None, policy=POLICY, guard=None):
    return env[4].consume(
        state or authorization.state,
        transaction_cookie=cookie or authorization.cookie.value,
        browser_scope=scope or env[7],
        policy=policy,
        guard=guard or (lambda: env[6][0]),
    )


def physical_keys(env):
    return env[2] and env[0].calls[-1][2][:2]


def test_real_wrapper_default_serializer_prefixes_lua_keys_once_and_shares_cluster_tag(env):
    auth = create(env)
    key, index = physical_keys(env)
    scope_digest = hashlib.sha256(env[7].encode()).hexdigest()
    state_digest = hashlib.sha256(auth.state.encode()).hexdigest()
    logical_record = f"casdoor:auth:{{{scope_digest}}}:{state_digest}"
    logical_index = f"casdoor:auth-browser:{{{scope_digest}}}"
    prefix = env[1]._get_prefix()
    assert key == serialize_redis_name(logical_record, prefix)
    assert index == serialize_redis_name(logical_index, prefix)
    assert key.startswith("deploy-prefix:") and index.startswith("deploy-prefix:")
    assert key.count("deploy-prefix:") == index.count("deploy-prefix:") == 1
    assert key.split("{")[1].split("}")[0] == index.split("{")[1].split("}")[0]
    assert env[7] not in key + index and auth.state not in key + index


def test_roundtrip_generates_256_bit_state_nonce_verifier_and_server_utc(env):
    auth = create(env)
    assert len(base64.urlsafe_b64decode(auth.state + "=")) == 32
    assert len(base64.urlsafe_b64decode(auth.nonce + "=")) == 32
    assert auth.authorization_parameters["code_challenge_method"] == "S256"
    assert auth.authorization_parameters["scope"] == "openid profile email"
    key = physical_keys(env)[0]
    raw_record = env[0].values[key][0]
    record = json.loads(raw_record)
    data = json.loads(record["data"])
    private = json.loads(
        env[2].decrypt(
            data["encrypted_verifier"],
            context=EncryptionContext(
                EncryptionPurpose.AUTH_VERIFIER,
                NS,
                REV,
                data["record_id"],
            ),
        )
    )
    assert len(base64.urlsafe_b64decode(private["verifier"] + "=")) == 32
    expected = base64.urlsafe_b64encode(hashlib.sha256(private["verifier"].encode()).digest()).rstrip(b"=")
    assert auth.code_challenge == expected.decode()
    assert data["auth_started_at"] == NOW.isoformat()
    assert "auth_started_at" not in inspect.signature(env[4].create).parameters
    result = consume(env, auth)
    assert result.auth_started_at == NOW
    assert result.nonce == auth.nonce
    assert result.code_verifier == private["verifier"]
    assert private["verifier"] not in raw_record
    assert env[5].invite is None


def test_scope_state_and_cookie_values_are_not_stored_in_plaintext(env):
    invite_context = replace(env[5], invite="opaque-invite-capability")
    auth = create(env, context=invite_context)
    stored = next(iter(env[0].values.values()))[0]
    for value in (auth.state, env[7], auth.cookie.value, "opaque-invite-capability"):
        assert value not in stored
    assert env[7] not in repr(auth) and auth.cookie.value not in repr(auth.cookie)


def test_fifth_live_tab_survives_sixth_rejection_then_expiry_reclaims_slot(env):
    tabs = [create(env) for _ in range(5)]
    keys_before = set(env[0].values)
    with pytest.raises(AuthTransactionError, match="limit"):
        create(env)
    assert set(env[0].values) == keys_before
    for tab in tabs:
        consume(env, tab)
    tabs = [create(env) for _ in range(5)]
    env[0].now += 300
    env[3][0] += timedelta(seconds=300)
    with pytest.raises(AuthTransactionError):
        consume(env, tabs[0])
    assert create(env).state


@pytest.mark.parametrize("wrong", ["state", "cookie", "scope"])
def test_wrong_browser_inputs_do_not_spend_other_browser_transaction(env, wrong):
    auth = create(env)
    kwargs = {"state": auth.state, "cookie": auth.cookie.value, "scope": env[7]}
    kwargs[wrong] = secrets.token_urlsafe(32)
    with pytest.raises(AuthTransactionError):
        consume(env, auth, **kwargs)
    assert consume(env, auth).context == env[5]


@pytest.mark.parametrize(
    "change",
    [
        {"namespace_id": uuid4()},
        {"revision_id": uuid4()},
        {"allowed": False},
        {"mode": AuthMode.LINK},
        {"registered_redirect_uri": "https://other.example/callback"},
    ],
)
def test_fresh_guard_rejects_changed_active_context_without_spending(change, env):
    auth = create(env)
    original = env[6][0]
    env[6][0] = replace(original, **change)
    with pytest.raises(AuthTransactionError):
        consume(env, auth)
    env[6][0] = original
    consume(env, auth)


def test_ordinary_draft_save_leaves_active_guard_valid_but_diagnostic_is_draft_bound(env):
    active = create(env)
    unused_draft_revision = uuid4()
    assert unused_draft_revision != REV
    assert consume(env, active).context.revision_id == REV

    account_source = SourceSessionContext(ACCOUNT, ACCOUNT, ACCOUNT, "a" * 64, True)
    diagnostic_context = replace(env[5], mode=AuthMode.DIAGNOSTIC, source=account_source)
    env[6][0] = CurrentAuthContext(NS, REV, AuthMode.DIAGNOSTIC, CALLBACK, account_source, allowed=True)
    diagnostic = create(env, context=diagnostic_context)
    current = env[6][0]
    env[6][0] = replace(current, revision_id=unused_draft_revision)
    with pytest.raises(AuthTransactionError):
        consume(env, diagnostic)
    env[6][0] = current
    assert consume(env, diagnostic).context.mode == AuthMode.DIAGNOSTIC


@pytest.mark.parametrize("field", ["source", "identity_id", "action"])
def test_source_identity_action_and_refresh_linkage_are_current_guard_inputs(field, env):
    identity = uuid4()
    source = SourceSessionContext(ACCOUNT, ACCOUNT, ACCOUNT, "b" * 64)
    context = replace(env[5], mode=AuthMode.REAUTH_UNLINK, source=source, identity_id=identity, action="unlink")
    env[6][0] = CurrentAuthContext(NS, REV, AuthMode.REAUTH_UNLINK, CALLBACK, source, identity, "unlink", allowed=True)
    auth = create(env, context=context)
    previous = env[6][0]
    if field == "source":
        replacement = replace(previous.source, refresh_digest="c" * 64)
        env[6][0] = replace(previous, source=replacement)
    elif field == "identity_id":
        env[6][0] = replace(previous, identity_id=uuid4())
    else:
        env[6][0] = replace(previous, action="other")
    with pytest.raises(AuthTransactionError):
        consume(env, auth)
    env[6][0] = previous
    result = consume(env, auth)
    assert result.context.identity_id == identity and result.context.action == "unlink"
    assert auth.authorization_parameters["prompt"] == "login"
    assert auth.authorization_parameters["max_age"] == "0"


def test_diagnostic_requires_management_and_current_revocation_is_denied(env):
    source = SourceSessionContext(ACCOUNT, ACCOUNT, ACCOUNT, "d" * 64, True)
    context = replace(env[5], mode=AuthMode.DIAGNOSTIC, source=source)
    env[6][0] = CurrentAuthContext(NS, REV, AuthMode.DIAGNOSTIC, CALLBACK, source, allowed=True)
    auth = create(env, context=context)
    previous = env[6][0]
    env[6][0] = replace(previous, source=replace(source, management_authorized=False))
    with pytest.raises(AuthTransactionError):
        consume(env, auth)
    env[6][0] = replace(previous, allowed=False)
    with pytest.raises(AuthTransactionError):
        consume(env, auth)


def test_refresh_digest_is_only_linkage_and_source_account_must_match(env):
    refresh_token = "synthetic-current-request-refresh"
    digest = env[2].refresh_source_digest(refresh_token)
    source = SourceSessionContext(ACCOUNT, ACCOUNT, ACCOUNT, digest)
    context = replace(env[5], mode=AuthMode.LINK, source=source)
    env[6][0] = CurrentAuthContext(NS, REV, AuthMode.LINK, CALLBACK, source, allowed=True)
    auth = create(env, context=context)
    serialized = next(iter(env[0].values.values()))[0]
    assert digest in serialized and refresh_token not in serialized
    assert env[2].matches_refresh_source(refresh_token, digest)
    assert not env[2].matches_refresh_source("different-refresh", digest)
    result = consume(env, auth)
    assert result.context.source == source
    with pytest.raises(AuthTransactionError):
        SourceSessionContext(ACCOUNT, uuid4(), ACCOUNT, digest)
    # A matching digest alone does not survive the fresh session-owner decision.
    auth = create(env, context=context)
    env[6][0] = replace(env[6][0], allowed=False)
    with pytest.raises(AuthTransactionError):
        consume(env, auth)


@pytest.mark.parametrize("changed", ["namespace", "revision", "record", "purpose"])
def test_verifier_envelope_is_bound_to_namespace_revision_record_and_purpose(changed, env):
    auth = create(env)
    key = physical_keys(env)[0]
    record = json.loads(env[0].values[key][0])
    data = json.loads(record["data"])
    binding = EncryptionContext(EncryptionPurpose.AUTH_VERIFIER, NS, REV, data["record_id"])
    wrong = {
        "namespace": replace(binding, namespace_id=uuid4()),
        "revision": replace(binding, revision_id=uuid4()),
        "record": replace(binding, record_id=str(uuid4())),
        "purpose": replace(binding, purpose=EncryptionPurpose.AVATAR_URL),
    }[changed]
    data["encrypted_verifier"] = env[2].encrypt("synthetic", context=wrong)
    record["data"] = json.dumps(data)
    env[0].values[key] = (json.dumps(record), env[0].values[key][1])
    with pytest.raises(AuthTransactionError, match="record_invalid"):
        consume(env, auth)
    assert key not in env[0].values


def test_post_consume_guard_failure_spends_record_and_never_replays(env):
    auth = create(env)
    before = len(env[0].calls)
    env[0].after_eval = lambda: env[6].__setitem__(0, replace(env[6][0], allowed=False))
    with pytest.raises(AuthTransactionError, match="context_changed"):
        consume(env, auth)
    env[0].after_eval = None
    env[6][0] = CurrentAuthContext(NS, REV, AuthMode.LOGIN, CALLBACK, None, allowed=True)
    assert len(env[0].calls) == before + 1
    with pytest.raises(AuthTransactionError):
        consume(env, auth)


@pytest.mark.parametrize("operation", ["create", "consume"])
def test_lost_eval_reply_is_not_retried_and_is_redacted(operation, env):
    auth = create(env) if operation == "consume" else None
    count = len(env[0].calls)
    env[0].drop_reply = True
    with pytest.raises(AuthTransactionError, match="storage_uncertain") as caught:
        create(env) if operation == "create" else consume(env, auth)
    assert len(env[0].calls) == count + 1
    assert "evaluator" not in str(caught.value) and caught.value.retry_allowed is False


def test_model_concurrency_yields_five_slots_and_one_consumer(env):
    def make(_):
        try:
            return create(env)
        except AuthTransactionError:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        tabs = [tab for tab in pool.map(make, range(10)) if tab]
    assert len(tabs) == 5
    one = tabs[0]

    def use(_):
        try:
            return consume(env, one)
        except AuthTransactionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(use, range(8)))
    assert sum(result is not None for result in results) == 1


def test_cookie_directives_are_host_only_scoped_and_callback_query_dto_forbids_tokens(env):
    auth = create(env)
    assert auth.cookie.name == transaction_cookie_name(auth.state)
    assert auth.cookie.path == COOKIE_PATH + "/callback"
    assert auth.scope_cookie.path == COOKIE_PATH
    for directive in (auth.cookie, auth.scope_cookie):
        assert directive.httponly and directive.samesite == "Lax"
        assert directive.secure and directive.domain is None
    assert consume(env, auth).clear_cookie.value == ""
    for query in (
        {"state": "s", "code": "c", "access_token": "unexpected"},
        {"state": "s", "error": "denied", "mode": "login"},
    ):
        with pytest.raises(ValidationError):
            CasdoorCallbackQuery.model_validate(query)


def test_loopback_is_the_only_plain_http_cookie_exception(env):
    for host in ("localhost", "127.0.0.1", "[::1]"):
        policy = CookiePolicy("http://" + host + ":5001", allow_loopback_http=True)
        context = replace(env[5], registered_redirect_uri=policy.backend_origin + COOKIE_PATH + "/callback")
        env[6][0] = replace(env[6][0], registered_redirect_uri=context.registered_redirect_uri)
        auth = create(env, context=context, policy=policy)
        assert not auth.cookie.secure and not auth.scope_cookie.secure
    for url, allowed in (("http://public.example", True), ("http://localhost", False)):
        with pytest.raises(AuthTransactionError):
            CookiePolicy(url, allowed)


def test_actual_lua_text_places_all_consume_checks_before_delete_and_uses_server_time():
    delete_at = CONSUME_SCRIPT.index("redis.call('DEL'")
    for required in (
        "redis.call('GET'",
        "redis.call('TTL'",
        "record.owner ~= ARGV[1]",
        "record.context ~= ARGV[2]",
        "redis.call('ZSCORE'",
    ):
        assert CONSUME_SCRIPT.index(required) < delete_at
    assert CONSUME_SCRIPT.index("redis.call('DEL'") < CONSUME_SCRIPT.index("redis.call('ZREM'")
    assert "return record.data" in CONSUME_SCRIPT
    assert "redis.call('TIME')" in CREATE_SCRIPT
    assert "redis.call('ZREMRANGEBYSCORE'" in CREATE_SCRIPT
    assert "redis.call('ZCARD', KEYS[2]) >= 5" in CREATE_SCRIPT
    assert "'EX', 300, 'NX'" in CREATE_SCRIPT


def test_module_is_foundation_only_without_session_or_login_cookie_output():
    from core.casdoor import auth_transactions

    assert not hasattr(auth_transactions, "redis_client")
    assert "login_cookie" not in auth_transactions.ConsumedAuthTransaction.__dataclass_fields__
    assert "session" not in auth_transactions.ConsumedAuthTransaction.__dataclass_fields__
    assert "scope_cookie" in auth_transactions.CreatedAuthorization.__dataclass_fields__
