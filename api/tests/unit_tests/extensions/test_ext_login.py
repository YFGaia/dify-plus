import json
from collections.abc import Callable
from typing import cast
from unittest import mock

import pytest
from flask import Flask, Response, request
from sqlalchemy.orm import Session
from werkzeug.exceptions import Unauthorized

from constants import COOKIE_NAME_ACCESS_TOKEN
from core.logging.context import clear_request_context, get_identity_context
from extensions import ext_login
from extensions.ext_login import unauthorized_handler
from models.account import TenantAccountRole


@pytest.fixture(autouse=True)
def _reset_logging_context():
    clear_request_context()
    yield
    clear_request_context()


def test_unauthorized_handler_returns_json_response() -> None:
    response = unauthorized_handler()

    assert isinstance(response, Response)
    assert response.status_code == 401
    assert response.content_type == "application/json"
    assert json.loads(response.get_data(as_text=True)) == {
        "code": "unauthorized",
        "message": "Unauthorized.",
    }


def test_on_user_logged_in_sets_account_logging_identity() -> None:
    account = ext_login.Account(name="Test Account", email="test@example.com")
    account.id = "account-id"
    account._current_tenant = ext_login.Tenant(name="Test Tenant")
    account._current_tenant.id = "tenant-id"
    clear_request_context()

    ext_login.on_user_logged_in(None, account)

    assert get_identity_context() == ("tenant-id", "account-id", "account")


def test_on_user_logged_in_sets_end_user_logging_identity() -> None:
    end_user = ext_login.EndUser(
        id="end-user-id",
        tenant_id="tenant-id",
        type="browser",
    )
    clear_request_context()

    ext_login.on_user_logged_in(None, end_user)

    assert get_identity_context() == ("tenant-id", "end-user-id", "browser")


def test_on_user_logged_in_does_not_break_auth_when_identity_is_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    account = ext_login.Account(name="Test Account", email="test@example.com")
    account.id = "account-id"
    clear_request_context()

    with (
        mock.patch.object(
            ext_login.Account,
            "current_tenant_id",
            new_callable=mock.PropertyMock,
            side_effect=RuntimeError("unavailable"),
        ),
        caplog.at_level("ERROR", logger=ext_login.logger.name),
    ):
        ext_login.on_user_logged_in(None, account)

    assert get_identity_context() == ("", "", "")
    assert "Failed to set logging identity context" in caplog.text


def test_on_user_logged_in_logs_unsupported_user_type(caplog: pytest.LogCaptureFixture) -> None:
    unsupported_user = cast(ext_login.LoginUser, object())
    clear_request_context()

    with caplog.at_level("ERROR", logger=ext_login.logger.name):
        ext_login.on_user_logged_in(None, unsupported_user)

    assert get_identity_context() == ("", "", "")
    assert "Failed to set logging identity context" in caplog.text


@pytest.mark.parametrize("token_transport", ["cookie", "header"])
def test_console_path_without_blueprint_loads_account_from_valid_session(
    sqlite_session: Session, config_overrides: Callable[..., None], token_transport: str
) -> None:
    app = Flask(__name__)
    config_overrides(
        SECRET_KEY="console-path-auth-regression-test-key",
        ADMIN_API_KEY_ENABLE=False,
        CONSOLE_WEB_URL="http://console.example.com",
        CONSOLE_API_URL="http://api.example.com",
        COOKIE_DOMAIN="",
    )
    account = ext_login.Account(name="Test Account", email="test@example.com")
    tenant = ext_login.Tenant(name="Test Tenant")
    sqlite_session.add_all([account, tenant])
    sqlite_session.flush()
    sqlite_session.add(
        ext_login.TenantAccountJoin(
            tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.OWNER, current=True
        )
    )
    sqlite_session.commit()
    token = ext_login.AccountService.get_account_jwt_token(account)
    headers = (
        {"Cookie": f"{COOKIE_NAME_ACCESS_TOKEN}={token}"}
        if token_transport == "cookie"
        else {"Authorization": f"Bearer {token}"}
    )

    with app.test_request_context("/console/api/test", headers=headers):
        assert request.blueprint is None
        result = ext_login._load_user_from_request(request, sqlite_session)

    assert result is account
    assert result.current_tenant_id == tenant.id
    assert result.current_role == TenantAccountRole.OWNER


def test_console_path_without_blueprint_rejects_missing_token(
    sqlite_session: Session, config_overrides: Callable[..., None]
) -> None:
    config_overrides(ADMIN_API_KEY_ENABLE=False)
    app = Flask(__name__)

    with app.test_request_context("/console/api/test"):
        assert request.blueprint is None
        with pytest.raises(Unauthorized, match="Invalid Authorization token"):
            ext_login._load_user_from_request(request, sqlite_session)


@pytest.mark.parametrize(
    "claims",
    [
        {"user_id": "account-id", "token_source": "webapp"},
        {"end_user_id": "end-user-id", "token_source": "webapp"},
        {"end_user_id": "end-user-id"},
        {},
        {"user_id": ""},
    ],
    ids=["account-with-token-source", "webapp-end-user", "end-user-only", "missing-user-id", "empty-user-id"],
)
def test_console_path_without_blueprint_rejects_non_account_claims(
    sqlite_session: Session, config_overrides: Callable[..., None], claims: dict[str, str]
) -> None:
    config_overrides(SECRET_KEY="console-path-auth-regression-test-key", ADMIN_API_KEY_ENABLE=False)
    app = Flask(__name__)
    token = ext_login.PassportService().issue(claims)

    with app.test_request_context("/console/api/test", headers={"Authorization": f"Bearer {token}"}):
        assert request.blueprint is None
        with pytest.raises(Unauthorized, match="Invalid Authorization token"):
            ext_login._load_user_from_request(request, sqlite_session)


@pytest.mark.parametrize("path", ["/api/test", "/console/api", "/console/api-other/test", "/"])
def test_non_console_path_without_blueprint_does_not_use_console_authentication(
    sqlite_session: Session, config_overrides: Callable[..., None], path: str
) -> None:
    config_overrides(ADMIN_API_KEY_ENABLE=False)
    app = Flask(__name__)

    with app.test_request_context(path, headers={"Authorization": "Bearer invalid-console-token"}):
        assert request.blueprint is None
        assert ext_login._load_user_from_request(request, sqlite_session) is None


def test_admin_api_key_header_takes_precedence_over_console_cookie(
    sqlite_session: Session, config_overrides: Callable[..., None]
) -> None:
    app = Flask(__name__)
    tenant = ext_login.Tenant(name="Test Tenant")
    tenant.id = "workspace-id"
    account = ext_login.Account(name="Test Account", email="test@example.com")
    sqlite_session.add_all([tenant, account])
    sqlite_session.flush()
    tenant_account_join = ext_login.TenantAccountJoin(
        tenant_id=tenant.id,
        account_id=account.id,
        role=TenantAccountRole.OWNER,
    )
    sqlite_session.add(tenant_account_join)
    sqlite_session.commit()
    config_overrides(
        ADMIN_API_KEY_ENABLE=True,
        ADMIN_API_KEY="admin-key",
        CONSOLE_WEB_URL="http://console.example.com",
        CONSOLE_API_URL="http://api.example.com",
        COOKIE_DOMAIN="",
    )

    with app.test_request_context(
        "/console/api/test",
        headers={
            "Authorization": "Bearer admin-key",
            "Cookie": f"{COOKIE_NAME_ACCESS_TOKEN}=console-session",
            "X-WORKSPACE-ID": "workspace-id",
        },
    ):
        result = ext_login._load_user_from_request(request, sqlite_session)

    assert result is account
    assert account.current_tenant is tenant
