"""The authenticated permission response carries its identity and is never cached."""

import inspect
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from controllers.console import system_management_permissions_extend as controller


@pytest.mark.parametrize("allowed", [True, False])
def test_permission_response_echoes_identity_without_http_caching(app, monkeypatch, allowed):
    account = SimpleNamespace(id=str(uuid4()), current_tenant_id=str(uuid4()))
    principal = Mock()
    principal._get_current_object.return_value = account
    permission = Mock(return_value=allowed)
    monkeypatch.setattr(controller, "current_user", principal)
    monkeypatch.setattr(controller.SystemManagementAccessService, "can_manage", permission)

    with app.test_request_context("/console/api/system-manage-extend/permissions"):
        response = inspect.unwrap(controller.SystemManagementPermissionsExtend.get)(
            controller.SystemManagementPermissionsExtend()
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {
        "can_manage_system": allowed,
        "workspace_id": account.current_tenant_id,
        "account_id": account.id,
    }
    permission.assert_called_once_with(account, session=controller.db.session)


def test_permission_schema_rejects_truthy_non_boolean_grant():
    with pytest.raises(ValidationError):
        controller.SystemManagementPermissionsResponse(
            can_manage_system="true", workspace_id=uuid4(), account_id=uuid4()
        )
