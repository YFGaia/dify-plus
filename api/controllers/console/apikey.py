from datetime import datetime
from itertools import starmap  # 二开部分 - 密钥额度限制
from uuid import UUID

import flask_restx
from flask import request  # 二开部分 - 密钥额度限制
from flask_restx import Resource
from flask_restx._http import HTTPStatus
from pydantic import field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import Forbidden

from configs import dify_config
from controllers.common.schema import register_response_schema_models
from extensions.ext_database import db
from fields.base import ResponseModel
from libs.helper import dump_response, to_timestamp
from libs.login import current_account_with_tenant, login_required  # 二开部分 - 密钥额度限制
from models import Account
from models.api_token_money_extend import ApiTokenMoneyExtend  # 二开部分 - 密钥额度限制
from models.dataset import Dataset
from models.enums import ApiTokenType
from models.model import ApiToken, App
from services.api_token_service import ApiTokenCache

from . import console_ns
from .wraps import (
    RBACPermission,
    RBACResourceScope,
    account_initialization_required,
    edit_permission_required,
    rbac_permission_required,
    setup_required,
    with_current_tenant_id,
    with_current_user,
)

# 二开部分 - 密钥额度限制：无额度记录（老密钥）时的兜底展示值
DEFAULT_QUOTA_EXTEND = {
    "description": "",
    "accumulated_quota": 0.0,
    "day_limit_quota": -1.0,
    "month_limit_quota": -1.0,
    "day_used_quota": 0.0,
    "month_used_quota": 0.0,
}


class ApiKeyItem(ResponseModel):
    id: str
    type: str
    token: str
    last_used_at: int | None = None
    created_at: int | None = None
    # 二开部分begin - 密钥额度限制
    description: str | None = None
    accumulated_quota: float | None = None
    day_limit_quota: float | None = None
    month_limit_quota: float | None = None
    month_used_quota: float | None = None
    day_used_quota: float | None = None
    # 二开部分end - 密钥额度限制

    @field_validator("last_used_at", "created_at", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | int | None) -> int | None:
        return to_timestamp(value)


class ApiKeyList(ResponseModel):
    data: list[ApiKeyItem]


register_response_schema_models(console_ns, ApiKeyItem, ApiKeyList)


def _merge_token_with_quota_extend(token: ApiToken, quota: ApiTokenMoneyExtend | None) -> dict:
    """二开部分 - 密钥额度限制：合并 ApiToken 与额度记录为响应 dict（额度字段覆盖同名键，id 保持密钥 ID）。

    注意：调用方可能在 ``db.session.commit()`` 之后调用本函数，此时两个 ORM 对象处于
    expired 状态，直接读 ``__dict__`` 会得到空 dict（进而 ``token_dict["id"]`` KeyError
    → 接口 500）。先做一次属性访问触发 SQLAlchemy 重载，再读 ``__dict__``。
    """
    _ = token.id
    if quota is not None:
        _ = quota.app_token_id
    token_dict = {k: v for k, v in token.__dict__.items() if not k.startswith("_sa")}
    if quota is not None:
        quota_dict = {k: v for k, v in quota.__dict__.items() if not k.startswith("_sa")}
    else:
        quota_dict = DEFAULT_QUOTA_EXTEND
    merged_data = {**token_dict, **DEFAULT_QUOTA_EXTEND, **quota_dict}
    merged_data["id"] = token_dict["id"]
    return merged_data


def _get_resource(resource_id, tenant_id, resource_model):
    with sessionmaker(db.engine).begin() as session:
        resource = session.execute(
            select(resource_model).filter_by(id=resource_id, tenant_id=tenant_id)
        ).scalar_one_or_none()

    if resource is None:
        flask_restx.abort(HTTPStatus.NOT_FOUND, message=f"{resource_model.__name__} not found.")

    return resource


class BaseApiKeyListResource(Resource):
    method_decorators = [account_initialization_required, login_required, setup_required]

    resource_type: ApiTokenType | None = None
    resource_model: type | None = None
    resource_id_field: str | None = None
    token_prefix: str | None = None
    max_keys = 10

    def get(self, resource_id: str, current_tenant_id: str) -> dict[str, object]:
        return dump_response(ApiKeyList, self._get_api_key_list(resource_id, current_tenant_id))

    def _get_api_key_list(self, resource_id: str, current_tenant_id: str) -> ApiKeyList:
        assert self.resource_id_field is not None, "resource_id_field must be set"

        _get_resource(resource_id, current_tenant_id, self.resource_model)

        # --------------------- 二开部分begin - 密钥额度限制 ---------------------
        # LEFT JOIN 额度表：确保没有额度记录的老密钥也能显示
        rows = db.session.execute(
            select(ApiToken, ApiTokenMoneyExtend)
            .outerjoin(ApiTokenMoneyExtend, ApiToken.id == ApiTokenMoneyExtend.app_token_id)
            .where(ApiToken.type == self.resource_type, getattr(ApiToken, self.resource_id_field) == resource_id)
        ).all()
        keys = list(starmap(_merge_token_with_quota_extend, rows))
        # --------------------- 二开部分end - 密钥额度限制 ---------------------
        return ApiKeyList.model_validate({"data": keys}, from_attributes=True)

    @edit_permission_required
    def post(self, resource_id: str, current_tenant_id: str) -> tuple[dict[str, object], int]:
        return dump_response(ApiKeyItem, self._create_api_key(resource_id, current_tenant_id)), 201

    def _create_api_key(self, resource_id: str, current_tenant_id: str) -> "ApiKeyItem":
        assert self.resource_id_field is not None, "resource_id_field must be set"
        _get_resource(resource_id, current_tenant_id, self.resource_model)
        current_key_count: int = (
            db.session.scalar(
                select(func.count(ApiToken.id)).where(
                    ApiToken.type == self.resource_type, getattr(ApiToken, self.resource_id_field) == resource_id
                )
            )
            or 0
        )

        if current_key_count >= self.max_keys:
            flask_restx.abort(
                HTTPStatus.BAD_REQUEST,
                message=f"Cannot create more than {self.max_keys} API keys for this resource type.",
                custom="max_keys_exceeded",
            )

        key = ApiToken.generate_api_key(self.token_prefix or "", 24)
        assert self.resource_type is not None, "resource_type must be set"
        api_token = ApiToken()
        setattr(api_token, self.resource_id_field, resource_id)
        api_token.tenant_id = current_tenant_id
        api_token.token = key
        api_token.type = self.resource_type
        db.session.add(api_token)
        db.session.commit()

        # --------------------- 二开部分Begin - 密钥额度限制 ---------------------
        data = request.get_json(silent=True) or {}
        quota_extend = ApiTokenMoneyExtend(
            app_token_id=api_token.id,
            description=data.get("description", "默认"),
            accumulated_quota=0,
            day_used_quota=0,
            month_used_quota=0,
            day_limit_quota=data.get("day_limit_quota", -1),
            month_limit_quota=data.get("month_limit_quota", -1),
        )
        db.session.add(quota_extend)
        db.session.commit()
        # 返回合并额度字段后的响应模型，避免创建响应中额度字段全为 null
        return ApiKeyItem.model_validate(_merge_token_with_quota_extend(api_token, quota_extend), from_attributes=True)
        # --------------------- 二开部分End - 密钥额度限制 ---------------------

    # --------------------- 二开部分Begin - 密钥额度限制 ---------------------
    def put(self, resource_id):
        """更新密钥的额度配置（description / day_limit_quota / month_limit_quota），admin/owner 限定。"""
        assert self.resource_id_field is not None, "resource_id_field must be set"
        resource_id = str(resource_id)
        current_user, current_tenant_id = current_account_with_tenant()
        _get_resource(resource_id, current_tenant_id, self.resource_model)

        if not current_user.is_admin_or_owner:
            raise Forbidden()

        data = request.get_json(silent=True) or {}
        api_key_id = data.get("id", "")

        key = db.session.scalar(
            select(ApiToken)
            .where(
                getattr(ApiToken, self.resource_id_field) == resource_id,
                ApiToken.type == self.resource_type,
                ApiToken.id == api_key_id,
            )
            .limit(1)
        )

        if key is None:
            flask_restx.abort(HTTPStatus.NOT_FOUND, message="API key not found")

        api_token_money_extend = db.session.scalar(
            select(ApiTokenMoneyExtend).where(ApiTokenMoneyExtend.app_token_id == api_key_id).limit(1)
        )
        if api_token_money_extend:
            if "description" in data:
                api_token_money_extend.description = data["description"]
            if "day_limit_quota" in data:
                api_token_money_extend.day_limit_quota = data["day_limit_quota"]
            if "month_limit_quota" in data:
                api_token_money_extend.month_limit_quota = data["month_limit_quota"]
        db.session.commit()

        merged_data = _merge_token_with_quota_extend(key, api_token_money_extend)
        return ApiKeyItem.model_validate(merged_data, from_attributes=True).model_dump(mode="json"), 200

    # --------------------- 二开部分End - 密钥额度限制 ---------------------


class BaseApiKeyResource(Resource):
    method_decorators = [account_initialization_required, login_required, setup_required]

    resource_type: ApiTokenType | None = None
    resource_model: type | None = None
    resource_id_field: str | None = None

    def delete(
        self, resource_id: str, api_key_id: str, current_tenant_id: str, current_user: Account
    ) -> tuple[str, int]:
        self._delete_api_key(resource_id, api_key_id, current_tenant_id, current_user)
        return "", 204

    def _delete_api_key(
        self,
        resource_id: str,
        api_key_id: str,
        current_tenant_id: str,
        current_user: Account,
    ) -> None:
        assert self.resource_id_field is not None, "resource_id_field must be set"
        _get_resource(resource_id, current_tenant_id, self.resource_model)

        if not dify_config.RBAC_ENABLED and not current_user.is_admin_or_owner:
            raise Forbidden()

        key = db.session.scalar(
            select(ApiToken)
            .where(
                getattr(ApiToken, self.resource_id_field) == resource_id,
                ApiToken.type == self.resource_type,
                ApiToken.id == api_key_id,
            )
            .limit(1)
        )

        if key is None:
            flask_restx.abort(HTTPStatus.NOT_FOUND, message="API key not found")

        # Invalidate cache before deleting from database
        # Type assertion: key is guaranteed to be non-None here because abort() raises
        assert key is not None  # nosec - for type checker only
        ApiTokenCache.delete(key.token, key.type)

        db.session.execute(delete(ApiToken).where(ApiToken.id == api_key_id))
        db.session.commit()

        # 二开部分Begin - 密钥额度限制：额度记录软删，保留历史用量
        db.session.execute(
            update(ApiTokenMoneyExtend).where(ApiTokenMoneyExtend.app_token_id == api_key_id).values(is_deleted=True)
        )
        db.session.commit()
        # 二开部分End - 密钥额度限制


@console_ns.route("/apps/<uuid:resource_id>/api-keys")
class AppApiKeyListResource(BaseApiKeyListResource):
    @console_ns.doc("get_app_api_keys")
    @console_ns.doc(description="Get all API keys for an app")
    @console_ns.doc(params={"resource_id": "App ID"})
    @console_ns.response(200, "API keys retrieved successfully", console_ns.models[ApiKeyList.__name__])
    @with_current_tenant_id
    def get(self, current_tenant_id: str, resource_id: UUID) -> dict[str, object]:
        """Get all API keys for an app"""
        return dump_response(ApiKeyList, self._get_api_key_list(str(resource_id), current_tenant_id))

    @console_ns.doc("create_app_api_key")
    @console_ns.doc(description="Create a new API key for an app")
    @console_ns.doc(params={"resource_id": "App ID"})
    @console_ns.response(201, "API key created successfully", console_ns.models[ApiKeyItem.__name__])
    @console_ns.response(400, "Maximum keys exceeded")
    @with_current_tenant_id
    @edit_permission_required
    @rbac_permission_required(RBACResourceScope.APP, RBACPermission.APP_RELEASE_AND_VERSION)
    def post(self, current_tenant_id: str, resource_id: UUID) -> tuple[dict[str, object], int]:
        """Create a new API key for an app"""
        return dump_response(ApiKeyItem, self._create_api_key(str(resource_id), current_tenant_id)), 201

    resource_type = ApiTokenType.APP
    resource_model = App
    resource_id_field = "app_id"
    token_prefix = "app-"


@console_ns.route("/apps/<uuid:resource_id>/api-keys/<uuid:api_key_id>")
class AppApiKeyResource(BaseApiKeyResource):
    @console_ns.doc("delete_app_api_key")
    @console_ns.doc(description="Delete an API key for an app")
    @console_ns.doc(params={"resource_id": "App ID", "api_key_id": "API key ID"})
    @console_ns.response(204, "API key deleted successfully")
    @with_current_user
    @with_current_tenant_id
    @rbac_permission_required(RBACResourceScope.APP, RBACPermission.APP_RELEASE_AND_VERSION)
    def delete(
        self, current_tenant_id: str, current_user: Account, resource_id: UUID, api_key_id: UUID
    ) -> tuple[str, int]:
        """Delete an API key for an app"""
        self._delete_api_key(str(resource_id), str(api_key_id), current_tenant_id, current_user)
        return "", 204

    resource_type = ApiTokenType.APP
    resource_model = App
    resource_id_field = "app_id"


@console_ns.route("/datasets/<uuid:resource_id>/api-keys")
class DatasetApiKeyListResource(BaseApiKeyListResource):
    @console_ns.doc("get_dataset_api_keys")
    @console_ns.doc(description="Get all API keys for a dataset")
    @console_ns.doc(params={"resource_id": "Dataset ID"})
    @console_ns.response(200, "API keys retrieved successfully", console_ns.models[ApiKeyList.__name__])
    @with_current_tenant_id
    def get(self, current_tenant_id: str, resource_id: UUID) -> dict[str, object]:
        """Get all API keys for a dataset"""
        return dump_response(ApiKeyList, self._get_api_key_list(str(resource_id), current_tenant_id))

    @console_ns.doc("create_dataset_api_key")
    @console_ns.doc(description="Create a new API key for a dataset")
    @console_ns.doc(params={"resource_id": "Dataset ID"})
    @console_ns.response(201, "API key created successfully", console_ns.models[ApiKeyItem.__name__])
    @console_ns.response(400, "Maximum keys exceeded")
    @with_current_tenant_id
    @edit_permission_required
    @rbac_permission_required(RBACResourceScope.DATASET, RBACPermission.DATASET_API_KEY_MANAGE)
    def post(self, current_tenant_id: str, resource_id: UUID) -> tuple[dict[str, object], int]:
        """Create a new API key for a dataset"""
        return dump_response(ApiKeyItem, self._create_api_key(str(resource_id), current_tenant_id)), 201

    resource_type = ApiTokenType.DATASET
    resource_model = Dataset
    resource_id_field = "dataset_id"
    token_prefix = "ds-"


@console_ns.route("/datasets/<uuid:resource_id>/api-keys/<uuid:api_key_id>")
class DatasetApiKeyResource(BaseApiKeyResource):
    @console_ns.doc("delete_dataset_api_key")
    @console_ns.doc(description="Delete an API key for a dataset")
    @console_ns.doc(params={"resource_id": "Dataset ID", "api_key_id": "API key ID"})
    @console_ns.response(204, "API key deleted successfully")
    @with_current_user
    @with_current_tenant_id
    @rbac_permission_required(RBACResourceScope.DATASET, RBACPermission.DATASET_API_KEY_MANAGE)
    def delete(
        self, current_tenant_id: str, current_user: Account, resource_id: UUID, api_key_id: UUID
    ) -> tuple[str, int]:
        """Delete an API key for a dataset"""
        self._delete_api_key(str(resource_id), str(api_key_id), current_tenant_id, current_user)
        return "", 204

    resource_type = ApiTokenType.DATASET
    resource_model = Dataset
    resource_id_field = "dataset_id"
