"""
Extend: 系统管理功能 — Controller 路由与权限装饰器
迁移自 Admin Center (Go+Vue) 至 Dify Console 原生技术栈
"""

import logging
from collections.abc import Callable
from functools import wraps

from flask import abort, request
from flask_restx import Resource
from pydantic import BaseModel, ValidationError

from controllers.common.schema import (
    register_response_schema_models,
    register_schema_models,
)
from controllers.console import api
from controllers.console.dingtalk_schemas_extend import (
    DingTalkConfigPayload,
    DingTalkConfigResponse,
    EmailLookupTestPayload,
    IntegrationTestResponse,
)
from controllers.console.wraps import account_initialization_required, setup_required
from extensions.ext_database import db
from libs.helper import dump_response
from libs.login import current_user, login_required
from models.system_extend import CodeExecutionControlExtend
from services.dingtalk_email_lookup_extend import lookup_email
from services.system_manage_extend import (
    CodeExecutionControlService,
    QuotaManageService,
    SystemIntegrationManageService,
)
from services.system_management_access_service_extend import SystemManagementAccessService

logger = logging.getLogger(__name__)

register_schema_models(api, DingTalkConfigPayload, EmailLookupTestPayload)
register_response_schema_models(api, DingTalkConfigResponse, IntegrationTestResponse)


def system_admin_required_extend[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    """
    确保当前用户有系统管理权限:
    1. 用户已登录（由外层 @login_required 保证）
    2. 当前 workspace 是数据库固定关联的初始化空间
    3. 用户在该空间的实际数据库角色是 owner/admin
    """

    @wraps(f)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        if not SystemManagementAccessService.can_manage(current_user, session=db.session):
            abort(403, "System admin permission required.")
        return f(*args, **kwargs)

    return decorated


# ==================== 钉钉配置 ====================


class DingTalkConfigExtend(Resource):
    """钉钉 SSO 配置管理"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    @api.response(200, "DingTalk configuration", api.models["DingTalkConfigResponse"])
    def get(self):
        """获取钉钉配置"""
        config = SystemIntegrationManageService.get_config(classify=1)
        return dump_response(DingTalkConfigResponse, config), 200

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    @api.expect(api.models["DingTalkConfigPayload"])
    @api.response(200, "Configuration saved", api.models["IntegrationTestResponse"])
    def post(self):
        """保存钉钉配置"""
        data = request.get_json()
        if not data:
            abort(400, "Request body is required.")
        try:
            payload = DingTalkConfigPayload.model_validate(data)
            SystemIntegrationManageService.set_config(classify=1, data=payload.model_dump(exclude_none=True))
            return {"result": "success"}, 200
        except ValidationError:
            abort(400, "Invalid DingTalk configuration.")
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("Failed to save DingTalk config")
            abort(500, f"Failed to save config: {e}")


class DingTalkTestExtend(Resource):
    """测试钉钉连接"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    @api.response(200, "DingTalk connection test", api.models["IntegrationTestResponse"])
    def get(self):
        """测试钉钉 AppKey/AppSecret 是否有效"""
        try:
            result = SystemIntegrationManageService.test_dingtalk_connection()
            return result, 200
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("DingTalk test failed")
            abort(500, f"Test failed: {e}")


class DingTalkTestCallbackExtend(Resource):
    """钉钉测试回调"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """处理钉钉测试回调"""
        data = request.get_json()
        if not data or "code" not in data:
            abort(400, "Auth code is required.")
        try:
            result = SystemIntegrationManageService.dingtalk_test_callback(data["code"])
            return result, 200
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("DingTalk test callback failed")
            abort(500, f"Callback failed: {e}")


# ==================== OAuth2 配置 ====================


class OAuth2ConfigExtend(Resource):
    """OAuth2.0 集成配置管理"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def get(self):
        """获取 OAuth2 配置"""
        config = SystemIntegrationManageService.get_config(classify=4)
        return config, 200

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """保存 OAuth2 配置"""
        data = request.get_json()
        if not data:
            abort(400, "Request body is required.")
        try:
            SystemIntegrationManageService.set_config(classify=4, data=data)
            return {"result": "success"}, 200
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("Failed to save OAuth2 config")
            abort(500, f"Failed to save config: {e}")


class OAuth2TestExtend(Resource):
    """测试 OAuth2 连接"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """测试 OAuth2 连接"""
        data = request.get_json()
        if not data:
            abort(400, "Request body is required.")
        try:
            result = SystemIntegrationManageService.test_oauth2_connection(data)
            return result, 200
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("OAuth2 test failed")
            abort(500, f"Test failed: {e}")


# ==================== 邮箱 API ====================


class EmailApiTestExtend(Resource):
    """测试钉钉企业邮箱查询；兼容旧连通性测试请求。"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    @api.expect(api.models["EmailLookupTestPayload"])
    @api.response(200, "Enterprise email lookup test", api.models["IntegrationTestResponse"])
    def post(self):
        """使用草稿配置和钉钉用户 ID 查询邮箱，不创建账号。"""
        try:
            payload = EmailLookupTestPayload.model_validate(request.get_json())
            if payload.config is not None:
                result = lookup_email(payload.user_id, payload.config)
            else:
                result = SystemIntegrationManageService.test_email_api(api_url=payload.url, api_key=payload.key)
            return dump_response(IntegrationTestResponse, result), 200
        except ValidationError:
            abort(400, "Invalid email lookup test request.")
        except ValueError as e:
            abort(400, str(e))
        except Exception:
            abort(500, "Email lookup test failed.")


# ==================== 转发 Token ====================


class ForwardTokenListExtend(Resource):
    """转发 Token 列表管理"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def get(self):
        """获取转发 Token 列表"""
        try:
            tokens = SystemIntegrationManageService.get_forward_tokens()
            return {"tokens": tokens}, 200
        except Exception as e:
            logger.exception("Failed to get forward tokens")
            abort(500, f"Failed to get tokens: {e}")

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """创建转发 Token"""
        data = request.get_json()
        if not data or "name" not in data:
            abort(400, "Token name is required.")
        try:
            token = SystemIntegrationManageService.create_forward_token(name=data["name"])
            return token, 201
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("Failed to create forward token")
            abort(500, f"Failed to create token: {e}")


class ForwardTokenDetailExtend(Resource):
    """转发 Token 详情操作"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def delete(self, seq):
        """删除转发 Token"""
        try:
            SystemIntegrationManageService.delete_forward_token(seq=seq)
            return {"result": "success"}, 200
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("Failed to delete forward token")
            abort(500, f"Failed to delete token: {e}")


# ==================== 路由注册 ====================

api.add_resource(DingTalkConfigExtend, "/system-manage-extend/integration/dingtalk")
api.add_resource(DingTalkTestExtend, "/system-manage-extend/integration/dingtalk/test")
api.add_resource(DingTalkTestCallbackExtend, "/system-manage-extend/integration/dingtalk/test-callback")
api.add_resource(OAuth2ConfigExtend, "/system-manage-extend/integration/oauth2")
api.add_resource(OAuth2TestExtend, "/system-manage-extend/integration/oauth2/test")
api.add_resource(EmailApiTestExtend, "/system-manage-extend/integration/email-api/test")
api.add_resource(ForwardTokenListExtend, "/system-manage-extend/forward-tokens")
api.add_resource(ForwardTokenDetailExtend, "/system-manage-extend/forward-tokens/<int:seq>")


# ==================== 用户额度管理 ====================


class QuotaListQueryExtend(BaseModel):
    """用户额度分页列表查询参数（原 reqparse 参数定义的等价 Pydantic 模型）"""

    page: int = 1
    page_size: int = 10
    keyword: str = ""


class QuotaManagementListExtend(Resource):
    """用户额度管理 — 分页列表查询"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def get(self):
        """获取用户额度分页列表，支持按 name/email 搜索"""
        args = QuotaListQueryExtend.model_validate(request.args.to_dict(flat=True))

        try:
            result = QuotaManageService.get_quota_list(
                page=args.page,
                page_size=args.page_size,
                keyword=args.keyword or "",
            )
            return result, 200
        except Exception as e:
            logger.exception("Failed to get quota list")
            abort(500, f"Failed to get quota list: {e}")


class QuotaManagementSetExtend(Resource):
    """用户额度管理 — 设置单用户额度"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """设置指定用户的总额度（UPSERT）"""
        data = request.get_json()
        if not data:
            abort(400, "Request body is required.")

        account_id = data.get("account_id", "").strip()
        quota = data.get("quota")

        if not account_id:
            abort(400, "account_id is required.")
        if quota is None or not isinstance(quota, (int, float)):
            abort(400, "quota must be a number.")

        try:
            QuotaManageService.set_user_quota(account_id=account_id, quota=float(quota))
            return {"result": "success"}, 200
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("Failed to set user quota")
            abort(500, f"Failed to set quota: {e}")


api.add_resource(QuotaManagementListExtend, "/system-manage-extend/quota-management")
api.add_resource(QuotaManagementSetExtend, "/system-manage-extend/quota-management/set")


# ==================== 代码执行控制（sandbox-full 授权名单） ====================


def _serialize_code_execution_control(record: CodeExecutionControlExtend) -> dict:
    """按 API 契约序列化单条授权记录（created_at 为 ISO8601 字符串）"""
    return {
        "id": str(record.id),
        "email": record.email,
        "created_by": str(record.created_by) if record.created_by else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


class CodeExecutionControlListExtend(Resource):
    """sandbox-full 授权邮箱名单 — 查询与添加"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def get(self):
        """获取授权邮箱名单（created_at 升序）"""
        records = CodeExecutionControlService.list_emails()
        return {"items": [_serialize_code_execution_control(r) for r in records]}, 200

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """添加授权邮箱；邮箱格式非法或重复返回 400"""
        data = request.get_json(silent=True)
        if not data or "email" not in data:
            abort(400, "email is required.")
        try:
            record, cache_synced = CodeExecutionControlService.add_email(
                email=data["email"],
                created_by=current_user.id,
            )
        except ValueError as e:
            abort(400, str(e))
        return {
            "result": "success",
            "item": _serialize_code_execution_control(record),
            "cache_synced": cache_synced,
        }, 201


class CodeExecutionControlDetailExtend(Resource):
    """sandbox-full 授权邮箱名单 — 删除单条记录"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def delete(self, record_id: str):
        """删除授权记录；记录不存在返回 404"""
        try:
            cache_synced = CodeExecutionControlService.remove_email(record_id=record_id)
        except ValueError as e:
            abort(404, str(e))
        return {"result": "success", "cache_synced": cache_synced}, 200


api.add_resource(CodeExecutionControlListExtend, "/system-manage-extend/code-execution-control")
api.add_resource(CodeExecutionControlDetailExtend, "/system-manage-extend/code-execution-control/<string:record_id>")
