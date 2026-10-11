"""Endpoint and schema tests for DingTalk-owned email lookup configuration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask
from pydantic import ValidationError
from werkzeug.exceptions import BadRequest, Forbidden

from controllers.console.dingtalk_schemas_extend import (
    DingTalkConfigPayload,
    EmailLookupTestPayload,
)
from controllers.console.system_manage_extend import DingTalkConfigExtend, EmailApiTestExtend
from models.account import TenantAccountRole
from services.dingtalk_auth_service_extend import DingTalkAuthError


@pytest.fixture
def system_admin(app: Flask, monkeypatch: pytest.MonkeyPatch):
    del app  # Keep the Flask app fixture initialized before this fixture.
    import controllers.console.system_manage_extend as controller_module
    from controllers.console import wraps as wraps_module

    account = MagicMock()
    account.id = "system-admin"
    account.current_role = TenantAccountRole.ADMIN
    account.is_admin_or_owner = True
    monkeypatch.setattr(wraps_module.dify_config, "DEPLOYMENT_EDITION", "CLOUD")
    monkeypatch.setattr("libs.login.dify_config.LOGIN_DISABLED", True)
    monkeypatch.setattr(wraps_module, "current_account_with_tenant", lambda: (account, "tenant-1"))
    monkeypatch.setattr(controller_module, "current_user", account)

    def can_manage(actor, session):
        del actor, session
        return True

    monkeypatch.setattr(controller_module.SystemManagementAccessService, "can_manage", can_manage)
    return account


def test_dingtalk_payload_accepts_email_lookup_and_future_config_fields():
    config = {
        "email_api": {
            "enabled": True,
            "url": "https://directory.example.test/email",
            "request_param_field": "unionId",
            "response_email_field": "data[0].mail",
            "future_provider_option": {"region": "synthetic"},
        },
        "future_dingtalk_option": "preserved",
    }

    payload = DingTalkConfigPayload.model_validate({"config": config})

    assert payload.model_dump(exclude_none=True) == {"config": config}


def test_dingtalk_payload_rejects_unknown_top_level_fields():
    with pytest.raises(ValidationError):
        DingTalkConfigPayload.model_validate({"email_api": {"url": "https://example.test"}})


def test_email_test_payload_accepts_config_and_legacy_url_shape():
    assert (
        EmailLookupTestPayload.model_validate(
            {"config": {"enabled": True, "url": "https://directory.example.test"}, "user_id": "ding-1"}
        ).user_id
        == "ding-1"
    )
    assert (
        EmailLookupTestPayload.model_validate({"url": "https://directory.example.test", "key": "synthetic-key"}).url
        == "https://directory.example.test"
    )


def test_dingtalk_post_persists_config_under_integration(monkeypatch, app, system_admin):
    import controllers.console.system_manage_extend as controller_module

    save = MagicMock(return_value={"result": "success", "revision": "new-revision"})
    monkeypatch.setattr(controller_module, "DingTalkAuthService", lambda: SimpleNamespace(save=save))
    config = {
        "email_api": {
            "enabled": True,
            "url": "https://directory.example.test/email",
            "method": "GET",
            "request_param_field": "userId",
            "response_email_field": "data[0].email",
        }
    }

    with app.test_request_context(
        "/console/api/system-manage-extend/integration/dingtalk",
        method="POST",
        json={"app_key": "synthetic-app", "config": config},
    ):
        body, status = DingTalkConfigExtend().post()

    assert status == 200
    assert body["result"] == "success"
    assert body["revision"] == "new-revision"
    save.assert_called_once_with(system_admin, {"app_key": "synthetic-app", "config": config})


@pytest.mark.usefixtures("system_admin")
def test_dingtalk_post_rejects_invalid_email_lookup_configuration(monkeypatch, app):
    import controllers.console.system_manage_extend as controller_module

    save = MagicMock(side_effect=DingTalkAuthError("invalid_email_lookup"))
    monkeypatch.setattr(controller_module, "DingTalkAuthService", lambda: SimpleNamespace(save=save))
    with app.test_request_context(
        "/console/api/system-manage-extend/integration/dingtalk",
        method="POST",
        json={"config": {"email_api": {"enabled": True, "url": "file:///etc/passwd"}}},
    ):
        with pytest.raises(BadRequest):
            DingTalkConfigExtend().post()


@pytest.mark.usefixtures("system_admin")
def test_email_test_endpoint_uses_draft_config_and_user_id(monkeypatch, app):
    import controllers.console.system_manage_extend as controller_module

    lookup = MagicMock(
        return_value={
            "result": "success",
            "status_code": 200,
            "email": "user@example.test",
            "message": "Email lookup succeeded.",
        }
    )
    monkeypatch.setattr(controller_module, "lookup_email", lookup)
    config = {"enabled": True, "url": "https://directory.example.test/email"}
    with app.test_request_context(
        "/console/api/system-manage-extend/integration/email-api/test",
        method="POST",
        json={"config": config, "user_id": "ding-456"},
    ):
        body, status = EmailApiTestExtend().post()

    assert status == 200
    assert body["result"] == "success"
    assert body["email"] == "user@example.test"
    lookup.assert_called_once_with("ding-456", config)


def test_dingtalk_endpoint_permission_is_enforced(app, system_admin, monkeypatch):
    import controllers.console.system_manage_extend as controller_module

    system_admin.current_role = TenantAccountRole.NORMAL

    def cannot_manage(actor, session):
        del actor, session
        return False

    monkeypatch.setattr(controller_module.SystemManagementAccessService, "can_manage", cannot_manage)
    with app.test_request_context("/console/api/system-manage-extend/integration/dingtalk", method="GET"):
        with pytest.raises(Forbidden):
            DingTalkConfigExtend().get()
