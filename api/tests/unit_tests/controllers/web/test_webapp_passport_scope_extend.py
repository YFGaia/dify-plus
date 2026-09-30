"""Signed WebApp passports must honor the caller's explicit app scope."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from flask import Flask
from sqlalchemy.orm import Session
from werkzeug.exceptions import Unauthorized

from controllers.web import wraps
from controllers.web.error import WebAppAuthAccessDeniedError
from libs.passport import PassportService
from libs.token import _real_cookie_name
from models.enums import EndUserType
from models.model import App, AppMode, CustomizeTokenStrategy, EndUser, Site


@pytest.fixture
def passport_apps(sqlite_session: Session, config_overrides, monkeypatch):
    config_overrides(SECRET_KEY="synthetic-webapp-scope-test-key-at-least-32-bytes")
    monkeypatch.setattr(wraps.SystemFeatureService, "is_webapp_auth_enabled", lambda: False)
    fixtures = []
    for mode in (AppMode.COMPLETION, AppMode.CHAT, AppMode.WORKFLOW):
        app = App(
            tenant_id=str(uuid4()), name=f"Scope {mode.value}", mode=mode.value, enable_site=True, enable_api=True
        )
        sqlite_session.add(app)
        sqlite_session.flush()
        site = Site(
            app_id=app.id,
            title=app.name,
            default_language="en-US",
            customize_token_strategy=CustomizeTokenStrategy.NOT_ALLOW,
            code=f"scope-{mode.value}",
        )
        user = EndUser(tenant_id=app.tenant_id, app_id=app.id, type=EndUserType.BROWSER, session_id="browser-session")
        sqlite_session.add_all((site, user))
        sqlite_session.flush()
        token = PassportService().issue(
            {
                "app_code": site.code,
                "app_id": app.id,
                "end_user_id": user.id,
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            }
        )
        fixtures.append((app.id, user.id, site.code, token))
    sqlite_session.commit()
    return fixtures


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("source", ["header", "query", "cookie", "no_explicit_code"])
def test_same_app_passport_resolves_real_app_and_anonymous_browser_user(passport_apps, index: int, source: str):
    app_id, user_id, code, token = passport_apps[index]
    headers = {"X-App-Passport": token}
    argument = None
    if source == "query":
        argument = code
    elif source != "no_explicit_code":
        headers["X-App-Code"] = code
    if source == "cookie":
        headers = {"X-App-Code": code, "Cookie": f"{_real_cookie_name('passport-' + code)}={token}"}
    with Flask(__name__).test_request_context("/site", headers=headers):
        app, user = wraps.decode_jwt_token(app_code=argument, user_id="browser-session")
    assert (app.id, user.id) == (app_id, user_id)


@pytest.mark.parametrize("pair", [(0, 1), (1, 0)])
@pytest.mark.parametrize("source", ["header", "query", "conflicting_header_and_query"])
@pytest.mark.parametrize("enterprise_auth", [False, True])
def test_cross_app_scope_rejected_before_database_or_enterprise_dispatch(
    passport_apps, pair, source: str, enterprise_auth: bool, monkeypatch
):
    _, _, token_code, token = passport_apps[pair[0]]
    _, _, requested_code, _ = passport_apps[pair[1]]
    headers = {"X-App-Passport": token}
    argument = None
    if source == "query":
        argument = requested_code
    else:
        headers["X-App-Code"] = requested_code
        if source == "conflicting_header_and_query":
            argument = token_code
    monkeypatch.setattr(wraps.SystemFeatureService, "is_webapp_auth_enabled", lambda: enterprise_auth)
    with (
        Flask(__name__).test_request_context("/site", headers=headers),
        patch.object(wraps.session_factory, "create_session") as create_session,
        patch.object(wraps.AppService, "get_app_id_by_code") as get_app_id,
        pytest.raises(WebAppAuthAccessDeniedError, match="requested app") as error,
    ):
        wraps.decode_jwt_token(app_code=argument)
    assert error.value.code == 401
    create_session.assert_not_called()
    get_app_id.assert_not_called()


@pytest.mark.parametrize("token_kind", ["missing", "invalid_signature"])
def test_missing_and_wrong_signature_passports_remain_unauthorized(passport_apps, token_kind: str):
    _, _, code, token = passport_apps[0]
    headers = {"X-App-Code": code}
    if token_kind == "invalid_signature":
        config = PassportService()
        config.sk = "different-synthetic-signing-key-at-least-32-bytes"
        headers["X-App-Passport"] = config.issue(PassportService().verify(token))
    with Flask(__name__).test_request_context("/site", headers=headers), pytest.raises(Unauthorized):
        wraps.decode_jwt_token()
