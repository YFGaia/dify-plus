"""Database and signed-token acceptance for built-in WebApp identity isolation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import scoped_session

from controllers.console import wraps as console_wraps
from controllers.web import completion, login, workflow
from controllers.web.completion import is_end_login as real_is_end_login
from controllers.web.error_extend import WebAuthRequiredErrorExtend
from enums import DeploymentEdition
from libs.passport import PassportService
from models.account import Account, Tenant, TenantAccountJoin
from models.enums import CustomizeTokenStrategy
from models.model import App, DifySetup, EndUser, Site
from models.model_extend import AppExtend
from services import webapp_auth_service_extend as switch_module


@pytest.fixture
def persisted(monkeypatch, config_overrides, sqlite_session_factory):
    config_overrides(
        SECRET_KEY="m03-signed-console-cookie-secret",
        DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY,
        RBAC_ENABLED=False,
    )
    console_wraps._is_setup_completed.reset_success()
    sessions = scoped_session(sqlite_session_factory)
    monkeypatch.setattr(login.db, "session", sessions)
    monkeypatch.setattr(switch_module, "redis_client", MagicMock(get=lambda _key: None))
    monkeypatch.setattr(completion, "is_end_login", real_is_end_login)
    monkeypatch.setattr(workflow, "is_end_login", real_is_end_login)
    with sqlite_session_factory() as session:
        account = Account(name="Console", email="console@example.com")
        tenant = Tenant(name="Workspace")
        session.add_all([account, tenant, DifySetup(version="M03-test")])
        session.commit()
        session.add(TenantAccountJoin(account_id=account.id, tenant_id=tenant.id, role="owner", current=True))
        app_row = App(tenant_id=tenant.id, name="Builtin", mode="chat", enable_site=True, enable_api=True)
        session.add(app_row)
        session.commit()
        session.add(
            Site(
                app_id=app_row.id,
                title="Builtin",
                code="valid-code",
                default_language="en-US",
                customize_token_strategy=CustomizeTokenStrategy.NOT_ALLOW,
            )
        )
        end_user = EndUser(app_id=app_row.id, tenant_id=tenant.id, type="browser", session_id="browser")
        session.add(end_user)
        session.commit()
        ids = SimpleNamespace(account_id=account.id, app_id=app_row.id, end_user_id=end_user.id)
    yield sessions, ids
    sessions.remove()
    console_wraps._is_setup_completed.reset_success()


def _cookie(account_id, *, subject="Console API Passport"):
    token = PassportService().issue(
        {"sub": subject, "user_id": account_id, "exp": datetime.now(UTC) + timedelta(minutes=5)}
    )
    return {"Cookie": f"access_token={token}"}


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("kind", ["completion", "chat", "workflow"])
def test_persisted_generation_matrix(app, persisted, monkeypatch, sqlite_session_factory, enabled, authenticated, kind):
    sessions, ids = persisted
    with sqlite_session_factory.begin() as session:
        session.add(AppExtend(id=str(uuid4()), app_id=ids.app_id, webapp_auth_enabled=enabled))
    module = workflow if kind == "workflow" else completion
    endpoint = {
        "workflow": workflow.WorkflowRunApi,
        "chat": completion.ChatApi,
        "completion": completion.CompletionApi,
    }[kind]
    generated = []
    monkeypatch.setattr(module, "is_money_limit", lambda _user: False)
    monkeypatch.setattr(module.AppGenerateService, "generate", lambda **kwargs: generated.append(kwargs) or "generated")
    monkeypatch.setattr(module.AppGenerateServiceExtend, "calculate_cumulative_usage", lambda **_kwargs: None)
    monkeypatch.setattr(module.helper, "compact_generate_response", lambda _response: {"ok": True})
    headers = _cookie(ids.account_id) if authenticated else {"Authorization": "Bearer webapp-only"}
    app_model = SimpleNamespace(id=ids.app_id, mode=kind)
    with app.test_request_context("/", method="POST", json={"inputs": {}, "query": "hello"}, headers=headers):
        user = sessions.get(EndUser, ids.end_user_id)
        if not authenticated and enabled is not False:
            with pytest.raises(WebAuthRequiredErrorExtend):
                endpoint().post(app_model, user)
            assert not generated
        else:
            assert endpoint().post(app_model, user) == {"ok": True}
            assert generated[-1]["args"].get("account_id") == (ids.account_id if authenticated else None)
    with sqlite_session_factory() as session:
        user = session.get(EndUser, ids.end_user_id)
        assert user.external_user_id == (ids.account_id if authenticated else None)


@pytest.mark.parametrize("enabled", [None, True, False])
def test_status_reads_real_site_and_app_switch(app, persisted, sqlite_session_factory, enabled):
    _sessions, ids = persisted
    with sqlite_session_factory.begin() as session:
        session.add(AppExtend(id=str(uuid4()), app_id=ids.app_id, webapp_auth_enabled=enabled))
    with app.test_request_context("/web/login/status?app_code=valid-code", headers=_cookie(ids.account_id)):
        response = login.LoginStatusApi().get()
    assert response["console_logged_in"] is True
    assert response["webapp_auth_enabled_extend"] is (enabled is not False)


@pytest.mark.usefixtures("persisted")
def test_missing_and_invalid_code_never_report_public_switch(app):
    with app.test_request_context("/web/login/status"):
        response = login.LoginStatusApi().get()
    assert response["webapp_auth_enabled_extend"] is True
    with app.test_request_context("/web/login/status?app_code=unknown"):
        with pytest.raises(ValueError, match="not found"):
            login.LoginStatusApi().get()


def test_apps_are_isolated_in_real_switch_lookup(app, persisted, sqlite_session_factory):
    _sessions, ids = persisted
    with sqlite_session_factory.begin() as session:
        session.add(AppExtend(id=str(uuid4()), app_id=ids.app_id, webapp_auth_enabled=False))
        other = App(tenant_id=str(uuid4()), name="Other", mode="chat", enable_site=True, enable_api=True)
        session.add(other)
        session.flush()
        session.add(
            Site(
                app_id=other.id,
                title="Other",
                code="other-code",
                default_language="en-US",
                customize_token_strategy=CustomizeTokenStrategy.NOT_ALLOW,
            )
        )
    with app.test_request_context("/web/login/status?app_code=valid-code"):
        assert login.LoginStatusApi().get()["webapp_auth_enabled_extend"] is False
    with app.test_request_context("/web/login/status?app_code=other-code"):
        assert login.LoginStatusApi().get()["webapp_auth_enabled_extend"] is True
