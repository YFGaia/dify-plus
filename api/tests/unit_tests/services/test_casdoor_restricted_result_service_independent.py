"""Independent regression for a remote account disabled during the fresh C2 snapshot."""

import logging
from urllib.parse import urlsplit

from core.casdoor import auth_transactions as auth
from core.casdoor.errors import CasdoorErrorCode
from core.helper import ssrf_proxy
from services.casdoor_local_http_service_extend import _CompleteResult
from test_casdoor_local_http_service_extend import begin, complete, counts
from test_gateway import response

pytest_plugins = ("test_casdoor_local_http_service_extend",)


def test_second_actual_get_user_disabled_does_not_create_restricted_result(http_flow, monkeypatch, caplog):
    flow = http_flow
    scope, _ = begin(flow, return_path="/apps/private-return")
    original = ssrf_proxy.make_request_with_deadline
    observed = []
    marker = "private-remote-disabled-wire-sentinel"

    def request(method, url, **kwargs):
        actual = original(method, url, **kwargs)
        if urlsplit(url).path.endswith("/api/get-user"):
            observed.append(actual)
            if len(observed) == 2:
                payload = actual.json()
                assert payload["data"]["isForbidden"] is False
                payload["data"]["isForbidden"] = True
                payload["data"]["private_marker"] = marker
                return response(payload)
        return actual

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", request)
    caplog.set_level(logging.DEBUG)

    result = complete(flow, scope)

    assert type(result) is _CompleteResult
    assert result.error is not None
    assert result.error.code is CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED
    assert result.status == result.error.status == 403
    assert result.error.message == "This authorization is not permitted."
    assert result.redirect is None and result.tokens is None
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    assert result.phases.local_outcome == "not_started"
    assert result.phases.finalization_outcome == "not_started"
    assert result.phases.token_outcome == "not_started"
    assert result.phases.cleanup_released is True

    assert len(observed) == 2
    assert len(flow.control.consumed) == 1
    assert len([path for path, _ in flow.control.requests if path.endswith("/access_token")]) == 1
    assert len([path for path, _ in flow.control.requests if path.endswith("/api/get-user")]) == 2
    assert not [path for path, _ in flow.control.requests if path.endswith("/api/get-roles")]
    assert flow.lease.data == {}
    assert flow.lease.releases
    assert not flow.control.tokens and not flow.control.billing
    assert set(counts(flow).values()) == {0}

    assert not [call for call in flow.init.calls if call[0] == auth.CREATE_RESTRICTED_RESULT_SCRIPT]
    assert not [call for call in flow.init.calls if call[0] == auth.CONSUME_RESTRICTED_RESULT_SCRIPT]
    assert not flow.init.records
    assert marker not in caplog.text
