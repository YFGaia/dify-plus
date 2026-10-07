"""Actual HTTP-service -> fresh store/gateway/signed C2/I19/issuer, offline only.

SQLite and wire models are not physical Redis, mounted transport or G0 evidence.
The private service subclass overrides exactly the two approved production seams.
"""

import json
import math
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import jwt
import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor import auth_transactions as auth
from core.casdoor.claims import NativeTokenContract, NativeTokenSchema
from core.casdoor.configuration import PublicCertificatePolicy
from core.casdoor.gateway import GatewayOperation
from core.casdoor.permissions import CasdoorManagementPolicy
from core.casdoor.request_safety import CasdoorRequestLimiter
from core.casdoor.role_graph import DirectorySnapshotContract
from core.helper import ssrf_proxy
from models.account import Account, AccountStatus, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorConfigRevisionExtend,
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorManagedMembershipExtend,
    CasdoorNamespaceExtend,
    CasdoorValidationExtend,
)
from pydantic import SecretStr
from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from services.casdoor_local_http_service_extend import CasdoorLocalHttpService, _RestrictedNavigation
from services.casdoor_local_login_coordinator_service_extend import CasdoorLocalLoginCoordinatorService
from services.casdoor_local_login_finalization_service_extend import CasdoorLocalLoginFinalizationService
from services.entities.account_login_entities import AuthTokenPair
from sqlalchemy.orm import Session
from test_auth_initialization import UNSET
from test_auth_initialization import InitializationRedis as OriginalInitializationRedis
from test_casdoor_local_login_coordinator_service_extend import signing as original_signing
from test_casdoor_local_login_service_extend import local as original_local
from test_casdoor_local_login_service_extend import login_env as login_env
from test_casdoor_login_account_service_extend import seed
from test_claims import sign
from test_gateway import SyntheticStrategy, response
from test_leases import FakeRedisLua

local_fixture = original_local
signing = original_signing


class InitializationRedis(OriginalInitializationRedis):
    """Casdoor HTTP wire adds only result scripts; auth/A0 stay inherited."""

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
                    if type(record) is dict and set(record) == {
                        "schema_version",
                        "owner",
                        "scope",
                        "context",
                        "issued_at",
                        "expires_at",
                        "data",
                    }:
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
def http_flow(local_fixture, signing, monkeypatch):
    from contextlib import nullcontext

    local = local_fixture
    key, pin = signing
    for model in (CasdoorAuditExtend, CasdoorValidationExtend):
        model.__table__.create(local.engine)
    opened = set()
    reads = []

    class TrackedSession(Session):
        def __init__(self):
            super().__init__(local.engine)
            opened.add(self)
            reads.append(self)

        def close(self):
            super().close()
            opened.discard(self)

    configuration = CasdoorConfigurationService(
        session_factory=TrackedSession,
        management_policy=CasdoorManagementPolicy(),
        secret_key="offline-http-key",
        rbac_enabled=False,
    )
    config = local.config.model_copy(
        update={
            "certificates": (
                PublicCertificatePolicy(
                    pem=pin.pem,
                    kid=pin.kid,
                    not_before=pin.not_before,
                    accept_until=pin.accept_until,
                ),
            )
        }
    )
    with local.session.begin():
        owner = configuration._repository(local.session)
        integration = owner._integration()
        snapshot = owner.save_draft(
            config,
            etag=integration.etag,
            actor_account_id=UUID(int=700),
            secret=SecretStr("offline-http-client-secret"),
        )
        integration.active_revision_id = str(snapshot.draft_revision_id)
        local.session.flush()
        revision = owner._revision(integration.id, integration.active_revision_id)
        context = replace(
            local.env[1],
            revision_id=UUID(revision.id),
            active_revision_id=UUID(revision.id),
            config_digest=revision.config_digest,
        )
    local.env = (local.session, context, *local.env[2:])
    local.config = config
    control = SimpleNamespace(
        created=[],
        consumed=[],
        operations=[],
        requests=[],
        tokens=[],
        billing=[],
        limits=[],
        budgets=[],
        fail_token=None,
        limit_count=0,
        hook=lambda path: None,
        bad_nonce=False,
        token_variant=None,
        bad_roles=False,
        role_read_failure=False,
        bad_discovery=False,
        fail_limit=False,
        fail_billing=False,
        fail_release=False,
        cancel_release=None,
        token_hook=lambda: None,
    )
    init_wire = InitializationRedis()
    lease_wire = FakeRedisLua(monotonic)

    def external():
        assert not opened and not local.session.in_transaction()

    class RedisWire:
        def _get_prefix(self):
            return "offline-http"

        def eval(self, script, numkeys, *args):
            external()
            return init_wire.eval(script, numkeys, *args)

        def zremrangebyscore(self, name, lower, upper):
            external()
            control.limits.append(name)
            if control.fail_limit:
                raise TimeoutError("private rate sentinel")
            return 0

        def zcard(self, name):
            return control.limit_count

        def zadd(self, name, mapping):
            assert mapping
            return 1

        def expire(self, name, ttl):
            return True

        def setex(self, name, ttl, value):
            external()
            assert lease_wire.data
            control.tokens.append((name, ttl, value))
            control.token_hook()
            if len(control.tokens) == control.fail_token:
                raise TimeoutError("private token sentinel")

        def delete(self, *names):
            pytest.fail("HTTP service cannot revoke account-wide sessions")

    redis = RedisWire()

    class RequestRedisFacade:
        """Explicit composite synthetic wire; production never uses this seam."""

        def __getattr__(self, name):
            return getattr(redis, name)

        def lock(self, name, **kwargs):
            return lease_wire.lock(name, **kwargs)

        def _casdoor_cleanup_scope(self):
            return nullcontext()

    control.runtime_scopes = []
    control.fail_close = False

    class SyntheticScope:
        def __init__(self, deadline):
            self.client = RequestRedisFacade()
            self.deadline = deadline
            self.finish_calls = 0

        def finish(self):
            self.finish_calls += 1
            return not control.fail_close

    class SyntheticRuntime:
        def open(self, *, deadline):
            scope = SyntheticScope(deadline)
            control.runtime_scopes.append(scope)
            return scope

    class BillingWire:
        def delete(self, name):
            external()
            assert lease_wire.data
            control.billing.append(name)
            if control.fail_billing:
                raise TimeoutError("private billing sentinel")
            return 0

    monkeypatch.setattr("services.billing_service.redis_client", BillingWire())
    monkeypatch.setattr(dify_config, "SECRET_KEY", "offline-http-issuer-key-for-synthetic-tests")
    finalizer = CasdoorLocalLoginFinalizationService(
        session_factory=TrackedSession,
        configuration_factory=configuration._repository,
        session_gateway=RedisAccountSessionGateway(redis=redis),
    )
    coordinator = CasdoorLocalLoginCoordinatorService(
        session_factory=TrackedSession,
        configuration_service=configuration,
        account_owner=local.env[3],
        redis_client=lease_wire,
        finalization_service=finalizer,
    )
    settings = SimpleNamespace(
        CONSOLE_API_URL="https://console.example.test",
        CONSOLE_WEB_URL="https://web.example.test",
        DEPLOY_ENV="PRODUCTION",
        RBAC_ENABLED=False,
    )

    class _OfflineHttpService(CasdoorLocalHttpService):
        def _coordinator_for_request(self):
            return coordinator

        def _directory_inputs(self, operation, *, reviewed=False):
            strategy = SyntheticStrategy(operation)
            return (
                NativeTokenContract(
                    NativeTokenSchema.FLAT_USER_V1,
                    context.issuer,
                    context.organization,
                    context.application,
                    context.client_id,
                    "a" * 64,
                    "b" * 64,
                ),
                DirectorySnapshotContract(strategy.proof, "d" * 64, "e" * 64),
                strategy,
            )

    service = _OfflineHttpService(
        session_factory=TrackedSession,
        configuration_service=configuration,
        account_activation=local.env[3]._activation,
        redis_client=redis,
        settings=settings,
        redis_runtime_factory=SyntheticRuntime(),
    )
    original_create, original_consume = auth.AuthTransactionStore.create, auth.AuthTransactionStore.consume
    original_operation = GatewayOperation.__post_init__
    original_limit = CasdoorRequestLimiter.check_and_increment

    def limited(limiter, *args, **kwargs):
        control.budgets.append(kwargs["deadline"])
        return original_limit(limiter, *args, **kwargs)

    def create(store, *args, **kwargs):
        result = original_create(store, *args, **kwargs)
        control.created.append(result)
        return result

    def consume(store, *args, **kwargs):
        result = original_consume(store, *args, **kwargs)
        control.consumed.append(result)
        return result

    def operation_start(operation):
        original_operation(operation)
        control.operations.append(operation)

    monkeypatch.setattr(auth.AuthTransactionStore, "create", create)
    monkeypatch.setattr(auth.AuthTransactionStore, "consume", consume)
    monkeypatch.setattr(GatewayOperation, "__post_init__", operation_start)
    monkeypatch.setattr(CasdoorRequestLimiter, "check_and_increment", limited)

    def transport(method, url, **kwargs):
        external()
        operation = control.operations[-1]
        assert kwargs["deadline"] == operation.deadline
        assert urlsplit(url).scheme == "https"
        path = urlsplit(url).path
        control.requests.append((path, operation.deadline))
        control.hook(path)
        if path.endswith("openid-configuration"):
            return response(
                {
                    "issuer": config.expected_issuer,
                    "authorization_endpoint": (
                        "http://localhost" if control.bad_discovery else config.browser_frontend_url
                    )
                    + "/login/oauth/authorize",
                    "token_endpoint": config.backend_api_url + "/api/login/oauth/access_token",
                    "userinfo_endpoint": config.backend_api_url + "/api/userinfo",
                }
            )
        if path.endswith("access_token"):
            now = datetime.now(UTC).timestamp()
            common = {"iss": context.issuer, "aud": context.client_id, "iat": now, "exp": now + 300}
            if control.token_variant == "wrong_issuer":
                common["iss"] = "https://wrong-issuer.example.test"
            # Bind this signed response to THIS actual create, never chain's
            # already-consumed transaction or a prior operation's deadline.
            nonce = "bad-current-nonce" if control.bad_nonce else control.created[-1].nonce
            token_response = {
                "token_type": "Bearer",
                "access_token": sign(key, common | {"owner": context.organization, "id": context.subject}),
            }
            if control.token_variant != "missing_id_token":
                token_response["id_token"] = sign(key, common | {"sub": context.subject, "nonce": nonce})
            return response(token_response)
        if path.endswith("userinfo"):
            return response(
                {"sub": context.subject, "name": "Remote Name", "email": "new@example.test", "email_verified": True}
            )
        data = {
            "/api/get-user": {
                "owner": context.organization,
                "id": context.subject,
                "name": "person",
                "isForbidden": False,
                "isDeleted": False,
                "groups": [],
            },
            "/api/get-organization": {"owner": "admin", "name": context.organization, "accountItems": []},
            "/api/get-roles": [
                {
                    "owner": context.organization,
                    "name": "operators",
                    "isEnabled": True,
                    "roles": [],
                    "users": [context.organization + "/person"],
                    "groups": [],
                }
            ],
        }
        if path == "/api/get-roles":
            if control.role_read_failure:
                return response({"status": "ok", "data": {"unexpected": "not-a-complete-list"}})
            if control.bad_roles:
                data[path] = [
                    {
                        "owner": context.organization,
                        "name": "operators",
                        "isEnabled": True,
                        "roles": [context.organization + "/missing-role"],
                        "users": [context.organization + "/person"],
                        "groups": [],
                    }
                ]
        return response({"status": "ok", "data": data[path]})

    def release_hook(command, key):
        if command == "before_release":
            if control.cancel_release is not None:
                raise control.cancel_release
            if control.fail_release:
                raise TimeoutError("private cleanup sentinel")

    lease_wire.hook = release_hook
    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    yield SimpleNamespace(
        service=service,
        settings=settings,
        local=local,
        config=configuration,
        control=control,
        init=init_wire,
        lease=lease_wire,
        redis=redis,
        coordinator=coordinator,
        opened=opened,
        reads=reads,
        finalizer=finalizer,
    )
    assert not opened


def begin(flow, **navigation):
    first = flow.service.start(browser_scope=None, server_ip="192.0.2.7", **navigation)
    assert first.status == 303 and first.error is None
    assert not flow.control.created and not flow.control.operations and not flow.control.requests
    scope = first.cookies[0].value
    handle = parse_qs(urlsplit(first.redirect).query)["init"][0]
    second = flow.service.start(browser_scope=scope, init_handle=handle, server_ip="192.0.2.7")
    assert second.status == 302 and second.error is None
    return scope, second


def complete(flow, scope, **changes):
    created = flow.control.created[-1]
    return flow.service.complete(
        **(
            dict(
                state=created.state,
                transaction_cookie=created.cookie.value,
                browser_scope=scope,
                code="offline-http-code",
                server_ip="192.0.2.7",
            )
            | changes
        )
    )


def counts(flow):
    with Session(flow.local.engine) as reader:
        return {
            model: reader.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (
                Account,
                AccountMoneyExtend,
                CasdoorIdentityExtend,
                TenantAccountJoin,
            )
        }


@pytest.mark.parametrize("development", [False, True])
def test_actual_scope_roundtrip_signed_login_finalizes_and_issues(http_flow, development):
    f = http_flow
    if development:
        f.settings.CONSOLE_API_URL = "http://127.0.0.1:5001"
        f.settings.CONSOLE_WEB_URL = "http://localhost:3000"
        f.settings.DEPLOY_ENV = "DEVELOPMENT"
    scope, redirect = begin(f, return_path="/apps/example", locale="zh-Hans", timezone="Asia/Shanghai")
    authorization = parse_qs(urlsplit(redirect.redirect).query)
    assert authorization["client_id"] == [f.local.config.client_id]
    assert authorization["redirect_uri"] == [f.settings.CONSOLE_API_URL + auth.COOKIE_PATH + "/callback"]
    assert authorization["nonce"] == [f.control.created[0].nonce]
    assert authorization["code_challenge_method"] == ["S256"]
    result = complete(f, scope)
    assert result.status == 302 and result.error is None and type(result.tokens) is AuthTokenPair
    assert result.redirect == f.settings.CONSOLE_WEB_URL + "/apps/example"
    assert (
        result.phases.local_outcome,
        result.phases.finalization_outcome,
        result.phases.token_outcome,
        result.phases.cleanup_released,
    ) == ("committed", "committed", "issued", True)
    assert len(f.control.consumed) == 1 and f.control.consumed[0].nonce == f.control.created[0].nonce
    assert len(f.control.operations) == 2
    callback_budget = f.control.operations[-1].deadline
    assert callback_budget == f.control.budgets[-1]
    assert f.control.operations[0].deadline == f.control.budgets[1]
    assert all(budget == callback_budget for path, budget in f.control.requests[1:])
    assert len([path for path, _ in f.control.requests if path.endswith("access_token")]) == 1
    pair = result.tokens
    claims = jwt.decode(pair.access_token, dify_config.SECRET_KEY, algorithms=["HS256"])
    assert claims["sub"] == "Console API Passport" and pair.csrf_token and len(pair.refresh_token) == 128
    assert len(f.control.tokens) == 2 and not f.lease.data
    assert counts(f) == {Account: 1, AccountMoneyExtend: 1, CasdoorIdentityExtend: 1, TenantAccountJoin: 2}
    with Session(f.local.engine) as reader:
        account = reader.get(Account, claims["user_id"])
        assert account.status is AccountStatus.ACTIVE and account.initialized_at
        assert account.last_login_ip == "192.0.2.7" and account.last_login_at
        assert account.interface_language == "zh-Hans" and account.timezone == "Asia/Shanghai"
        assert set(reader.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))) == {
            CasdoorFinalizationState.FINALIZED,
        }
        assert reader.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 1
    assert result.cookies == (f.control.consumed[0].clear_cookie,)
    assert result.cookies[0].secure is (not development)
    assert pair.access_token not in result.redirect and "offline-http-client-secret" not in repr(result)
    assert "casdoor_tx_" not in repr(result) and "casdoor_browser_scope" not in repr(redirect)


def test_existing_initialized_preferences_are_preserved(http_flow):
    f = http_flow
    seed(f.local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    scope, _ = begin(f, locale="zh-Hans", timezone="Asia/Shanghai")
    assert complete(f, scope).tokens
    with Session(f.local.engine) as reader:
        account = reader.scalar(sa.select(Account))
        assert (account.interface_language, account.timezone, account.interface_theme) == (
            "ja-JP",
            "Asia/Tokyo",
            "dark",
        )


def test_invalid_navigation_uses_original_defaults_and_existing_scope(http_flow):
    f = http_flow
    scope = auth.new_browser_scope(auth.CookiePolicy(f.settings.CONSOLE_API_URL)).value
    result = f.service.start(
        browser_scope=scope,
        return_path="https://evil.example",
        locale="invalid",
        timezone="invalid",
        server_ip="192.0.2.7",
    )
    assert result.status == 302
    completed = complete(f, scope)
    assert completed.redirect == f.settings.CONSOLE_WEB_URL + "/apps"
    stored = f.control.consumed[0].context
    assert (stored.locale, stored.timezone) == (None, None)
    with Session(f.local.engine) as reader:
        account = reader.scalar(sa.select(Account))
        assert (account.interface_language, account.timezone) == ("en-US", "America/New_York")
    assert all(call[0] != auth.CREATE_INITIALIZATION_SCRIPT for call in f.init.calls)


def test_init_mismatch_and_extra_navigation_do_not_create_or_rotate(http_flow):
    f = http_flow
    first = f.service.start(browser_scope=None, return_path="/apps/stored", server_ip="192.0.2.7")
    handle = parse_qs(urlsplit(first.redirect).query)["init"][0]
    scope = first.cookies[0].value
    wrong = auth.new_browser_scope(auth.CookiePolicy(f.settings.CONSOLE_API_URL)).value
    for extra in ({"browser_scope": wrong}, {"browser_scope": scope, "return_path": "/apps/other"}):
        result = f.service.start(init_handle=handle, server_ip="192.0.2.7", **extra)
        assert result.status == 400 and not result.cookies and not result.redirect
        assert not f.control.created and not f.control.requests
    correct = f.service.start(browser_scope=scope, init_handle=handle, server_ip="192.0.2.7")
    assert correct.status == 302
    replay = f.service.start(browser_scope=scope, init_handle=handle, server_ip="192.0.2.7")
    assert replay.status == 400 and len(f.control.created) == 1
    assert complete(f, scope).redirect.endswith("/apps/stored")


def test_provider_cancellation_spends_once_without_exchange_or_business(http_flow, caplog):
    f = http_flow
    scope, _ = begin(f)
    before = len(f.control.requests)
    result = complete(f, scope, code=None, provider_error="private provider detail")
    assert result.status == 400 and result.tokens is None and result.cookies[0].max_age == 0
    assert len(f.control.consumed) == 1 and len(f.control.requests) == before
    assert set(counts(f).values()) == {0} and not f.control.tokens
    assert complete(f, scope).tokens is None and len(f.control.requests) == before
    assert "private provider detail" not in caplog.text


@pytest.mark.parametrize("mode", ["default-http", "lowercase-development", "http-provider"])
def test_callback_development_permission_never_weakens_provider_policy(http_flow, mode):
    f = http_flow
    f.settings.CONSOLE_API_URL = "http://localhost:5001"
    if mode == "lowercase-development":
        f.settings.DEPLOY_ENV = "development"
    if mode == "http-provider":
        f.settings.DEPLOY_ENV = "DEVELOPMENT"
        f.control.bad_discovery = True
        first = f.service.start(browser_scope=None, server_ip="192.0.2.7")
        result = f.service.start(
            browser_scope=first.cookies[0].value,
            init_handle=parse_qs(urlsplit(first.redirect).query)["init"][0],
            server_ip="192.0.2.7",
        )
    else:
        result = f.service.start(browser_scope=None, server_ip="192.0.2.7")
        assert not f.init.calls and not f.control.requests
    assert result.error and not result.redirect and not result.cookies and not f.control.tokens


@pytest.mark.parametrize("failure", ["rate", "rate-storage", "auth-storage"])
def test_fixed_429_and_503_not_masked_as_redirects(http_flow, failure):
    f = http_flow
    f.control.limit_count = 20 if failure == "rate" else 0
    f.control.fail_limit = failure == "rate-storage"
    f.init.before_fault = failure == "auth-storage"
    result = f.service.start(browser_scope=None, server_ip="192.0.2.7")
    assert result.status == (429 if failure == "rate" else 503)
    assert result.error.retry_after_seconds == (60 if failure == "rate" else None)
    assert not result.redirect and not result.cookies and not f.control.requests
    if failure != "auth-storage":
        assert not f.reads and not f.init.calls


def test_production_factory_and_empty_key_are_lazy_zero_io(http_flow, monkeypatch):
    f = http_flow
    from enums import DeploymentEdition

    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.CLOUD)
    configuration = CasdoorConfigurationService(
        session_factory=lambda: pytest.fail("production missing G0 touched SQL"),
        management_policy=CasdoorManagementPolicy(),
        secret_key="",
        rbac_enabled=False,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("production missing G0 touched crypto")

    monkeypatch.setattr("services.casdoor_local_http_service_extend.CasdoorCrypto", forbidden)
    service = CasdoorLocalHttpService(
        session_factory=configuration._session_factory,
        configuration_service=configuration,
        account_activation=f.service._account_activation,
        redis_client=f.redis,
        settings=f.settings,
    )
    first = service.start(browser_scope=None, server_ip="192.0.2.7")
    state = auth.new_browser_scope(auth.CookiePolicy(f.settings.CONSOLE_API_URL)).value
    second = service.complete(
        state=state, transaction_cookie=None, browser_scope=None, code="private-code", server_ip="192.0.2.7"
    )
    assert first.error.code.value == second.error.code.value == "config_conflict"
    assert not f.reads and not f.init.calls and not f.control.limits and not f.control.requests
    assert not first.cookies and second.cookies[0].name == auth.transaction_cookie_name(state)
    assert second.tokens is None


def mutate_active(flow, kind):
    with Session(flow.local.engine) as session, session.begin():
        owner = flow.config._repository(session)
        integration = owner._integration()
        if kind in ("draft", "revision"):
            snapshot = owner.save_draft(
                flow.local.config.model_copy(update={"button_text": "Changed draft"}),
                etag=integration.etag,
                actor_account_id=UUID(int=700),
                secret=None,
            )
            if kind == "revision":
                integration.active_revision_id = str(snapshot.draft_revision_id)
        elif kind == "disable":
            integration.enabled = False
        elif kind == "fence":
            session.execute(sa.update(CasdoorNamespaceExtend).values(fence_epoch=1))
        elif kind == "digest":
            session.execute(sa.update(CasdoorConfigRevisionExtend).values(config_digest="0" * 64))
        elif kind == "rbac":
            flow.settings.RBAC_ENABLED = True
        elif kind == "runtime":
            flow.settings.CONSOLE_WEB_URL = "https://changed.example.test"


@pytest.mark.parametrize("drift", ["draft", "revision", "disable", "fence", "digest", "rbac", "runtime"])
def test_post_exchange_fresh_guard_allows_unrelated_draft_only(http_flow, drift):
    f = http_flow
    scope, _ = begin(f)
    f.control.hook = lambda path: mutate_active(f, drift) if path.endswith("access_token") else None
    result = complete(f, scope)
    assert len(f.control.consumed) == 1
    assert len([p for p, _ in f.control.requests if p.endswith("access_token")]) == 1
    if drift == "draft":
        assert result.tokens and result.status == 302
    else:
        assert result.tokens is None and result.redirect is None and result.error
        assert set(counts(f).values()) == {0} and not f.control.tokens and not f.lease.sets
        assert result.cookies == (f.control.consumed[0].clear_cookie,)


def test_initialization_postguard_drift_returns_no_unconfirmed_cookie(http_flow):
    f = http_flow
    f.init.after_eval = lambda: mutate_active(f, "fence")
    result = f.service.start(browser_scope=None, server_ip="192.0.2.7")
    assert result.error and result.cookies == () and result.redirect is None
    assert len(f.init.records) == 1 and not f.control.created and not f.control.requests


def test_actual_signature_nonce_failure_stops_before_business(http_flow, caplog):
    f = http_flow
    scope, _ = begin(f)
    f.control.bad_nonce = True
    result = complete(f, scope)
    assert result.tokens is None and result.error
    assert result.phases.local_outcome == "not_started"
    assert set(counts(f).values()) == {0} and not f.control.tokens
    assert "bad-current-nonce" not in caplog.text


@pytest.mark.parametrize("failure", ["issuer1", "issuer2", "cleanup"])
def test_real_pending_partial_issue_and_cleanup_never_deliver(http_flow, failure, caplog):
    f = http_flow
    scope, _ = begin(f)
    f.control.fail_token = {"issuer1": 1, "issuer2": 2}.get(failure)
    f.control.fail_release = failure == "cleanup"
    result = complete(f, scope)
    if failure == "cleanup":
        assert type(result) is _RestrictedNavigation and not hasattr(result, "tokens")
        assert result.redirect == f.settings.CONSOLE_WEB_URL + "/signin/casdoor-result?handoff=" + result.handoff
        assert len(result.handoff) == 43 and len(result.cookies) == 3
        assert result.cookies[0] == f.control.consumed[0].clear_cookie
        redeemed = f.service.restricted_result(
            handoff=result.handoff, browser_scope=scope, result_cookie=result.cookies[1].value, server_ip="192.0.2.7"
        )
        assert redeemed.status == 200 and redeemed.payload.code.value == "authorization_pending"
        assert redeemed.payload.correlation_id == str(result.correlation_id)
    else:
        assert result.tokens is None and result.redirect is None and result.error
        assert result.cookies == (f.control.consumed[0].clear_cookie,)
    assert result.phases.local_outcome == "committed"
    assert len(f.control.consumed) == 1
    with Session(f.local.engine) as reader:
        actual = set(reader.scalars(sa.select(CasdoorManagedMembershipExtend.finalization)))
    assert actual == {CasdoorFinalizationState.FINALIZED}
    assert result.phases.finalization_outcome == "committed"
    assert result.phases.token_outcome == ("issued" if failure == "cleanup" else "unknown")
    assert result.phases.cleanup_released is (failure != "cleanup")
    assert "private" not in caplog.text


def test_local_community_login_skips_cloud_billing_cache(http_flow):
    f = http_flow
    scope, _ = begin(f)

    result = complete(f, scope)

    assert result.tokens and result.error is None
    assert result.phases.finalization_outcome == "committed"
    assert result.phases.token_outcome == "issued"
    assert not f.control.billing
    with Session(f.local.engine) as reader:
        assert set(reader.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))) == {
            CasdoorFinalizationState.FINALIZED
        }


def test_committed_i19_ack_failure_preserves_unknown_phase_no_delivery(http_flow):
    f = http_flow
    scope, _ = begin(f)

    def flushed(session, context):
        if any(isinstance(obj, Account) and obj.last_login_ip == "192.0.2.7" for obj in session.dirty):
            session.info["offline_http_i19"] = True

    def committed(session):
        if session.info.pop("offline_http_i19", False):
            raise RuntimeError("private finalization ACK sentinel")

    sa.event.listen(Session, "after_flush", flushed)
    sa.event.listen(Session, "after_commit", committed)
    try:
        result = complete(f, scope)
    finally:
        sa.event.remove(Session, "after_flush", flushed)
        sa.event.remove(Session, "after_commit", committed)
    assert result.tokens is None and result.redirect is None and not f.control.tokens
    assert result.phases.local_outcome == "committed"
    assert result.phases.finalization_outcome == "unknown" and result.phases.cleanup_released
    with Session(f.local.engine) as reader:
        assert set(reader.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))) == {
            CasdoorFinalizationState.FINALIZED,
        }


@pytest.mark.parametrize("stage", ["provider", "cleanup", "issuer-and-cleanup"])
def test_baseexception_preserves_primary_and_only_safe_transport_attributes(http_flow, stage):
    f = http_flow
    scope, _ = begin(f)

    class Stop(BaseException):
        def __str__(self):
            pytest.fail("primary cancellation must never be serialized")

    primary, secondary = Stop(), Stop()
    if stage == "provider":

        def stop_provider(path):
            if path.endswith("access_token"):
                raise primary

        f.control.hook = stop_provider
    elif stage == "cleanup":
        f.control.cancel_release = primary
    else:

        def stop_issue():
            raise primary

        f.control.token_hook = stop_issue
        f.control.cancel_release = secondary
    with pytest.raises(Stop) as caught:
        complete(f, scope)
    assert caught.value is primary
    assert caught.value._casdoor_clear_cookies == (f.control.consumed[0].clear_cookie,)
    phases = caught.value._casdoor_phase_facts
    assert phases.local_outcome == ("not_started" if stage == "provider" else "committed")
    assert (
        phases.token_outcome == {"provider": "not_started", "cleanup": "issued", "issuer-and-cleanup": "unknown"}[stage]
    )
    assert not hasattr(primary, "tokens")


def test_uncertain_consume_is_not_retried_and_only_this_cookie_is_cleared(http_flow):
    f = http_flow
    scope, _ = begin(f)
    before = len(f.init.calls)
    f.init.lose_reply = True
    result = complete(f, scope)
    assert result.status == 503 and result.tokens is None and len(f.init.calls) == before + 1
    assert not f.control.consumed and len(f.control.requests) == 1
    assert result.cookies[0].name == auth.transaction_cookie_name(f.control.created[-1].state)
    assert not f.init.records


def test_malformed_state_cannot_choose_cookie_name(http_flow):
    f = http_flow
    result = f.service.complete(
        state="arbitrary-session-cookie",
        transaction_cookie="sentinel",
        browser_scope=None,
        code="sentinel",
        server_ip="192.0.2.7",
    )
    assert result.status == 400 and result.cookies == () and result.tokens is None
    assert not f.init.calls and not f.control.requests


def test_expired_shared_budget_after_exchange_cannot_enter_business(http_flow, monkeypatch):
    import time

    f = http_flow
    scope, _ = begin(f)

    def late(path):
        if path.endswith("access_token"):
            expired = f.control.operations[-1].deadline + 1
            monkeypatch.setattr(time, "monotonic", lambda: expired)

    f.control.hook = late
    result = complete(f, scope)
    assert result.status == 503 and result.tokens is None
    assert len(f.control.consumed) == 1 and not f.lease.sets and not f.control.tokens
    assert set(counts(f).values()) == {0}
    assert f.control.operations[-1].deadline == f.control.budgets[-1]


def test_original_lease_failure_retains_pending_public_code(http_flow):
    f = http_flow
    scope, _ = begin(f)

    def unavailable(command, key):
        if command == "before_set":
            raise TimeoutError("private lease failure")

    f.lease.hook = unavailable
    result = complete(f, scope)
    assert type(result) is _RestrictedNavigation and not hasattr(result, "tokens")
    assert len(result.cookies) == 3
    assert result.redirect == f.settings.CONSOLE_WEB_URL + "/signin/casdoor-result?handoff=" + result.handoff
    redeemed = f.service.restricted_result(
        handoff=result.handoff, browser_scope=scope, result_cookie=result.cookies[1].value, server_ip="192.0.2.7"
    )
    assert redeemed.status == 200 and redeemed.payload.code.value == "authorization_pending"
    assert not f.control.tokens
    assert set(counts(f).values()) == {0}


def test_application_services_shares_exact_original_owners_and_stays_lazy(monkeypatch):
    from unittest.mock import MagicMock

    from enums import DeploymentEdition
    from extensions.ext_application_services import build_application_services
    from extensions.ext_redis import RedisClientWrapper
    from sqlalchemy.orm import sessionmaker

    class NoSQL(sessionmaker):
        def __call__(self, **kwargs):
            pytest.fail("unexpected SQL at build")

    monkeypatch.setattr(dify_config, "SECRET_KEY", "")
    factory = NoSQL()
    redis = MagicMock(spec=RedisClientWrapper)
    services = build_application_services(
        database_client=factory, deployment_edition=DeploymentEdition.COMMUNITY, initialization_password="", redis=redis
    )
    assert type(services.casdoor_local_http) is CasdoorLocalHttpService
    assert services.casdoor_local_http._configuration_service is services.casdoor_configuration
    assert services.casdoor_local_http._account_activation is services.account_activation
    assert services.casdoor_local_http._redis_client is redis
    assert services.casdoor_local_http._settings is dify_config
    assert not redis.mock_calls
