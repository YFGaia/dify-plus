"""HTTP navigation uses state-bound service results and never exposes tokens in URLs."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controllers.console.app import ding_talk_extend as controller
from services.dingtalk_auth_service_extend import DingTalkAuthError


@pytest.mark.parametrize("code", ["invalid_transaction", "configuration_changed", "login_disabled"])
def test_callback_failure_issues_no_authentication_cookie(app, monkeypatch, code):
    service = Mock()
    service.consume.side_effect = DingTalkAuthError(code)
    monkeypatch.setattr(controller, "DingTalkAuthService", lambda: service)
    monkeypatch.setattr(controller.dify_config, "CONSOLE_WEB_URL", "https://console.example.test")
    # setup_required is exercised by existing setup tests; this test owns callback transport.
    with app.test_request_context("/console/api/auth/dingtalk/callback?authCode=code"):
        response = controller.DingTalk.get.__wrapped__(controller.DingTalk())
    assert response.status_code == 302
    assert response.location == "https://console.example.test/signin?dingtalk_error=" + code
    assert not response.headers.getlist("Set-Cookie")
    service.complete.assert_not_called()


def test_legacy_callback_requires_state(app, monkeypatch):
    service = Mock()
    service.consume.side_effect = DingTalkAuthError("invalid_transaction")
    monkeypatch.setattr(controller, "DingTalkAuthService", lambda: service)
    with app.test_request_context("/console/api/ding-talk/third-party/login?authCode=code"):
        controller.DingTalk.get.__wrapped__(controller.DingTalk())
    service.consume.assert_called_once_with("", None)
    service.complete.assert_not_called()


def test_diagnostic_callback_keeps_local_session(app, monkeypatch):
    service = Mock()
    service.consume.return_value = {"source": {"account_id": "owner"}}
    service.complete.return_value = None
    service.cookie_name.return_value = "dingtalk_auth_binding"
    service.callback_url.return_value = "https://console.example.test/console/api/auth/dingtalk/callback"
    monkeypatch.setattr(controller, "DingTalkAuthService", lambda: service)
    monkeypatch.setattr(controller, "current_user", SimpleNamespace(id="owner"))
    monkeypatch.setattr(controller.dify_config, "CONSOLE_WEB_URL", "https://console.example.test")
    setters = [Mock(), Mock(), Mock()]
    for name, setter in zip(
        ("set_access_token_to_cookie", "set_refresh_token_to_cookie", "set_csrf_token_to_cookie"), setters
    ):
        monkeypatch.setattr(controller, name, setter)
    with app.test_request_context("/console/api/auth/dingtalk/callback?state=" + "a" * 43 + "&authCode=code"):
        response = controller.DingTalk.get.__wrapped__(controller.DingTalk())
    assert response.location.endswith("/system-manage-extend/system-integration?tab=dingtalk&dingtalk_result=success")
    for setter in setters:
        setter.assert_not_called()
    assert "Max-Age=0" in response.headers["Set-Cookie"]


def test_public_display_fails_closed_and_is_not_cached(app, monkeypatch):
    service = Mock()
    service.display.side_effect = RuntimeError("provider-private-secret")
    monkeypatch.setattr(controller, "DingTalkAuthService", lambda: service)
    with app.test_request_context("/console/api/auth/dingtalk/display"):
        response = controller.DingTalkDisplay().get()
    assert response.json == {"enabled": False, "button_text": "钉钉", "start_path": "/console/api/auth/dingtalk/start"}
    assert response.headers["Cache-Control"] == "no-store"
