"""Actual same-callback result producer/reader; SQLite and raw offline wires only.

Production G0 and mounted navigation are deliberately separate acceptance gates.
No coordinator/domain/store return is fabricated to establish producer admission.
"""

from copy import deepcopy
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from core.casdoor import auth_transactions as auth
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.errors import CasdoorErrorCode
from core.helper import ssrf_proxy
from models.account import Tenant, TenantStatus
from services import casdoor_local_http_service_extend as service_module
from services.casdoor_local_http_service_extend import (
    CasdoorLocalHttpService,
    _CancelledNavigation,
    _CompleteResult,
    _RestrictedNavigation,
    _RestrictedResult,
)
from sqlalchemy.orm import Session
from test_casdoor_local_http_service_extend import begin, complete, counts, mutate_active
from test_gateway import response

pytest_plugins = ("test_casdoor_local_http_service_extend",)
IP = "192.0.2.7"


def redeem(flow, navigation, scope, **changes):
    return flow.service.restricted_result(
        **dict(
            handoff=navigation.handoff,
            browser_scope=scope,
            result_cookie=navigation.cookies[1].value,
            server_ip=IP,
        )
        | changes
    )


def pending(flow):
    scope, _ = begin(flow, return_path="/apps/private-return")
    flow.control.fail_release = True
    result = complete(flow, scope)
    assert type(result) is _RestrictedNavigation
    return scope, result


def result_calls(flow, script):
    return [call for call in flow.init.calls if call[0] == script]


def role_wire(monkeypatch, kind):
    original = ssrf_proxy.make_request_with_deadline

    def request(method, url, **kwargs):
        actual = original(method, url, **kwargs)
        if urlsplit(url).path.endswith("get-roles"):
            data = actual.json()
            data["data"][0]["roles"] = ["Org/operators" if kind == "cycle" else "Org/missing"]
            return response(data)
        return actual

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", request)


def assert_navigation(flow, navigation, scope, code):
    assert type(navigation) is _RestrictedNavigation
    assert not any(hasattr(navigation, field) for field in ("tokens", "error", "status", "authorized"))
    assert len(navigation.handoff) == 43
    assert navigation.redirect == flow.settings.CONSOLE_WEB_URL + "/signin/casdoor-result?handoff=" + navigation.handoff
    assert len(navigation.cookies) == 3
    clear, result, refreshed = navigation.cookies
    assert clear == flow.control.consumed[-1].clear_cookie
    assert result.name == auth.result_cookie_name(navigation.handoff)
    assert (result.path, result.max_age) == (auth.COOKIE_PATH + "/result", 300)
    assert (refreshed.name, refreshed.value, refreshed.path, refreshed.max_age) == (
        auth.SCOPE_COOKIE_NAME,
        scope,
        auth.COOKIE_PATH,
        300,
    )
    before = (counts(flow), len(flow.control.requests), len(flow.control.tokens), len(flow.control.consumed))
    read = redeem(flow, navigation, scope)
    assert type(read) is _RestrictedResult and read.status == 200 and read.error is None
    assert read.payload.public_projection() == {
        "code": code.value,
        "correlation_id": str(navigation.correlation_id),
        "retry_allowed": False,
    }
    assert read.correlation_id == navigation.correlation_id
    assert read.cookies == (auth.CookieDirective(result.name, "", 0, result.path, result.secure),)
    assert before == (counts(flow), len(flow.control.requests), len(flow.control.tokens), len(flow.control.consumed))
    return read


@pytest.mark.parametrize("kind", ["cycle", "dangling"])
def test_actual_signed_c2_role_graph_failure_returns_closed_unknown(http_flow, monkeypatch, kind):
    flow = http_flow
    scope, _ = begin(flow, return_path="/apps/never-follow")
    role_wire(monkeypatch, kind)
    navigation = complete(flow, scope)
    assert_navigation(flow, navigation, scope, CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
    assert navigation.phases.local_outcome == "not_started"
    assert len(flow.control.consumed) == 1 and set(counts(flow).values()) == {0}
    assert not flow.control.tokens
    assert len([path for path, _ in flow.control.requests if path.endswith("access_token")]) == 1


@pytest.mark.parametrize("kind", ["missing", "unavailable"])
def test_actual_tenant_scope_rejection_precedes_workspace_mapping(http_flow, kind):
    flow = http_flow
    scope, _ = begin(flow)
    with Session(flow.local.engine) as session, session.begin():
        tenant = session.get(Tenant, str(UUID(int=200)))
        if kind == "missing":
            session.delete(tenant)
        else:
            tenant.status = TenantStatus.ARCHIVE
    navigation = complete(flow, scope)
    # _workspaces rejects before resolve_workspace_plan. This is the real
    # pending producer, not a fabricated MappingError workspace producer.
    assert_navigation(flow, navigation, scope, CasdoorErrorCode.AUTHORIZATION_PENDING)
    assert set(counts(flow).values()) == {0} and not flow.control.tokens


def test_same_scope_cross_ticket_nonce_preserves_both_actual_results(http_flow, monkeypatch):
    flow = http_flow
    scope, _ = begin(flow)
    role_wire(monkeypatch, "cycle")
    first = complete(flow, scope)
    second_start = flow.service.start(browser_scope=scope, server_ip=IP)
    assert second_start.status == 302
    second = complete(flow, scope)
    assert first.handoff != second.handoff
    before = deepcopy(flow.init.records)
    wrong = redeem(flow, first, scope, result_cookie=second.cookies[1].value)
    assert wrong.status == 400 and wrong.payload is None
    assert before == flow.init.records
    assert wrong.cookies[0].name == first.cookies[1].name
    for navigation in (first, second):
        read = redeem(flow, navigation, scope)
        assert read.status == 200 and read.correlation_id == navigation.correlation_id
    assert len(flow.control.consumed) == 2 and not flow.control.tokens


@pytest.mark.parametrize("failure", ["nonce", "scope", "missing-nonce", "expired", "replay", "lost-reply"])
def test_reader_failure_never_restores_reissues_or_clears_other_cookies(http_flow, failure):
    flow = http_flow
    scope, navigation = pending(flow)
    before_counts = counts(flow)
    before_tokens, before_requests = len(flow.control.tokens), len(flow.control.requests)
    changes = {}
    if failure in ("nonce", "scope"):
        changes["result_cookie" if failure == "nonce" else "browser_scope"] = auth.new_browser_scope(
            auth.CookiePolicy(flow.settings.CONSOLE_API_URL)
        ).value
    elif failure == "missing-nonce":
        changes["result_cookie"] = None
    elif failure == "expired":
        flow.init.now += 301
    elif failure == "replay":
        assert redeem(flow, navigation, scope).status == 200
    elif failure == "lost-reply":
        flow.init.lose_reply = True
    before_records = deepcopy(flow.init.records)
    before_calls = len(result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT))
    failed = redeem(flow, navigation, scope, **changes)
    assert failed.status == (503 if failure == "lost-reply" else 400)
    assert failed.payload is None and failed.error and failed.correlation_id != navigation.correlation_id
    assert len(failed.cookies) == 1 and failed.cookies[0].name == navigation.cookies[1].name
    assert failed.cookies[0].max_age == 0
    assert len(result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)) == before_calls + (
        0 if failure == "missing-nonce" else 1
    )
    if failure in ("nonce", "scope", "missing-nonce"):
        assert before_records == flow.init.records
        assert redeem(flow, navigation, scope).status == 200
    elif failure == "lost-reply":
        assert not flow.init.records
        flow.init.lose_reply = False
        assert redeem(flow, navigation, scope).status == 400
    assert counts(flow) == before_counts
    assert (len(flow.control.tokens), len(flow.control.requests)) == (before_tokens, before_requests)
    assert len(flow.control.consumed) == 1


@pytest.mark.parametrize("drift", ["draft", "revision", "disable", "fence", "digest", "rbac", "runtime"])
def test_reader_uses_fresh_full_active_binding(http_flow, drift):
    flow = http_flow
    scope, navigation = pending(flow)
    mutate_active(flow, drift)
    read = redeem(flow, navigation, scope)
    assert read.status == 200 if drift == "draft" else read.payload is None and read.error is not None
    assert len(read.cookies) == 1 and read.cookies[0].name == navigation.cookies[1].name
    assert len(flow.control.consumed) == 1 and len(flow.control.tokens) == 2


@pytest.mark.parametrize("failure", ["before-set", "lost-reply", "postguard", "lastguard", "logging"])
def test_producer_failure_only_returns_original_clear_and_leaves_no_retry(http_flow, monkeypatch, failure):
    flow = http_flow
    scope, _ = begin(flow)
    flow.control.fail_release = True
    original_eval = flow.init.eval

    def evaluate(script, count, *args):
        if script == auth.CREATE_RESTRICTED_RESULT_SCRIPT:
            if failure == "before-set":
                raise TimeoutError("private create failure")
            result = original_eval(script, count, *args)
            if failure == "lost-reply":
                raise TimeoutError("private create reply")
            if failure == "postguard":
                mutate_active(flow, "fence")
            return result
        return original_eval(script, count, *args)

    monkeypatch.setattr(flow.init, "eval", evaluate)
    original_record = service_module.record_safety_event
    failed_once = []

    def record(event):
        if event.result_code is CasdoorErrorCode.AUTHORIZATION_PENDING and not failed_once:
            failed_once.append(True)
            if failure == "lastguard":
                mutate_active(flow, "fence")
            if failure == "logging":
                raise RuntimeError("private log fault")
        return original_record(event)

    monkeypatch.setattr(service_module, "record_safety_event", record)
    result = complete(flow, scope)
    if failure == "logging":
        assert type(result) is _RestrictedNavigation
        assert result.phases.local_outcome == result.phases.finalization_outcome == "committed"
        assert result.phases.token_outcome == "issued" and result.phases.cleanup_released is False
        assert len(flow.control.consumed) == 1 and len(flow.control.tokens) == 2
        assert len(flow.init.records) == 1
        assert all(expiry == flow.init.now + 300 for _, expiry in flow.init.records.values())
        assert not result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)
        assert_navigation(flow, result, scope, CasdoorErrorCode.AUTHORIZATION_PENDING)
        assert not flow.init.records
        assert len(result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)) == 1
        replay = redeem(flow, result, scope)
        assert replay.status == 400 and replay.payload is None
        assert len(flow.control.consumed) == 1 and len(flow.control.tokens) == 2
        assert not flow.init.records and len(result_calls(flow, auth.CREATE_RESTRICTED_RESULT_SCRIPT)) == 1
        return
    assert type(result) is _CompleteResult and result.error and result.tokens is None and result.redirect is None
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    assert result.phases.local_outcome == result.phases.finalization_outcome == "committed"
    assert result.phases.token_outcome == "issued" and result.phases.cleanup_released is False
    assert len(flow.control.consumed) == 1 and len(flow.control.tokens) == 2
    assert len(flow.init.records) == (0 if failure == "before-set" else 1)
    assert all(expiry == flow.init.now + 300 for _, expiry in flow.init.records.values())
    assert not result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)


def test_consumer_does_not_decrypt_exchange_issue_or_refresh_scope(http_flow, monkeypatch):
    flow = http_flow
    scope, navigation = pending(flow)

    def forbidden(*args, **kwargs):
        pytest.fail("display result performed secret/provider/token work")

    monkeypatch.setattr(CasdoorCrypto, "decrypt", forbidden)
    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", forbidden)
    monkeypatch.setattr(flow.redis, "setex", forbidden)
    before = len(flow.control.limits)
    assert_navigation(flow, navigation, scope, CasdoorErrorCode.AUTHORIZATION_PENDING)
    assert len(flow.control.limits) == before + 1


@pytest.mark.parametrize("handoff", ["bad", "session", "A" * 42, "A" * 43 + "=", ["A" * 43]])
def test_malformed_handoff_has_no_chosen_clear_or_io(http_flow, handoff):
    flow = http_flow
    result = flow.service.restricted_result(handoff=handoff, result_cookie=None, browser_scope=None, server_ip=IP)
    assert result.status == 400 and result.payload is None and not result.cookies
    assert not flow.reads and not flow.init.calls and not flow.control.limits


def test_consumer_original_production_gate_precedes_io(http_flow):
    flow = http_flow
    service = CasdoorLocalHttpService(
        session_factory=flow.service._session_factory,
        configuration_service=flow.config,
        account_activation=flow.service._account_activation,
        redis_client=flow.redis,
        settings=flow.settings,
    )
    handoff = auth.new_browser_scope(auth.CookiePolicy(flow.settings.CONSOLE_API_URL)).value
    result = service.restricted_result(handoff=handoff, browser_scope=None, result_cookie=None, server_ip=IP)
    assert result.error.code is CasdoorErrorCode.CONFIG_CONFLICT and result.payload is None
    assert len(result.cookies) == 1 and not flow.reads and not flow.init.calls and not flow.control.limits


@pytest.mark.parametrize("return_path", ["/apps/private", "https://evil.example", "//evil.example"])
def test_true_access_denied_fixed_navigation_and_replay_stays_error(http_flow, return_path, monkeypatch):
    flow = http_flow
    scope, _ = begin(flow, return_path=return_path)
    events = []
    original_record = service_module.record_safety_event

    def record(event):
        events.append(event)
        return original_record(event)

    monkeypatch.setattr(service_module, "record_safety_event", record)
    before = len(flow.control.requests)
    result = complete(flow, scope, code=None, provider_error="access_denied")
    assert type(result) is _CancelledNavigation
    assert result.redirect == flow.settings.CONSOLE_WEB_URL + "/signin"
    assert not any(hasattr(result, field) for field in ("tokens", "error", "status", "authorized"))
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    assert len(events) == 1 and events[0].result_code is CasdoorErrorCode.INVALID_TRANSACTION
    replay = complete(flow, scope, code=None, provider_error="access_denied")
    assert type(replay) is _CompleteResult and replay.status == 400 and replay.redirect is None
    assert len(flow.control.requests) == before and not flow.control.tokens
    assert not result_calls(flow, auth.CREATE_RESTRICTED_RESULT_SCRIPT)
    assert len(flow.control.consumed) == 1 and set(counts(flow).values()) == {0}


@pytest.mark.parametrize("failure", ["wrong-scope", "expired", "postguard", "lastguard"])
def test_cancellation_requires_current_consume_and_last_guard(http_flow, monkeypatch, failure):
    flow = http_flow
    scope, _ = begin(flow)
    if failure == "wrong-scope":
        scope = auth.new_browser_scope(auth.CookiePolicy(flow.settings.CONSOLE_API_URL)).value
    elif failure == "expired":
        flow.init.now += 301
    elif failure == "postguard":
        flow.init.after_eval = lambda: mutate_active(flow, "fence")
    else:
        original = service_module.record_safety_event

        def record(event):
            if event.result_code is CasdoorErrorCode.INVALID_TRANSACTION:
                mutate_active(flow, "fence")
            return original(event)

        monkeypatch.setattr(service_module, "record_safety_event", record)
    result = complete(flow, scope, code=None, provider_error="access_denied")
    assert type(result) is _CompleteResult and result.error and result.redirect is None
    assert not flow.control.tokens and len(flow.control.requests) == 1
    assert not result_calls(flow, auth.CREATE_RESTRICTED_RESULT_SCRIPT)


def test_result_create_baseexception_preserves_primary_and_phase_metadata(http_flow, monkeypatch):
    flow = http_flow
    scope, _ = begin(flow)
    flow.control.fail_release = True

    class Stop(BaseException):
        pass

    private_marker = "private-result-create-baseexception-marker"
    primary = Stop(private_marker)
    original = flow.init.eval

    def evaluate(script, count, *args):
        result = original(script, count, *args)
        if script == auth.CREATE_RESTRICTED_RESULT_SCRIPT:
            raise primary
        return result

    monkeypatch.setattr(flow.init, "eval", evaluate)
    scopes_before = len(flow.control.runtime_scopes)
    result = complete(flow, scope)
    assert type(result) is _CompleteResult and result.status == 503
    assert result.error is not None and result.error.code is CasdoorErrorCode.PROVIDER_UNAVAILABLE
    assert result.error.status == 503 and result.error.message == "Start a new authorization request."
    assert result.error.payload() == {
        "code": "provider_unavailable",
        "correlation_id": str(result.correlation_id),
        "retry_allowed": False,
    }
    assert result.error.retry_allowed is False and result.error.retry_after_seconds is None
    assert result.redirect is None and result.tokens is None
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    assert result.cookies[0].value == "" and result.cookies[0].max_age == 0
    assert result.phases.token_outcome == "issued" and len(flow.control.tokens) == 2
    assert result.phases.cleanup_released is False
    assert len(flow.init.records) == 1 and len(flow.control.consumed) == 1
    assert len(result_calls(flow, auth.CREATE_RESTRICTED_RESULT_SCRIPT)) == 1
    assert len(flow.control.runtime_scopes) == scopes_before + 1
    assert all(runtime.finish_calls == 1 for runtime in flow.control.runtime_scopes)
    public = repr((result.redirect, result.tokens, result.cookies, result.error.payload(), result.error.message))
    assert private_marker not in public and str(primary) not in public and type(primary).__name__ not in public


def test_actual_directory_timeout_retains_fixed_error_not_result(http_flow):
    flow = http_flow
    scope, _ = begin(flow)

    def timeout(path):
        if path.endswith("get-roles"):
            raise TimeoutError("private directory timeout")

    flow.control.hook = timeout
    result = complete(flow, scope)
    assert type(result) is _CompleteResult and result.error and result.redirect is None
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    assert not result_calls(flow, auth.CREATE_RESTRICTED_RESULT_SCRIPT)
    assert not flow.control.tokens and set(counts(flow).values()) == {0}


@pytest.mark.parametrize("failure", ["rate", "rate-storage", "result-storage"])
def test_reader_limiter_or_storage_failure_is_fixed_and_nonconsuming(http_flow, failure):
    flow = http_flow
    scope, navigation = pending(flow)
    before_records = deepcopy(flow.init.records)
    before_consumes = len(result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT))
    flow.control.limit_count = 40 if failure == "rate" else 0
    flow.control.fail_limit = failure == "rate-storage"
    flow.init.before_fault = failure == "result-storage"
    result = redeem(flow, navigation, scope)
    assert result.status == (429 if failure == "rate" else 503)
    assert result.payload is None and result.error.retry_after_seconds == (60 if failure == "rate" else None)
    assert len(result.cookies) == 1 and result.cookies[0].name == navigation.cookies[1].name
    assert flow.init.records == before_records
    assert len(result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)) == before_consumes + (
        1 if failure == "result-storage" else 0
    )
    assert len(flow.control.consumed) == 1 and len(flow.control.tokens) == 2


@pytest.mark.parametrize("failure", ["postguard", "lastguard", "deadline"])
def test_reader_postconsume_failure_stays_spent_without_payload(http_flow, monkeypatch, failure):
    flow = http_flow
    scope, navigation = pending(flow)
    if failure == "postguard":
        flow.init.after_eval = lambda: mutate_active(flow, "fence")
    elif failure == "deadline":

        def late():
            expired = flow.control.budgets[-1] + 1
            monkeypatch.setattr(service_module.time, "monotonic", lambda: expired)

        flow.init.after_eval = late
    else:
        original = service_module.record_safety_event

        def record(event):
            if event.result_code is CasdoorErrorCode.AUTHORIZATION_PENDING:
                mutate_active(flow, "fence")
            return original(event)

        monkeypatch.setattr(service_module, "record_safety_event", record)
    result = redeem(flow, navigation, scope)
    assert result.payload is None and result.error and result.correlation_id != navigation.correlation_id
    assert len(result.cookies) == 1 and result.cookies[0].name == navigation.cookies[1].name
    assert not flow.init.records
    assert len(result_calls(flow, auth.CONSUME_RESTRICTED_RESULT_SCRIPT)) == 1
    assert len(flow.control.consumed) == 1 and len(flow.control.tokens) == 2
