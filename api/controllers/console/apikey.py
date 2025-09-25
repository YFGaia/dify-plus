from typing import Any, Optional

import flask_restx
from flask import request  # 二开部分 - 密钥额度限制
from flask_login import current_user
from flask_restx import Resource, fields, marshal_with
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased  # 二开部分 - 密钥额度限制
from werkzeug.exceptions import Forbidden

from extensions.ext_database import db
from libs.helper import TimestampField
from libs.login import login_required
from models.api_token_money_extend import ApiTokenMoneyExtend  # 二开部分 - 密钥额度限制
from models.dataset import Dataset
from models.model import ApiToken, App

from . import api
from .wraps import account_initialization_required, setup_required

api_key_fields = {
    "id": fields.String,
    "type": fields.String,
    "token": fields.String,
    "last_used_at": TimestampField,
    "created_at": TimestampField,
    # 二开部分begin - 密钥额度限制
    "description": fields.String,
    "accumulated_quota": fields.Float,
    "day_limit_quota": fields.Float,
    "month_limit_quota": fields.Float,
    "month_used_quota": fields.Float,
    "day_used_quota": fields.Float,
    # 二开部分end - 密钥额度限制
}

api_key_list = {"data": fields.List(fields.Nested(api_key_fields), attribute="items")}


def _get_resource(resource_id, tenant_id, resource_model):
    if resource_model == App:
        with Session(db.engine) as session:
            resource = session.execute(
                select(resource_model).filter_by(id=resource_id, tenant_id=tenant_id)
            ).scalar_one_or_none()
    else:
        with Session(db.engine) as session:
            resource = session.execute(
                select(resource_model).filter_by(id=resource_id, tenant_id=tenant_id)
            ).scalar_one_or_none()

    if resource is None:
        flask_restx.abort(404, message=f"{resource_model.__name__} not found.")

    return resource


class BaseApiKeyListResource(Resource):
    method_decorators = [account_initialization_required, login_required, setup_required]

    resource_type: str | None = None
    resource_model: Optional[Any] = None
    resource_id_field: str | None = None
    token_prefix: str | None = None
    max_keys = 10

    @marshal_with(api_key_list)
    def get(self, resource_id):
        assert self.resource_id_field is not None, "resource_id_field must be set"
        resource_id = str(resource_id)
        _get_resource(resource_id, current_user.current_tenant_id, self.resource_model)
        # keys = (
        #     db.session.query(ApiToken)
        #     .where(ApiToken.type == self.resource_type, getattr(ApiToken, self.resource_id_field) == resource_id)
        #     .all()
        # )

        # --------------------- 二开部分begin - 密钥额度限制 ---------------------
        # 定义别名，用于后续的join操作
        ApiTokenAlias = aliased(ApiToken)

        # 连表查询
        api_token_money_extend_query = (
            db.session.query(ApiTokenMoneyExtend, ApiTokenAlias)
            .join(ApiTokenAlias, ApiTokenMoneyExtend.app_token_id == ApiTokenAlias.id)
            .filter(
                ApiTokenAlias.type == self.resource_type, getattr(ApiTokenAlias, self.resource_id_field) == resource_id
            )
            .all()
        )
        # 将两个表的数据合并到一个字典中
        keys = []
        for api_token, api_token_money_extend in api_token_money_extend_query:
            merged_data = {**api_token.__dict__, **api_token_money_extend.__dict__}
            keys.append(merged_data)
        # --------------------- 二开部分end - 密钥额度限制 ---------------------
        return {"items": keys}

    @marshal_with(api_key_fields)
    def post(self, resource_id):
        assert self.resource_id_field is not None, "resource_id_field must be set"
        resource_id = str(resource_id)
        _get_resource(resource_id, current_user.current_tenant_id, self.resource_model)
        if not current_user.is_admin_or_owner:
            raise Forbidden()

        current_key_count = (
            db.session.query(ApiToken)
            .where(ApiToken.type == self.resource_type, getattr(ApiToken, self.resource_id_field) == resource_id)
            .count()
        )

        if current_key_count >= self.max_keys:
            flask_restx.abort(
                400,
                message=f"Cannot create more than {self.max_keys} API keys for this resource type.",
                custom="max_keys_exceeded",
            )

        key = ApiToken.generate_api_key(self.token_prefix, 24)
        api_token = ApiToken()
        setattr(api_token, self.resource_id_field, resource_id)
        api_token.tenant_id = current_user.current_tenant_id
        api_token.token = key
        api_token.type = self.resource_type
        db.session.add(api_token)
        db.session.commit()

        # --------------------- 二开部分Begin - 密钥额度限制 ---------------------
        content_type = request.headers.get("Content-Type")
        if content_type == "application/json":
            try:
                data = request.get_json(silent=True)
            except:
                data = {}
        else:
            data = {}
        if data is None:
            data = {}

        # 获取day_limit_quota和month_limit_quota，如果不存在则使用默认值-1
        day_limit_quota = data.get("day_limit_quota", -1)
        month_limit_quota = data.get("month_limit_quota", -1)
        description = data.get("description", "默认")
        db.session.add(
            ApiTokenMoneyExtend(
                app_token_id=api_token.id,
                description=description,
                accumulated_quota=0,
                day_used_quota=0,
                month_used_quota=0,
                day_limit_quota=day_limit_quota,
                month_limit_quota=month_limit_quota,
            )
        )
        db.session.commit()
        # --------------------- 二开部分End - 密钥额度限制 ---------------------

        return api_token, 201

    # --------------------- 二开部分Begin - 密钥额度限制 ---------------------
    @marshal_with(api_key_fields)
    def put(self, resource_id):
        resource_id = str(resource_id)
        _get_resource(resource_id, current_user.current_tenant_id, self.resource_model)

        if not current_user.is_admin_or_owner:
            raise Forbidden()

        content_type = request.headers.get("Content-Type")
        if content_type == "application/json":
            try:
                data = request.get_json(silent=True)
            except:
                data = {}
        else:
            data = {}
        if data is None:
            data = {}
        api_key_id = data.get("id", "")

        key = (
            db.session.query(ApiToken)
            .filter(
                getattr(ApiToken, self.resource_id_field) == resource_id,
                ApiToken.type == self.resource_type,
                ApiToken.id == api_key_id,
            )
            .first()
        )

        if key is None:
            flask_restful.abort(404, message="API密钥未找到")

        data = request.get_json()

        # 更新ApiTokenMoneyExtend表中的相关字段
        api_token_money_extend = ApiTokenMoneyExtend.query.filter_by(app_token_id=api_key_id).first()
        if api_token_money_extend:
            if 'description' in data:
                api_token_money_extend.description = data['description']
            if 'day_limit_quota' in data:
                api_token_money_extend.day_limit_quota = data['day_limit_quota']
            if 'month_limit_quota' in data:
                api_token_money_extend.month_limit_quota = data['month_limit_quota']

        db.session.commit()

        # 重新查询以获取更新后的数据
        updated_key = (
            db.session.query(ApiToken, ApiTokenMoneyExtend)
            .join(ApiTokenMoneyExtend, ApiToken.id == ApiTokenMoneyExtend.app_token_id)
            .filter(ApiToken.id == api_key_id)
            .first()
        )

        if updated_key:
            api_token, api_token_money_extend = updated_key
            merged_data = {**api_token.__dict__, **api_token_money_extend.__dict__}
            return merged_data, 200
        else:
            flask_restful.abort(500, message="更新API密钥时发生错误")
    # --------------------- 二开部分End - 密钥额度限制 ---------------------


class BaseApiKeyResource(Resource):
    method_decorators = [account_initialization_required, login_required, setup_required]

    resource_type: str | None = None
    resource_model: Optional[Any] = None
    resource_id_field: str | None = None

    def delete(self, resource_id, api_key_id):
        assert self.resource_id_field is not None, "resource_id_field must be set"
        resource_id = str(resource_id)
        api_key_id = str(api_key_id)
        _get_resource(resource_id, current_user.current_tenant_id, self.resource_model)

        # The role of the current user in the ta table must be admin or owner
        if not current_user.is_admin_or_owner:
            raise Forbidden()

        key = (
            db.session.query(ApiToken)
            .where(
                getattr(ApiToken, self.resource_id_field) == resource_id,
                ApiToken.type == self.resource_type,
                ApiToken.id == api_key_id,
            )
            .first()
        )

        if key is None:
            flask_restx.abort(404, message="API key not found")

        db.session.query(ApiToken).where(ApiToken.id == api_key_id).delete()
        db.session.commit()

        # 二开部分Begin - 密钥额度限制
        db.session.query(ApiTokenMoneyExtend).filter(ApiTokenMoneyExtend.app_token_id == api_key_id).update(
            {ApiTokenMoneyExtend.is_deleted: True}
        )
        db.session.commit()
        # 二开部分End - 密钥额度限制

        return {"result": "success"}, 204


class AppApiKeyListResource(BaseApiKeyListResource):
    def after_request(self, resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    resource_type = "app"
    resource_model = App
    resource_id_field = "app_id"
    token_prefix = "app-"


class AppApiKeyResource(BaseApiKeyResource):
    def after_request(self, resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    resource_type = "app"
    resource_model = App
    resource_id_field = "app_id"


class DatasetApiKeyListResource(BaseApiKeyListResource):
    def after_request(self, resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    resource_type = "dataset"
    resource_model = Dataset
    resource_id_field = "dataset_id"
    token_prefix = "ds-"


class DatasetApiKeyResource(BaseApiKeyResource):
    def after_request(self, resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    resource_type = "dataset"
    resource_model = Dataset
    resource_id_field = "dataset_id"


api.add_resource(AppApiKeyListResource, "/apps/<uuid:resource_id>/api-keys")
api.add_resource(AppApiKeyResource, "/apps/<uuid:resource_id>/api-keys/<uuid:api_key_id>")
api.add_resource(DatasetApiKeyListResource, "/datasets/<uuid:resource_id>/api-keys")
api.add_resource(DatasetApiKeyResource, "/datasets/<uuid:resource_id>/api-keys/<uuid:api_key_id>")
