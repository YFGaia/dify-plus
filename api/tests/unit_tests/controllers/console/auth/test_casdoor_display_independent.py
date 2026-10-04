"""Independent cross-component guard: display configuration is not SSO readiness."""

import pytest
import sqlalchemy as sa
from core.helper import ssrf_proxy
from extensions.ext_application_services import ApplicationServices
from services.casdoor_local_http_service_extend import CasdoorLocalHttpService
from sqlalchemy.orm import Session
from test_casdoor_display_extend import PATH as DISPLAY_PATH
from test_casdoor_display_extend import error_response, seed, success

pytest_plugins = ("test_casdoor_display_extend",)


def test_enabled_display_does_not_make_original_login_ready(harness, certificate, monkeypatch):
    seed(harness, certificate, button="Visible Casdoor")
    assert type(harness.registry) is ApplicationServices
    assert type(harness.registry.casdoor_local_http) is CasdoorLocalHttpService

    displayed = harness.client.get(DISPLAY_PATH)
    success(displayed, enabled=True, button="Visible Casdoor")

    statements = []
    engine = harness.factory.kw["bind"]

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    def deny(*_args, **_kwargs):
        pytest.fail("the production login gate performed session or provider work")

    sa.event.listen(engine, "before_cursor_execute", capture)
    harness.redis.reset_mock()
    monkeypatch.setattr(Session, "__init__", deny)
    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", deny)
    try:
        login = harness.client.get("/console/api/auth/casdoor/login")
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)

    error_response(login, "config_conflict", 409)
    assert statements == []
    assert not harness.redis.mock_calls
    assert not harness.app.login_manager.mock_calls
    assert "synthetic-display-private-secret" not in login.get_data(as_text=True)
