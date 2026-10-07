"""Unit tests for controllers.web.workflow endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from controllers.web.error import (
    NotWorkflowAppError,
    ProviderNotInitializeError,
    ProviderQuotaExceededError,
    TriggerWorkflowServiceModeUnavailableError,
)
from controllers.web.workflow import WorkflowRunApi, WorkflowTaskStopApi
from core.errors.error import ProviderTokenNotInitError, QuotaExceededError
from models.enums import EndUserType
from models.model import App, AppMode, EndUser
from services.errors.app import (
    TriggerWorkflowServiceModeUnavailableError as TriggerWorkflowServiceModeUnavailableServiceError,
)


def _workflow_app() -> App:
    return App(id="app-1", tenant_id="tenant-1", mode=AppMode.WORKFLOW)


def _chat_app() -> App:
    return App(id="app-1", tenant_id="tenant-1", mode=AppMode.CHAT)


def _end_user() -> EndUser:
    return EndUser(
        id="eu-1",
        tenant_id="tenant-1",
        type=EndUserType.BROWSER,
        session_id="session-1",
    )


# ---------------------------------------------------------------------------
# WorkflowRunApi
# ---------------------------------------------------------------------------
class TestWorkflowRunApi:
    def test_wrong_mode_raises(self, app: Flask) -> None:
        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(NotWorkflowAppError):
                WorkflowRunApi().post(_chat_app(), _end_user())

    @patch("controllers.web.workflow.helper.compact_generate_response", return_value={"result": "ok"})
    @patch("controllers.web.workflow.AppGenerateService.generate")
    @patch("controllers.web.workflow.web_ns")
    def test_happy_path(self, mock_ns: MagicMock, mock_gen: MagicMock, mock_compact: MagicMock, app: Flask) -> None:
        mock_ns.payload = {"inputs": {"key": "val"}}
        mock_gen.return_value = "response"

        with app.test_request_context("/workflows/run", method="POST"):
            result = WorkflowRunApi().post(_workflow_app(), _end_user())

        assert result == {"result": "ok"}

    @patch(
        "controllers.web.workflow.AppGenerateService.generate",
        side_effect=ProviderTokenNotInitError(description="not init"),
    )
    @patch("controllers.web.workflow.web_ns")
    def test_provider_not_init(self, mock_ns: MagicMock, mock_gen: MagicMock, app: Flask) -> None:
        mock_ns.payload = {"inputs": {}}

        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(ProviderNotInitializeError):
                WorkflowRunApi().post(_workflow_app(), _end_user())

    @patch(
        "controllers.web.workflow.AppGenerateService.generate",
        side_effect=TriggerWorkflowServiceModeUnavailableServiceError(),
    )
    @patch("controllers.web.workflow.web_ns")
    def test_trigger_workflow_returns_stable_unavailable_error(
        self,
        mock_ns: MagicMock,
        mock_gen: MagicMock,
        app: Flask,
    ) -> None:
        mock_ns.payload = {"inputs": {}}

        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(TriggerWorkflowServiceModeUnavailableError) as exc_info:
                WorkflowRunApi().post(_workflow_app(), _end_user())

        assert exc_info.value.code == 403
        assert exc_info.value.error_code == "trigger_workflow_service_mode_unavailable"

    @patch(
        "controllers.web.workflow.AppGenerateService.generate",
        side_effect=QuotaExceededError(),
    )
    @patch("controllers.web.workflow.web_ns")
    def test_quota_exceeded(self, mock_ns: MagicMock, mock_gen: MagicMock, app: Flask) -> None:
        mock_ns.payload = {"inputs": {}}

        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(ProviderQuotaExceededError):
                WorkflowRunApi().post(_workflow_app(), _end_user())


# ---------------------------------------------------------------------------
# WorkflowTaskStopApi
# ---------------------------------------------------------------------------
class TestWorkflowTaskStopApi:
    def test_wrong_mode_raises(self, app: Flask) -> None:
        with app.test_request_context("/workflows/tasks/task-1/stop", method="POST"):
            with pytest.raises(NotWorkflowAppError):
                WorkflowTaskStopApi().post(_chat_app(), _end_user(), "task-1")

    @patch("controllers.web.workflow.GraphEngineManager.send_stop_command")
    @patch("controllers.web.workflow.AppQueueManager.set_stop_flag_no_user_check")
    def test_stop_calls_both_mechanisms(self, mock_legacy: MagicMock, mock_graph: MagicMock, app: Flask) -> None:
        with app.test_request_context("/workflows/tasks/task-1/stop", method="POST"):
            result = WorkflowTaskStopApi().post(_workflow_app(), _end_user(), "task-1")

        assert result == {"result": "success"}
        mock_legacy.assert_called_once_with("task-1")
        mock_graph.assert_called_once_with("task-1")


@pytest.mark.parametrize("actor", [None, "account-a", "account-b"])
def test_workflow_account_actor_comes_from_current_console_identity(
    app: Flask, monkeypatch: pytest.MonkeyPatch, actor: str | None
) -> None:
    from types import SimpleNamespace

    user = _end_user()
    user.external_user_id = "old-bound-account"
    resolver = MagicMock(return_value=SimpleNamespace(id=actor) if actor else None)
    monkeypatch.setattr("controllers.web.workflow.is_end_login", resolver)
    with (
        patch("controllers.web.workflow.WebAppAuthExtendService.is_webapp_auth_enabled", return_value=False),
        patch("controllers.web.workflow.web_ns") as namespace,
        patch("controllers.web.workflow.AppGenerateService.generate") as generate,
        patch("controllers.web.workflow.AppGenerateServiceExtend.calculate_cumulative_usage"),
        patch("controllers.web.workflow.helper.compact_generate_response"),
        app.test_request_context("/workflows/run", method="POST"),
    ):
        namespace.payload = {
            "inputs": dict[str, str](),
            "account_id": "forged-account",
            "from_account_id": "forged-account",
        }
        WorkflowRunApi().post(_workflow_app(), user)
    resolver.assert_called_once_with(user)
    args = generate.call_args.kwargs["args"]
    assert args.get("account_id") == actor
    assert "from_account_id" not in args
    assert generate.call_args.kwargs["user"] is user
