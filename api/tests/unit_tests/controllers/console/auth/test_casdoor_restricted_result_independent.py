"""Independent mounted regression for the third restricted callback cookie write."""

import json
import logging

import pytest
from controllers.console.auth import casdoor_extend as transport
from core.casdoor import auth_transactions as auth
from test_casdoor_local_http_extend import PREFIX, assert_fixed, begin, callback, cookie, old_cookies
from test_casdoor_restricted_result_service_extend import role_wire

pytest_plugins = ("test_casdoor_local_http_extend",)


@pytest.fixture
def no_session_grants(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("restricted transport called ordinary session setter")

    for name in ("set_access_token_to_cookie", "set_refresh_token_to_cookie", "set_csrf_token_to_cookie"):
        monkeypatch.setattr(transport, name, forbidden)


def test_third_scope_cookie_write_fault_discards_temporary_navigation_response(
    mounted, monkeypatch, no_session_grants, caplog
):
    m = mounted
    old_sessions = old_cookies(m)
    other_state = begin(m)
    state = begin(m, return_path="/apps/private-sentinel")
    scope_before = cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX)
    assert scope_before is not None
    other_transaction = cookie(m, auth.transaction_cookie_name(other_state), PREFIX + "/callback")
    assert other_transaction is not None

    role_wire(monkeypatch, "cycle")
    initial_records = dict(m.f.init.records)
    emitted_before_fault = []
    original_set_cookie = transport.Response.set_cookie

    def set_cookie(response, key, *args, **kwargs):
        original_set_cookie(response, key, *args, **kwargs)
        if key == auth.SCOPE_COOKIE_NAME:
            emitted_before_fault.extend(response.headers.getlist("Set-Cookie"))
            raise RuntimeError("private-scope-cookie-sentinel")

    monkeypatch.setattr(transport.Response, "set_cookie", set_cookie)
    caplog.set_level(logging.DEBUG)
    response = callback(m, state)

    assert_fixed(response, 503)
    response_cookies = response.headers.getlist("Set-Cookie")
    assert len(response_cookies) == 1
    assert response_cookies[0].startswith(auth.transaction_cookie_name(state) + "=")
    assert "private-scope-cookie-sentinel" not in response.get_data(as_text=True) + caplog.text

    # The actual callback produced all three temporary writes before the third failed.
    assert len(emitted_before_fault) == 3
    assert emitted_before_fault[0].startswith(auth.transaction_cookie_name(state) + "=")
    result_cookie_header = emitted_before_fault[1]
    result_cookie_name, _, result_cookie_value = result_cookie_header.split(";", 1)[0].partition("=")
    assert result_cookie_name.startswith("casdoor_result_")
    assert len(result_cookie_value) == 43
    scope_header = emitted_before_fault[2].split(";", 1)[0]
    assert scope_header == f"{auth.SCOPE_COOKIE_NAME}={scope_before.value}"

    # Headers from the temporary redirect never reach the client; only safe txclear does.
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is None
    assert cookie(m, auth.SCOPE_COOKIE_NAME, PREFIX).value == scope_before.value
    assert cookie(m, auth.transaction_cookie_name(other_state), PREFIX + "/callback").value == other_transaction.value
    assert all(cookie(m, name).value == value for name, value in old_sessions.items())
    assert not any(item.key.startswith("casdoor_result_") for item in m.client._cookies.values())

    # Navigation creation is durable even though delivery failed; its modeled EX is exactly 300 seconds.
    assert len(m.f.init.records) == len(initial_records)
    removed_keys = set(initial_records) - set(m.f.init.records)
    new_records = {key: value for key, value in m.f.init.records.items() if key not in initial_records}
    assert len(removed_keys) == len(new_records) == 1
    assert len(new_records) == 1
    for raw, expires in new_records.values():
        assert expires == m.f.init.now + 300
        record = json.loads(raw)
        assert json.loads(record["data"])["code"] == "role_snapshot_unknown"
        assert json.loads(record["data"])["retry_allowed"] is False
    assert len(m.f.control.consumed) == 1
    assert not m.f.control.tokens
