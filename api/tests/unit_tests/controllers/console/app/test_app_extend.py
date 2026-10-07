"""Console context-marker authorization and schema regression tests."""

from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from controllers.common.rbac import RBACPermission, checks, locators
from controllers.console.app import app_extend as controller
from libs import login
from models.agent import AgentScope
from tests.unit_tests.config_override import apply_config_overrides


@pytest.fixture
def context_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    service = MagicMock()
    service.message_context_app.return_value = SimpleNamespace(id="trusted-app")
    service.message_context.return_value = ["new", "old"]
    service.delete_message_context.return_value = "ok"
    monkeypatch.setattr(controller, "RecommendedAppService", service)
    monkeypatch.setattr(
        controller,
        "current_account_with_tenant",
        lambda: (SimpleNamespace(id="account", has_edit_permission=True), "tenant"),
    )
    apply_config_overrides(monkeypatch, RBAC_ENABLED=True)
    monkeypatch.setattr(locators.RBACResourceService, "get_app_agent_binding", lambda *_: None)
    monkeypatch.setattr(locators.RBACResourceService, "get_app_maintainer", lambda *_: None)
    monkeypatch.setattr(checks.RBACService.CheckAccess, "check", lambda *_args, **_kwargs: True)
    return service


def invoke(app: Flask, method: str, query: dict[str, str]) -> object:
    resource = controller.MessageContextApi()
    with app.test_request_context("/message/context", method=method.upper(), query_string=query):
        return unwrap(getattr(resource, method))(resource)


@pytest.mark.parametrize(
    ("method", "query"),
    [
        ("get", {}),
        ("get", {"conversation_id": ""}),
        ("delete", {}),
        ("delete", {"conversation_id": "c"}),
        ("delete", {"message_id": "m"}),
        ("delete", {"conversation_id": "c", "message_id": ""}),
        ("delete", {"conversation_id": "", "message_id": "m"}),
    ],
)
def test_missing_ids_stay_bad_request(
    app: Flask, context_service: MagicMock, method: str, query: dict[str, str]
) -> None:
    with pytest.raises(BadRequest):
        invoke(app, method, query)
    assert context_service.mock_calls == []


@pytest.mark.parametrize("method", ["get", "delete"])
def test_unresolved_conversation_never_accesses_context(app: Flask, context_service: MagicMock, method: str) -> None:
    context_service.message_context_app.side_effect = NotFound()
    with pytest.raises(NotFound):
        invoke(app, method, {"conversation_id": "missing", "message_id": "m"})
    context_service.message_context.assert_not_called()
    context_service.delete_message_context.assert_not_called()


@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("allowed", [False, True])
def test_rbac_uses_resolved_owner_and_operation(
    app: Flask, monkeypatch: pytest.MonkeyPatch, context_service: MagicMock, method: str, agent: bool, allowed: bool
) -> None:
    binding = SimpleNamespace(id="agent", scope=AgentScope.ROSTER) if agent else None
    monkeypatch.setattr(locators.RBACResourceService, "get_app_agent_binding", lambda *_: binding)
    access = MagicMock(return_value=allowed)
    monkeypatch.setattr(checks.RBACService.CheckAccess, "check", access)
    query = {"conversation_id": "conversation", "message_id": "message", "app_id": "attacker-app"}
    if allowed:
        assert invoke(app, method, query) == (["new", "old"] if method == "get" else "ok")
        kwargs = {"tenant_id": "tenant", "app_id": "trusted-app", "conversation_id": "conversation"}
        if method == "get":
            context_service.message_context.assert_called_once_with(**kwargs)
        else:
            context_service.delete_message_context.assert_called_once_with(**kwargs, message_id="message")
    else:
        with pytest.raises(Forbidden):
            invoke(app, method, query)
        context_service.message_context.assert_not_called()
        context_service.delete_message_context.assert_not_called()
    scene = (
        (RBACPermission.AGENT_TEST_AND_RUN if method == "get" else RBACPermission.AGENT_EDIT)
        if agent
        else (RBACPermission.APP_VIEW_LAYOUT if method == "get" else RBACPermission.APP_EDIT)
    )
    assert access.call_args.args == ("tenant", "account")
    assert access.call_args.kwargs["scene"] == scene
    assert access.call_args.kwargs["resource_id"] == ("agent" if agent else "trusted-app")
    context_service.message_context_app.assert_called_once_with(tenant_id="tenant", conversation_id="conversation")


@pytest.mark.parametrize("method", ["get", "delete"])
def test_legacy_member_forbidden(
    app: Flask, monkeypatch: pytest.MonkeyPatch, context_service: MagicMock, method: str
) -> None:
    apply_config_overrides(monkeypatch, RBAC_ENABLED=False)
    monkeypatch.setattr(
        controller, "current_account_with_tenant", lambda: (SimpleNamespace(has_edit_permission=False), "tenant")
    )
    with pytest.raises(Forbidden):
        invoke(app, method, {"conversation_id": "c", "message_id": "m"})
    context_service.message_context.assert_not_called()
    context_service.delete_message_context.assert_not_called()


@pytest.mark.parametrize("method", ["get", "delete"])
def test_anonymous_request_rejected_by_login(
    app: Flask, monkeypatch: pytest.MonkeyPatch, context_service: MagicMock, method: str
) -> None:
    apply_config_overrides(monkeypatch, LOGIN_DISABLED=False)
    monkeypatch.setattr("controllers.console.wraps._is_setup_completed", lambda: True)
    monkeypatch.setattr(login, "_resolve_current_user", lambda: None)
    manager = MagicMock()
    manager.unauthorized.return_value = app.response_class(status=401)
    monkeypatch.setattr(login, "_get_login_manager", lambda: manager)
    with app.test_request_context("/message/context", method=method.upper()):
        response = getattr(controller.MessageContextApi(), method)()
    assert response.status_code == 401
    assert context_service.mock_calls == []


def test_documented_query_and_response_shapes() -> None:
    for method, query_model, response_model in [
        ("get", controller.MessageContextQuery, controller.MessageContextResponse),
        ("delete", controller.DeleteMessageContextQuery, controller.DeleteMessageContextResponse),
    ]:
        docs = getattr(controller.MessageContextApi, method).__apidoc__
        assert set(docs["params"]) == set(query_model.model_fields)
        assert all(param["required"] and param["in"] == "query" for param in docs["params"].values())
        assert docs["responses"]["200"][1].name == response_model.__name__
    assert controller.MessageContextResponse.model_json_schema()["type"] == "array"
    assert controller.MessageContextResponse.model_json_schema()["items"] == {"type": "string"}
    assert controller.DeleteMessageContextResponse.model_json_schema()["type"] == "string"


@pytest.mark.parametrize("method", ["get", "delete"])
def test_legacy_editor_allowed(
    app: Flask, monkeypatch: pytest.MonkeyPatch, context_service: MagicMock, method: str
) -> None:
    apply_config_overrides(monkeypatch, RBAC_ENABLED=False)
    assert invoke(app, method, {"conversation_id": "c", "message_id": "m"}) == (
        ["new", "old"] if method == "get" else "ok"
    )
    context_service.message_context_app.assert_called_once_with(tenant_id="tenant", conversation_id="c")


@pytest.mark.parametrize("method", ["get", "delete"])
def test_workflow_only_agent_binding_has_no_context_access(
    app: Flask, monkeypatch: pytest.MonkeyPatch, context_service: MagicMock, method: str
) -> None:
    monkeypatch.setattr(
        locators.RBACResourceService,
        "get_app_agent_binding",
        lambda *_: SimpleNamespace(id="agent", scope=AgentScope.WORKFLOW_ONLY),
    )
    with pytest.raises(NotFound):
        invoke(app, method, {"conversation_id": "c", "message_id": "m"})
    context_service.message_context.assert_not_called()
    context_service.delete_message_context.assert_not_called()
