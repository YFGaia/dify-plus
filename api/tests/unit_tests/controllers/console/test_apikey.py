from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from flask import Flask
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from controllers.console.agent.roster import AgentApiKeyListApi
from controllers.console.apikey import (
    AppApiKeyListResource,
    BaseApiKeyListResource,
    BaseApiKeyResource,
    DatasetApiKeyListResource,
)
from controllers.console.datasets.datasets import DatasetApiKeyApi
from core.rbac import RBACPermission, RBACResourceScope
from enums import DeploymentEdition
from models import Account
from models.account import AccountStatus, TenantAccountRole
from models.enums import ApiTokenType
from models.model import ApiToken, App, AppMode, IconType
from services.agent.errors import AgentAccessNotReadyError


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


def _persist_app(session: Session, *, mode: AppMode = AppMode.CHAT, app_id: str = "app-1") -> App:
    app = App(
        id=app_id,
        tenant_id="tenant-1",
        name="API key app",
        mode=mode,
        icon_type=IconType.EMOJI,
        icon="chat",
        icon_background="#ffffff",
        enable_site=False,
        enable_api=True,
    )
    session.add(app)
    session.flush()
    return app


def test_list_api_keys_uses_injected_session_and_tenant_id(sqlite_session: Session) -> None:
    resource = _make_list_resource()
    raw_get = cast(
        Callable[[BaseApiKeyListResource, object, str, str], dict[str, object]],
        inspect.unwrap(BaseApiKeyListResource.get),
    )
    session = sqlite_session
    _persist_app(session)
    api_key = ApiToken(
        type=ApiTokenType.APP,
        token="app-token",
        app_id="app-1",
        tenant_id="tenant-1",
    )
    api_key.id = "key-1"
    session.add(api_key)
    session.add(
        ApiToken(
            type=ApiTokenType.APP,
            token="foreign-app-token",
            app_id="app-1",
            tenant_id="tenant-2",
        )
    )
    legacy_api_key = ApiToken(type=ApiTokenType.APP, token="legacy-app-token", app_id="app-1", tenant_id=None)
    session.add(legacy_api_key)
    session.commit()

    result = raw_get(resource, session, "app-1", "tenant-1")
    data = cast(list[dict[str, object]], result["data"])

    assert {item["token"] for item in data} == {"app-token", "legacy-app-token"}


def test_create_api_key_uses_injected_session_and_tenant_id(sqlite_session: Session) -> None:
    resource = _make_list_resource()
    raw_post = cast(
        Callable[[BaseApiKeyListResource, object, str, str], tuple[dict[str, object], int]],
        inspect.unwrap(BaseApiKeyListResource.post),
    )
    session = sqlite_session
    _persist_app(session)
    session.add_all(
        [
            ApiToken(type=ApiTokenType.APP, token=f"foreign-token-{index}", app_id="app-1", tenant_id="tenant-2")
            for index in range(resource.max_keys)
        ]
    )
    session.commit()
    commits: list[str] = []
    event.listen(session, "after_commit", lambda _session: commits.append("commit"))

    with patch(
        "controllers.console.apikey.ApiToken.generate_api_key", return_value="app-generated-token"
    ) as generate_api_key:
        result, status = raw_post(resource, session, "app-1", "tenant-1")

    assert status == 201
    assert result["token"] == "app-generated-token"
    api_token = session.scalar(select(ApiToken).where(ApiToken.token == "app-generated-token"))
    assert api_token is not None
    assert api_token.app_id == "app-1"
    assert api_token.tenant_id == "tenant-1"
    assert api_token.type == ApiTokenType.APP
    generate_api_key.assert_called_once_with("app-", 24, session=session)
    assert commits == ["commit"]


def test_create_api_key_counts_legacy_tokens(sqlite_session: Session) -> None:
    resource = _make_list_resource()
    _persist_app(sqlite_session)
    sqlite_session.add_all(
        [
            ApiToken(type=ApiTokenType.APP, token=f"legacy-token-{index}", app_id="app-1", tenant_id=None)
            for index in range(resource.max_keys)
        ]
    )
    sqlite_session.commit()

    with pytest.raises(BadRequest):
        resource._create_api_key("app-1", "tenant-1", session=sqlite_session)


def test_create_agent_api_key_requires_published_access(sqlite_session: Session) -> None:
    resource = _make_list_resource()
    session = sqlite_session
    app = _persist_app(session, mode=AppMode.AGENT)

    with patch(
        "controllers.console.apikey.AppService.ensure_agent_app_access_ready",
        side_effect=AgentAccessNotReadyError(),
    ) as ensure_access_ready:
        with pytest.raises(AgentAccessNotReadyError):
            resource._create_api_key("app-1", "tenant-1", session=session)

    ensure_access_ready.assert_called_once_with(app, session=session)
    assert session.scalar(select(ApiToken)) is None


def test_delete_api_key_rejects_non_admin_account(sqlite_session: Session) -> None:
    resource = _make_key_resource()
    raw_delete = cast(
        Callable[[BaseApiKeyResource, object, str, str, str, Account], tuple[str, int]],
        inspect.unwrap(BaseApiKeyResource.delete),
    )
    session = sqlite_session
    _persist_app(session)

    with pytest.raises(Forbidden):
        raw_delete(
            resource,
            session,
            "app-1",
            "key-1",
            "tenant-1",
            _make_account(TenantAccountRole.NORMAL),
        )


def test_delete_api_key_uses_injected_session_user_and_tenant(sqlite_session: Session) -> None:
    resource = _make_key_resource()
    raw_delete = cast(
        Callable[[BaseApiKeyResource, object, str, str, str, Account], tuple[str, int]],
        inspect.unwrap(BaseApiKeyResource.delete),
    )
    session = sqlite_session
    _persist_app(session)
    api_key = ApiToken(type=ApiTokenType.APP, token="app-token", app_id="app-1", tenant_id=None)
    api_key.id = "key-1"
    session.add(api_key)
    session.commit()
    commits: list[str] = []
    event.listen(session, "after_commit", lambda _session: commits.append("commit"))

    with patch("controllers.console.apikey.ApiTokenCache.delete") as delete_cache:
        result, status = raw_delete(
            resource,
            session,
            "app-1",
            "key-1",
            "tenant-1",
            _make_account(TenantAccountRole.OWNER),
        )

    delete_cache.assert_called_once_with("app-token", ApiTokenType.APP)
    assert session.get(ApiToken, "key-1") is None
    assert commits == ["commit"]
    assert result == ""
    assert status == 204


def test_delete_api_key_rejects_foreign_tenant_token(sqlite_session: Session) -> None:
    resource = _make_key_resource()
    session = sqlite_session
    _persist_app(session)
    api_key = ApiToken(type=ApiTokenType.APP, token="foreign-token", app_id="app-1", tenant_id="tenant-2")
    api_key.id = "key-1"
    session.add(api_key)
    session.commit()

    with patch("controllers.console.apikey.ApiTokenCache.delete") as delete_cache:
        with pytest.raises(NotFound):
            resource._delete_api_key(
                "app-1",
                "key-1",
                "tenant-1",
                _make_account(TenantAccountRole.OWNER),
                session=session,
            )

    delete_cache.assert_not_called()
    assert session.get(ApiToken, "key-1") is api_key


def test_api_key_lists_require_matching_rbac_permission(config_overrides: Callable[..., None]) -> None:
    config_overrides(
        DEPLOYMENT_EDITION=DeploymentEdition.CLOUD,
        LOGIN_DISABLED=True,
        RBAC_ENABLED=True,
    )
    app = Flask(__name__)
    account = _make_account(TenantAccountRole.OWNER)
    api_id = UUID("00000000-0000-0000-0000-000000000001")
    cases = [
        (
            lambda: AppApiKeyListResource().get(resource_id=api_id),
            {
                "scene": RBACPermission.APP_RELEASE_AND_VERSION,
                "resource_type": RBACResourceScope.APP,
                "resource_id": str(api_id),
            },
        ),
        (
            lambda: DatasetApiKeyApi().get(),
            {"scene": RBACPermission.DATASET_API_KEY_MANAGE, "resource_type": None, "resource_id": None},
        ),
        (
            lambda: DatasetApiKeyListResource().get(resource_id=api_id),
            {
                "scene": RBACPermission.DATASET_API_KEY_MANAGE,
                "resource_type": RBACResourceScope.DATASET,
                "resource_id": str(api_id),
            },
        ),
    ]

    with (
        app.test_request_context("/"),
        patch("controllers.console.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.rbac.locators.agent_binding", return_value=None),
        patch("controllers.common.rbac.locators.PlainApp.owner_id", return_value=None),
        patch("controllers.common.rbac.locators.DatasetId.owner_id", return_value=None),
        patch.object(BaseApiKeyListResource, "_get_api_key_list") as get_api_key_list,
    ):
        for invoke, expected_kwargs in cases:
            with patch(
                "controllers.common.rbac.checks.RBACService.CheckAccess.check", return_value=False
            ) as check_access:
                with pytest.raises(Forbidden):
                    invoke()

            check_access.assert_called_once_with(
                "tenant-1",
                account.id,
                scene=expected_kwargs["scene"],
                resource_type=expected_kwargs["resource_type"],
                resource_id=expected_kwargs["resource_id"],
            )

    get_api_key_list.assert_not_called()


def test_api_key_lists_reject_legacy_read_only_members(config_overrides: Callable[..., None]) -> None:
    config_overrides(
        DEPLOYMENT_EDITION=DeploymentEdition.CLOUD,
        LOGIN_DISABLED=True,
        RBAC_ENABLED=False,
    )
    app = Flask(__name__)
    account = _make_account(TenantAccountRole.NORMAL)
    api_id = UUID("00000000-0000-0000-0000-000000000001")
    current_user = MagicMock()
    current_user._get_current_object.return_value = account
    current_user.has_edit_permission = False

    with (
        app.test_request_context("/"),
        patch("libs.login.current_user", current_user),
        patch("controllers.console.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch.object(BaseApiKeyListResource, "_get_api_key_list") as get_api_key_list,
    ):
        for invoke in (
            lambda: AppApiKeyListResource().get(resource_id=api_id),
            lambda: AgentApiKeyListApi().get(agent_id=api_id),
            lambda: DatasetApiKeyApi().get(),
            lambda: DatasetApiKeyListResource().get(resource_id=api_id),
        ):
            with pytest.raises(Forbidden):
                invoke()

    get_api_key_list.assert_not_called()


@pytest.fixture(autouse=True)
def quota_uuid_default(sqlite_session):
    """Emulate the PostgreSQL UUID default without changing production metadata."""
    import uuid

    sqlite_session.connection().connection.driver_connection.create_function(
        "uuid_generate_v4", 0, lambda: str(uuid.uuid4())
    )


def test_app_key_quota_crud_and_legacy_fallback(sqlite_session):
    from controllers.console.apikey import ApiKeyQuotaUpdatePayload
    from models.api_token_money_extend import ApiTokenMessageJoinsExtend, ApiTokenMoneyExtend

    session = sqlite_session
    resource = _make_list_resource()
    _persist_app(session)
    app = Flask(__name__)
    with app.test_request_context(json={"description": "test", "day_limit_quota": 2, "month_limit_quota": -1}):
        item = resource._create_api_key("app-1", "tenant-1", session=session)
    quota = session.scalar(select(ApiTokenMoneyExtend).where(ApiTokenMoneyExtend.app_token_id == item.id))
    assert quota is not None
    assert item.description == "test"
    assert item.day_limit_quota == 2
    assert item.month_limit_quota == -1
    assert item.id != quota.id
    with patch("controllers.console.apikey.ApiTokenCache.delete") as invalidate:
        updated = resource._update_api_key(
            "app-1",
            "tenant-1",
            _make_account(TenantAccountRole.OWNER),
            ApiKeyQuotaUpdatePayload(id=item.id, day_limit_quota=4),
            session=session,
        )
    assert updated.description == "test"
    assert updated.day_limit_quota == 4
    invalidate.assert_called_once_with(item.token, ApiTokenType.APP)
    session.add(ApiTokenMessageJoinsExtend(app_token_id=item.id, record_id="message-1", app_mode="chat"))
    session.commit()
    with patch("controllers.console.apikey.ApiTokenCache.delete") as invalidate:
        _make_key_resource()._delete_api_key(
            "app-1", item.id, "tenant-1", _make_account(TenantAccountRole.OWNER), session=session
        )
    invalidate.assert_called_once_with(item.token, ApiTokenType.APP)
    assert session.get(ApiToken, item.id) is None
    historical_join = session.scalar(select(ApiTokenMessageJoinsExtend))
    assert historical_join.app_token_id == item.id
    assert historical_join.record_id == "message-1"
    session.refresh(quota)
    assert quota.is_deleted

    legacy = ApiToken(id="legacy", token="legacy", type=ApiTokenType.APP, app_id="app-1", tenant_id="tenant-1")
    session.add(legacy)
    session.commit()
    item = resource._get_api_key_list("app-1", "tenant-1", session=session).data[0]
    assert (item.description, item.accumulated_quota, item.day_used_quota, item.month_used_quota) == ("", 0, 0, 0)
    assert (item.day_limit_quota, item.month_limit_quota) == (-1, -1)
    with patch("controllers.console.apikey.ApiTokenCache.delete"):
        resource._update_api_key(
            "app-1",
            "tenant-1",
            _make_account(TenantAccountRole.ADMIN),
            ApiKeyQuotaUpdatePayload(id="legacy", month_limit_quota=3),
            session=session,
        )
    assert (
        session.scalar(
            select(ApiTokenMoneyExtend).where(ApiTokenMoneyExtend.app_token_id == "legacy")
        ).month_limit_quota
        == 3
    )


@pytest.mark.parametrize("failure", ["quota_insert", "commit"])
def test_quota_create_failure_rolls_back_token_and_quota(sqlite_session, failure):
    from models.api_token_money_extend import ApiTokenMoneyExtend

    session = sqlite_session
    _persist_app(session)
    session.commit()

    def fail(*_args):
        raise RuntimeError("injected failure")

    target = ApiTokenMoneyExtend if failure == "quota_insert" else session
    event_name = "before_insert" if failure == "quota_insert" else "before_commit"
    event.listen(target, event_name, fail)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            _make_list_resource()._create_api_key("app-1", "tenant-1", session=session)
        session.rollback()
    finally:
        event.remove(target, event_name, fail)
    assert session.scalar(select(ApiToken)) is None
    assert session.scalar(select(ApiTokenMoneyExtend)) is None


@pytest.mark.parametrize("case", ["member", "foreign", "wrong_app", "missing"])
def test_quota_update_rejects_unauthorized_key(sqlite_session, case):
    from controllers.console.apikey import ApiKeyQuotaUpdatePayload
    from models.api_token_money_extend import ApiTokenMoneyExtend

    _persist_app(sqlite_session)
    sqlite_session.add(
        ApiToken(
            id="key",
            token="secret",
            type=ApiTokenType.APP,
            app_id="other" if case == "wrong_app" else "app-1",
            tenant_id="tenant-2" if case == "foreign" else "tenant-1",
        )
    )
    sqlite_session.commit()
    role = TenantAccountRole.NORMAL if case == "member" else TenantAccountRole.OWNER
    with (
        pytest.raises(Forbidden if case == "member" else NotFound),
        patch("controllers.console.apikey.ApiTokenCache.delete") as invalidate,
    ):
        _make_list_resource()._update_api_key(
            "app-1",
            "tenant-1",
            _make_account(role),
            ApiKeyQuotaUpdatePayload(id="absent" if case == "missing" else "key", day_limit_quota=1),
            session=sqlite_session,
        )
    invalidate.assert_not_called()
    assert sqlite_session.scalar(select(ApiTokenMoneyExtend)) is None


@pytest.mark.parametrize("limit", [-2, -0.1, float("inf"), float("nan")])
def test_quota_payload_rejects_invalid_limits(limit):
    from pydantic import ValidationError

    from controllers.console.apikey import ApiKeyQuotaPayload

    with pytest.raises(ValidationError):
        ApiKeyQuotaPayload(day_limit_quota=limit)


def test_dataset_binding_reveal_once_tenant_isolation_and_no_quota(sqlite_session):
    from controllers.console.datasets.datasets import DatasetApiDeleteApi
    from models.api_token_money_extend import ApiTokenMoneyExtend
    from models.dataset import Dataset
    from models.model import DatasetApiTokenBinding

    dataset = Dataset(id="dataset-one", tenant_id="tenant-1", name="one", created_by="owner")
    sqlite_session.add(dataset)
    sqlite_session.commit()
    resource = DatasetApiKeyApi()
    with Flask(__name__).test_request_context(json={"dataset_ids": [dataset.id, dataset.id]}):
        created, status = inspect.unwrap(resource.post)(resource, sqlite_session, "tenant-1")
        sqlite_session.commit()
    assert status == 200
    assert created["token"].startswith("dataset-")
    assert created["dataset_ids"] == [dataset.id]
    listing = inspect.unwrap(resource.get)(resource, sqlite_session, "tenant-1")
    assert listing["data"][0]["token"] != created["token"]
    assert "..." in listing["data"][0]["token"]
    assert listing["data"][0]["dataset_ids"] == [dataset.id]
    assert inspect.unwrap(resource.get)(resource, sqlite_session, "tenant-other")["data"] == []
    assert sqlite_session.scalar(select(ApiTokenMoneyExtend)) is None
    with Flask(__name__).test_request_context(json={"dataset_ids": [dataset.id]}), pytest.raises(BadRequest):
        inspect.unwrap(resource.post)(resource, sqlite_session, "tenant-other")
    # SQLite needs FK enforcement enabled explicitly; upstream binding uses ON DELETE CASCADE.
    sqlite_session.rollback()
    sqlite_session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    deleter = DatasetApiDeleteApi()
    with patch("controllers.console.datasets.datasets.ApiTokenCache.delete") as invalidate:
        inspect.unwrap(deleter.delete)(deleter, sqlite_session, "tenant-1", UUID(created["id"]))
        sqlite_session.commit()
    invalidate.assert_called_once_with(created["token"], ApiTokenType.DATASET)
    assert sqlite_session.scalar(select(DatasetApiTokenBinding)) is None


def test_app_quota_put_preserves_rbac_before_mutation(config_overrides):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD, LOGIN_DISABLED=True, RBAC_ENABLED=True)
    account = _make_account(TenantAccountRole.OWNER)
    api_id = UUID("00000000-0000-0000-0000-000000000001")
    with (
        Flask(__name__).test_request_context(json={"id": "key"}),
        patch("controllers.console.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.rbac.locators.agent_binding", return_value=None),
        patch("controllers.common.rbac.locators.PlainApp.owner_id", return_value=None),
        patch("controllers.common.rbac.checks.RBACService.CheckAccess.check", return_value=False) as check,
        patch.object(BaseApiKeyListResource, "_update_api_key") as update,
    ):
        with pytest.raises(Forbidden):
            AppApiKeyListResource().put(resource_id=api_id)
    check.assert_called_once()
    assert check.call_args.kwargs["scene"] == RBACPermission.APP_RELEASE_AND_VERSION
    update.assert_not_called()


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_quota_mutation_commit_failure_keeps_original_rows(sqlite_session, operation):
    from controllers.console.apikey import ApiKeyQuotaUpdatePayload
    from models.api_token_money_extend import ApiTokenMoneyExtend

    session = sqlite_session
    resource = _make_list_resource()
    _persist_app(session)
    item = resource._create_api_key("app-1", "tenant-1", session=session)
    key_id = item.id

    def fail(_session):
        raise RuntimeError("commit failure")

    def mutate():
        if operation == "edit":
            resource._update_api_key(
                "app-1",
                "tenant-1",
                _make_account(TenantAccountRole.OWNER),
                ApiKeyQuotaUpdatePayload(id=key_id, day_limit_quota=5),
                session=session,
            )
        else:
            _make_key_resource()._delete_api_key(
                "app-1",
                key_id,
                "tenant-1",
                _make_account(TenantAccountRole.OWNER),
                session=session,
            )

    event.listen(session, "before_commit", fail)
    try:
        with (
            patch("controllers.console.apikey.ApiTokenCache.delete"),
            pytest.raises(RuntimeError, match="commit failure"),
        ):
            mutate()
        session.rollback()
    finally:
        event.remove(session, "before_commit", fail)
    quota = session.scalar(select(ApiTokenMoneyExtend).where(ApiTokenMoneyExtend.app_token_id == key_id))
    assert session.get(ApiToken, key_id) is not None
    assert quota.day_limit_quota == -1
    assert quota.is_deleted is False
