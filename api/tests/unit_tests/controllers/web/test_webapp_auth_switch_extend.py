"""extend: WebApp 访问认证开关在生成端点上的门禁行为单测。

覆盖三个 fork 强制登录挂点共用的门禁语义（以 ChatApi / WorkflowRunApi 为代表）：
- 开关开启（默认）+ 未登录 Console -> 401 WebAuthRequiredErrorExtend；
- 开关关闭 + 未登录 Console -> 放行（匿名访问，走上游公开 WebApp 语义）；
- 已登录 Console -> 短路，不查询开关（保持既有行为与计费归因）。

conftest 的 autouse fixture 默认把 is_end_login 桩为已登录，这里按用例覆写为 None。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from controllers.web.completion import ChatApi, CompletionApi
from controllers.web.error_extend import WebAuthRequiredErrorExtend
from controllers.web.workflow import WorkflowRunApi
from services.webapp_auth_service_extend import WebAppAuthExtendService


def _chat_app() -> SimpleNamespace:
    return SimpleNamespace(id="app-1", mode="chat")


def _workflow_app() -> SimpleNamespace:
    return SimpleNamespace(id="app-1", mode="workflow")


def _completion_app() -> SimpleNamespace:
    return SimpleNamespace(id="app-1", mode="completion")


def _end_user() -> SimpleNamespace:
    return SimpleNamespace(id="eu-1")


@pytest.mark.parametrize(
    ("endpoint", "module_name", "app_factory", "payload"),
    [
        (CompletionApi, "controllers.web.completion", _completion_app, {"inputs": {}, "query": "hi"}),
        (ChatApi, "controllers.web.completion", _chat_app, {"inputs": {}, "query": "hi"}),
        (WorkflowRunApi, "controllers.web.workflow", _workflow_app, {"inputs": {}}),
    ],
)
@pytest.mark.parametrize("auth_enabled", [None, True, False])
@pytest.mark.parametrize("logged_in", [False, True])
def test_generation_auth_matrix(
    endpoint,
    module_name: str,
    app_factory,
    payload: dict,
    auth_enabled: bool | None,
    logged_in: bool,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three generation routes honor NULL/true/false auth for anonymous and Console users."""
    module = __import__(module_name, fromlist=["*"])
    user = SimpleNamespace(id="console-account") if logged_in else None
    monkeypatch.setattr(module, "is_end_login", lambda _end_user: user)
    monkeypatch.setattr(module, "is_money_limit", lambda _end_user: False)
    monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", lambda _app_id: auth_enabled is not False)
    monkeypatch.setattr(module.AppGenerateService, "generate", lambda **_kwargs: "generated")
    monkeypatch.setattr(module.AppGenerateServiceExtend, "calculate_cumulative_usage", lambda **_kwargs: None)
    monkeypatch.setattr(module.helper, "compact_generate_response", lambda _response: {"ok": True})

    request_path = {CompletionApi: "/completion-messages", ChatApi: "/chat-messages", WorkflowRunApi: "/workflows/run"}[
        endpoint
    ]
    with app.test_request_context(request_path, method="POST", json=payload):
        if not logged_in and auth_enabled is not False:
            with pytest.raises(WebAuthRequiredErrorExtend):
                endpoint().post(app_factory(), _end_user())
        else:
            assert endpoint().post(app_factory(), _end_user()) == {"ok": True}


class TestChatApiWebAppAuthSwitch:
    def test_webapp_bearer_does_not_bypass_console_login_gate(
        self, app: Flask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("controllers.web.completion.is_end_login", lambda _end_user: None)
        monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", lambda _app_id: True)
        with app.test_request_context(
            "/chat-messages", method="POST", json={}, headers={"Authorization": "Bearer webapp-access-token"}
        ):
            with pytest.raises(WebAuthRequiredErrorExtend):
                ChatApi().post(_chat_app(), _end_user())

    def test_anonymous_blocked_when_auth_enabled(self, app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("controllers.web.completion.is_end_login", lambda _end_user: None)
        monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", lambda _app_id: True)

        with app.test_request_context("/chat-messages", method="POST"):
            with pytest.raises(WebAuthRequiredErrorExtend):
                ChatApi().post(_chat_app(), _end_user())

    @patch("controllers.web.completion.helper.compact_generate_response", return_value={"answer": "reply"})
    @patch("controllers.web.completion.AppGenerateService.generate", return_value="response")
    @patch("controllers.web.completion.web_ns")
    def test_anonymous_allowed_when_auth_disabled(
        self,
        mock_ns: MagicMock,
        mock_gen: MagicMock,
        mock_compact: MagicMock,
        app: Flask,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("controllers.web.completion.is_end_login", lambda _end_user: None)
        switch = MagicMock(return_value=False)
        monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", switch)
        mock_ns.payload = {"inputs": {}, "query": "hi"}

        with app.test_request_context("/chat-messages", method="POST"):
            result = ChatApi().post(_chat_app(), _end_user())

        mock_compact.assert_called_once()
        mock_gen.assert_called_once()
        assert result == {"answer": "reply"}
        switch.assert_called_once_with("app-1")
        # 匿名放行时不做计费归因
        assert "account_id" not in mock_gen.call_args.kwargs["args"]

    @patch("controllers.web.completion.helper.compact_generate_response", return_value={"answer": "reply"})
    @patch("controllers.web.completion.AppGenerateService.generate", return_value="response")
    @patch("controllers.web.completion.web_ns")
    def test_logged_in_skips_switch_lookup(
        self,
        mock_ns: MagicMock,
        mock_gen: MagicMock,
        mock_compact: MagicMock,
        app: Flask,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # conftest 已把 is_end_login 桩为已登录账户；这里仅验证开关查询被短路
        switch = MagicMock(return_value=True)
        monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", switch)
        mock_ns.payload = {"inputs": {}, "query": "hi"}

        with app.test_request_context("/chat-messages", method="POST"):
            result = ChatApi().post(_chat_app(), _end_user())

        mock_compact.assert_called_once()
        mock_gen.assert_called_once()
        assert result == {"answer": "reply"}
        switch.assert_not_called()
        # 已登录时保留计费归因
        assert mock_gen.call_args.kwargs["args"]["account_id"] == "test-account-id"


class TestWorkflowRunApiWebAppAuthSwitch:
    def test_webapp_bearer_does_not_bypass_console_login_gate(
        self, app: Flask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("controllers.web.workflow.is_end_login", lambda _end_user: None)
        monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", lambda _app_id: True)
        with app.test_request_context(
            "/workflows/run", method="POST", json={}, headers={"Authorization": "Bearer webapp-access-token"}
        ):
            with pytest.raises(WebAuthRequiredErrorExtend):
                WorkflowRunApi().post(_workflow_app(), _end_user())

    def test_anonymous_blocked_when_auth_enabled(self, app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("controllers.web.workflow.is_end_login", lambda _end_user: None)
        monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", lambda _app_id: True)

        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(WebAuthRequiredErrorExtend):
                WorkflowRunApi().post(_workflow_app(), _end_user())

    @patch("controllers.web.workflow.helper.compact_generate_response", return_value={"result": "ok"})
    @patch("controllers.web.workflow.AppGenerateService.generate", return_value="response")
    @patch("controllers.web.workflow.web_ns")
    def test_anonymous_allowed_when_auth_disabled(
        self,
        mock_ns: MagicMock,
        mock_gen: MagicMock,
        mock_compact: MagicMock,
        app: Flask,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("controllers.web.workflow.is_end_login", lambda _end_user: None)
        switch = MagicMock(return_value=False)
        monkeypatch.setattr(WebAppAuthExtendService, "is_webapp_auth_enabled", switch)
        mock_ns.payload = {"inputs": {}}

        with app.test_request_context("/workflows/run", method="POST"):
            result = WorkflowRunApi().post(_workflow_app(), _end_user())

        mock_gen.assert_called_once()
        mock_compact.assert_called_once()
        assert result == {"result": "ok"}
        switch.assert_called_once_with("app-1")
