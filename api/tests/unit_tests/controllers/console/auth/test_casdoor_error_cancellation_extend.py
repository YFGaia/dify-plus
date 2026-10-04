"""Mounted logging/correlation boundaries with signed, offline A2/C2/I19 wires."""

import logging
from dataclasses import fields, replace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from controllers.console.auth import casdoor_extend as transport
from core.casdoor import auth_transactions as auth
from core.casdoor.request_safety import RequestAction, SafetyEvent, SafetyResultCode
from libs.token import _real_cookie_name
from services import casdoor_local_http_service_extend as service_module
from test_casdoor_local_http_extend import (
    PREFIX,
    assert_fixed,
    begin,
    callback,
    cookie,
    old_cookies,
    send,
)
from test_casdoor_local_http_service_extend import counts, mutate_active
from test_casdoor_restricted_result_extend import issue, redeem
from test_casdoor_restricted_result_service_extend import result_calls

pytest_plugins = ("test_casdoor_local_http_extend",)
LOCAL = UUID(int=900)
BRANCHES = ("start", "normal", "domain", "create", "read", "cancel")


class Stop(BaseException):
    pass


class PrivateFault(Exception):
    def __getattribute__(self, name):
        if name in {"code", "correlation_id", "reason", "__cause__", "__context__", "_casdoor_clear_cookies"}:
            pytest.fail("read private exception attribute: " + name)
        return super().__getattribute__(name)

    def __str__(self):
        pytest.fail("formatted private exception")

    def __repr__(self):
        pytest.fail("represented private exception")


def prepare(m, monkeypatch, branch):
    if branch == "read":
        _, handle, _ = issue(m, monkeypatch, "cleanup")
        return "restricted_result", lambda: redeem(m, handle)
    state = begin(m)
    if branch == "start":
        return "start", lambda: send(m, "/login")
    if branch == "create":
        m.f.control.fail_release = True
    if branch in ("domain", "cancel"):
        error = "access_denied" if branch == "cancel" else "private-provider-error"
        return "complete", lambda: send(m, "/callback", query_string={"state": state, "error": error})
    return "complete", lambda: callback(m, state)


def observe(m, monkeypatch, method, transform=lambda result: result):
    actuals = []
    original = getattr(m.f.service, method)

    def run(**kwargs):
        actual = original(**kwargs)
        actuals.append(actual)
        return transform(actual)

    monkeypatch.setattr(m.f.service, method, run)
    return actuals


def capture(monkeypatch):
    events = []
    original = service_module.record_safety_event

    def record(event):
        events.append(event)
        original(event)

    monkeypatch.setattr(service_module, "record_safety_event", record)
    monkeypatch.setattr(transport, "record_safety_event", record)
    return events


def closed(event):
    assert type(event) is SafetyEvent and type(event.correlation_id) is UUID
    values = event.values()
    assert values["references"] == {} and set(values["summary"].values()) == {0}
    assert set(values) == {"action", "result_code", "correlation_id", "references", "summary"}


@pytest.mark.parametrize("branch", BRANCHES)
def test_six_persistent_ordinary_logger_faults_preserve_actual_outcomes(mounted, monkeypatch, branch):
    m = mounted
    method, invoke = prepare(m, monkeypatch, branch)
    actuals = observe(m, monkeypatch, method)
    events = []

    def broken(event):
        closed(event)
        events.append(event)
        raise PrivateFault("private-log-secret")

    monkeypatch.setattr(service_module, "record_safety_event", broken)
    monkeypatch.setattr(transport, "record_safety_event", broken)
    charges = len(m.f.control.limits)
    response = invoke()
    assert len(m.f.control.limits) == charges + 1
    expected = 200 if branch == "read" else 400 if branch == "domain" else 302
    assert response.status_code == expected and len(events) == 1
    assert events[0].correlation_id == actuals[0].correlation_id
    assert events[0].action is (RequestAction.START if branch == "start" else RequestAction.CALLBACK)
    if branch in ("normal", "create", "read"):
        assert len(m.f.control.tokens) == 2 and len(m.f.control.consumed) == 1
        assert set(counts(m.f).values()) != {0}
    if branch == "normal":
        assert cookie(m, _real_cookie_name("access_token"))
        assert len(response.headers.getlist("Set-Cookie")) == 4
        assert events[0].result_code is SafetyResultCode.SUCCESS
    elif branch == "create":
        result = actuals[0]
        assert result.phases.cleanup_released is False
        assert len(m.f.init.records) == 1 and len(response.headers.getlist("Set-Cookie")) == 3
        handle = parse_qs(urlsplit(response.location).query)["handoff"][0]
        read = redeem(m, handle)
        assert_fixed(read, 200)
        assert read.json["correlation_id"] == str(result.correlation_id)
        assert not m.f.init.records
        assert_fixed(redeem(m, handle), 400)
        assert len(m.f.control.tokens) == 2 and len(m.f.control.consumed) == 1
    elif branch == "read":
        assert response.json["correlation_id"] == str(actuals[0].correlation_id)
        assert not m.f.init.records and len(result_calls(m.f, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)) == 1
    elif branch in ("cancel", "domain"):
        assert not m.f.control.tokens and len(m.f.control.consumed) == 1
        assert len(response.headers.getlist("Set-Cookie")) == 1
        if branch == "cancel":
            assert response.location == m.f.settings.CONSOLE_WEB_URL + "/signin"
        else:
            assert_fixed(response, 400)
            assert response.json["correlation_id"] == str(actuals[0].correlation_id)


@pytest.mark.parametrize("branch", BRANCHES)
def test_six_logger_baseexceptions_keep_identity_without_response(mounted, monkeypatch, branch):
    m = mounted
    _, invoke = prepare(m, monkeypatch, branch)
    jar = dict(m.client._cookies)
    primary = Stop()
    calls = []

    def stop(event):
        calls.append(event)
        raise primary

    monkeypatch.setattr(service_module, "record_safety_event", stop)
    monkeypatch.setattr(transport, "record_safety_event", stop)
    with pytest.raises(Stop) as caught:
        invoke()
    assert caught.value is primary and m.client._cookies == jar and len(calls) == 1
    if branch == "read":
        assert not m.f.init.records
    if branch == "create":
        assert len(m.f.init.records) == 1 and primary._casdoor_phase_facts.cleanup_released is False


@pytest.mark.parametrize("stage", ["preparse", "service"])
@pytest.mark.parametrize(
    "suffix,method", [("/login", "start"), ("/callback", "complete"), ("/result", "restricted_result")]
)
def test_preparse_and_service_throw_use_local_uuid_without_private_attributes(
    mounted, monkeypatch, caplog, stage, suffix, method
):
    m = mounted
    state = begin(m)
    _, handle, _ = issue(m, monkeypatch)
    query = (
        {}
        if suffix == "/login"
        else {"state": state, "code": "private-code"}
        if suffix == "/callback"
        else {"handoff": handle}
    )
    monkeypatch.setattr(transport, "uuid4", lambda: LOCAL)
    caplog.set_level(logging.INFO)
    events = capture(monkeypatch)
    before = (len(m.f.control.consumed), len(m.f.control.requests), len(m.f.control.tokens))
    cause = RuntimeError("private-cause@example.test")

    def fail(*args, **kwargs):
        raise PrivateFault("private-query-cookie-url") from cause

    monkeypatch.setattr(
        transport if stage == "preparse" else m.f.service, "_query" if stage == "preparse" else method, fail
    )
    response = send(m, suffix, query_string=query)
    assert_fixed(response, 503)
    assert response.json["correlation_id"] == str(LOCAL)
    assert len(events) == 1 and events[0].correlation_id == LOCAL
    closed(events[0])
    assert before == (len(m.f.control.consumed), len(m.f.control.requests), len(m.f.control.tokens))
    assert all(
        value not in caplog.text + response.get_data(as_text=True)
        for value in ("private-code", "private-cause", "private-query")
    )
    assert all(not record.exc_info and not record.stack_info for record in caplog.records)


@pytest.mark.parametrize("branch", ["start", "normal", "cancel", "create", "read"])
@pytest.mark.parametrize("corruption", ["subclass", "uuid-string", "late-directive"])
def test_exact_private_variant_and_uuid_before_directives(mounted, monkeypatch, branch, corruption):
    m = mounted
    method, invoke = prepare(m, monkeypatch, branch)
    monkeypatch.setattr(transport, "uuid4", lambda: LOCAL)
    events = capture(monkeypatch)

    def corrupt(actual):
        if corruption == "subclass":
            derived = type("UntrustedResult", (type(actual),), {})
            return derived(**{field.name: getattr(actual, field.name) for field in fields(actual)})
        if corruption == "uuid-string":
            return replace(actual, correlation_id=str(actual.correlation_id))
        return replace(actual, cookies=[])

    actuals = observe(m, monkeypatch, method, corrupt)
    response = invoke()
    assert_fixed(response, 400)
    expected = actuals[0].correlation_id if corruption == "late-directive" else LOCAL
    assert response.json["correlation_id"] == str(expected)
    assert len(events) == 2 and events[-1].correlation_id == expected
    assert events[0].correlation_id == actuals[0].correlation_id


@pytest.mark.parametrize("branch", ["normal", "create", "read"])
def test_late_cookie_fault_discards_partial_headers_with_actual_correlation(mounted, monkeypatch, branch):
    m = mounted
    old = old_cookies(m)
    other = begin(m)
    method, invoke = prepare(m, monkeypatch, branch)
    scope = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value
    actuals = observe(m, monkeypatch, method)
    events = capture(monkeypatch)
    original = transport.Response.set_cookie
    partial = []

    def fail(response, key, *args, **kwargs):
        original(response, key, *args, **kwargs)
        target = _real_cookie_name("refresh_token") if branch == "normal" else "casdoor_result_"
        if response.status_code in (200, 302) and (key == target or key.startswith(target)):
            partial.append(response.headers.getlist("Set-Cookie"))
            raise PrivateFault("private-partial-cookie")

    monkeypatch.setattr(transport.Response, "set_cookie", fail)
    response = invoke()
    assert_fixed(response, 503)
    assert response.json["correlation_id"] == str(actuals[0].correlation_id)
    assert len(events) == 2 and {event.correlation_id for event in events} == {actuals[0].correlation_id}
    assert len(partial) == 1 and len(partial[0]) == (1 if branch == "read" else 2)
    assert len(response.headers.getlist("Set-Cookie")) == 1
    assert all(cookie(m, key).value == value for key, value in old.items())
    assert cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value == scope
    assert cookie(m, auth.transaction_cookie_name(other), PREFIX + "/callback")
    assert len(m.f.control.consumed) == 1 and len(m.f.control.tokens) == 2
    assert len(m.f.init.records) == (2 if branch == "create" else 1)


@pytest.mark.parametrize("failure,status", [("rate", 429), ("storage", 503)])
def test_result_domain_failure_has_fresh_uuid_one_event_and_original_retry(mounted, monkeypatch, failure, status):
    m = mounted
    _, handle, _ = issue(m, monkeypatch, "cleanup")
    produced = next(iter(m.f.init.records.values()))
    actuals = observe(m, monkeypatch, "restricted_result")
    events = capture(monkeypatch)
    m.f.control.limit_count = 40 if failure == "rate" else 0
    m.f.init.before_fault = failure == "storage"
    response = redeem(m, handle)
    assert_fixed(response, status)
    assert len(events) == 1 and events[0].correlation_id == actuals[0].correlation_id
    assert str(actuals[0].correlation_id) not in produced[0]
    assert response.json["correlation_id"] == str(actuals[0].correlation_id)
    assert response.headers.get("Retry-After") == ("60" if failure == "rate" else None)
    assert len(m.f.init.records) == 1 and len(m.f.control.tokens) == 2


@pytest.mark.parametrize("stage", ["provider", "issuer-cleanup"])
def test_business_baseexception_remains_primary_without_secondary_log(mounted, monkeypatch, stage):
    m = mounted
    state = begin(m)
    jar = dict(m.client._cookies)
    primary, secondary = Stop(), Stop()
    calls = []

    def forbidden(event):
        calls.append(event)
        raise secondary

    def stop():
        raise primary

    if stage == "provider":

        def hook(path):
            if path.endswith("access_token"):
                raise primary

        m.f.control.hook = hook
    else:
        m.f.control.token_hook = stop
        m.f.control.cancel_release = secondary
    monkeypatch.setattr(service_module, "record_safety_event", forbidden)
    monkeypatch.setattr(transport, "record_safety_event", forbidden)
    with pytest.raises(Stop) as caught:
        callback(m, state)
    assert caught.value is primary and not calls and m.client._cookies == jar
    assert primary._casdoor_clear_cookies == (m.f.control.consumed[0].clear_cookie,)


@pytest.mark.parametrize("branch", ["cancel", "create", "read"])
@pytest.mark.parametrize("mutation", ["fence", "deadline"])
def test_logger_ordinary_fault_does_not_bypass_original_last_guard(mounted, monkeypatch, branch, mutation):
    m = mounted
    _, invoke = prepare(m, monkeypatch, branch)
    events = []

    def mutate(event):
        events.append(event)
        if len(events) == 1:
            if mutation == "fence":
                mutate_active(m.f, "fence")
            else:
                expired = m.f.control.budgets[-1] + 100
                monkeypatch.setattr(service_module.time, "monotonic", lambda: expired)
        raise PrivateFault("private-observation-fault")

    monkeypatch.setattr(service_module, "record_safety_event", mutate)
    response = invoke()
    assert_fixed(response, 400 if mutation == "fence" else 503)
    assert len(events) == 2 and response.json["correlation_id"] == str(events[-1].correlation_id)
    assert len(response.headers.getlist("Set-Cookie")) == 1
    assert len(m.f.init.records) == (1 if branch == "create" else 0)
    if branch == "read":
        assert events[0].correlation_id != events[1].correlation_id


@pytest.mark.parametrize("suffix", ["/login", "/callback", "/result"])
@pytest.mark.parametrize("cancellation", [False, True])
def test_transport_logger_failure_keeps_precharge_fixed_error_or_primary(mounted, monkeypatch, suffix, cancellation):
    m = mounted
    monkeypatch.setattr(transport, "uuid4", lambda: LOCAL)
    calls = []
    primary = Stop()
    jar = dict(m.client._cookies)

    def broken(event):
        closed(event)
        calls.append(event)
        if cancellation:
            raise primary
        raise PrivateFault("private-log-secret")

    monkeypatch.setattr(transport, "record_safety_event", broken)
    if cancellation:
        with pytest.raises(Stop) as caught:
            send(m, suffix, query_string={"unknown": "private-query"})
        assert caught.value is primary
    else:
        response = send(m, suffix, query_string={"unknown": "private-query"})
        assert_fixed(response, 400)
        assert response.json["correlation_id"] == str(LOCAL)
    assert len(calls) == 1 and calls[0].correlation_id == LOCAL
    assert not m.f.control.limits and not m.f.control.requests and not m.f.control.consumed
    assert m.client._cookies == jar
