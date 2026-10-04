"""Mounted Console routes and cookie jar through actual A2/C2/C1/I19, offline.

The reused A2 fixture replaces only its two concrete production seams. SQLite,
Redis wire models and synthetic signed provider data are not live SSO evidence.
"""

import logging
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import jwt
import pytest
import sqlalchemy as sa
from configs import dify_config
from controllers.console import api, bp
from core.casdoor import auth_transactions as auth
from enums import DeploymentEdition
from extensions import ext_request_logging
from extensions.ext_application_services import ApplicationServices, build_application_services
from flask import Flask
from libs.token import _real_cookie_name
from models.account import Account
from services.casdoor_local_http_service_extend import CasdoorLocalHttpService
from sqlalchemy.orm import Session, sessionmaker
from test_casdoor_local_http_service_extend import counts

pytest_plugins = ("test_casdoor_local_http_service_extend",)
PREFIX = auth.COOKIE_PATH
HOST = "console.example.test"


@pytest.fixture
def mounted(http_flow, monkeypatch):
    f = http_flow
    for key in ("CONSOLE_API_URL", "CONSOLE_WEB_URL", "DEPLOY_ENV"):
        monkeypatch.setattr(dify_config, key, getattr(f.settings, key))
    monkeypatch.setattr(dify_config, "COOKIE_DOMAIN", "")
    monkeypatch.setattr(dify_config, "ENABLE_REQUEST_LOGGING", True)

    class NoSQL(sessionmaker):
        def __call__(self, **kwargs):
            pytest.fail("production factory performed SQL")

    services = build_application_services(
        database_client=NoSQL(),
        deployment_edition=DeploymentEdition.COMMUNITY,
        initialization_password="",
        redis=f.redis,
    )
    assert type(services) is ApplicationServices
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME=HOST)
    app.extensions["application_services"] = replace(services, casdoor_local_http=f.service)
    app.register_blueprint(bp)
    ext_request_logging.init_app(app)
    return SimpleNamespace(app=app, client=app.test_client(), f=f, services=services)


def send(m, suffix, *, client=None, method="GET", **kwargs):
    return (client or m.client).open(
        PREFIX + suffix,
        method=method,
        base_url=m.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
        **kwargs,
    )


def begin(m, *, client=None, **query):
    first = send(m, "/login", client=client, query_string=query)
    if first.status_code == 303:
        assert len(first.headers.getlist("Set-Cookie")) == 1
        assert first.location.startswith(m.f.settings.CONSOLE_API_URL + PREFIX + "/login?init=")
        first = send(m, "/login", client=client, query_string=urlsplit(first.location).query)
    assert first.status_code == 302
    return parse_qs(urlsplit(first.location).query)["state"][0]


def callback(m, state, **kwargs):
    return send(m, "/callback", query_string={"state": state, "code": "synthetic-code"}, **kwargs)


def cookie(m, name, path="/", client=None):
    return (client or m.client).get_cookie(name, domain=urlsplit(m.f.settings.CONSOLE_API_URL).hostname, path=path)


def old_cookies(m):
    values = {}
    for name in ("access_token", "refresh_token", "csrf_token"):
        name = _real_cookie_name(name)
        values[name] = "old-" + name
        m.client.set_cookie(name, values[name], domain=HOST, path="/")
    return values


def assert_fixed(response, status):
    assert response.status_code == status and "Location" not in response.headers
    assert set(response.json) == {"code", "correlation_id", "retry_allowed"}
    UUID(response.json["correlation_id"])
    assert response.json["retry_allowed"] is False
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("development", [False, True])
def test_real_registered_roundtrip_signed_success_and_original_cookie_helpers(mounted, monkeypatch, development):
    m = mounted
    if development:
        m.f.settings.CONSOLE_API_URL = "http://127.0.0.1:5001"
        m.f.settings.CONSOLE_WEB_URL = "http://localhost:3000"
        m.f.settings.DEPLOY_ENV = "DEVELOPMENT"
        for key in ("CONSOLE_API_URL", "CONSOLE_WEB_URL", "DEPLOY_ENV"):
            monkeypatch.setattr(dify_config, key, getattr(m.f.settings, key))
    state = begin(m, return_path="/apps/stored", locale="zh-Hans", timezone="Asia/Shanghai")
    current = auth.transaction_cookie_name(state)
    tx = cookie(m, current, PREFIX + "/callback")
    assert tx and tx.http_only and tx.secure is (not development) and tx.same_site == "Lax"
    assert tx.origin_only is True and tx.max_age == 300
    scope = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX)
    assert scope and scope.max_age == 300 and scope.http_only and scope.same_site == "Lax"
    response = callback(m, state)
    assert response.status_code == 302 and response.location == m.f.settings.CONSOLE_WEB_URL + "/apps/stored"
    assert len(response.headers.getlist("Set-Cookie")) == 4
    assert cookie(m, current, PREFIX + "/callback") is None
    assert cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value == scope.value
    access = cookie(m, _real_cookie_name("access_token"))
    refresh = cookie(m, _real_cookie_name("refresh_token"))
    csrf = cookie(m, _real_cookie_name("csrf_token"))
    assert access.http_only and refresh.http_only and not csrf.http_only
    assert all(
        c.secure is (not development) and c.same_site == "Lax" and c.origin_only for c in (access, refresh, csrf)
    )
    claims = jwt.decode(access.value, dify_config.SECRET_KEY, algorithms=["HS256"])
    assert len(refresh.value) == 128 and csrf.value
    with Session(m.f.local.engine) as session:
        account = session.get(Account, claims["user_id"])
        assert account.interface_language == "zh-Hans" and account.timezone == "Asia/Shanghai"
        assert account.last_login_ip == "192.0.2.7"
    assert m.f.control.consumed[0].nonce == m.f.control.created[0].nonce
    assert len(m.f.control.tokens) == 2 and len(m.f.control.consumed) == 1
    assert len([p for p, _ in m.f.control.requests if p.endswith("access_token")]) == 1
    assert all(
        secret not in response.location + response.get_data(as_text=True)
        for secret in (access.value, refresh.value, csrf.value, "new@example.test")
    )


def test_registered_schema_and_query_documentation(mounted):
    m = mounted
    rules = {rule.rule: rule for rule in m.app.url_map.iter_rules()}
    for suffix in ("/login", "/callback"):
        assert {"GET", "HEAD", "OPTIONS"} == rules[PREFIX + suffix].methods
    with m.app.test_request_context():
        schema = api.__schema__
    for suffix in ("login", "callback"):
        operation = schema["paths"]["/auth/casdoor/" + suffix]["get"]
        assert operation["security"] == []
        assert all(parameter["in"] == "query" for parameter in operation["parameters"])
        assert operation["responses"]["429"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "CasdoorResultResponse"
        )
    assert "CasdoorLoginQuery" in api.models


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "POST", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("suffix", ["/login", "/callback"])
def test_non_get_never_calls_service_or_changes_cookie_jar(mounted, monkeypatch, method, suffix):
    m = mounted
    state = begin(m)
    before = dict(m.client._cookies)

    def forbidden(*args, **kwargs):
        pytest.fail("non-GET reached service")

    monkeypatch.setattr(m.f.service, "start", forbidden)
    monkeypatch.setattr(m.f.service, "complete", forbidden)
    response = send(m, suffix, method=method, query_string={"state": state, "code": "private-sentinel"})
    assert response.status_code == (204 if method == "OPTIONS" else 405)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert not response.headers.getlist("Set-Cookie") and m.client._cookies == before
    assert not m.f.control.consumed and not m.f.control.tokens


@pytest.mark.parametrize("suffix,status", [("", 404), ("/future", 404), ("/future/deep", 404), ("/login/future", 404)])
def test_prefix_routing_errors_have_headers(mounted, suffix, status):
    response = send(mounted, suffix)
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store" and response.headers["Referrer-Policy"] == "no-referrer"


def test_lookalike_untouched(mounted):
    response = mounted.client.get(PREFIX + "-other", base_url=mounted.f.settings.CONSOLE_API_URL)
    assert response.status_code == 404
    assert "Cache-Control" not in response.headers and "Referrer-Policy" not in response.headers


@pytest.mark.parametrize(
    "query",
    [
        [("locale", "en-US"), ("locale", "en-US")],
        {"token": "private-sentinel"},
        {"mode": "link"},
        {"init": "a" * 43, "locale": "en-US"},
        {"init": "bad"},
        {"return_path": "x" * 2049},
        {"timezone": "x" * 65},
        {"locale": "x" * 65},
    ],
)
def test_strict_start_input_before_service(mounted, query):
    assert_fixed(send(mounted, "/login", query_string=query), 400)
    assert not mounted.f.reads and not mounted.f.control.limits and not mounted.f.init.calls


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate-code",
        "duplicate-state",
        "bad-state",
        "missing-state",
        "both",
        "neither",
        "description",
        "token",
        "access_token",
        "mode",
        "account",
        "realm",
        "roles",
        "redirect_uri",
    ],
)
def test_strict_callback_only_unique_canonical_state_can_clear(mounted, kind, caplog):
    m = mounted
    state = begin(m)
    name = auth.transaction_cookie_name(state)
    before = len(m.f.init.calls)
    query = [("state", state), ("code", "private-sentinel")]
    if kind == "duplicate-code":
        query.append(query[1])
    elif kind == "duplicate-state":
        query.append(query[0])
    elif kind == "bad-state":
        query[0] = ("state", "bad-state")
    elif kind == "missing-state":
        query = query[1:]
    elif kind == "both":
        query.append(("error", "private-sentinel"))
    elif kind == "neither":
        query = query[:1]
    elif kind == "description":
        query.append(("error_description", "private-sentinel"))
    else:
        query.append((kind, "private-sentinel"))
    response = send(m, "/callback", query_string=query)
    assert_fixed(response, 400)
    should_clear = kind not in ("duplicate-state", "bad-state", "missing-state")
    assert (cookie(m, name, PREFIX + "/callback") is None) is should_clear
    assert len(m.f.init.calls) == before and not m.f.control.consumed and not m.f.control.tokens
    assert "private-sentinel" not in response.get_data(as_text=True) + caplog.text


@pytest.mark.parametrize("target", ["scope-start", "scope-callback", "tx-callback"])
def test_duplicate_security_cookie_values_rejected(mounted, target):
    m = mounted
    state = begin(m)
    name = auth.transaction_cookie_name(state)
    scope = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value
    tx = cookie(m, name, PREFIX + "/callback").value
    pairs = [(auth.SCOPE_COOKIE_NAME, scope), (name, tx)]
    pairs.append(pairs[1] if target == "tx-callback" else pairs[0])
    raw = m.app.test_client(use_cookies=False)
    before = len(m.f.init.calls)
    response = send(
        m,
        "/login" if target == "scope-start" else "/callback",
        client=raw,
        query_string={} if target == "scope-start" else {"state": state, "code": "synthetic-code"},
        headers={"Cookie": "; ".join(f"{key}={value}" for key, value in pairs)},
    )
    assert_fixed(response, 400)
    assert len(m.f.init.calls) == before and not m.f.control.consumed and not m.f.control.tokens


def test_delayed_scope_overwrite_rejects_old_init_then_fresh_login(mounted):
    m = mounted
    first = send(m, "/login", query_string={"return_path": "/apps/original"})
    old_scope = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value
    m.client.delete_cookie(auth.SCOPE_COOKIE_NAME, domain=HOST, path=PREFIX)
    second = send(m, "/login")
    fresh_scope = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value
    assert old_scope != fresh_scope
    assert_fixed(send(m, "/login", query_string=urlsplit(first.location).query), 400)
    assert not m.f.control.created
    assert send(m, "/login", query_string=urlsplit(second.location).query).status_code == 302
    assert callback(m, m.f.control.created[-1].state).status_code == 302


def test_five_tabs_sixth_rejected_no_eviction_and_distinct_browsers(mounted):
    m = mounted
    states = [begin(m) for _ in range(5)]
    assert len(set(states)) == 5
    before = dict(m.client._cookies)
    assert_fixed(send(m, "/login"), 400)
    assert m.client._cookies == before and len(m.f.control.created) == 5
    other = m.app.test_client()
    other_state = begin(m, client=other)
    assert other_state not in states
    # Wire signer uses the most recent created nonce, so cancel each original
    # transaction to demonstrate they remain independently consumable.
    for state in states:
        response = send(m, "/callback", query_string={"state": state, "error": "denied"})
        assert_fixed(response, 400)
    assert len(m.f.control.consumed) == 5
    assert callback(m, other_state, client=other).status_code == 302


@pytest.mark.parametrize("failure", ["cross-browser", "replay", "expired"])
def test_binding_replay_and_expiry_no_new_session(mounted, failure):
    m = mounted
    state = begin(m)
    if failure == "cross-browser":
        response = callback(m, state, client=m.app.test_client())
    elif failure == "expired":
        m.f.init.now += 301
        response = callback(m, state)
    else:
        assert_fixed(send(m, "/callback", query_string={"state": state, "error": "denied"}), 400)
        response = callback(m, state)
    assert_fixed(response, 400)
    assert not m.f.control.tokens and set(counts(m.f).values()) == {0}


@pytest.mark.parametrize(
    "failure,status",
    [
        ("provider", 400),
        ("rate", 429),
        ("storage", 503),
        ("nonce", 400),
        ("billing", 503),
        ("issuer", 503),
        ("cleanup", 302),
    ],
)
def test_failures_clear_only_this_transaction_preserve_old_sessions(mounted, caplog, monkeypatch, failure, status):
    m = mounted
    old = old_cookies(m)
    other = begin(m)
    state = begin(m)
    if failure == "rate":
        m.f.control.limit_count = 100
    if failure == "storage":
        m.f.control.fail_limit = True
    if failure == "nonce":
        m.f.control.bad_nonce = True
    if failure == "billing":
        m.f.control.fail_billing = True
    if failure == "issuer":
        m.f.control.fail_token = 1
    if failure == "cleanup":
        m.f.control.fail_release = True
        from controllers.console.auth import casdoor_extend as transport
        from models.account import TenantAccountJoin
        from models.account_money_extend import AccountMoneyExtend
        from models.casdoor_extend import (
            CasdoorFinalizationState,
            CasdoorIdentityExtend,
            CasdoorManagedMembershipExtend,
        )

        def forbidden(*args, **kwargs):
            pytest.fail("restricted navigation called ordinary session setter")

        for setter in ("set_access_token_to_cookie", "set_refresh_token_to_cookie", "set_csrf_token_to_cookie"):
            monkeypatch.setattr(transport, setter, forbidden)
    caplog.set_level(logging.DEBUG)
    response = (
        send(
            m,
            "/callback",
            query_string={"state": state, "error": "private-sentinel", "error_description": "private-description"},
        )
        if failure == "provider"
        else callback(m, state)
    )
    if failure == "cleanup":
        assert response.status_code == status and len(response.headers.getlist("Set-Cookie")) == 3
        handoff = parse_qs(urlsplit(response.location).query)["handoff"][0]
        assert response.location == m.f.settings.CONSOLE_WEB_URL + "/signin/casdoor-result?handoff=" + handoff
        assert len(m.f.control.consumed) == 1 and len(m.f.control.tokens) == 2
        assert counts(m.f) == {Account: 1, AccountMoneyExtend: 1, CasdoorIdentityExtend: 1, TenantAccountJoin: 2}
        with Session(m.f.local.engine) as session:
            assert set(session.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))) == {
                CasdoorFinalizationState.FINALIZED,
            }
        response = send(m, "/result", query_string={"handoff": handoff})
        assert_fixed(response, 200)
        assert response.json["code"] == "authorization_pending"
        assert cookie(m, auth.result_cookie_name(handoff), PREFIX + "/result") is None
        assert len(m.f.control.consumed) == 1 and len(m.f.control.tokens) == 2
    else:
        assert_fixed(response, status)
    assert response.headers.get("Retry-After") == ("60" if failure == "rate" else None)
    assert len(response.headers.getlist("Set-Cookie")) == 1
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is None
    assert cookie(m, auth.transaction_cookie_name(other), PREFIX + "/callback")
    assert cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX)
    assert all(cookie(m, name).value == value for name, value in old.items())
    assert "Received Request" in caplog.text
    assert all(
        value not in caplog.text + response.get_data(as_text=True)
        for value in (
            "private-sentinel",
            "private-description",
            "synthetic-code",
            "bad-current-nonce",
            "offline-http-client-secret",
            "new@example.test",
        )
    )


@pytest.mark.parametrize("stage", ["provider", "cleanup", "issuer-and-cleanup"])
def test_cancellation_same_primary_no_response_cookie_not_physically_cleared(mounted, stage):
    m = mounted
    old = old_cookies(m)
    state = begin(m)

    class Stop(BaseException):
        def __str__(self):
            pytest.fail("cancellation serialized")

    primary, secondary = Stop(), Stop()

    def stop():
        raise primary

    if stage == "provider":

        def stop_provider(path):
            if path.endswith("access_token"):
                raise primary

        m.f.control.hook = stop_provider
    elif stage == "cleanup":
        m.f.control.cancel_release = primary
    else:
        m.f.control.token_hook = stop
        m.f.control.cancel_release = secondary
    with pytest.raises(Stop) as caught:
        callback(m, state)
    assert caught.value is primary
    assert primary._casdoor_clear_cookies == (m.f.control.consumed[0].clear_cookie,)
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is not None
    assert all(cookie(m, name).value == value for name, value in old.items())


def test_original_production_gate_missing_proof_zero_io(mounted):
    m = mounted
    assert type(m.services.casdoor_local_http) is CasdoorLocalHttpService
    m.app.extensions["application_services"] = m.services
    assert_fixed(send(m, "/login"), 409)
    state = auth.new_browser_scope(auth.CookiePolicy(m.f.settings.CONSOLE_API_URL)).value
    assert_fixed(callback(m, state), 409)
    assert not m.f.reads and not m.f.init.calls and not m.f.control.limits and not m.f.control.requests


@pytest.mark.parametrize("value", [None, "not-an-ip"])
def test_invalid_direct_ip_never_uses_forwarding_headers(mounted, value):
    m = mounted
    response = m.client.get(
        PREFIX + "/login",
        base_url=m.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": value},
        headers={"X-Forwarded-For": "192.0.2.9", "CF-Connecting-IP": "192.0.2.9"},
    )
    assert_fixed(response, 400)
    assert not m.f.reads and not m.f.init.calls and not m.f.control.limits


@pytest.mark.parametrize("return_path", ["https://evil.example.test/private", "//evil.example.test", "/apps/safe"])
def test_server_navigation_and_direct_ip_ignore_request_authority(mounted, return_path):
    m = mounted
    state = begin(m, return_path=return_path, locale="invalid", timezone="invalid")
    response = callback(m, state, headers={"Host": "attacker.example.test", "X-Forwarded-For": "203.0.113.9"})
    # Host changes browser cookie delivery. Missing original bindings must fail,
    # while forwarded authority headers cannot change the next valid request.
    assert_fixed(response, 400)
    assert not m.f.control.consumed
    response = callback(
        m,
        state,
        headers={
            "X-Forwarded-Host": "attacker.example.test",
            "X-Forwarded-For": "203.0.113.9",
            "CF-Connecting-IP": "203.0.113.10",
        },
    )
    assert response.status_code == 302
    expected = return_path if return_path == "/apps/safe" else "/apps"
    assert response.location == m.f.settings.CONSOLE_WEB_URL + expected
    context = m.f.control.consumed[0].context
    assert (context.locale, context.timezone) == ("en-US", "America/New_York")
    with Session(m.f.local.engine) as session:
        assert session.scalar(sa.select(Account.last_login_ip)) == "192.0.2.7"


@pytest.mark.parametrize("failure,status", [("rate", 429), ("rate-storage", 503), ("auth-storage", 503)])
def test_start_original_rate_and_storage_errors_have_no_cookie(mounted, failure, status):
    m = mounted
    m.f.control.limit_count = 20 if failure == "rate" else 0
    m.f.control.fail_limit = failure == "rate-storage"
    m.f.init.before_fault = failure == "auth-storage"
    response = send(m, "/login")
    assert_fixed(response, status)
    assert not response.headers.getlist("Set-Cookie") and not m.client._cookies
    assert response.headers.get("Retry-After") == ("60" if failure == "rate" else None)


def test_delayed_scope_cookie_breaks_old_transaction_without_rotation(mounted):
    m = mounted
    state = begin(m)
    original = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value
    m.client.set_cookie(
        auth.SCOPE_COOKIE_NAME, auth.new_browser_scope(m.f.service._policy()).value, domain=HOST, path=PREFIX
    )
    assert_fixed(callback(m, state), 400)
    assert not m.f.control.consumed and not m.f.control.tokens
    assert cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value != original
    fresh = begin(m)
    assert callback(m, fresh).status_code == 302


def test_real_unknown_provider_exception_is_fixed_private_error(mounted, caplog):
    m = mounted
    state = begin(m)

    def broken(path):
        if path.endswith("access_token"):
            raise RuntimeError("private-provider-exception-sentinel")

    m.f.control.hook = broken
    caplog.set_level(logging.DEBUG)
    response = callback(m, state)
    assert_fixed(response, 503)
    assert "private-provider-exception-sentinel" not in caplog.text + response.get_data(as_text=True)
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is None
    assert not m.f.control.tokens


def test_preconsume_cancellation_retains_usable_transaction_until_ttl(mounted, monkeypatch):
    m = mounted
    state = begin(m)

    class Stop(BaseException):
        pass

    primary = Stop()
    original = m.f.redis.zcard

    def stop(name):
        raise primary

    monkeypatch.setattr(m.f.redis, "zcard", stop)
    with pytest.raises(Stop) as caught:
        callback(m, state)
    assert caught.value is primary
    assert not m.f.control.consumed and not m.f.control.tokens
    assert primary._casdoor_clear_cookies[0].name == auth.transaction_cookie_name(state)
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback")
    monkeypatch.setattr(m.f.redis, "zcard", original)
    assert callback(m, state).status_code == 302
