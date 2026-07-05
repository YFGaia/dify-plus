"""
Extend: 系统管理功能 — Controller 路由与权限装饰器
迁移自 Admin Center (Go+Vue) 至 Dify Console 原生技术栈
"""

import logging
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from flask import abort, request
from flask_restx import Resource
from pydantic import BaseModel

from controllers.console import api
from controllers.console.wraps import account_initialization_required, setup_required
from libs.login import current_user, login_required
from services.system_manage_extend import QuotaManageService, SystemIntegrationManageService

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def system_admin_required_extend(f: Callable[P, R]) -> Callable[P, R]:
    """
    确保当前用户有系统管理权限:
    1. 用户已登录（由外层 @login_required 保证）
    2. 用户是当前 workspace 的 owner
    3. 或用户是 admin 角色
    """

    @wraps(f)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        if not current_user.is_admin_or_owner:
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
    def get(self):
        """获取钉钉配置"""
        config = SystemIntegrationManageService.get_config(classify=1)
        return config, 200

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """保存钉钉配置"""
        data = request.get_json()
        if not data:
            abort(400, "Request body is required.")
        try:
            SystemIntegrationManageService.set_config(classify=1, data=data)
            return {"result": "success"}, 200
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
    """测试邮箱 API"""

    @setup_required
    @login_required
    @account_initialization_required
    @system_admin_required_extend
    def post(self):
        """测试邮箱 API 连通性"""
        data = request.get_json()
        if not data:
            abort(400, "Request body is required.")
        try:
            result = SystemIntegrationManageService.test_email_api(
                api_url=data.get("url", ""),
                api_key=data.get("key", ""),
            )
            return result, 200
        except ValueError as e:
            abort(400, str(e))
        except Exception as e:
            logger.exception("Email API test failed")
            abort(500, f"Test failed: {e}")


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
