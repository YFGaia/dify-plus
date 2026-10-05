"""Real provenance/refresh owners with encrypted metadata and bounded fake Redis.

Synthetic signed deployment fixture proves composition, not a live IdP rollout.
"""

import math
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from flask import Flask
from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_session_service_extend import CasdoorSessionService, SessionProvenanceSeed
from test_account_login_service import Dependencies
from test_casdoor_local_http_service_extend import begin, complete

pytest_plugins = ("test_casdoor_production_login_policy_extend",)
ACCOUNT = UUID("11111111-1111-4111-8111-111111111111")
NAMESPACE = UUID("22222222-2222-4222-8222-222222222222")
REVISION = UUID("33333333-3333-4333-8333-333333333333")


class RedisWire:
    def __init__(self):
        self.values = {}
        self.expires = {}
        self.fail_cas = False
        self.commands = []

    def get(self, key):
        self.commands.append(("get", key))
        value = self.values.get(key)
        return value.encode() if isinstance(value, str) else value

    def ttl(self, key):
        return math.floor(self.expires[key] - time.time()) if key in self.expires else -2

    def setex(self, key, ttl, value):
        ttl = ttl.total_seconds() if isinstance(ttl, timedelta) else ttl
        self.values[key], self.expires[key] = value, time.time() + ttl

    def set(self, key, value, *, ex, nx):
        assert nx is True
        if key in self.values:
            return False
        self.setex(key, ex, value)
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.expires.pop(key, None)

    def eval(self, script, count, key, expected, *replacement):
        assert count == 1
        if self.fail_cas or self.values.get(key) != expected:
            return 0
        if replacement:
            assert "PTTL" in script
            self.values[key] = replacement[0]
        else:
            assert "DEL" in script
            self.delete(key)
        return 1


class Runtime:
    def __init__(self, wire):
        self.wire, self.scopes = wire, []
        self.fail_open = self.fail_close = False
        self.before_open = lambda: None

    def open(self, *, deadline):
        self.before_open()
        if self.fail_open:
            raise TimeoutError("private source sentinel")
        scope = SimpleNamespace(client=self.wire, deadline=deadline, finished=False)

        def finish():
            scope.finished = True
            return not self.fail_close

        scope.finish = finish
        self.scopes.append(scope)
        return scope


@pytest.fixture
def source(monkeypatch):
    wire = RedisWire()
    runtime = Runtime(wire)
    service = CasdoorSessionService(
        settings=SimpleNamespace(CONSOLE_API_URL="https://console.example.test", DEPLOY_ENV="PRODUCTION"),
        secret_key="synthetic-session-encryption",
        redis_runtime_factory=runtime,
    )
    wire.setex("refresh_token:old-refresh", 120, str(ACCOUNT))
    wire.setex("account_refresh_token:" + str(ACCOUNT), 120, "another-browser-refresh")
    seed = SessionProvenanceSeed(ACCOUNT, NAMESPACE, REVISION, time.time() + 90)
    cookie = service.create(seed=seed, refresh_token="old-refresh", deadline=time.monotonic() + 10)
    assert cookie is not None
    monkeypatch.setattr(
        "services.account_login_adapters.PassportService.issue", lambda self, payload: "synthetic-access"
    )
    return SimpleNamespace(service=service, wire=wire, runtime=runtime, cookie=cookie, seed=seed)


def observation(source, refresh="old-refresh", account=ACCOUNT):
    return source.service.observe(account_id=str(account), refresh_token=refresh, opaque=source.cookie.value)


def test_encrypted_source_uses_actual_request_mapping_not_latest_index(source):
    result = observation(source)
    assert result.source == "casdoor"
    assert result.rp_logout_available is False
    assert source.cookie.max_age <= 90
    raw = source.wire.values["casdoor:session:" + source.cookie.value]
    assert "old-refresh" not in raw
    assert str(ACCOUNT) not in raw
    assert "id_token" not in raw
    assert not any(key.startswith("account_refresh_token:") for _, key in source.wire.commands)


@pytest.mark.parametrize("failure", ["revoked", "wrong_account", "password_login", "tampered", "expired"])
def test_stale_or_untrusted_context_never_reports_casdoor(source, failure):
    account, refresh = ACCOUNT, "old-refresh"
    key = "casdoor:session:" + source.cookie.value
    if failure == "revoked":
        source.wire.delete("refresh_token:old-refresh")
    elif failure == "wrong_account":
        account = NAMESPACE
    elif failure == "password_login":
        refresh = "new-password-refresh"
        source.wire.setex("refresh_token:" + refresh, 120, str(ACCOUNT))
    elif failure == "tampered":
        source.wire.values[key] = source.wire.values[key].replace(str(REVISION), str(NAMESPACE))
    else:
        old = source.service._decode(source.cookie.value, source.wire.values[key])
        metadata = {**old.metadata, "expires_at": time.time() - 1}
        source.wire.values[key] = source.service._encode(source.cookie.value, NAMESPACE, REVISION, metadata)
    assert observation(source, refresh, account).source == "local_only"


def test_actual_refresh_owner_migrates_only_matching_source_and_preserves_original_pair(source):
    dependencies = Dependencies()
    dependencies.sessions = RedisAccountSessionGateway(redis=source.wire)
    observer = source.service.refresh_observer(source.cookie.value)
    old_expiry = source.wire.expires["casdoor:session:" + source.cookie.value]
    pair = dependencies.service().refresh("old-refresh", observer=observer)
    assert pair.access_token == "synthetic-access"
    assert len(pair.refresh_token) == 128
    assert observer.clear_cookie is False
    assert observation(source, pair.refresh_token).source == "casdoor"
    assert observation(source, "old-refresh").source == "local_only"
    assert source.wire.expires["casdoor:session:" + source.cookie.value] == old_expiry


@pytest.mark.parametrize("failure", ["open", "cas", "close"])
def test_migration_failure_keeps_refresh_and_defaults_to_clear_cookie(source, failure):
    dependencies = Dependencies()
    dependencies.sessions = RedisAccountSessionGateway(redis=source.wire)
    observer = source.service.refresh_observer(source.cookie.value)
    if failure == "open":
        source.runtime.fail_open = True
    elif failure == "cas":
        source.wire.fail_cas = True
    else:
        source.runtime.fail_close = True
    pair = dependencies.service().refresh("old-refresh", observer=observer)
    assert pair.access_token == "synthetic-access"
    assert source.wire.get("refresh_token:" + pair.refresh_token) == str(ACCOUNT).encode()
    assert observer.clear_cookie is True


def test_consumption_is_single_use_and_does_not_revoke_other_refresh(source):
    source.service.consume(account_id=str(ACCOUNT), refresh_token="old-refresh", opaque=source.cookie.value)
    assert observation(source).source == "local_only"
    assert source.wire.get("refresh_token:old-refresh") == str(ACCOUNT).encode()


def test_record_ttl_is_bounded_by_actual_refresh_expiry_without_arbitrary_day_cap(source):
    source.wire.setex("refresh_token:short", 5, str(ACCOUNT))
    cookie = source.service.create(seed=source.seed, refresh_token="short", deadline=time.monotonic() + 10)
    assert cookie is not None
    assert 0 < cookie.max_age <= 5
    expired_seed = SessionProvenanceSeed(ACCOUNT, NAMESPACE, REVISION, time.time() - 1)
    assert source.service.create(seed=expired_seed, refresh_token="short", deadline=time.monotonic() + 10) is None


def test_real_production_login_records_source_only_after_mandatory_scope_close(production, source):
    flow = production.flow
    flow.service._session_service = source.service

    def prepare():
        assert all(scope.finish_calls == 1 for scope in flow.control.runtime_scopes)
        for key, ttl, value in flow.control.tokens:
            source.wire.setex(key, ttl, value)

    source.runtime.before_open = prepare
    scope, _ = begin(flow)
    result = complete(flow, scope)
    assert result.error is None
    assert result.tokens is not None
    assert result.source_cookie is not None
    assert result.provenance is not None
    assert (
        source.service.observe(
            account_id=str(result.provenance.account_id),
            refresh_token=result.tokens.refresh_token,
            opaque=result.source_cookie.value,
        ).source
        == "casdoor"
    )


@pytest.mark.parametrize("failure", ["optional_open", "optional_close", "mandatory_close"])
def test_optional_failure_never_weakens_mandatory_delivery_gate(production, source, failure):
    flow = production.flow
    flow.service._session_service = source.service
    source.runtime.before_open = lambda: [source.wire.setex(key, ttl, value) for key, ttl, value in flow.control.tokens]
    scope, _ = begin(flow)
    prior = len(source.runtime.scopes)
    if failure == "optional_open":
        source.runtime.fail_open = True
    elif failure == "optional_close":
        source.runtime.fail_close = True
    else:
        flow.control.fail_close = True
    result = complete(flow, scope)
    assert result.source_cookie is None
    if failure == "mandatory_close":
        assert result.error is not None
        assert result.tokens is None
        assert len(source.runtime.scopes) == prior
    else:
        assert result.error is None
        assert result.tokens is not None


@pytest.mark.parametrize("failure", ["runtime", "cookie_policy", "service_property"])
def test_mounted_original_logout_clears_source_even_if_optional_context_is_unavailable(source, monkeypatch, failure):
    from controllers.console import wraps
    from controllers.console.auth import login

    dependencies = Dependencies()
    dependencies.sessions = RedisAccountSessionGateway(redis=source.wire)
    services = SimpleNamespace(
        accounts=SimpleNamespace(authentication=dependencies.service()), casdoor_session=source.service
    )
    cookie_name = source.service.cookie_name()
    if failure == "runtime":
        source.runtime.fail_open = True
    elif failure == "cookie_policy":
        source.service._settings.CONSOLE_API_URL = "invalid-provider-setting"
    else:

        class BrokenOptionalServices:
            accounts = services.accounts

            @property
            def casdoor_session(self):
                raise RuntimeError("private construction sentinel")

        services = BrokenOptionalServices()
    monkeypatch.setattr(login, "application_services", lambda: services)
    monkeypatch.setattr(login, "current_account_with_tenant_optional", lambda: (SimpleNamespace(id=str(ACCOUNT)), None))
    monkeypatch.setattr(login.flask_login, "logout_user", lambda: None)
    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    monkeypatch.setattr(login, "extract_refresh_token", lambda request: "old-refresh")
    app = Flask(__name__)
    from flask_restx import Api

    api = Api(app)
    api.add_resource(login.LogoutApi, "/logout")
    with app.test_client() as client:
        client.set_cookie(cookie_name, source.cookie.value)
        response = client.post("/logout")
    assert response.json == {"result": "success"}
    cookies = response.headers.getlist("Set-Cookie")
    assert any("casdoor_source=" in cookie and "Max-Age=0" in cookie for cookie in cookies)
    # Original latest-index semantics remain: this request's non-latest mapping
    # was not silently made an all-session revocation policy.
    assert source.wire.get("refresh_token:old-refresh") == str(ACCOUNT).encode()
    assert source.wire.get("account_refresh_token:" + str(ACCOUNT)) is None


@pytest.mark.parametrize("failure", ["cookie_policy", "service_property"])
def test_mounted_original_refresh_survives_optional_instantiation_or_cookie_policy_failure(
    source, monkeypatch, failure
):
    from controllers.console.auth import login
    from flask_restx import Api
    from libs.token import _real_cookie_name

    dependencies = Dependencies()
    dependencies.sessions = RedisAccountSessionGateway(redis=source.wire)
    cookie_name = source.service.cookie_name()
    services = SimpleNamespace(
        accounts=SimpleNamespace(authentication=dependencies.service()), casdoor_session=source.service
    )
    if failure == "cookie_policy":
        source.service._settings.CONSOLE_API_URL = "invalid-provider-setting"
    else:

        class BrokenOptionalServices:
            accounts = services.accounts

            @property
            def casdoor_session(self):
                raise RuntimeError("private construction sentinel")

        services = BrokenOptionalServices()
    monkeypatch.setattr(login, "application_services", lambda: services)
    app = Flask(__name__)
    api = Api(app)
    api.add_resource(login.RefreshTokenApi, "/refresh-token")
    with app.test_client() as client:
        client.set_cookie(_real_cookie_name("refresh_token"), "old-refresh")
        client.set_cookie(cookie_name, source.cookie.value)
        response = client.post("/refresh-token")
    assert response.json == {"result": "success"}
    assert any("casdoor_source=" in value and "Max-Age=0" in value for value in response.headers.getlist("Set-Cookie"))


@pytest.mark.parametrize("fail_close", [False, True])
def test_mounted_original_refresh_delivers_pair_and_clears_only_unconfirmed_source(source, monkeypatch, fail_close):
    from http.cookies import SimpleCookie

    from controllers.console.auth import login
    from flask_restx import Api
    from libs.token import _real_cookie_name

    dependencies = Dependencies()
    dependencies.sessions = RedisAccountSessionGateway(redis=source.wire)
    services = SimpleNamespace(
        accounts=SimpleNamespace(authentication=dependencies.service()), casdoor_session=source.service
    )
    monkeypatch.setattr(login, "application_services", lambda: services)
    source.runtime.fail_close = fail_close
    app = Flask(__name__)
    api = Api(app)
    api.add_resource(login.RefreshTokenApi, "/refresh-token")
    with app.test_client() as client:
        client.set_cookie(_real_cookie_name("refresh_token"), "old-refresh")
        client.set_cookie(source.service.cookie_name(), source.cookie.value)
        response = client.post("/refresh-token")
    assert response.json == {"result": "success"}
    cookies = SimpleCookie()
    for value in response.headers.getlist("Set-Cookie"):
        cookies.load(value)
    refresh = cookies[_real_cookie_name("refresh_token")].value
    assert source.wire.get("refresh_token:" + refresh) == str(ACCOUNT).encode()
    if fail_close:
        assert cookies[source.service.cookie_name()]["max-age"] == "0"
    else:
        assert source.service.cookie_name() not in cookies
        assert observation(source, refresh).source == "casdoor"


def test_broken_optional_observer_does_not_replace_primary_refresh_success_or_failure():
    from services import account_errors

    dependencies = Dependencies()
    calls = []

    class Broken:
        def before_rotation(self, **kwargs):
            calls.append("before")
            raise RuntimeError("private observer sentinel")

        def after_rotation(self, **kwargs):
            calls.append("after")
            raise RuntimeError("private observer sentinel")

        def unavailable(self):
            calls.append("clear")

    pair = dependencies.service().refresh("old-refresh", observer=Broken())
    assert pair is not None
    assert calls == ["before", "clear", "after", "clear"]
    calls.clear()
    dependencies.sessions.refresh_account_id = None
    with pytest.raises(account_errors.InvalidRefreshTokenError):
        dependencies.service().refresh("old-refresh", observer=Broken())
    assert calls == []


def test_no_remaining_optional_budget_never_opens_source_runtime(source):
    scopes = len(source.runtime.scopes)
    assert source.service.create(seed=source.seed, refresh_token="old-refresh", deadline=time.monotonic() - 1) is None
    assert len(source.runtime.scopes) == scopes


def test_optional_production_composition_stays_lazy_and_construction_failure_is_unavailable(monkeypatch):
    settings = SimpleNamespace(SECRET_KEY="", CONSOLE_API_URL="invalid-cookie-policy", DEPLOY_ENV="PRODUCTION")
    service = CasdoorSessionService.for_production(settings)
    assert type(service) is CasdoorSessionService
    assert service.observe(account_id=str(ACCOUNT), refresh_token=None, opaque=None).source == "local_only"

    def broken(settings):
        raise RuntimeError("private composition sentinel")

    monkeypatch.setattr("services.casdoor_session_service_extend.CasdoorRedisRuntimeFactory", broken)
    assert CasdoorSessionService.for_production(settings) is None


def test_mandatory_original_logout_error_is_not_converted_to_optional_success(source, monkeypatch):
    from controllers.console import wraps
    from controllers.console.auth import login
    from flask_restx import Api

    dependencies = Dependencies()
    authentication = dependencies.service()
    calls = []
    primary = RuntimeError("synthetic primary logout failure")

    def logout(account_id):
        calls.append(account_id)
        raise primary

    monkeypatch.setattr(authentication, "logout", logout)
    services = SimpleNamespace(accounts=SimpleNamespace(authentication=authentication), casdoor_session=source.service)
    monkeypatch.setattr(login, "application_services", lambda: services)
    monkeypatch.setattr(login, "current_account_with_tenant_optional", lambda: (SimpleNamespace(id=str(ACCOUNT)), None))
    monkeypatch.setattr(login, "extract_refresh_token", lambda request: "old-refresh")
    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    app = Flask(__name__)
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    Api(app).add_resource(login.LogoutApi, "/logout")
    with app.test_client() as client:
        client.set_cookie(source.service.cookie_name(), source.cookie.value)
        with pytest.raises(RuntimeError) as captured:
            client.post("/logout")
        assert captured.value is primary
        assert client.get_cookie(source.service.cookie_name()).value == source.cookie.value
    assert calls == [str(ACCOUNT)]
    # Preparation is read-only. A mandatory local failure must not consume the
    # source or create any provider handoff before the original owner succeeds.
    assert observation(source).source == "casdoor"


@pytest.fixture
def factory_login(production, monkeypatch):
    """Actual production composition and full mounted auth transport, no subclass."""
    from configs import dify_config
    from controllers.console import bp
    from enums import DeploymentEdition
    from extensions.ext_application_services import build_application_services
    from extensions.ext_login import DifyLoginManager, _load_user_from_request
    from sqlalchemy.orm import sessionmaker

    f = production.flow
    session_factory = f.service._session_factory
    mandatory_runtime = f.service._redis_runtime_factory
    for field in ("CONSOLE_API_URL", "CONSOLE_WEB_URL", "DEPLOY_ENV", "RBAC_ENABLED"):
        monkeypatch.setattr(dify_config, field, getattr(f.settings, field))
    monkeypatch.setattr(dify_config, "SECRET_KEY", "offline-http-key")
    monkeypatch.setattr(dify_config, "COOKIE_DOMAIN", "")
    monkeypatch.setattr(dify_config, "CASDOOR_DEPLOYMENT_AUTHORITY_PATH", str(production.authority))
    monkeypatch.setattr(dify_config, "CASDOOR_DEPLOYMENT_EVIDENCE_PATH", production.policy._evidence_path)

    class TrackedFactory(sessionmaker):
        def __call__(self, **kwargs):
            return session_factory()

    services = build_application_services(
        database_client=TrackedFactory(),
        deployment_edition=DeploymentEdition.COMMUNITY,
        initialization_password="",
        redis=f.redis,
    )
    assert type(services.casdoor_session) is CasdoorSessionService
    assert services.casdoor_local_http._session_service is services.casdoor_session
    assert services.casdoor_local_http._account_activation is services.account_activation
    # Replace only actual external wires, keeping production instances/owners.
    services.casdoor_local_http._redis_runtime_factory = mandatory_runtime
    wire = RedisWire()
    runtime = Runtime(wire)
    services.casdoor_session._runtime = runtime

    def prepare_source_wire():
        assert all(scope.finish_calls == 1 for scope in f.control.runtime_scopes)
        for key, ttl, value in f.control.tokens:
            wire.setex(key, ttl, value)

    runtime.before_open = prepare_source_wire
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME="console.example.test", SECRET_KEY="synthetic-flask")
    manager = DifyLoginManager()
    manager.init_app(app)

    @manager.request_loader
    def load(req):
        with session_factory() as session:
            return _load_user_from_request(req, session)

    app.extensions["application_services"] = services
    app.register_blueprint(bp)
    client = app.test_client()
    return SimpleNamespace(f=f, services=services, wire=wire, runtime=runtime, client=client, app=app)


def mounted_get(d, path, **kwargs):
    from libs.token import _real_cookie_name

    csrf = d.client.get_cookie(_real_cookie_name("csrf_token"), domain="console.example.test")
    headers = {"X-CSRF-Token": csrf.value} if csrf is not None else {}
    return d.client.get(
        path,
        base_url=d.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
        headers=headers,
        **kwargs,
    )


def mounted_normal_login(d):
    first = mounted_get(d, "/console/api/auth/casdoor/login")
    assert first.status_code == 303, first.json
    prepared = urlsplit(first.location)
    second = mounted_get(d, prepared.path + "?" + prepared.query)
    assert second.status_code == 302, second.json
    state = parse_qs(urlsplit(second.location).query)["state"][0]
    result = mounted_get(
        d, "/console/api/auth/casdoor/callback", query_string={"state": state, "code": "synthetic-code"}
    )
    assert result.status_code == 302, result.json
    return result


def test_actual_factory_normal_callback_delivers_source_and_mounted_get_uses_actual_login_owner(factory_login):
    from libs.token import _real_cookie_name

    d = factory_login
    callback = mounted_normal_login(d)
    source_cookie = d.client.get_cookie(d.services.casdoor_session.cookie_name(), domain="console.example.test")
    assert source_cookie is not None
    assert len(source_cookie.value) == 43
    assert len(callback.headers.getlist("Set-Cookie")) == 5
    assert "id_token" not in callback.location
    current = mounted_get(d, "/console/api/auth/casdoor/session")
    assert current.status_code == 200, current.json
    assert current.json["source"] == "casdoor"
    assert current.json["verified"] is True
    assert current.json["rp_logout_available"] is False
    assert current.json["expires_at"].endswith("Z")
    assert set(current.json) == {"source", "verified", "rp_logout_available", "expires_at"}
    assert current.headers["Cache-Control"] == "no-store"
    assert current.headers["Referrer-Policy"] == "no-referrer"
    refresh = d.client.get_cookie(_real_cookie_name("refresh_token"), domain="console.example.test").value
    d.wire.delete("refresh_token:" + refresh)
    # A revoked request mapping stays denied even if encrypted source and
    # browser digest still match. Later copies must not restore the mapping.
    d.runtime.before_open = lambda: None
    revoked = mounted_get(d, "/console/api/auth/casdoor/session")
    assert revoked.json == {"source": "local_only", "verified": False, "rp_logout_available": False, "expires_at": None}
    d.client.delete_cookie(_real_cookie_name("access_token"), domain="console.example.test")
    unauthenticated = mounted_get(d, "/console/api/auth/casdoor/session")
    assert unauthenticated.status_code in (401, 403)


def test_actual_factory_optional_cleanup_failure_still_delivers_original_token_cookies(factory_login):
    from libs.token import _real_cookie_name

    d = factory_login
    d.runtime.fail_close = True
    mounted_normal_login(d)
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert d.client.get_cookie(_real_cookie_name(name), domain="console.example.test") is not None
    assert d.client.get_cookie(d.services.casdoor_session.cookie_name(), domain="console.example.test") is None
    current = mounted_get(d, "/console/api/auth/casdoor/session")
    assert current.json == {"source": "local_only", "verified": False, "rp_logout_available": False, "expires_at": None}
