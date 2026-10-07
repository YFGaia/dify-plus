"""Independent setter-boundary regression through the mounted Console app."""

import sqlalchemy as sa
from controllers.console.auth import casdoor_extend as transport
from core.casdoor import auth_transactions as auth
from models.account import Account
from sqlalchemy.orm import Session
from test_casdoor_local_http_extend import (
    PREFIX,
    assert_fixed,
    begin,
    callback,
    cookie,
    old_cookies,
)

pytest_plugins = ("test_casdoor_local_http_extend",)


def test_partial_session_cookie_write_is_discarded_on_setter_failure(mounted, monkeypatch):
    m = mounted
    old = old_cookies(m)
    state = begin(m)
    name = auth.transaction_cookie_name(state)
    temporary_headers = []
    original_setter = transport.set_access_token_to_cookie

    def write_then_fail(request, response, token):
        original_setter(request, response, token)
        temporary_headers.extend(response.headers.getlist("Set-Cookie"))
        raise RuntimeError("private-session-setter-sentinel")

    monkeypatch.setattr(transport, "set_access_token_to_cookie", write_then_fail)
    response = callback(m, state)

    assert_fixed(response, 503)
    assert len(temporary_headers) == 1 and "access_token=" in temporary_headers[0]
    delivered = response.headers.getlist("Set-Cookie")
    assert len(delivered) == 1 and delivered[0].startswith(name + "=")
    assert temporary_headers[0] not in delivered
    assert cookie(m, name, PREFIX + "/callback") is None
    assert all(cookie(m, cookie_name).value == value for cookie_name, value in old.items())
    assert "private-session-setter-sentinel" not in response.get_data(as_text=True)
    assert len(m.f.control.consumed) == 1 and len(m.f.control.tokens) == 2
    with Session(m.f.local.engine) as session:
        assert session.scalar(sa.select(Account.last_login_ip)) == "192.0.2.7"
