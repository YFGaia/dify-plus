"""code-execution-control 端点单测（openspec p5-admin-decommission 任务 2.7）。

采用与 test_extension.py 相同的模式：绕过/替身化 console 守卫的外部依赖后直接调用
handler 方法。权限（403）分支通过真实的 `system_admin_required_extend` 装饰器覆盖
（非 admin/owner 的 current_user 触发 Forbidden），无需起完整 app + 登录会话。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from controllers.console.system_manage_extend import (
    CodeExecutionControlDetailExtend,
    CodeExecutionControlListExtend,
    system_admin_required_extend,
)
from models.account import AccountStatus, TenantAccountRole


def _make_record(email: str = "user@example.com") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        created_by=uuid.uuid4(),
        created_at=datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def admin_account(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """绕过 setup/login/初始化守卫，并将 current_user 替换为 admin 角色账号"""
    import controllers.console.system_manage_extend as controller_module
    from controllers.console import wraps as wraps_module

    account = MagicMock()
    account.id = "account-123"
    account.status = AccountStatus.ACTIVE
    account.is_authenticated = True
    account.is_admin_or_owner = True
    account.current_role = TenantAccountRole.ADMIN
    monkeypatch.setattr(
        controller_module.SystemManagementAccessService,
        "can_manage",
        lambda candidate, session: candidate.current_role in {TenantAccountRole.OWNER, TenantAccountRole.ADMIN},
    )

    monkeypatch.setattr(wraps_module.dify_config, "DEPLOYMENT_EDITION", "CLOUD")
    monkeypatch.setattr("libs.login.dify_config.LOGIN_DISABLED", True)
    monkeypatch.setattr(wraps_module, "current_account_with_tenant", lambda: (account, "tenant-123"))
    monkeypatch.setattr(controller_module, "current_user", account)
    return account


class TestSystemAdminRequiredExtendDecorator:
    def test_non_admin_gets_403(self, app: Flask, monkeypatch: pytest.MonkeyPatch):
        import controllers.console.system_manage_extend as controller_module

        member = MagicMock()
        member.is_admin_or_owner = False
        member.current_role = TenantAccountRole.NORMAL
        monkeypatch.setattr(controller_module.SystemManagementAccessService, "can_manage", lambda *args, **kwargs: False)
        monkeypatch.setattr(controller_module, "current_user", member)

        @system_admin_required_extend
        def protected() -> str:
            return "ok"

        with pytest.raises(Forbidden):
            protected()

    def test_admin_or_owner_passes(self, app: Flask, monkeypatch: pytest.MonkeyPatch):
        import controllers.console.system_manage_extend as controller_module

        admin = MagicMock()
        admin.is_admin_or_owner = True
        admin.current_role = TenantAccountRole.ADMIN
        monkeypatch.setattr(controller_module.SystemManagementAccessService, "can_manage", lambda *args, **kwargs: True)
        monkeypatch.setattr(controller_module, "current_user", admin)

        @system_admin_required_extend
        def protected() -> str:
            return "ok"

        assert protected() == "ok"

    @pytest.mark.parametrize(
        "role", [TenantAccountRole.NORMAL, TenantAccountRole.EDITOR, TenantAccountRole.DATASET_OPERATOR]
    )
    @pytest.mark.usefixtures("app")
    def test_rbac_enabled_does_not_promote_workspace_members(self, monkeypatch: pytest.MonkeyPatch, role):
        import controllers.console.system_manage_extend as controller_module
        from controllers.console import wraps as wraps_module

        member = MagicMock()
        member.is_admin_or_owner = True
        member.current_role = role
        monkeypatch.setattr(wraps_module.dify_config, "RBAC_ENABLED", True)
        monkeypatch.setattr(controller_module, "current_user", member)

        @system_admin_required_extend
        def protected() -> str:
            return "ok"

        with pytest.raises(Forbidden):
            protected()


class TestCodeExecutionControlEndpoints:
    def test_non_admin_list_endpoint_returns_403(
        self, app: Flask, admin_account: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        admin_account.is_admin_or_owner = False
        admin_account.current_role = TenantAccountRole.NORMAL

        with app.test_request_context("/console/api/system-manage-extend/code-execution-control", method="GET"):
            with pytest.raises(Forbidden):
                CodeExecutionControlListExtend().get()

    def test_get_returns_serialized_items(self, app: Flask, admin_account: MagicMock, monkeypatch: pytest.MonkeyPatch):
        record = _make_record()
        monkeypatch.setattr(
            "controllers.console.system_manage_extend.CodeExecutionControlService.list_emails",
            MagicMock(return_value=[record]),
        )

        with app.test_request_context("/console/api/system-manage-extend/code-execution-control", method="GET"):
            body, status = CodeExecutionControlListExtend().get()

        assert status == 200
        assert body == {
            "items": [
                {
                    "id": str(record.id),
                    "email": record.email,
                    "created_by": str(record.created_by),
                    "created_at": record.created_at.isoformat(),
                }
            ]
        }

    def test_post_creates_record(self, app: Flask, admin_account: MagicMock, monkeypatch: pytest.MonkeyPatch):
        record = _make_record()
        add_mock = MagicMock(return_value=(record, True))
        monkeypatch.setattr("controllers.console.system_manage_extend.CodeExecutionControlService.add_email", add_mock)

        with app.test_request_context(
            "/console/api/system-manage-extend/code-execution-control",
            method="POST",
            json={"email": record.email},
        ):
            body, status = CodeExecutionControlListExtend().post()

        assert status == 201
        assert body["result"] == "success"
        assert body["cache_synced"] is True
        assert body["item"]["email"] == record.email
        add_mock.assert_called_once_with(email=record.email, created_by="account-123")

    def test_post_without_body_returns_400(self, app: Flask, admin_account: MagicMock):
        with app.test_request_context("/console/api/system-manage-extend/code-execution-control", method="POST"):
            with pytest.raises(BadRequest):
                CodeExecutionControlListExtend().post()

    def test_post_duplicate_or_invalid_email_returns_400(
        self, app: Flask, admin_account: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "controllers.console.system_manage_extend.CodeExecutionControlService.add_email",
            MagicMock(side_effect=ValueError("Email already exists: user@example.com")),
        )

        with app.test_request_context(
            "/console/api/system-manage-extend/code-execution-control",
            method="POST",
            json={"email": "user@example.com"},
        ):
            with pytest.raises(BadRequest):
                CodeExecutionControlListExtend().post()

    def test_delete_returns_cache_synced(self, app: Flask, admin_account: MagicMock, monkeypatch: pytest.MonkeyPatch):
        remove_mock = MagicMock(return_value=True)
        monkeypatch.setattr(
            "controllers.console.system_manage_extend.CodeExecutionControlService.remove_email", remove_mock
        )

        with app.test_request_context(
            "/console/api/system-manage-extend/code-execution-control/rec-1", method="DELETE"
        ):
            body, status = CodeExecutionControlDetailExtend().delete("rec-1")

        assert status == 200
        assert body == {"result": "success", "cache_synced": True}
        remove_mock.assert_called_once_with(record_id="rec-1")

    def test_delete_missing_record_returns_404(
        self, app: Flask, admin_account: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "controllers.console.system_manage_extend.CodeExecutionControlService.remove_email",
            MagicMock(side_effect=ValueError("Record not found: rec-404")),
        )

        with app.test_request_context(
            "/console/api/system-manage-extend/code-execution-control/rec-404", method="DELETE"
        ):
            with pytest.raises(NotFound):
                CodeExecutionControlDetailExtend().delete("rec-404")
