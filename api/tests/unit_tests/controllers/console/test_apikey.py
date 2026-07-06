from __future__ import annotations

import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from flask import Flask  # 二开部分 - 密钥额度限制：create 测试需要请求上下文
from werkzeug.exceptions import Forbidden

from controllers.console.apikey import BaseApiKeyListResource, BaseApiKeyResource
from models import Account
from models.account import AccountStatus, TenantAccountRole
from models.enums import ApiTokenType
from models.model import ApiToken, App


def _make_list_resource() -> BaseApiKeyListResource:
    resource = BaseApiKeyListResource()
    resource.resource_type = ApiTokenType.APP
    resource.resource_model = App
    resource.resource_id_field = "app_id"
    resource.token_prefix = "app-"
    return resource


def _make_key_resource() -> BaseApiKeyResource:
    resource = BaseApiKeyResource()
    resource.resource_type = ApiTokenType.APP
    resource.resource_model = App
    resource.resource_id_field = "app_id"
    return resource


def _make_account(role: TenantAccountRole) -> Account:
    account = Account(
        name="Test User",
        email=f"{role.value}@example.com",
        status=AccountStatus.ACTIVE,
    )
    account.id = f"{role.value}-user"
    account.role = role
    return account


def test_list_api_keys_uses_injected_tenant_id() -> None:
    resource = _make_list_resource()
    api_key = SimpleNamespace(
        id="key-1",
        type=ApiTokenType.APP,
        token="app-token",
        last_used_at=None,
        created_at=None,
    )

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
    ):
        # 二开部分 - 密钥额度限制：fork 改为 execute(select(ApiToken, ApiTokenMoneyExtend)).all()
        # 返回 (token, quota) 行；quota 为 None 时走 DEFAULT_QUOTA_EXTEND 兜底
        db_mock.session.execute.return_value.all.return_value = [(api_key, None)]

        result = resource.get("app-1", "tenant-1")

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    assert result == {
        "data": [
            {
                "id": "key-1",
                "type": "app",
                "token": "app-token",
                "last_used_at": None,
                "created_at": None,
                # 二开部分 - 密钥额度限制：无额度记录（老密钥）时的兜底展示值
                "description": "",
                "accumulated_quota": 0.0,
                "day_limit_quota": -1.0,
                "month_limit_quota": -1.0,
                "day_used_quota": 0.0,
                "month_used_quota": 0.0,
            }
        ]
    }


def test_create_api_key_uses_injected_tenant_id() -> None:
    resource = _make_list_resource()
    raw_post = cast(
        Callable[[BaseApiKeyListResource, str, str], tuple[dict[str, object], int]],
        inspect.unwrap(BaseApiKeyListResource.post),
    )

    def add_api_token(api_token: ApiToken) -> None:
        api_token.id = "key-1"

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
        patch("controllers.console.apikey.ApiToken.generate_api_key", return_value="app-generated-token"),
    ):
        db_mock.session.scalar.return_value = 0
        db_mock.session.add.side_effect = add_api_token

        # 二开部分 - 密钥额度限制：_create_api_key 读取 request 携带的额度参数，
        # werkzeug LocalProxy 无法直接 patch，用最小请求上下文提供 request
        flask_app = Flask(__name__)
        with flask_app.test_request_context("/apps/app-1/api-keys", method="POST", json={}):
            result, status = raw_post(resource, "app-1", "tenant-1")

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    assert status == 201
    assert result["token"] == "app-generated-token"
    # 二开部分 - 密钥额度限制：add 依次为 ApiToken 与 ApiTokenMoneyExtend，各 commit 一次
    api_token = db_mock.session.add.call_args_list[0].args[0]
    assert api_token.app_id == "app-1"
    assert api_token.tenant_id == "tenant-1"
    assert api_token.type == ApiTokenType.APP
    assert db_mock.session.add.call_count == 2
    assert db_mock.session.commit.call_count == 2


def test_delete_api_key_rejects_non_admin_account() -> None:
    resource = _make_key_resource()

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
    ):
        with pytest.raises(Forbidden):
            resource.delete("app-1", "key-1", "tenant-1", _make_account(TenantAccountRole.NORMAL))

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    db_mock.session.scalar.assert_not_called()


def test_delete_api_key_uses_injected_user_and_tenant() -> None:
    resource = _make_key_resource()
    api_key = SimpleNamespace(token="app-token", type=ApiTokenType.APP)

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
        patch("controllers.console.apikey.ApiTokenCache.delete") as delete_cache,
    ):
        db_mock.session.scalar.return_value = api_key

        result, status = resource.delete("app-1", "key-1", "tenant-1", _make_account(TenantAccountRole.OWNER))

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    delete_cache.assert_called_once_with("app-token", ApiTokenType.APP)
    # 二开部分 - 密钥额度限制：删除 token 后额度记录软删（execute/commit 各两次）
    assert db_mock.session.execute.call_count == 2
    assert db_mock.session.commit.call_count == 2
    assert result == ""
    assert status == 204
