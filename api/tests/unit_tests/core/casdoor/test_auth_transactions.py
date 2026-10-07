"""Synthetic, offline Python model of Lua contracts; never real Redis acceptance."""

import base64
import hashlib
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
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

NAMESPACE = UUID("10000000-0000-4000-8000-000000000001")
REVISION = UUID("20000000-0000-4000-8000-000000000001")
ACCOUNT = UUID("30000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
CALLBACK = "https://console.example.test" + COOKIE_PATH + "/callback"
POLICY = CookiePolicy("https://console.example.test")


class FakeRedis:
    """Thread-safe model of script outcomes/TTL, not a Redis or Lua interpreter."""

    def __init__(self):
        self.now = NOW.timestamp()
        self.records = {}
        self.slots = {}
        self.calls = []
        self.lock = threading.Lock()
        self.after_eval = None
        self.lose_reply = False

    def eval(self, script, numkeys, *args):
        assert numkeys == 2
        key, index, *argv = args
        assert key.split("{")[1].split("}")[0] == index.split("{")[1].split("}")[0]
        with self.lock:
            self.calls.append((script, args))
            slots = self.slots.setdefault(index, {})
            if script == CREATE_SCRIPT:
                for state, expires in list(slots.items()):
                    if expires <= self.now:
                        del slots[state]
                if len(slots) >= 5:
                    result = 0
                elif key in self.records and self.records[key][1] > self.now:
                    result = -1
                else:
                    self.records[key] = (argv[0], self.now + 300)
                    slots[argv[1]] = self.now + 300
                    result = 1
            else:
                assert script == CONSUME_SCRIPT
                raw, expiry = self.records.get(key, (None, 0))
                record = json.loads(raw) if raw else None
                if (
                    record is None
                    or expiry - self.now < 1
                    or record["owner"] != argv[0]
                    or record["context"] != argv[1]
                    or argv[2] not in slots
                ):
                    result = None
                else:
                    del self.records[key]
                    del slots[argv[2]]
                    result = record["data"]
        if self.after_eval:
            self.after_eval()
        if self.lose_reply:
            raise TimeoutError("synthetic sensitive transport text")
        return result


@pytest.fixture
def setup():
    raw = FakeRedis()
    wrapper = RedisClientWrapper()
    wrapper.initialize(raw)
    wrapper._get_prefix = lambda: " test-prefix "
    protector = CasdoorCrypto(secret_key=secrets.token_bytes(32), key_version="synthetic-v1")
    clock = [NOW]
    store = AuthTransactionStore(wrapper, protector, clock=lambda: clock[0])
    context = TrustedAuthContext(
        NAMESPACE,
        REVISION,
        AuthMode.LOGIN,
        CALLBACK,
        "/apps/example",
        "synthetic-invite-only",
        "zh-Hans",
        "Asia/Shanghai",
    )
    guard = [CurrentAuthContext(NAMESPACE, REVISION, AuthMode.LOGIN, CALLBACK, None, allowed=True)]
    scope = new_browser_scope(POLICY).value
    return raw, store, protector, context, guard, scope, clock


def create(setup, **changes):
    _raw, store, _protector, context, guard, scope, _clock = setup
    return store.create(
        changes.get("context", context),
        browser_scope=changes.get("scope", scope),
        policy=changes.get("policy", POLICY),
        guard=lambda: guard[0],
    )


def consume(setup, created, **changes):
    _raw, store, _protector, _context, guard, scope, _clock = setup
    return store.consume(
        changes.get("state", created.state),
        transaction_cookie=changes.get("cookie", created.cookie.value),
        browser_scope=changes.get("scope", scope),
        policy=changes.get("policy", POLICY),
        guard=changes.get("guard", lambda: guard[0]),
    )


def test_roundtrip_pkce_minimal_cookie_and_privacy(setup):
    raw, _store, _protector, context, _guard, scope, _clock = setup
    created = create(setup)
    record = next(iter(raw.records.values()))[0]
    assert all(value not in record for value in (created.state, created.cookie.value, scope, context.invite))
    assert created.state not in repr(created) and created.cookie.value not in repr(created.cookie)
    assert context.invite not in repr(context)
    result = consume(setup, created)
    assert result.context == context
    assert result.auth_started_at == NOW and result.auth_started_at.tzinfo is not None
    assert result.nonce == created.nonce
    assert result.code_verifier not in record and result.code_verifier not in repr(result)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(result.code_verifier.encode()).digest()).rstrip(b"=")
    assert created.code_challenge == challenge.decode()
    assert created.cookie.name == transaction_cookie_name(created.state)
    assert created.cookie.path == COOKIE_PATH + "/callback"
    assert created.scope_cookie.path == COOKIE_PATH
    assert created.cookie.httponly and created.cookie.secure and created.cookie.samesite == "Lax"
    assert created.cookie.domain is None and created.cookie.max_age == 300
    assert result.clear_cookie.value == "" and result.clear_cookie.max_age == 0
    with pytest.raises(FrozenInstanceError):
        result.auth_started_at = NOW
    with pytest.raises(AuthTransactionError):
        consume(setup, created)


def test_five_tabs_sixth_rejected_without_evicting(setup):
    tabs = [create(setup) for _ in range(5)]
    with pytest.raises(AuthTransactionError, match="limit"):
        create(setup)
    assert len({tab.cookie.name for tab in tabs}) == 5
    for tab in reversed(tabs):
        consume(setup, tab)
    assert len(setup[0].records) == 0
    consume(setup, create(setup))


def test_concurrent_tabs_limit_is_atomic_in_model(setup):
    def attempt(_):
        try:
            return create(setup)
        except AuthTransactionError as error:
            return error.reason

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(10)))
    assert outcomes.count("limit") == 5
    assert len(setup[0].records) == 5


def test_simultaneous_consume_only_one_winner_in_model(setup):
    created = create(setup)

    def attempt(_):
        try:
            return consume(setup, created)
        except AuthTransactionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert sum(value is not None for value in outcomes) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"cookie": secrets.token_urlsafe(32)},
        {"scope": secrets.token_urlsafe(32)},
        {"state": secrets.token_urlsafe(32)},
        {"cookie": "bad"},
        {"scope": ""},
        {"state": "bad"},
    ],
)
def test_wrong_browser_cookie_or_state_does_not_consume_legitimate_transaction(setup, changes):
    created = create(setup)
    with pytest.raises(AuthTransactionError):
        consume(setup, created, **changes)
    assert consume(setup, created).context.mode == AuthMode.LOGIN


def test_expiry_and_slot_reclamation(setup):
    tabs = [create(setup) for _ in range(5)]
    setup[0].now += 300
    setup[6][0] += timedelta(seconds=300)
    with pytest.raises(AuthTransactionError):
        consume(setup, tabs[0])
    new = create(setup)
    assert consume(setup, new).auth_started_at == NOW + timedelta(seconds=300)


@pytest.mark.parametrize("delta", [-1, 300, 301])
def test_utc_clock_rejects_future_start_or_expiry_even_if_redis_model_is_stale(setup, delta):
    created = create(setup)
    setup[6][0] = NOW + timedelta(seconds=delta)
    with pytest.raises(AuthTransactionError):
        consume(setup, created)
    assert not setup[0].records


@pytest.mark.parametrize(
    "change",
    [
        {"namespace_id": uuid4()},
        {"revision_id": uuid4()},
        {"allowed": False},
        {"mode": AuthMode.LINK},
        {"registered_redirect_uri": "https://another.test/callback"},
    ],
)
def test_current_context_changes_rejected_before_delete(setup, change):
    created = create(setup)
    old = setup[4][0]
    setup[4][0] = replace(old, **change)
    with pytest.raises(AuthTransactionError):
        consume(setup, created)
    setup[4][0] = old
    consume(setup, created)


def test_saving_unrelated_draft_does_not_change_active_guard(setup):
    created = create(setup)
    draft_revision = uuid4()
    assert draft_revision != setup[3].revision_id
    # Configuration owner keeps the active-only projection unchanged on save.
    assert consume(setup, created).context.revision_id == REVISION


def test_late_active_change_consumes_then_fails_without_recovery(setup):
    created = create(setup)
    old = setup[4][0]
    setup[0].after_eval = lambda: setup[4].__setitem__(0, replace(old, revision_id=uuid4()))
    with pytest.raises(AuthTransactionError, match="context_changed"):
        consume(setup, created)
    setup[0].after_eval = None
    setup[4][0] = old
    with pytest.raises(AuthTransactionError):
        consume(setup, created)


def test_late_guard_exception_does_not_restore(setup):
    created = create(setup)
    count = [0]

    def guard():
        count[0] += 1
        if count[0] == 2:
            raise RuntimeError("synthetic private error")
        return setup[4][0]

    with pytest.raises(AuthTransactionError, match="guard_unavailable"):
        consume(setup, created, guard=guard)
    assert not setup[0].records


@pytest.mark.parametrize("phase", ["create", "consume"])
def test_lost_reply_never_automatically_retries_or_exposes_transport_message(setup, phase):
    created = create(setup) if phase == "consume" else None
    previous = len(setup[0].calls)
    setup[0].lose_reply = True
    with pytest.raises(AuthTransactionError, match="storage_uncertain") as error:
        create(setup) if phase == "create" else consume(setup, created)
    assert len(setup[0].calls) == previous + 1
    assert "sensitive" not in str(error.value) and error.value.retry_allowed is False
    assert len(setup[0].records) == (1 if phase == "create" else 0)


def source(setup, management=False):
    digest = setup[2].refresh_source_digest("synthetic-request-refresh-only")
    return SourceSessionContext(ACCOUNT, ACCOUNT, ACCOUNT, digest, management)


def mode_setup(setup, mode):
    src = source(setup, mode == AuthMode.DIAGNOSTIC)
    identity = uuid4() if mode == AuthMode.REAUTH_UNLINK else None
    action = "unlink" if identity else None
    setup = list(setup)
    setup[3] = replace(setup[3], mode=mode, source=src, identity_id=identity, action=action)
    setup[4][0] = CurrentAuthContext(NAMESPACE, REVISION, mode, CALLBACK, src, identity, action, allowed=True)
    return tuple(setup)


@pytest.mark.parametrize("mode", [AuthMode.LINK, AuthMode.DIAGNOSTIC, AuthMode.REAUTH_UNLINK])
def test_source_modes_keep_trusted_source_and_return_no_session_side_effects(setup, mode):
    setup = mode_setup(setup, mode)
    created = create(setup)
    assert "synthetic-request-refresh-only" not in next(iter(setup[0].records.values()))[0]
    params = created.authorization_parameters
    assert params["response_type"] == "code" and params["code_challenge_method"] == "S256"
    assert params["scope"] == "openid profile email"
    assert (params.get("prompt"), params.get("max_age")) == (
        ("login", "0") if mode == AuthMode.REAUTH_UNLINK else (None, None)
    )
    result = consume(setup, created)
    assert result.context.source == setup[3].source
    assert result.clear_cookie.name.startswith("casdoor_tx_")
    assert not hasattr(result, "login_cookie")


@pytest.mark.parametrize("change", ["refresh", "account", "permission", "identity", "action"])
def test_source_change_or_revocation_rejected_without_consuming_other_owner(setup, change):
    mode = AuthMode.DIAGNOSTIC if change == "permission" else AuthMode.REAUTH_UNLINK
    setup = mode_setup(setup, mode)
    created = create(setup)
    old = setup[4][0]
    if change == "refresh":
        new = replace(old, source=replace(old.source, refresh_digest="a" * 64))
    elif change == "account":
        other = uuid4()
        new = replace(old, source=SourceSessionContext(other, other, other, old.source.refresh_digest))
    elif change == "permission":
        new = replace(old, source=replace(old.source, management_authorized=False))
    elif change == "identity":
        new = replace(old, identity_id=uuid4())
    else:
        new = replace(old, action="other")
    setup[4][0] = new
    with pytest.raises(AuthTransactionError):
        consume(setup, created)
    setup[4][0] = old
    consume(setup, created)


def test_diagnostic_binds_exact_draft_and_disable_revokes(setup):
    setup = mode_setup(setup, AuthMode.DIAGNOSTIC)
    created = create(setup)
    old = setup[4][0]
    for changed in (replace(old, revision_id=uuid4()), replace(old, allowed=False)):
        setup[4][0] = changed
        with pytest.raises(AuthTransactionError):
            consume(setup, created)
    setup[4][0] = old
    assert consume(setup, created).context.mode == AuthMode.DIAGNOSTIC


@pytest.mark.parametrize("kind", ["namespace", "revision", "record", "purpose"])
def test_verifier_aad_binding_rejects_swapped_envelope(setup, kind):
    created = create(setup)
    key, (raw, expiry) = next(iter(setup[0].records.items()))
    record = json.loads(raw)
    data = json.loads(record["data"])
    encryption = EncryptionContext(EncryptionPurpose.AUTH_VERIFIER, NAMESPACE, REVISION, data["record_id"])
    wrong = {
        "namespace": replace(encryption, namespace_id=uuid4()),
        "revision": replace(encryption, revision_id=uuid4()),
        "record": replace(encryption, record_id=str(uuid4())),
        "purpose": replace(encryption, purpose=EncryptionPurpose.AVATAR_URL),
    }[kind]
    data["encrypted_verifier"] = setup[2].encrypt(
        json.dumps({"verifier": secrets.token_urlsafe(32), "invite": None}), context=wrong
    )
    record["data"] = json.dumps(data)
    setup[0].records[key] = (json.dumps(record), expiry)
    with pytest.raises(AuthTransactionError, match="record_invalid"):
        consume(setup, created)
    assert key not in setup[0].records


@pytest.mark.parametrize("prefix", ["", " test-prefix ", "casdoor:deployment"])
def test_actual_wrapper_raw_eval_and_actual_serializer_prefix_exactly_once(setup, prefix):
    setup[1]._redis._get_prefix = lambda: prefix
    created = create(setup)
    keys = setup[0].calls[-1][1][:2]
    scope_hash = hashlib.sha256(setup[5].encode()).hexdigest()
    state_hash = hashlib.sha256(created.state.encode()).hexdigest()
    assert keys[0] == serialize_redis_name(f"casdoor:auth:{{{scope_hash}}}:{state_hash}", prefix)
    assert keys[1] == serialize_redis_name(f"casdoor:auth-browser:{{{scope_hash}}}", prefix)
    assert created.state not in keys[0] and setup[5] not in keys[1]
    consume(setup, created)


@pytest.mark.parametrize(
    "path", ["https://outside.test", "//outside.test", "/a/../b", "/a%2fb", "/a?token=x", "/a#b", "/a\\b", "/a\n"]
)
def test_navigation_rejects_untrusted_or_ambiguous_paths(setup, path):
    with pytest.raises(AuthTransactionError):
        replace(setup[3], return_path=path)


@pytest.mark.parametrize(
    "field,value",
    [("invite", "x" * 513), ("locale", "x" * 65), ("timezone", "x" * 65), ("locale", ""), ("invite", "\ud800")],
)
def test_context_text_is_bounded(setup, field, value):
    with pytest.raises(AuthTransactionError):
        replace(setup[3], **{field: value})


@pytest.mark.parametrize(
    "origin,allow",
    [
        ("http://public.test", True),
        ("http://127.0.0.1", False),
        ("https://user@host.test", False),
        ("https://host.test/a", False),
        ("https://host.test?x=1", False),
    ],
)
def test_cookie_policy_rejects_insecure_or_invalid_origins(origin, allow):
    with pytest.raises(AuthTransactionError):
        CookiePolicy(origin, allow)


@pytest.mark.parametrize("origin", ["http://localhost:5001", "http://127.0.0.1:5001", "http://[::1]:5001"])
def test_explicit_loopback_http_exception(setup, origin):
    policy = CookiePolicy(origin, True)
    context = replace(setup[3], registered_redirect_uri=origin + COOKIE_PATH + "/callback")
    setup[4][0] = replace(setup[4][0], registered_redirect_uri=context.registered_redirect_uri)
    created = create(setup, policy=policy, context=context)
    assert created.cookie.secure is False
    assert consume(setup, created, policy=policy).clear_cookie.secure is False


@pytest.mark.parametrize("clock", [datetime(2026, 10, 1), NOW.astimezone(timezone(timedelta(hours=8)))])
def test_auth_started_at_requires_server_utc_before_any_write(setup, clock):
    setup[6][0] = clock
    with pytest.raises(AuthTransactionError, match="clock_invalid"):
        create(setup)
    assert not setup[0].calls


def test_exact_callback_origin_mismatch_before_creation(setup):
    with pytest.raises(AuthTransactionError):
        create(setup, policy=CookiePolicy("https://other.test"))
    assert not setup[0].calls


def test_lua_source_checks_owner_and_context_before_atomic_delete():
    assert CONSUME_SCRIPT.index("record.owner ~= ARGV[1]") < CONSUME_SCRIPT.index("redis.call('DEL'")
    assert CONSUME_SCRIPT.index("record.context ~= ARGV[2]") < CONSUME_SCRIPT.index("redis.call('DEL'")
    assert "redis.call('TTL'" in CONSUME_SCRIPT and "return record.data" in CONSUME_SCRIPT
    assert "redis.call('TIME')" in CREATE_SCRIPT and "'EX', 300, 'NX'" in CREATE_SCRIPT
    assert "ZCARD" in CREATE_SCRIPT and ">= 5" in CREATE_SCRIPT


def test_source_context_requires_refresh_access_same_account(setup):
    with pytest.raises(AuthTransactionError):
        SourceSessionContext(ACCOUNT, uuid4(), ACCOUNT, "a" * 64)
    with pytest.raises(AuthTransactionError):
        SourceSessionContext(ACCOUNT, ACCOUNT, uuid4(), "a" * 64)
    with pytest.raises(AuthTransactionError):
        SourceSessionContext(ACCOUNT, ACCOUNT, ACCOUNT, "raw-refresh")


def test_revoked_session_same_hmac_still_requires_validity_guard(setup):
    setup = mode_setup(setup, AuthMode.LINK)
    created = create(setup)
    setup[4][0] = replace(setup[4][0], allowed=False)
    with pytest.raises(AuthTransactionError, match="context_changed"):
        consume(setup, created)
    # An unchanged digest is deliberately insufficient to bypass allowed=False.
    assert len(setup[0].records) == 1


def test_missing_guard_projection_or_initial_guard_error_rejected_safely(setup):
    created = create(setup)
    with pytest.raises(AuthTransactionError, match="context_changed"):
        consume(setup, created, guard=lambda: None)

    def broken_guard():
        raise RuntimeError("synthetic private authentication detail")

    with pytest.raises(AuthTransactionError, match="guard_unavailable") as error:
        consume(setup, created, guard=broken_guard)
    assert "private" not in str(error.value)
    consume(setup, created)


def test_late_disabled_create_has_no_authorization_or_cookie_result(setup):
    old = setup[4][0]
    setup[0].after_eval = lambda: setup[4].__setitem__(0, replace(old, allowed=False))
    with pytest.raises(AuthTransactionError, match="context_changed"):
        create(setup)
    assert len(setup[0].records) == 1  # orphan expires; not rolled back/replayed


def test_callback_policy_origin_error_preserves_legitimate_transaction(setup):
    created = create(setup)
    with pytest.raises(AuthTransactionError, match="context_changed"):
        consume(setup, created, policy=CookiePolicy("https://other.test"))
    consume(setup, created)


@pytest.mark.parametrize(
    "extra", ["access_token", "id_token", "invite", "return_path", "mode", "account_id", "unknown"]
)
def test_existing_callback_dto_rejects_extra_query_not_controller_routing_acceptance(extra):
    from controllers.console.casdoor_schemas_extend import CasdoorCallbackQuery
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CasdoorCallbackQuery(state=secrets.token_urlsafe(32), code="synthetic-code-only", **{extra: "synthetic"})


@pytest.mark.parametrize("origin", ["https://[broken", "https://host.test:bad", "https://host.test:70000"])
def test_malformed_origin_has_stable_non_sensitive_error(origin):
    with pytest.raises(AuthTransactionError) as error:
        CookiePolicy(origin)
    assert origin not in str(error.value)
