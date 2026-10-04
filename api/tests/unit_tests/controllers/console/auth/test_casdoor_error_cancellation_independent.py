"""Independent mounted regression for late callback cookie failures."""

from uuid import UUID

import pytest
from controllers.console.auth import casdoor_extend as transport
from core.casdoor import auth_transactions as auth
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.request_safety import RequestAction, SafetyEvent
from services import casdoor_local_http_service_extend as service_module
from test_casdoor_local_http_extend import begin, send

pytest_plugins = ("test_casdoor_local_http_extend",)


class PrivateSetterFault(Exception):
    def __getattribute__(self, name):
        if name in {"code", "correlation_id", "reason", "__cause__", "__context__"}:
            pytest.fail("transport inspected private exception metadata: " + name)
        return super().__getattribute__(name)

    def __str__(self):
        pytest.fail("transport formatted a private setter exception")

    def __repr__(self):
        pytest.fail("transport represented a private setter exception")


def test_returned_callback_error_then_clear_cookie_fault_keeps_adopted_correlation(mounted, monkeypatch, caplog):
    m = mounted
    state = begin(m)
    transaction_cookie = auth.transaction_cookie_name(state)
    events = []
    actual_results = []
    original_record = service_module.record_safety_event
    original_complete = m.f.service.complete
    original_set_cookie = transport.Response.set_cookie
    partial_headers = []

    def record(event):
        assert type(event) is SafetyEvent and type(event.correlation_id) is UUID
        assert event.action is RequestAction.CALLBACK
        assert event.values()["references"] == {}
        assert set(event.values()["summary"].values()) == {0}
        events.append(event)
        original_record(event)

    def complete(**kwargs):
        result = original_complete(**kwargs)
        actual_results.append(result)
        return result

    setter_faults = 0

    def fail_once(response, key, *args, **kwargs):
        nonlocal setter_faults
        original_set_cookie(response, key, *args, **kwargs)
        if key == transaction_cookie and setter_faults == 0:
            setter_faults += 1
            partial_headers.append(response.headers.getlist("Set-Cookie"))
            raise PrivateSetterFault("private cookie writer detail")

    monkeypatch.setattr(service_module, "record_safety_event", record)
    monkeypatch.setattr(transport, "record_safety_event", record)
    monkeypatch.setattr(m.f.service, "complete", complete)
    monkeypatch.setattr(transport.Response, "set_cookie", fail_once)

    response = send(m, "/callback", query_string={"state": state, "error": "private-provider-error"})

    assert response.status_code == 503
    assert len(actual_results) == 1 and actual_results[0].error is not None
    correlation = actual_results[0].correlation_id
    assert response.json["correlation_id"] == str(correlation)
    assert [event.correlation_id for event in events] == [correlation, correlation]
    assert [event.result_code for event in events] == [
        actual_results[0].error.code,
        CasdoorErrorCode.PROVIDER_UNAVAILABLE,
    ]
    assert setter_faults == 1 and len(partial_headers) == 1 and len(partial_headers[0]) == 1
    assert len(response.headers.getlist("Set-Cookie")) == 1
    assert response.headers.getlist("Set-Cookie")[0].startswith(transaction_cookie + "=")
    assert len(m.f.control.consumed) == 1
    assert not m.f.control.tokens
    assert not m.f.init.records
    assert "private cookie writer detail" not in caplog.text
