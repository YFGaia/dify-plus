import base64
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest
from flask import Flask, request
from jwt import InvalidTokenError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from werkzeug.exceptions import Unauthorized

import services.errors.account
from controllers.console import wraps as console_wraps
from controllers.web.login import EmailCodeLoginApi, EmailCodeLoginSendEmailApi, LoginApi, LoginStatusApi, LogoutApi
from enums import DeploymentEdition
from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.model import DifySetup
from services.entities.auth_audit_entities import LoginFailureReason

pytestmark = pytest.mark.parametrize("sqlite_session", [(DifySetup,)], indirect=True)


def encode_code(code: str) -> str:
    return base64.b64encode(code.encode("utf-8")).decode()


def assert_login_failure_logged(caplog: pytest.LogCaptureFixture, email: str, reason: LoginFailureReason) -> None:
    records = [record for record in caplog.records if record.name == "controllers.web.login"]
    assert len(records) == 1
    assert records[0].args[0] == email
    assert records[0].args[1] == reason


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture(autouse=True)
def _patch_wraps(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine: Engine,
    sqlite_session: Session,
):
    wraps_features = SimpleNamespace(enable_email_password_login=True)
    console_dify = SimpleNamespace(DEPLOYMENT_EDITION=DeploymentEdition.ENTERPRISE)
    web_dify = SimpleNamespace(DEPLOYMENT_EDITION=DeploymentEdition.ENTERPRISE)
    sqlite_session.add(DifySetup(version="test"))
    sqlite_session.commit()
    console_wraps._is_setup_completed.reset_success()
    session_registry = scoped_session(sessionmaker(bind=sqlite_engine, expire_on_commit=False))
    monkeypatch.setattr(console_wraps.db, "session", session_registry)
    with (
        patch("controllers.console.wraps.dify_config", console_dify),
        patch(
            "controllers.console.wraps.SystemFeatureService.is_email_password_login_enabled",
            return_value=wraps_features.enable_email_password_login,
        ),
        patch("controllers.web.login.dify_config", web_dify),
    ):
        yield
    session_registry.remove()
    console_wraps._is_setup_completed.reset_success()


class TestEmailCodeLoginSendEmailApi:
    @patch("controllers.web.login.WebAppAuthService.send_email_code_login_email")
    @patch("controllers.web.login.WebAppAuthService.get_user_through_email")
    def test_should_fetch_account_with_original_email(
        self,
        mock_get_user,
        mock_send_email,
        app: Flask,
    ):
        account = Account(name="Test User", email="user@example.com")
        mock_get_user.return_value = account
        mock_send_email.return_value = "token-123"

        with app.test_request_context(
            "/web/email-code-login",
            method="POST",
            json={"email": "User@Example.com", "language": "en-US"},
        ):
            response = EmailCodeLoginSendEmailApi().post()

        assert response == {"result": "success", "data": "token-123"}
        mock_get_user.assert_called_once_with("User@Example.com", ANY)
        mock_send_email.assert_called_once_with(account=account, language="en-US")


class TestEmailCodeLoginApi:
    @patch("controllers.web.login.AccountService.reset_login_error_rate_limit")
    @patch("controllers.web.login.WebAppAuthService.login", return_value="new-access-token")
    @patch("controllers.web.login.WebAppAuthService.get_user_through_email")
    @patch("controllers.web.login.WebAppAuthService.revoke_email_code_login_token")
    @patch("controllers.web.login.WebAppAuthService.get_email_code_login_data")
    def test_should_normalize_email_before_validating(
        self,
        mock_get_token_data,
        mock_revoke_token,
        mock_get_user,
        mock_login,
        mock_reset_login_rate,
        app: Flask,
    ):
        mock_get_token_data.return_value = {"email": "User@Example.com", "code": "123456"}
        mock_get_user.return_value = Account(name="Test User", email="user@example.com")

        with app.test_request_context(
            "/web/email-code-login/validity",
            method="POST",
            json={"email": "User@Example.com", "code": encode_code("123456"), "token": "token-123"},
        ):
            response = EmailCodeLoginApi().post()

        assert response == {"result": "success", "data": {"access_token": "new-access-token"}}
        mock_get_user.assert_called_once_with("User@Example.com", ANY)
        mock_revoke_token.assert_called_once_with("token-123")
        mock_login.assert_called_once()
        mock_reset_login_rate.assert_called_once_with("user@example.com")


class TestLoginApi:
    @patch("controllers.web.login.WebAppAuthService.login", return_value="access-tok")
    @patch("controllers.web.login.WebAppAuthService.authenticate")
    def test_login_success(self, mock_auth: MagicMock, mock_login: MagicMock, app: Flask) -> None:
        mock_auth.return_value = Account(name="Test User", email="user@example.com")

        with app.test_request_context(
            "/web/login",
            method="POST",
            json={"email": "user@example.com", "password": base64.b64encode(b"Valid1234").decode()},
        ):
            response = LoginApi().post()

        assert response["data"]["access_token"] == "access-tok"
        mock_auth.assert_called_once()

    @patch(
        "controllers.web.login.WebAppAuthService.authenticate",
        side_effect=services.errors.account.AccountLoginError(),
    )
    def test_login_banned_account(self, mock_auth: MagicMock, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        from controllers.console.error import AccountBannedError

        with caplog.at_level(logging.WARNING, logger="controllers.web.login"):
            with app.test_request_context(
                "/web/login",
                method="POST",
                json={"email": "user@example.com", "password": base64.b64encode(b"Valid1234").decode()},
            ):
                with pytest.raises(AccountBannedError):
                    LoginApi().post()

        assert_login_failure_logged(caplog, "user@example.com", LoginFailureReason.ACCOUNT_BANNED)

    @patch(
        "controllers.web.login.WebAppAuthService.authenticate",
        side_effect=services.errors.account.AccountPasswordError(),
    )
    def test_login_wrong_password(self, mock_auth: MagicMock, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        from controllers.console.auth.error import AuthenticationFailedError

        with caplog.at_level(logging.WARNING, logger="controllers.web.login"):
            with app.test_request_context(
                "/web/login",
                method="POST",
                json={"email": "user@example.com", "password": base64.b64encode(b"Valid1234").decode()},
            ):
                with pytest.raises(AuthenticationFailedError):
                    LoginApi().post()

        assert_login_failure_logged(caplog, "user@example.com", LoginFailureReason.INVALID_CREDENTIALS)

    @patch(
        "controllers.web.login.WebAppAuthService.authenticate",
        side_effect=services.errors.account.AccountNotFoundError(),
    )
    def test_login_account_not_found(self, mock_auth: MagicMock, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        from controllers.console.auth.error import AuthenticationFailedError

        with caplog.at_level(logging.WARNING, logger="controllers.web.login"):
            with app.test_request_context(
                "/web/login",
                method="POST",
                json={"email": "missing@example.com", "password": base64.b64encode(b"Valid1234").decode()},
            ):
                with pytest.raises(AuthenticationFailedError):
                    LoginApi().post()

        assert_login_failure_logged(caplog, "missing@example.com", LoginFailureReason.ACCOUNT_NOT_FOUND)

    @patch("controllers.web.login.WebAppAuthService.get_email_code_login_data", return_value=None)
    def test_email_code_login_logs_invalid_token(
        self, mock_get_token_data: MagicMock, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="controllers.web.login"):
            with app.test_request_context(
                "/web/email-code-login/validity",
                method="POST",
                json={"email": "user@example.com", "code": encode_code("123456"), "token": "token-123"},
            ):
                with pytest.raises(InvalidTokenError):
                    EmailCodeLoginApi().post()

        mock_get_token_data.assert_called_once_with("token-123")
        assert_login_failure_logged(caplog, "user@example.com", LoginFailureReason.INVALID_EMAIL_CODE_TOKEN)

    @patch("controllers.web.login.WebAppAuthService.revoke_email_code_login_token")
    @patch(
        "controllers.web.login.WebAppAuthService.get_user_through_email",
        side_effect=Unauthorized("Account is banned."),
    )
    @patch(
        "controllers.web.login.WebAppAuthService.get_email_code_login_data",
        return_value={"email": "User@Example.com", "code": "123456"},
    )
    def test_email_code_login_logs_banned_account(
        self,
        mock_get_token_data: MagicMock,
        mock_get_user: MagicMock,
        mock_revoke_token: MagicMock,
        app: Flask,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from controllers.console.error import AccountBannedError

        with caplog.at_level(logging.WARNING, logger="controllers.web.login"):
            with app.test_request_context(
                "/web/email-code-login/validity",
                method="POST",
                json={"email": "User@Example.com", "code": encode_code("123456"), "token": "token-123"},
            ):
                with pytest.raises(AccountBannedError):
                    EmailCodeLoginApi().post()

        mock_get_token_data.assert_called_once_with("token-123")
        mock_revoke_token.assert_called_once_with("token-123")
        assert_login_failure_logged(caplog, "user@example.com", LoginFailureReason.ACCOUNT_BANNED)


class TestLoginStatusApi:
    @patch("controllers.web.login.extract_webapp_access_token", return_value=None)
    def test_no_app_code_returns_logged_in_false(self, mock_extract: MagicMock, app: Flask) -> None:
        with app.test_request_context("/web/login/status"):
            result = LoginStatusApi().get()

        assert result["logged_in"] is False
        assert result["app_logged_in"] is False

    @patch("controllers.web.login.extract_webapp_access_token", return_value=None)
    @patch("controllers.web.login.get_console_account_extend", return_value=None)
    def test_status_without_app_code_reports_console_identity_separately(
        self, mock_console: MagicMock, mock_webapp_token: MagicMock, app: Flask
    ) -> None:
        with app.test_request_context("/web/login/status"):
            result = LoginStatusApi().get()
        assert result["logged_in"] is False
        assert result["app_logged_in"] is False
        assert result["console_logged_in"] is False
        mock_console.assert_called_once()

    @patch("controllers.web.login.WebAppAuthExtendService.is_webapp_auth_enabled", return_value=False)
    @patch("controllers.web.login.WebAppAuthService.is_app_require_permission_check", return_value=True)
    @patch("controllers.web.login.AppService.get_app_id_by_code", return_value="app-1")
    @patch("controllers.web.login.get_console_account_extend", return_value=None)
    @patch("controllers.web.login.extract_webapp_access_token", return_value=None)
    def test_status_resolved_app_code_reports_actual_switch(
        self,
        mock_token: MagicMock,
        mock_console: MagicMock,
        mock_app_id: MagicMock,
        mock_permission: MagicMock,
        mock_switch: MagicMock,
        app: Flask,
    ) -> None:
        with app.test_request_context("/web/login/status?app_code=resolved&user_id=missing"):
            result = LoginStatusApi().get()
        assert result["logged_in"] is False
        assert result["app_logged_in"] is False
        assert result["console_logged_in"] is False
        assert result["webapp_auth_enabled_extend"] is False
        mock_token.assert_called_once()
        mock_console.assert_called_once()
        mock_app_id.assert_called_once()
        mock_permission.assert_called_once()
        mock_switch.assert_called_once_with("app-1")


class TestConsoleIdentityExtend:
    def test_only_valid_console_passport_cookie_resolves_account(
        self, app: Flask, sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from libs.passport import PassportService
        from services.webapp_console_identity_extend import get_console_account_extend

        account = Account(name="Console", email="console@example.com")
        tenant = Tenant(name="Console tenant")
        sqlite_session.add_all([account, tenant])
        sqlite_session.commit()
        sqlite_session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.OWNER))
        sqlite_session.commit()
        from configs import dify_config

        monkeypatch.setattr(dify_config, "SECRET_KEY", "unit-test-console-passport-secret")
        passport = PassportService()

        def token(sub: str, user_id: str, exp: datetime | None = None) -> str:
            return passport.issue(
                {"sub": sub, "user_id": user_id, "exp": exp or datetime.now(UTC) + timedelta(minutes=5)}
            )

        with app.test_request_context(
            "/", headers={"Authorization": f"Bearer {token('Console API Passport', account.id)}"}
        ):
            assert get_console_account_extend(request, session=sqlite_session) is None
        with app.test_request_context(
            "/", headers={"Cookie": f"access_token={token('WebApp API Passport', account.id)}"}
        ):
            assert get_console_account_extend(request, session=sqlite_session) is None
        with app.test_request_context(
            "/", headers={"Cookie": f"access_token={token('Console API Passport', 'missing')}"}
        ):
            assert get_console_account_extend(request, session=sqlite_session) is None
        with app.test_request_context(
            "/",
            headers={
                "Cookie": "access_token="
                + token("Console API Passport", account.id, datetime.now(UTC) - timedelta(minutes=1))
            },
        ):
            assert get_console_account_extend(request, session=sqlite_session) is None
        with app.test_request_context(
            "/", headers={"Cookie": f"access_token={token('Console API Passport', account.id)}"}
        ):
            assert get_console_account_extend(request, session=sqlite_session).id == account.id

    def test_webapp_cookie_and_passport_headers_cannot_be_console_identity(
        self, app: Flask, sqlite_session: Session
    ) -> None:
        from extensions.ext_database import db
        from services.webapp_console_identity_extend import get_console_account_extend

        end_user = SimpleNamespace(external_user_id=None)
        with app.test_request_context(
            "/", headers={"Authorization": "Bearer webapp-token", "Cookie": "webapp_access_token=app-token"}
        ):
            assert get_console_account_extend(request, session=db.session()) is None
        assert end_user.external_user_id is None

    @patch("controllers.web.login.decode_jwt_token")
    @patch("controllers.web.login.PassportService")
    @patch("controllers.web.login.WebAppAuthService.is_app_require_permission_check", return_value=False)
    @patch("controllers.web.login.AppService.get_app_id_by_code", return_value="app-1")
    @patch("controllers.web.login.extract_webapp_access_token", return_value="tok")
    def test_public_app_user_logged_in(
        self,
        mock_extract: MagicMock,
        mock_app_id: MagicMock,
        mock_perm: MagicMock,
        mock_passport: MagicMock,
        mock_decode: MagicMock,
        app: Flask,
    ) -> None:
        mock_decode.return_value = (MagicMock(), MagicMock())

        with app.test_request_context("/web/login/status?app_code=code1"):
            result = LoginStatusApi().get()

        assert result["logged_in"] is True
        assert result["app_logged_in"] is True

    @patch("controllers.web.login.decode_jwt_token", side_effect=Exception("bad"))
    @patch("controllers.web.login.PassportService")
    @patch("controllers.web.login.WebAppAuthService.is_app_require_permission_check", return_value=True)
    @patch("controllers.web.login.AppService.get_app_id_by_code", return_value="app-1")
    @patch("controllers.web.login.extract_webapp_access_token", return_value="tok")
    def test_private_app_passport_fails(
        self,
        mock_extract: MagicMock,
        mock_app_id: MagicMock,
        mock_perm: MagicMock,
        mock_passport_cls: MagicMock,
        mock_decode: MagicMock,
        app: Flask,
    ) -> None:
        mock_passport_cls.return_value.verify.side_effect = Exception("bad")

        with app.test_request_context("/web/login/status?app_code=code1"):
            result = LoginStatusApi().get()

        assert result["logged_in"] is False
        assert result["app_logged_in"] is False


class TestLogoutApi:
    @patch("controllers.web.login.clear_webapp_access_token_from_cookie")
    def test_logout_success(self, mock_clear: MagicMock, app: Flask) -> None:
        with app.test_request_context("/web/logout", method="POST"):
            response = LogoutApi().post()

        assert response.get_json() == {"result": "success"}
        mock_clear.assert_called_once()
