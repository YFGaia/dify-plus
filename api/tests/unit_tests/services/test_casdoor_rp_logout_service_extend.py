"""Synthetic signed profiles + native RSA tokens; no actual OP acceptance.

The DB digest is produced by the real immutable revision repository. The real
claims, gateway and consumed-transaction owners feed the provider-only producer.
Only Redis and external provider wires are synthetic; public flags never grant.
"""

import math
import secrets
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from core.casdoor.auth_transactions import (
    AuthMode,
    AuthTransactionError,
    AuthTransactionStore,
    CookiePolicy,
    CurrentAuthContext,
    SourceSessionContext,
    TrustedAuthContext,
)
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.deployment_evidence import DeploymentEvidenceError
from core.casdoor.gateway import CasdoorTokenGateway, GatewayOperation, RawTokens
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from extensions.ext_redis import RedisClientWrapper
from pydantic import SecretStr
from repositories.casdoor_rp_logout_repository_extend import CasdoorRPLogoutRepository
from services.casdoor_rp_logout_service_extend import (
    RP_CALLBACK_PATH,
    CasdoorRPLogoutService,
    RPLogoutTokenSnapshot,
)
from services.casdoor_session_service_extend import CasdoorSessionService, SessionProvenanceSeed
from test_auth_transactions import FakeRedis
from test_casdoor_local_http_service_extend import begin, complete
from test_claims import sign

from tests.unit_tests.core.casdoor.test_deployment_evidence import documents

pytest_plugins = ("test_casdoor_production_login_policy_extend",)


class AuthRedis(FakeRedis):
    def eval(self, script, count, *args):
        if script == "return redis.call('GET', KEYS[1])":
            assert count == 1
            record, expiry = self.records.get(args[0], (None, 0))
            return record if expiry > self.now else None
        return super().eval(script, count, *args)


class RPRedis:
    """Wire honors logical command prefix and physical Lua keys separately."""

    def __init__(self, clock):
        self.clock, self.values, self.expiries = clock, {}, {}
        self.fail_cas = False

    def _get_prefix(self):
        return " synthetic-rp "

    def _key(self, key):
        return "synthetic-rp:" + key

    def _read(self, key):
        if key in self.values and self.expiries[key] <= self.clock():
            self.values.pop(key)
            self.expiries.pop(key)
        return self.values.get(key)

    def get(self, key):
        value = self._read(self._key(key))
        return value.encode() if value else None

    def set(self, key, value, *, ex, nx):
        key = self._key(key)
        if nx and self._read(key) is not None:
            return False
        self.values[key], self.expiries[key] = value, self.clock() + ex
        return True

    def ttl(self, key):
        key = self._key(key)
        return math.floor(self.expiries[key] - self.clock()) if self._read(key) is not None else -2

    def eval(self, script, count, *args):
        keys, argv = args[:count], args[count:]
        assert all(key.startswith("synthetic-rp:") for key in keys)
        if self.fail_cas:
            return 0
        if "EXISTS', KEYS[1]" in script:
            if any(self._read(key) is not None for key in keys):
                return 0
            for key, value in zip(keys, argv[:2], strict=True):
                self.values[key], self.expiries[key] = value, self.clock() + argv[2]
            return 1
        if self._read(keys[0]) != argv[0]:
            return 0
        if count == 3:
            if self._read(keys[2]) is not None:
                return 0
            expiry = min(self.expiries[keys[0]], self.clock() + argv[3])
            self.values[keys[0]], self.expiries[keys[0]] = argv[1], expiry
            self.values.pop(keys[1], None)
            self.expiries.pop(keys[1], None)
            self.values[keys[2]], self.expiries[keys[2]] = argv[2], expiry
            return 1
        if count == 1 and "SET', KEYS[1]" in script:
            self.values[keys[0]] = argv[1]
            return 1
        if count == 2:
            if argv[3] == "nx" and self._read(keys[1]) is not None:
                return 0
            self.values[keys[1]] = argv[1]
            self.expiries[keys[1]] = min(self.expiries[keys[0]], self.clock() + argv[2])
        self.values.pop(keys[0])
        self.expiries.pop(keys[0])
        return 1


class Runtime:
    def __init__(self, client):
        self.client, self.scopes = client, []
        self.fail_open = self.fail_close = False
        self.before_open = lambda: None

    def open(self, *, deadline):
        self.before_open()
        if self.fail_open:
            raise OSError("private RP runtime canary")
        scope = SimpleNamespace(client=self.client, deadline=deadline, finished=False)

        def finish():
            scope.finished = True
            return not self.fail_close

        scope.finish = finish
        self.scopes.append(scope)
        return scope


@pytest.fixture
def rp(production, tmp_path, signing):
    flow = production.flow
    config = flow.local.config.model_copy(update={"rp_logout": True})
    with flow.local.session.begin():
        owner = flow.config._repository(flow.local.session)
        integration = owner._integration()
        saved = owner.save_draft(
            config,
            etag=integration.etag,
            actor_account_id=UUID(int=700),
            secret=SecretStr("synthetic-rp-secret"),
        )
        revision = owner._revision(integration.id, str(saved.draft_revision_id))
        namespace, revision_id, digest = (
            UUID(revision.namespace_id),
            UUID(revision.id),
            revision.config_digest,
        )
    assert digest != config.config_digest()
    manifest = deepcopy(production.manifest)
    manifest["authority_id"] = "synthetic-authority"
    manifest["binding"].update(
        namespace_id=str(namespace),
        revision_id=str(revision_id),
        config_digest=digest,
        configuration_digest=config.config_digest(),
    )
    manifest["rp_logout"] = {
        "profile": "oidc_rp_initiated_logout_v1",
        "end_session_endpoint": config.expected_issuer.rstrip("/") + "/api/logout",
        "post_logout_redirect_uri": flow.settings.CONSOLE_API_URL.rstrip("/") + RP_CALLBACK_PATH,
        **{
            name: {"record_id": "synthetic-rp/" + name, "sha256": "a" * 64}
            for name in (
                "endpoint_semantics",
                "registered_post_logout_redirect",
                "state_round_trip",
            )
        },
    }
    key = Ed25519PrivateKey.generate()
    authority, envelope = tmp_path / "rp-authority.json", tmp_path / "rp-evidence.json"
    clock = [time.time()]
    production.policy._authority_path, production.policy._evidence_path = (
        str(authority),
        str(envelope),
    )
    production.policy._now = lambda: datetime.fromtimestamp(clock[0], UTC)

    def resign():
        authority_bytes, envelope_bytes = documents(key, manifest)
        authority.write_bytes(authority_bytes)
        envelope.write_bytes(envelope_bytes)

    resign()

    def resolve(binding):
        return production.policy.resolve(config, namespace, revision_id, digest, "off")

    policy = resolve(None)
    source = SourceSessionContext(UUID(int=701), UUID(int=701), UUID(int=701), "c" * 64, True)
    context = TrustedAuthContext(
        namespace,
        revision_id,
        AuthMode.DIAGNOSTIC,
        flow.settings.CONSOLE_API_URL + "/console/api/auth/casdoor/callback",
        source=source,
        diagnostic_binding="d" * 64,
        rp_logout_diagnostic=True,
    )
    current = CurrentAuthContext(
        namespace,
        revision_id,
        AuthMode.DIAGNOSTIC,
        context.registered_redirect_uri,
        source,
        diagnostic_binding="d" * 64,
        rp_logout_diagnostic=True,
        allowed=True,
    )
    redis = RedisClientWrapper()
    redis.initialize(AuthRedis())
    redis._get_prefix = lambda: "synthetic-rp-auth"
    store = AuthTransactionStore(redis, CasdoorCrypto(secret_key="synthetic-rp-auth", key_version="v1"))
    browser = secrets.token_urlsafe(32)
    cookie_policy = CookiePolicy(flow.settings.CONSOLE_API_URL)
    created = store.create(context, browser_scope=browser, policy=cookie_policy, guard=lambda: current)
    consumed = store.consume(
        created.state,
        transaction_cookie=created.cookie.value,
        browser_scope=browser,
        policy=cookie_policy,
        guard=lambda: current,
    )
    operation = GatewayOperation(config, "synthetic-rp-secret", context.registered_redirect_uri)
    raw = CasdoorTokenGateway(operation).exchange_code("synthetic-rp-code", code_verifier=consumed.code_verifier)
    clock[0] = time.time()
    wire = RPRedis(lambda: clock[0])
    runtime = Runtime(wire)
    guard = SimpleNamespace(valid=True, calls=[])

    def diagnostic_guard(binding, projection):
        guard.calls.append((binding, projection))
        if not guard.valid or projection != context.public_projection():
            raise ValueError("original diagnostic owner unavailable")
        return current

    service = CasdoorRPLogoutService(
        settings=flow.settings,
        secret_key="synthetic-rp-workflow",
        redis_runtime_factory=runtime,
        current_policy=resolve,
        diagnostic_guard=diagnostic_guard,
        clock=lambda: clock[0],
    )
    return SimpleNamespace(
        service=service,
        flow=flow,
        config=config,
        binding=policy.binding,
        policy=policy,
        manifest=manifest,
        resign=resign,
        authority=authority,
        envelope=envelope,
        clock=clock,
        wire=wire,
        runtime=runtime,
        guard=guard,
        consumed=consumed,
        operation=operation,
        raw=raw,
        signer=signing[0],
        context=context,
        current=current,
        store=store,
        browser=browser,
        revision_id=revision_id,
        digest=digest,
    )


def start(rp, **overrides):
    return rp.service.start_protocol(
        **{
            "configuration": rp.config,
            "binding": rp.binding,
            "consumed": rp.consumed,
            "raw_tokens": rp.raw,
            "operation": rp.operation,
            **overrides,
        }
    )


def navigate(rp):
    handoff = start(rp)
    assert handoff is not None
    opaque = handoff.handoff_path.rsplit("/", 1)[1]
    navigation = rp.service.navigate(opaque=opaque, browser_cookie=handoff.cookies[0].value)
    assert navigation is not None
    state = parse_qs(urlsplit(navigation.location).query)["state"][0]
    return handoff, opaque, navigation, state


def test_real_native_producer_single_use_exact_policy_observation(rp):
    assert rp.service.observation(rp.binding) is None
    handoff, opaque, navigation, state = navigate(rp)
    query = parse_qs(urlsplit(navigation.location).query)
    assert query["id_token_hint"] == [rp.raw.payload["id_token"]]
    assert query["post_logout_redirect_uri"] == [rp.manifest["rp_logout"]["post_logout_redirect_uri"]]
    assert navigation.location.startswith(rp.manifest["rp_logout"]["end_session_endpoint"] + "?")
    assert rp.raw.payload["id_token"] not in repr(navigation)
    assert rp.raw.payload["id_token"] not in repr(handoff)
    assert all(rp.raw.payload["id_token"] not in value for value in rp.wire.values.values())
    assert rp.service.navigate(opaque=opaque, browser_cookie=handoff.cookies[0].value) is None
    assert rp.service.complete_callback(state=state, browser_cookie=secrets.token_urlsafe(32)) is None
    assert rp.service.observation(rp.binding) is None
    result = rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value)
    assert result is not None
    assert result.proof_fingerprint == rp.policy.proof_fingerprint
    assert rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value) is None
    assert rp.service.observation(rp.binding) is not None
    assert all(scope.finished for scope in rp.runtime.scopes)
    assert len([path for path, _ in rp.flow.control.requests if path.endswith("access_token")]) == 1
    assert not rp.flow.control.tokens


@pytest.mark.parametrize(
    "failure",
    [
        "missing_profile",
        "revoked",
        "expired",
        "changed_binding",
        "source",
        "callback_origin",
    ],
)
def test_missing_current_authority_never_creates_workflow(rp, failure):
    if failure == "missing_profile":
        del rp.manifest["rp_logout"]
        rp.resign()
    elif failure == "revoked":
        rp.authority.unlink()
    elif failure == "expired":
        rp.clock[0] = rp.policy.expires_at.timestamp()
    elif failure == "changed_binding":
        rp.manifest["binding"]["config_digest"] = "f" * 64
        rp.resign()
    elif failure == "source":
        rp.guard.valid = False
    else:
        rp.manifest["rp_logout"]["post_logout_redirect_uri"] = "https://other-console.example.test" + RP_CALLBACK_PATH
        rp.resign()
    before = len(rp.flow.control.requests)
    assert start(rp) is None
    assert not rp.wire.values
    assert len(rp.flow.control.requests) == before


@pytest.mark.parametrize("failure", ["bad_nonce", "access_substitution", "unexchanged", "raw_flags"])
def test_actual_token_slots_and_consumed_exchange_are_required(rp, failure):
    raw = rp.raw
    if failure == "bad_nonce":
        claims = {
            "iss": rp.config.expected_issuer,
            "sub": rp.flow.local.env[1].subject,
            "aud": rp.config.client_id,
            "iat": rp.clock[0],
            "exp": rp.clock[0] + 300,
            "nonce": "wrong-nonce",
        }
        raw = RawTokens({**raw.payload, "id_token": sign(rp.signer, claims)})
    elif failure == "access_substitution":
        raw = RawTokens({**raw.payload, "id_token": raw.payload["access_token"]})
    elif failure == "unexchanged":
        rp.operation._exchange_started = False
    else:
        raw = SimpleNamespace(payload=raw.payload, passed=True)
    assert start(rp, raw_tokens=raw) is None
    assert not rp.wire.values


@pytest.mark.parametrize(
    "failure",
    [
        "revoke_before_navigation",
        "revoke_before_callback",
        "expired_state",
        "provider_error",
        "source_changed",
        "CAS",
    ],
)
def test_late_failure_or_missing_return_never_produces_observation(rp, failure):
    if failure == "revoke_before_navigation":
        handoff = start(rp)
        assert handoff is not None
        rp.authority.unlink()
        assert (
            rp.service.navigate(
                opaque=handoff.handoff_path.rsplit("/", 1)[1],
                browser_cookie=handoff.cookies[0].value,
            )
            is None
        )
        return
    _, _, navigation, state = navigate(rp)
    assert rp.service.observation(rp.binding) is None
    if failure == "revoke_before_callback":
        rp.authority.unlink()
    elif failure == "expired_state":
        rp.clock[0] += 301
    elif failure == "source_changed":
        rp.guard.valid = False
    elif failure == "CAS":
        rp.wire.fail_cas = True
    result = rp.service.complete_callback(
        state=state,
        browser_cookie=navigation.cookies[-1].value,
        provider_error=failure == "provider_error",
    )
    assert result is None
    assert rp.service.observation(rp.binding) is None


@pytest.mark.parametrize("failure", ["open", "close"])
def test_private_runtime_failure_suppresses_handoff_delivery(rp, failure):
    setattr(rp.runtime, "fail_" + failure, True)
    assert start(rp) is None
    assert not rp.flow.control.tokens


@pytest.mark.parametrize("callback", ["http://localhost:5001", "http://127.0.0.1:5001", "http://[::1]:5001"])
def test_signed_exact_loopback_callback_needs_explicit_development_owner(rp, callback):
    rp.manifest["rp_logout"]["post_logout_redirect_uri"] = callback + RP_CALLBACK_PATH
    rp.resign()
    rp.service._settings = SimpleNamespace(CONSOLE_API_URL=callback, DEPLOY_ENV="DEVELOPMENT")
    assert rp.service._reviewed(rp.binding).rp_logout.post_logout_redirect_uri == callback + RP_CALLBACK_PATH
    rp.service._settings.DEPLOY_ENV = "PRODUCTION"
    with pytest.raises(Exception):
        rp.service._reviewed(rp.binding)


@pytest.mark.parametrize(
    "field,value",
    [
        ("end_session_endpoint", "http://issuer.example.test/api/logout"),
        ("end_session_endpoint", "https://evil.example.test/api/logout"),
        ("end_session_endpoint", "https://issuer.example.test/api/logout?target=evil"),
        ("post_logout_redirect_uri", "http://console.example.test" + RP_CALLBACK_PATH),
        ("post_logout_redirect_uri", "https://console.example.test/elsewhere"),
        (
            "post_logout_redirect_uri",
            "https://user:secret@console.example.test" + RP_CALLBACK_PATH,
        ),
        (
            "post_logout_redirect_uri",
            "https://console.example.test" + RP_CALLBACK_PATH + "#evil",
        ),
        (
            "post_logout_redirect_uri",
            "https://console.example.test/console/api/auth/casdoor/%6cogout/callback",
        ),
        ("passed", True),
    ],
)
def test_signed_optional_profile_is_closed_and_exact(rp, field, value):
    rp.manifest["rp_logout"][field] = value
    rp.resign()
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        rp.service._current_policy(rp.binding)


def test_repository_rejects_aliases_and_preserves_prefixed_atomicity(rp):
    repo = CasdoorRPLogoutRepository(rp.wire)
    opaque = secrets.token_urlsafe(32)
    assert repo.create("handoff", opaque, "first", 30)
    assert repo.read("handoff", opaque) == "first"
    assert not repo.consume("handoff", opaque, "different")
    assert repo.consume("handoff", opaque, "first")
    assert repo.read("handoff", opaque) is None
    with pytest.raises(ValueError, match="^casdoor_rp_logout_invalid$"):
        repo.create("handoff", "B" * 43, "secret", 300)
    with pytest.raises(ValueError, match="^casdoor_rp_logout_invalid$"):
        repo.create("handoff", opaque, "secret", 301)


def test_private_rp_marker_is_exactly_guarded_and_never_a_public_flag(rp):
    ordinary = replace(rp.context, rp_logout_diagnostic=False)
    consumed = replace(rp.consumed, context=ordinary)
    before = len(rp.flow.control.requests)
    assert start(rp, consumed=consumed) is None
    assert len(rp.flow.control.requests) == before
    assert not rp.wire.values
    policy = CookiePolicy(rp.flow.settings.CONSOLE_API_URL)
    with pytest.raises(AuthTransactionError):
        rp.store.create(
            rp.context,
            browser_scope=rp.browser,
            policy=policy,
            guard=lambda: replace(rp.current, rp_logout_diagnostic=False),
        )
    created = rp.store.create(rp.context, browser_scope=rp.browser, policy=policy, guard=lambda: rp.current)
    hinted = rp.store.diagnostic_hint(created.state, browser_scope=rp.browser, transaction_cookie=created.cookie.value)
    assert hinted.rp_logout_diagnostic is True
    with pytest.raises(AuthTransactionError):
        rp.store.consume(
            created.state,
            browser_scope=rp.browser,
            transaction_cookie=created.cookie.value,
            policy=policy,
            guard=lambda: replace(rp.current, rp_logout_diagnostic=False),
        )
    consumed = rp.store.consume(
        created.state,
        browser_scope=rp.browser,
        transaction_cookie=created.cookie.value,
        policy=policy,
        guard=lambda: rp.current,
    )
    assert consumed.context.rp_logout_diagnostic is True
    with pytest.raises(AuthTransactionError):
        rp.store.consume(
            created.state,
            browser_scope=rp.browser,
            transaction_cookie=created.cookie.value,
            policy=policy,
            guard=lambda: rp.current,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"rp_logout_diagnostic": 1},
        {"rp_logout_diagnostic": "true"},
        {"reauth_diagnostic": True},
        {"mode": AuthMode.LOGIN, "source": None, "diagnostic_binding": None},
    ],
)
def test_private_marker_is_strict_diagnostic_only_and_mutually_exclusive(rp, changes):
    with pytest.raises(AuthTransactionError):
        replace(rp.context, **changes)
    with pytest.raises(AuthTransactionError):
        replace(rp.current, **changes).projection()


def source_owner(rp, *, observed=True):
    if observed:
        _, _, navigation, state = navigate(rp)
        assert rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value) is not None
    account = UUID(int=701)
    rp.wire.set("refresh_token:original-request", str(account), ex=180, nx=True)
    rp.wire.set("account_refresh_token:" + str(account), "different-browser", ex=180, nx=True)
    owner = CasdoorSessionService(
        settings=rp.flow.settings,
        secret_key="synthetic-rp-source",
        redis_runtime_factory=rp.runtime,
        rp_logout_service=rp.service,
    )
    candidate = RPLogoutTokenSnapshot(
        rp.binding, rp.policy.proof_fingerprint, rp.raw.payload["id_token"], rp.clock[0] + 180
    )
    seed = SessionProvenanceSeed(
        account, UUID(rp.binding.namespace_id), UUID(rp.binding.revision_id), rp.clock[0] + 180, candidate
    )
    cookie = owner.create(seed=seed, refresh_token="original-request", deadline=time.monotonic() + 5)
    assert cookie is not None
    return owner, account, cookie


def test_signed_profile_alone_retains_no_hint_and_local_logout_still_succeeds(rp):
    owner, account, cookie = source_owner(rp, observed=False)
    record = owner._run(lambda client: owner._matching(client, cookie.value, "original-request", str(account)))
    assert "rp_logout" not in record.metadata
    assert (
        owner.observe(
            account_id=str(account), refresh_token="original-request", opaque=cookie.value
        ).rp_logout_available
        is False
    )
    prepared = owner.prepare_logout(account_id=str(account), refresh_token="original-request", opaque=cookie.value)
    assert prepared is not None
    assert owner.complete_local_logout(prepared) is None
    assert (
        owner.observe(account_id=str(account), refresh_token="original-request", opaque=cookie.value).source
        == "local_only"
    )


def test_prepared_source_survives_local_failure_and_consumes_after_real_revocation(rp):
    owner, account, cookie = source_owner(rp)
    prepared = owner.prepare_logout(account_id=str(account), refresh_token="original-request", opaque=cookie.value)
    assert prepared is not None
    assert (
        owner.observe(
            account_id=str(account), refresh_token="original-request", opaque=cookie.value
        ).rp_logout_available
        is True
    )
    # Original successful local revoke: completion must use the private prepared
    # raw context rather than reauthorize a now-revoked refresh mapping.
    key = rp.wire._key("refresh_token:original-request")
    rp.wire.values.pop(key)
    rp.wire.expiries.pop(key)
    handoff = owner.complete_local_logout(prepared)
    assert handoff is not None
    assert owner.complete_local_logout(prepared) is None
    assert (
        rp.service.navigate(opaque=handoff.handoff_path.rsplit("/", 1)[1], browser_cookie=handoff.cookies[0].value)
        is not None
    )


@pytest.mark.parametrize("failure", ["concurrent_refresh", "revoke", "expired", "private_close"])
def test_late_optional_failure_keeps_local_success_without_provider_navigation(rp, failure):
    owner, account, cookie = source_owner(rp)
    prepared = owner.prepare_logout(account_id=str(account), refresh_token="original-request", opaque=cookie.value)
    assert prepared is not None
    if failure == "concurrent_refresh":
        rp.wire.set("refresh_token:new-request", str(account), ex=180, nx=True)
        observer = owner.refresh_observer(cookie.value)
        observer.before_rotation(refresh_token="original-request", account_id=str(account))
        observer.after_rotation(refresh_token="new-request", account_id=str(account))
        assert observer.clear_cookie is False
    elif failure == "revoke":
        rp.authority.unlink()
    elif failure == "expired":
        rp.clock[0] += 181
    else:
        rp.runtime.fail_close = True
    assert owner.complete_local_logout(prepared) is None


def test_true_ordinary_factory_retains_original_native_slot_only_after_observation(rp):
    _, _, navigation, state = navigate(rp)
    assert rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value) is not None
    from models.casdoor_extend import CasdoorIntegrationExtend

    with rp.flow.local.session.begin():
        integration = rp.flow.local.session.query(CasdoorIntegrationExtend).one()
        # Controlled offline active pointer, never a claim of real activation.
        integration.active_revision_id = str(rp.revision_id)
    context = replace(
        rp.flow.local.env[1], revision_id=rp.revision_id, active_revision_id=rp.revision_id, config_digest=rp.digest
    )
    rp.flow.local.env = (rp.flow.local.env[0], context, *rp.flow.local.env[2:])
    rp.flow.local.config = rp.config
    rp.flow.service._session_service = CasdoorSessionService(
        settings=rp.flow.settings,
        secret_key="synthetic-rp-source",
        redis_runtime_factory=rp.runtime,
        rp_logout_service=rp.service,
    )

    def bridge_actual_token_writes():
        assert all(scope.finish_calls == 1 for scope in rp.flow.control.runtime_scopes)
        for key, ttl, value in rp.flow.control.tokens:
            rp.wire.set(key, value, ex=ttl.total_seconds(), nx=True)

    rp.runtime.before_open = bridge_actual_token_writes
    # Start a fresh ordinary HTTP capture epoch. The prior diagnostic remains
    # only in the actual RP observation, never in ordinary transaction inputs.
    rp.flow.control.created.clear()
    rp.flow.control.consumed.clear()
    rp.flow.control.operations.clear()
    rp.flow.control.requests.clear()
    scope, _ = begin(rp.flow)
    result = complete(rp.flow, scope)
    assert result.tokens is not None
    assert result.source_cookie is not None
    assert result.provenance.rp_logout is not None
    owner = rp.flow.service._session_service
    record = owner._run(
        lambda client: owner._matching(
            client, result.source_cookie.value, result.tokens.refresh_token, result.provenance.account_id
        )
    )
    assert record is not None
    assert record.metadata["rp_logout"]["id_token"] == result.provenance.rp_logout.id_token
    assert record.metadata["rp_logout"]["proof_fingerprint"] == rp.policy.proof_fingerprint
    assert record.metadata["expires_at"] <= rp.service.observation(rp.binding).expires_at.timestamp()
    assert result.provenance.rp_logout.id_token not in record.raw


def test_anonymous_retry_rotates_one_use_handoff_state_without_extending_source(rp):
    owner, account, cookie = source_owner(rp)
    prepared = owner.prepare_logout(account_id=str(account), refresh_token="original-request", opaque=cookie.value)
    handoff = owner.complete_local_logout(prepared)
    assert handoff is not None
    grant_id, retry_browser = handoff.cookies[1].value, handoff.cookies[2].value
    opaque = handoff.handoff_path.rsplit("/", 1)[1]
    navigation = rp.service.navigate(opaque=opaque, browser_cookie=handoff.cookies[0].value)
    assert navigation is not None
    old_state = parse_qs(urlsplit(navigation.location).query)["state"][0]
    rp.clock[0] += 1
    assert rp.service.retry(opaque=grant_id, browser_cookie=secrets.token_urlsafe(32)) is None
    retried = rp.service.retry(opaque=grant_id, browser_cookie=retry_browser)
    assert retried is not None
    assert retried.handoff_path != handoff.handoff_path
    assert retried.cookies[0].max_age < handoff.cookies[0].max_age
    assert rp.service.complete_callback(state=old_state, browser_cookie=navigation.cookies[-1].value) is None
    navigation = rp.service.navigate(
        opaque=retried.handoff_path.rsplit("/", 1)[1], browser_cookie=retried.cookies[0].value
    )
    assert navigation is not None
    state = parse_qs(urlsplit(navigation.location).query)["state"][0]
    completed = rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value)
    assert completed is not None
    assert completed.completed is True
    assert all(directive.max_age == 0 for directive in completed.cookies)
    assert rp.service.retry(opaque=grant_id, browser_cookie=retry_browser) is None
    assert rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value) is None


@pytest.mark.parametrize("failure", ["provider_error", "no_return", "revoked", "expired", "limit"])
def test_failed_provider_return_is_local_only_with_bounded_explicit_retry(rp, failure):
    owner, account, cookie = source_owner(rp)
    prepared = owner.prepare_logout(account_id=str(account), refresh_token="original-request", opaque=cookie.value)
    handoff = owner.complete_local_logout(prepared)
    assert handoff is not None
    grant_id, retry_browser = handoff.cookies[1].value, handoff.cookies[2].value
    navigation = rp.service.navigate(
        opaque=handoff.handoff_path.rsplit("/", 1)[1], browser_cookie=handoff.cookies[0].value
    )
    assert navigation is not None
    state = parse_qs(urlsplit(navigation.location).query)["state"][0]
    if failure == "provider_error":
        completed = rp.service.complete_callback(
            state=state, browser_cookie=navigation.cookies[-1].value, provider_error=True
        )
        assert completed is not None
        assert completed.completed is False
    elif failure == "revoked":
        rp.authority.unlink()
    elif failure == "expired":
        rp.clock[0] += 181
    elif failure == "limit":
        for _ in range(5):
            assert rp.service.retry(opaque=grant_id, browser_cookie=retry_browser) is not None
    retried = rp.service.retry(opaque=grant_id, browser_cookie=retry_browser)
    if failure in ("provider_error", "no_return"):
        assert retried is not None
    else:
        assert retried is None
    assert (
        owner.observe(account_id=str(account), refresh_token="original-request", opaque=cookie.value).source
        == "local_only"
    )
    assert not rp.flow.control.tokens


def test_callback_runtime_cleanup_failure_cannot_publish_protocol_observation(rp):
    _, _, navigation, state = navigate(rp)
    rp.runtime.fail_close = True
    assert rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value) is None
    rp.runtime.fail_close = False
    assert rp.service.observation(rp.binding) is None
    assert rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value) is None


def test_observation_preserves_exact_submicrosecond_epoch_without_future_rounding(rp):
    # Move forward past the real consumed transaction's auth_started_at while
    # retaining a NumericDate that rounds upward at the microsecond boundary.
    rp.clock[0] = math.floor(rp.clock[0]) + 1 + 0.1234567
    _, _, navigation, state = navigate(rp)
    assert rp.service.complete_callback(state=state, browser_cookie=navigation.cookies[-1].value) is not None
    observed = rp.service.observation(rp.binding)
    assert observed is not None
    opaque = rp.service._observation_id(rp.policy.proof_fingerprint)
    repo = CasdoorRPLogoutRepository(rp.wire)
    binding, stored = rp.service._decode("observation", opaque, repo.read("observation", opaque))
    assert binding == rp.binding
    assert stored["checked_at"] == rp.clock[0]
    assert stored["checked_at"] <= rp.clock[0]


@pytest.mark.parametrize("untrusted", [None, False, True, {"passed": True}])
def test_diagnostic_guard_must_return_actual_current_context_not_flags(rp, untrusted):
    rp.service._diagnostic_guard = lambda binding, projection: untrusted
    assert start(rp) is None
    assert not rp.wire.values


def test_optional_production_factory_is_lazy_and_cannot_break_original_session_owner(monkeypatch):
    settings = SimpleNamespace(SECRET_KEY="", CONSOLE_API_URL="invalid-cookie-config", DEPLOY_ENV="PRODUCTION")
    service = CasdoorRPLogoutService.for_production(
        settings, current_policy=lambda binding: None, diagnostic_guard=None
    )
    assert type(service) is CasdoorRPLogoutService

    def unavailable(settings):
        raise RuntimeError("private RP factory canary")

    monkeypatch.setattr("services.casdoor_rp_logout_service_extend.CasdoorRedisRuntimeFactory", unavailable)
    assert (
        CasdoorRPLogoutService.for_production(settings, current_policy=lambda binding: None, diagnostic_guard=None)
        is None
    )
