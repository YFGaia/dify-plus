"""Request Redis binding and transport close gates with original business owners."""

from types import SimpleNamespace

import pytest
from core.casdoor import auth_transactions as auth
from core.casdoor.errors import CasdoorErrorCode
from services import casdoor_local_http_service_extend as module
from test_casdoor_local_http_service_extend import begin, complete

pytest_plugins = ("test_casdoor_local_http_service_extend",)


def test_missing_g0_precedes_runtime_and_missing_factory_denies(http_flow):
    flow = http_flow
    calls = []
    runtime = SimpleNamespace(open=lambda **kw: calls.append(kw))
    production = module.CasdoorLocalHttpService(
        session_factory=flow.service._session_factory,
        configuration_service=flow.config,
        account_activation=flow.service._account_activation,
        redis_client=flow.redis,
        settings=flow.settings,
        redis_runtime_factory=runtime,
    )
    for result in (
        production.start(browser_scope=None, server_ip="192.0.2.7"),
        production.complete(
            state="a" * 43, browser_scope=None, transaction_cookie=None, code="one", server_ip="192.0.2.7"
        ),
        production.restricted_result(handoff="b" * 43, browser_scope=None, result_cookie=None, server_ip="192.0.2.7"),
    ):
        assert result.error.code is CasdoorErrorCode.CONFIG_CONFLICT
    assert not calls and not flow.init.calls and not flow.control.limits
    flow.service._redis_runtime_factory = None
    result = flow.service.start(browser_scope=None, server_ip="192.0.2.7")
    assert result.status == 503 and not flow.control.runtime_scopes and not flow.init.calls


def test_request_binding_reaches_limiter_store_coordinator_and_leases(http_flow, monkeypatch):
    from core.casdoor.leases import CasdoorLeases
    from core.casdoor.request_safety import CasdoorRequestLimiter
    from services.casdoor_local_login_coordinator_service_extend import CasdoorLocalLoginCoordinatorService

    flow = http_flow
    clients = []
    for owner in (auth.AuthTransactionStore, CasdoorRequestLimiter, CasdoorLeases):
        original = owner.__init__

        def initialize(self, redis_client, *args, _original=original, **kwargs):
            clients.append((type(self), redis_client))
            _original(self, redis_client, *args, **kwargs)

        monkeypatch.setattr(owner, "__init__", initialize)
    original = CasdoorLocalLoginCoordinatorService._with_request_redis
    clones = []

    def bind(self, client):
        cloned = original(self, client)
        clones.append(cloned)
        return cloned

    monkeypatch.setattr(CasdoorLocalLoginCoordinatorService, "_with_request_redis", bind)
    scope, _ = begin(flow)
    clients.clear()
    result = complete(flow, scope)
    assert result.tokens is not None
    request = flow.control.runtime_scopes[-1]
    assert all(client is request.client for _, client in clients)
    assert {kind for kind, _ in clients} == {auth.AuthTransactionStore, CasdoorRequestLimiter, CasdoorLeases}
    clone = clones[-1]
    assert clone._redis_client is request.client
    assert clone._session_factory is flow.coordinator._session_factory
    assert clone._configuration_service is flow.coordinator._configuration_service
    assert clone._account_owner is flow.coordinator._account_owner
    assert clone._finalization is flow.finalizer
    assert flow.service._redis_client is flow.redis and flow.coordinator._redis_client is flow.lease
    assert len({id(s.client) for s in flow.control.runtime_scopes}) == len(flow.control.runtime_scopes)
    assert all(s.finish_calls == 1 for s in flow.control.runtime_scopes)


@pytest.mark.parametrize("kind", ["bootstrap", "authorize", "success", "cancel", "restricted", "reader"])
def test_close_failure_suppresses_every_candidate(http_flow, caplog, kind):
    flow = http_flow
    if kind == "bootstrap":
        flow.control.fail_close = True
        result = flow.service.start(browser_scope=None, server_ip="192.0.2.7")
    elif kind == "authorize":
        scope, _ = begin(flow)
        flow.control.fail_close = True
        result = flow.service.start(browser_scope=scope, server_ip="192.0.2.7")
    else:
        scope, _ = begin(flow)
        caplog.clear()
        flow.control.fail_release = kind in ("restricted", "reader")
        if kind == "reader":
            candidate = complete(flow, scope)
            flow.control.fail_close = True
            result = flow.service.restricted_result(
                handoff=candidate.handoff,
                browser_scope=scope,
                result_cookie=candidate.cookies[1].value,
                server_ip="192.0.2.7",
            )
        else:
            flow.control.fail_close = True
            result = complete(
                flow, scope, **({"code": None, "provider_error": "access_denied"} if kind == "cancel" else {})
            )
    assert result.status == 503 and result.error.code is CasdoorErrorCode.PROVIDER_UNAVAILABLE
    assert getattr(result, "tokens", None) is None and getattr(result, "payload", None) is None
    assert getattr(result, "redirect", None) is None
    assert all(cookie.max_age == 0 and cookie.value == "" for cookie in result.cookies)
    assert len(result.cookies) == (0 if kind in ("bootstrap", "authorize") else 1)
    if kind in ("success", "restricted"):
        assert result.phases.local_outcome == result.phases.finalization_outcome == "committed"
        assert result.phases.token_outcome == "issued" and len(flow.control.tokens) == 2
        assert result.phases.cleanup_released is (kind == "success")
    assert all(s.finish_calls == 1 for s in flow.control.runtime_scopes)
    assert not any(getattr(record, "casdoor_event", {}).get("result_code") == "success" for record in caplog.records)


def test_business_failure_survives_failed_finish(http_flow):
    flow = http_flow
    scope, _ = begin(flow)
    flow.control.limit_count = 100
    flow.control.fail_close = True
    result = complete(flow, scope)
    assert result.status == 429 and result.tokens is None
    assert flow.control.runtime_scopes[-1].finish_calls == 1


def test_primary_baseexception_outside_privacy_context_survives_finish_failure(http_flow, monkeypatch):
    flow = http_flow
    scope, _ = begin(flow)

    class Stop(BaseException):
        pass

    primary = Stop("primary")

    def stop(*args):
        raise primary

    flow.control.fail_close = True
    monkeypatch.setattr(type(flow.service), "_directory_inputs", stop)
    with pytest.raises(Stop) as caught:
        complete(flow, scope)
    assert caught.value is primary
    assert primary._casdoor_clear_cookies == (flow.control.consumed[-1].clear_cookie,)
    assert flow.control.runtime_scopes[-1].finish_calls == 1


def test_success_event_occurs_only_after_finish(http_flow, monkeypatch):
    flow = http_flow
    events = []

    def record(event):
        if event.result_code == "success":
            assert flow.control.runtime_scopes[-1].finish_calls == 1
            events.append(event)

    monkeypatch.setattr(module, "record_safety_event", record)
    scope, _ = begin(flow)
    result = complete(flow, scope)
    assert result.tokens and len(events) == 2


def test_finish_exception_suppresses_success_and_keeps_primary(http_flow, monkeypatch):
    flow = http_flow
    scope, _ = begin(flow)
    scope_type = type(flow.control.runtime_scopes[0])

    def fail(self):
        self.finish_calls += 1
        raise RuntimeError("private close")

    monkeypatch.setattr(scope_type, "finish", fail)
    result = complete(flow, scope)
    assert result.status == 503 and result.tokens is None
    assert result.phases.token_outcome == "issued" and result.phases.cleanup_released is True
    flow.control.limit_count = 100
    result = flow.service.start(browser_scope=None, server_ip="192.0.2.7")
    assert result.status == 429
    assert all(s.finish_calls == 1 for s in flow.control.runtime_scopes)
