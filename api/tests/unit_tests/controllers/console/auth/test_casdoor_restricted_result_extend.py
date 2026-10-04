"""Mounted R3 display transport through original signed A2/C2/store owners.

SQLite, offline raw HTTP and Redis wire models are not production/browser proof.
"""

import logging
from copy import deepcopy
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import sqlalchemy as sa
from configs import dify_config
from controllers.console import api
from controllers.console.auth import casdoor_extend as transport
from controllers.console.casdoor_schemas_extend import CasdoorRestrictedResultResponse, CasdoorResultQuery
from core.casdoor import auth_transactions as auth
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.errors import CasdoorErrorCode
from core.helper import ssrf_proxy
from libs.helper import dump_response
from models.account import Account, Tenant, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorFinalizationState, CasdoorIdentityExtend, CasdoorManagedMembershipExtend
from pydantic import ValidationError
from services import casdoor_local_http_service_extend as service_module
from sqlalchemy.orm import Session
from test_casdoor_local_http_extend import HOST, PREFIX, assert_fixed, begin, callback, cookie, old_cookies, send
from test_casdoor_local_http_service_extend import counts, mutate_active
from test_casdoor_restricted_result_service_extend import result_calls, role_wire

pytest_plugins = ("test_casdoor_local_http_extend",)


@pytest.fixture
def no_session_grants(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("restricted transport called ordinary session setter")

    for name in ("set_access_token_to_cookie", "set_refresh_token_to_cookie", "set_csrf_token_to_cookie"):
        monkeypatch.setattr(transport, name, forbidden)


def issue(m, monkeypatch, kind="cycle"):
    state = begin(m, return_path="/apps/private-return", locale="zh-Hans")
    if kind == "cleanup":
        m.f.control.fail_release = True
    elif kind == "missing":
        with Session(m.f.local.engine) as session, session.begin():
            session.delete(session.get(Tenant, str(UUID(int=200))))
    else:
        role_wire(monkeypatch, "cycle")
    response = callback(m, state)
    assert response.status_code == 302
    handle = parse_qs(urlsplit(response.location).query)["handoff"][0]
    return state, handle, response


def redeem(m, handle, **kwargs):
    return send(m, "/result", query_string={"handoff": handle}, **kwargs)


def effects(m):
    return counts(m.f), len(m.f.control.requests), len(m.f.control.tokens), len(m.f.control.consumed)


@pytest.mark.parametrize("kind", ["cycle", "cleanup", "missing"])
def test_actual_navigation_only_backend_get_consumes_closed_result(
    mounted, monkeypatch, no_session_grants, caplog, kind
):
    m = mounted
    old = old_cookies(m)
    other = begin(m)
    scope = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value
    actual_returns = []
    original = m.f.service.complete

    def complete(**kwargs):
        result = original(**kwargs)
        actual_returns.append(result)
        return result

    monkeypatch.setattr(m.f.service, "complete", complete)
    caplog.set_level(logging.DEBUG)
    state, handle, response = issue(m, monkeypatch, kind)
    assert response.location == m.f.settings.CONSOLE_WEB_URL + "/signin/casdoor-result?handoff=" + handle
    assert len(handle) == 43 and len(response.headers.getlist("Set-Cookie")) == 3
    assert response.headers["Cache-Control"] == "no-store" and response.headers["Referrer-Policy"] == "no-referrer"
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is None
    assert cookie(m, auth.transaction_cookie_name(other), PREFIX + "/callback")
    assert cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value == scope
    result_cookie = cookie(m, auth.result_cookie_name(handle), PREFIX + "/result")
    assert result_cookie.http_only and result_cookie.secure and result_cookie.origin_only
    assert result_cookie.same_site == "Lax" and result_cookie.max_age == 300
    assert not result_calls(m.f, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)
    before = effects(m)

    def forbidden(*args, **kwargs):
        pytest.fail("display GET performed secret/provider/token work")

    monkeypatch.setattr(CasdoorCrypto, "decrypt", forbidden)
    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", forbidden)
    monkeypatch.setattr(m.f.redis, "setex", forbidden)
    read = redeem(m, handle)
    assert_fixed(read, 200)
    assert read.json == {
        "code": "role_snapshot_unknown" if kind == "cycle" else "authorization_pending",
        "correlation_id": str(actual_returns[0].correlation_id),
        "retry_allowed": False,
    }
    assert len(read.headers.getlist("Set-Cookie")) == 1
    assert cookie(m, auth.result_cookie_name(handle), PREFIX + "/result") is None
    assert len(result_calls(m.f, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)) == 1
    assert effects(m) == before
    replay = redeem(m, handle)
    assert_fixed(replay, 400)
    assert effects(m) == before and all(cookie(m, name).value == value for name, value in old.items())
    assert all(
        value not in response.location + read.get_data(as_text=True) + caplog.text
        for value in ("new@example.test", "synthetic-code", "private-return", "offline-http-client-secret")
    )
    assert len(m.f.control.consumed) == 1
    assert len(m.f.control.tokens) == (2 if kind == "cleanup" else 0)
    if kind == "cleanup":
        assert counts(m.f) == {Account: 1, AccountMoneyExtend: 1, CasdoorIdentityExtend: 1, TenantAccountJoin: 2}
        with Session(m.f.local.engine) as session:
            assert set(session.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))) == {
                CasdoorFinalizationState.FINALIZED,
            }
    else:
        assert set(counts(m.f).values()) == {0}


@pytest.mark.parametrize("development", [False, True])
def test_real_cancellation_has_only_fixed_signin_and_current_clear(
    mounted, monkeypatch, no_session_grants, development
):
    m = mounted
    if development:
        m.f.settings.CONSOLE_API_URL = "http://127.0.0.1:5001"
        m.f.settings.CONSOLE_WEB_URL = "http://localhost:3000/"
        m.f.settings.DEPLOY_ENV = "DEVELOPMENT"
        for name in ("CONSOLE_API_URL", "CONSOLE_WEB_URL", "DEPLOY_ENV"):
            monkeypatch.setattr(dify_config, name, getattr(m.f.settings, name))
    state = begin(m, return_path="https://evil.example/private", locale="zh-Hans")
    before = len(m.f.control.requests)
    response = send(
        m,
        "/callback",
        query_string={"state": state, "error": "access_denied"},
        headers={"Origin": "https://evil.example", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"},
    )
    assert response.status_code == 302
    assert response.location == m.f.settings.CONSOLE_WEB_URL.rstrip("/") + "/signin"
    assert len(response.headers.getlist("Set-Cookie")) == 1
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is None
    assert len(m.f.control.requests) == before and not m.f.control.tokens
    assert set(counts(m.f).values()) == {0} and not m.f.init.records
    assert not result_calls(m.f, auth.CREATE_RESTRICTED_RESULT_SCRIPT)
    assert_fixed(send(m, "/callback", query_string={"state": state, "error": "access_denied"}), 400)
    next_state = begin(m)
    unknown = send(m, "/callback", query_string={"state": next_state, "error": "private-provider-error"})
    assert_fixed(unknown, 400)
    assert len(unknown.headers.getlist("Set-Cookie")) == 1 and not m.f.init.records


def test_restricted_loopback_navigation_keeps_original_cookie_policy(mounted, monkeypatch, no_session_grants):
    m = mounted
    m.f.settings.CONSOLE_API_URL = "http://127.0.0.1:5001"
    m.f.settings.CONSOLE_WEB_URL = "http://localhost:3000/"
    m.f.settings.DEPLOY_ENV = "DEVELOPMENT"
    for name in ("CONSOLE_API_URL", "CONSOLE_WEB_URL", "DEPLOY_ENV"):
        monkeypatch.setattr(dify_config, name, getattr(m.f.settings, name))
    _, handle, response = issue(m, monkeypatch)
    assert response.location == "http://localhost:3000/signin/casdoor-result?handoff=" + handle
    result_cookie = cookie(m, auth.result_cookie_name(handle), PREFIX + "/result")
    assert result_cookie.secure is False and result_cookie.http_only and result_cookie.origin_only
    assert result_cookie.same_site == "Lax" and result_cookie.max_age == 300
    assert_fixed(redeem(m, handle), 200)


@pytest.mark.parametrize("failure", ["scope", "nonce", "missing-nonce", "expired"])
def test_result_cookie_binding_errors_keep_other_tabs_and_old_sessions(
    mounted, monkeypatch, no_session_grants, failure
):
    m = mounted
    old = old_cookies(m)
    other = begin(m)
    _, handle, _ = issue(m, monkeypatch)
    name = auth.result_cookie_name(handle)
    current_nonce = cookie(m, name, PREFIX + "/result").value
    current_scope = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value
    records = deepcopy(m.f.init.records)
    if failure == "scope":
        m.client.set_cookie(auth.SCOPE_COOKIE_NAME, "A" * 43, domain=HOST, path=PREFIX)
    elif failure == "nonce":
        m.client.set_cookie(name, "A" * 43, domain=HOST, path=PREFIX + "/result")
    elif failure == "missing-nonce":
        m.client.delete_cookie(name, domain=HOST, path=PREFIX + "/result")
    else:
        m.f.init.now += 301
    before = effects(m)
    response = redeem(m, handle)
    assert_fixed(response, 400)
    assert len(response.headers.getlist("Set-Cookie")) == 1 and effects(m) == before
    assert cookie(m, auth.transaction_cookie_name(other), PREFIX + "/callback")
    assert all(cookie(m, key).value == value for key, value in old.items())
    if failure != "expired":
        assert m.f.init.records == records
        m.client.set_cookie(name, current_nonce, domain=HOST, path=PREFIX + "/result")
        m.client.set_cookie(auth.SCOPE_COOKIE_NAME, current_scope, domain=HOST, path=PREFIX)
        assert_fixed(redeem(m, handle), 200)


def test_second_ticket_valid_nonce_cannot_consume_first(mounted, monkeypatch, no_session_grants):
    m = mounted
    _, first, _ = issue(m, monkeypatch)
    state = begin(m)
    response = callback(m, state)
    second = parse_qs(urlsplit(response.location).query)["handoff"][0]
    first_name, second_name = auth.result_cookie_name(first), auth.result_cookie_name(second)
    original = cookie(m, first_name, PREFIX + "/result").value
    m.client.set_cookie(
        first_name, cookie(m, second_name, PREFIX + "/result").value, domain=HOST, path=PREFIX + "/result"
    )
    records = deepcopy(m.f.init.records)
    assert_fixed(redeem(m, first), 400)
    assert m.f.init.records == records and cookie(m, second_name, PREFIX + "/result")
    assert_fixed(redeem(m, second), 200)
    m.client.set_cookie(first_name, original, domain=HOST, path=PREFIX + "/result")
    assert_fixed(redeem(m, first), 200)


@pytest.mark.parametrize("drift,status", [("draft", 200), ("fence", 400), ("disable", 503), ("revision", 400)])
def test_result_reads_current_configuration(mounted, monkeypatch, no_session_grants, drift, status):
    m = mounted
    _, handle, _ = issue(m, monkeypatch)
    mutate_active(m.f, drift)
    before = effects(m)
    response = redeem(m, handle)
    assert_fixed(response, status)
    assert len(response.headers.getlist("Set-Cookie")) == 1 and effects(m) == before


@pytest.mark.parametrize("kind", ["duplicate", "malformed", "extra", "body", "scope-cookie", "result-cookie"])
def test_invalid_inputs_reject_before_service_and_choose_only_unique_canonical_clear(mounted, monkeypatch, kind):
    m = mounted
    _, handle, _ = issue(m, monkeypatch)
    name = auth.result_cookie_name(handle)
    records = deepcopy(m.f.init.records)
    query = {"handoff": handle}
    kwargs = {}
    if kind == "duplicate":
        query = [("handoff", handle), ("handoff", handle)]
    elif kind == "malformed":
        query = {"handoff": handle + "="}
    elif kind == "extra":
        query["token"] = "private-sentinel"
    elif kind == "body":
        kwargs["data"] = "private-sentinel"
    else:
        target = name if kind == "result-cookie" else auth.SCOPE_COOKIE_NAME
        value = cookie(m, target, PREFIX + "/result" if target == name else PREFIX).value
        kwargs["client"] = m.app.test_client(use_cookies=False)
        kwargs["headers"] = {"Cookie": f"{target}={value}; {target}={value}"}

    def forbidden(*args, **kwargs):
        pytest.fail("invalid transport input reached service")

    monkeypatch.setattr(m.f.service, "restricted_result", forbidden)
    response = send(m, "/result", query_string=query, **kwargs)
    assert_fixed(response, 400)
    assert len(response.headers.getlist("Set-Cookie")) == (0 if kind in ("duplicate", "malformed") else 1)
    assert m.f.init.records == records and not result_calls(m.f, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)
    assert "private-sentinel" not in response.get_data(as_text=True)


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
def test_non_get_does_not_parse_or_consume(mounted, monkeypatch, method):
    m = mounted
    _, handle, _ = issue(m, monkeypatch)
    jar = dict(m.client._cookies)
    records = deepcopy(m.f.init.records)

    def forbidden(*args, **kwargs):
        pytest.fail("non-GET parsed or reached service")

    monkeypatch.setattr(transport, "_query", forbidden)
    monkeypatch.setattr(transport, "_safe_result_clear", forbidden)
    monkeypatch.setattr(m.f.service, "restricted_result", forbidden)
    response = send(m, "/result", method=method, query_string={"handoff": handle}, data="private-sentinel")
    assert response.status_code == (204 if method == "OPTIONS" else 405)
    assert response.headers["Cache-Control"] == "no-store" and response.headers["Referrer-Policy"] == "no-referrer"
    assert not response.headers.getlist("Set-Cookie") and m.client._cookies == jar and m.f.init.records == records


@pytest.mark.parametrize("failure,status", [("rate", 429), ("rate-storage", 503), ("result-storage", 503)])
def test_result_rate_and_storage_errors_preserve_fixed_formats(
    mounted, monkeypatch, no_session_grants, failure, status
):
    m = mounted
    _, handle, _ = issue(m, monkeypatch)
    m.f.control.limit_count = 40 if failure == "rate" else 0
    m.f.control.fail_limit = failure == "rate-storage"
    m.f.init.before_fault = failure == "result-storage"
    records = deepcopy(m.f.init.records)
    before = effects(m)
    response = redeem(m, handle)
    assert_fixed(response, status)
    assert response.headers.get("Retry-After") == ("60" if failure == "rate" else None)
    assert len(response.headers.getlist("Set-Cookie")) == 1 and records == m.f.init.records and effects(m) == before


@pytest.mark.parametrize("branch", ["cancel", "restricted"])
def test_actual_last_return_guard_failure_stays_fixed(mounted, monkeypatch, no_session_grants, branch):
    m = mounted
    state = begin(m)
    if branch == "restricted":
        role_wire(monkeypatch, "cycle")
    original = service_module.record_safety_event
    target = CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN if branch == "restricted" else CasdoorErrorCode.INVALID_TRANSACTION
    changed = []

    def record(event):
        if event.result_code is target and not changed:
            changed.append(True)
            mutate_active(m.f, "fence")
        return original(event)

    monkeypatch.setattr(service_module, "record_safety_event", record)
    response = (
        callback(m, state)
        if branch == "restricted"
        else send(m, "/callback", query_string={"state": state, "error": "access_denied"})
    )
    assert_fixed(response, 400)
    assert len(response.headers.getlist("Set-Cookie")) == 1
    assert not m.f.control.tokens and not result_calls(m.f, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)
    assert len(m.f.init.records) == (1 if branch == "restricted" else 0)


@pytest.mark.parametrize("failure", ["scope-value", "redirect", "order", "late-cookie"])
def test_navigation_transport_fault_discards_all_partial_cookie_headers(
    mounted, monkeypatch, no_session_grants, failure
):
    m = mounted
    old = old_cookies(m)
    state = begin(m)
    role_wire(monkeypatch, "cycle")
    original = m.f.service.complete

    def complete(**kwargs):
        actual = original(**kwargs)
        if failure == "scope-value":
            return replace(actual, cookies=(*actual.cookies[:2], replace(actual.cookies[2], value="A" * 43)))
        if failure == "redirect":
            return replace(actual, redirect=actual.redirect + "&code=private-sentinel")
        if failure == "order":
            return replace(actual, cookies=tuple(reversed(actual.cookies)))
        return actual

    monkeypatch.setattr(m.f.service, "complete", complete)
    emitted = []
    original_set = transport.Response.set_cookie

    def set_cookie(response, key, *args, **kwargs):
        original_set(response, key, *args, **kwargs)
        if key.startswith("casdoor_result_") and failure == "late-cookie":
            emitted.extend(response.headers.getlist("Set-Cookie"))
            raise RuntimeError("private-sentinel")

    monkeypatch.setattr(transport.Response, "set_cookie", set_cookie)
    response = callback(m, state)
    assert_fixed(response, 503 if failure == "late-cookie" else 400)
    assert len(response.headers.getlist("Set-Cookie")) == 1
    assert response.headers.getlist("Set-Cookie")[0].startswith(auth.transaction_cookie_name(state) + "=")
    assert not any(c.key.startswith("casdoor_result_") for c in m.client._cookies.values())
    assert all(cookie(m, key).value == value for key, value in old.items())
    assert len(m.f.init.records) == 1
    assert all(expiry == m.f.init.now + 300 for _, expiry in m.f.init.records.values())
    assert len(emitted) == (2 if failure == "late-cookie" else 0)


def test_post_set_cancellation_keeps_exact_primary_and_browser_residue(mounted, monkeypatch, no_session_grants):
    m = mounted
    old = old_cookies(m)
    state = begin(m)
    role_wire(monkeypatch, "cycle")
    jar = dict(m.client._cookies)

    class Stop(BaseException):
        pass

    primary = Stop()
    original = m.f.init.eval

    def evaluate(script, count, *args):
        result = original(script, count, *args)
        if script == auth.CREATE_RESTRICTED_RESULT_SCRIPT:
            raise primary
        return result

    monkeypatch.setattr(m.f.init, "eval", evaluate)
    with pytest.raises(Stop) as caught:
        callback(m, state)
    assert caught.value is primary and m.client._cookies == jar
    assert primary._casdoor_clear_cookies == (m.f.control.consumed[0].clear_cookie,)
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback").max_age == 300
    assert all(cookie(m, name).value == value for name, value in old.items())
    assert len(m.f.init.records) == 1
    assert all(expiry == m.f.init.now + 300 for _, expiry in m.f.init.records.values())
    assert not m.f.control.tokens and not result_calls(m.f, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)


def test_direct_ip_and_no_forwarded_authority(mounted, monkeypatch, no_session_grants):
    m = mounted
    _, handle, _ = issue(m, monkeypatch)
    original = m.f.service.restricted_result
    received = []

    def read(**kwargs):
        received.append(kwargs["server_ip"])
        return original(**kwargs)

    monkeypatch.setattr(m.f.service, "restricted_result", read)
    response = redeem(m, handle, headers={"X-Forwarded-For": "203.0.113.1", "Origin": "https://evil.example"})
    assert_fixed(response, 200)
    assert received == ["192.0.2.7"]


@pytest.mark.parametrize("failure", ["correlation", "cookie-path", "late-clear"])
def test_actual_consumer_transport_failure_never_delivers_payload(mounted, monkeypatch, no_session_grants, failure):
    m = mounted
    old = old_cookies(m)
    _, handle, _ = issue(m, monkeypatch)
    original = m.f.service.restricted_result

    def read(**kwargs):
        actual = original(**kwargs)
        assert actual.status == 200
        if failure == "correlation":
            return replace(actual, correlation_id=UUID(int=1))
        if failure == "cookie-path":
            return replace(actual, cookies=(replace(actual.cookies[0], path="/"),))
        return actual

    monkeypatch.setattr(m.f.service, "restricted_result", read)
    original_set = transport.Response.set_cookie
    failed = []

    def set_cookie(response, key, *args, **kwargs):
        original_set(response, key, *args, **kwargs)
        if failure == "late-clear" and response.status_code == 200:
            failed.append(response.get_data(as_text=True))
            raise RuntimeError("private-cookie-error")

    monkeypatch.setattr(transport.Response, "set_cookie", set_cookie)
    response = redeem(m, handle)
    assert_fixed(response, 503 if failure == "late-clear" else 400)
    assert response.json["code"] != "role_snapshot_unknown"
    assert len(response.headers.getlist("Set-Cookie")) == 1 and not m.f.init.records
    assert cookie(m, auth.result_cookie_name(handle), PREFIX + "/result") is None
    assert all(cookie(m, name).value == value for name, value in old.items())
    assert len(failed) == (1 if failure == "late-clear" else 0)


def test_consumer_baseexception_has_no_response_and_cannot_restore_spent_result(
    mounted, monkeypatch, no_session_grants
):
    m = mounted
    _, handle, _ = issue(m, monkeypatch)
    jar = dict(m.client._cookies)

    class Stop(BaseException):
        pass

    primary = Stop()
    original = m.f.init.eval

    def evaluate(script, count, *args):
        result = original(script, count, *args)
        if script == auth.CONSUME_RESTRICTED_RESULT_SCRIPT:
            raise primary
        return result

    monkeypatch.setattr(m.f.init, "eval", evaluate)
    with pytest.raises(Stop) as caught:
        redeem(m, handle)
    assert caught.value is primary and m.client._cookies == jar and not m.f.init.records
    assert cookie(m, auth.result_cookie_name(handle), PREFIX + "/result").max_age == 300
    monkeypatch.setattr(m.f.init, "eval", original)
    assert_fixed(redeem(m, handle), 400)


def test_result_invalid_direct_ip_does_not_trust_forwarding(mounted, monkeypatch):
    m = mounted
    _, handle, _ = issue(m, monkeypatch)
    before = deepcopy(m.f.init.records)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid direct IP reached result service")

    monkeypatch.setattr(m.f.service, "restricted_result", forbidden)
    response = m.client.get(
        PREFIX + "/result",
        base_url=m.f.settings.CONSOLE_API_URL,
        query_string={"handoff": handle},
        environ_overrides={"REMOTE_ADDR": "not-an-ip"},
        headers={"X-Forwarded-For": "192.0.2.7"},
    )
    assert_fixed(response, 400)
    assert m.f.init.records == before and len(response.headers.getlist("Set-Cookie")) == 1


def test_native_schema_and_serializer_are_closed_and_get_has_no_body(mounted):
    with mounted.app.test_request_context():
        schema = api.__schema__
    operation = schema["paths"]["/auth/casdoor/result"]["get"]
    assert operation["security"] == [] and "requestBody" not in operation
    assert all(p["in"] == "query" for p in operation["parameters"])
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/CasdoorRestrictedResultResponse")
    native = schema["components"]["schemas"]["CasdoorRestrictedResultResponse"]
    assert native["properties"]["retry_allowed"]["const"] is False
    assert set(native["properties"]["code"]["enum"]) == {
        "role_snapshot_unknown",
        "workspace_unavailable",
        "authorization_pending",
    }
    dto = CasdoorRestrictedResultResponse.model_json_schema(mode="serialization")
    assert set(dto["properties"]) == {"code", "correlation_id", "retry_allowed"}
    assert set(dto["properties"]["code"]["enum"]) == {
        "role_snapshot_unknown",
        "workspace_unavailable",
        "authorization_pending",
    }
    assert dto["properties"]["retry_allowed"]["const"] is False
    query = CasdoorResultQuery.model_json_schema()["properties"]["handoff"]
    assert query["minLength"] == query["maxLength"] == 43 and query["pattern"] == r"^[A-Za-z0-9_-]{43}$"
    public = {"code": "authorization_pending", "correlation_id": str(UUID(int=1)), "retry_allowed": False}
    assert dump_response(CasdoorRestrictedResultResponse, public) == public
    for change in (
        {"code": "remote_account_disabled"},
        {"retry_allowed": 0},
        {"retry_allowed": True},
        {"correlation_id": UUID(int=1).hex},
        {"account_id": "private"},
        {"tokens": "private"},
    ):
        with pytest.raises(ValidationError):
            dump_response(CasdoorRestrictedResultResponse, public | change)
