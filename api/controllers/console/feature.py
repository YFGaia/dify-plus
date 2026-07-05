# extend: start CVE-2025-63387未授权访问（JWT 签发/校验所需依赖）
from datetime import UTC, datetime, timedelta

import jwt
from flask import make_response, request
from flask_restx import Resource
from werkzeug.exceptions import Forbidden

from configs import dify_config
from constants import COOKIE_NAME_LOGIN_CONFIG_TOKEN, HEADER_NAME_LOGIN_CONFIG_TOKEN
from controllers.common.schema import register_response_schema_models
from fields.base import ResponseModel
from libs.helper import dump_response, extract_remote_ip
from libs.login import current_account_with_tenant_optional, login_required
from services.feature_service import (
    FeatureModel,
    FeatureService,
    LimitationModel,
    SystemFeatureModel,
)

# extend: stop CVE-2025-63387未授权访问
from . import console_ns
from .wraps import (
    account_initialization_required,
    cloud_utm_record,
    setup_required,
    with_current_tenant_id,
)


class TrialModelsResponse(ResponseModel):
    trial_models: list[str]


class AppDslVersionResponse(ResponseModel):
    app_dsl_version: str


register_response_schema_models(
    console_ns,
    AppDslVersionResponse,
    FeatureModel,
    LimitationModel,
    SystemFeatureModel,
    TrialModelsResponse,
)


def _issue_login_config_jwt(ip: str) -> str:
    """extend: CVE-2025-63387 签发 JWT，payload 含 ip 与 1h 过期。"""
    payload = {
        "ip": ip,
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    return jwt.encode(payload, dify_config.SECRET_KEY, algorithm="HS256")


def _verify_login_config_token(token: str | None) -> bool:
    """extend: CVE-2025-63387 校验 JWT 签名、过期时间，以及当前请求 IP 与 payload.ip 一致。"""
    if not token:
        return False
    try:
        payload = jwt.decode(token, dify_config.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return payload.get("ip") == extract_remote_ip(request)


# extend: 防止部分健康监测system-features无响应
@console_ns.route("/system-features")
class SystemFeatureHealthApi(Resource):
    """extend: 防止部分健康监测system-features无响应"""

    @console_ns.doc("system-features")
    @console_ns.response(200, "Success")
    def get(self):
        return make_response({"ping": True})


# extend: start CVE-2025-63387未授权访问
@console_ns.route("/login_config_bootstrap")
class LoginConfigBootstrapApi(Resource):
    """extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的

    写入 features 相关 cookie（值为 JWT，含 ip 与 1h 过期），
    同时返回 token 供前端在跨域时通过 Header 携带。
    """

    @console_ns.doc("login_config_bootstrap")
    @console_ns.response(200, "Success")
    def get(self):
        client_ip = extract_remote_ip(request)
        token = _issue_login_config_jwt(client_ip)
        resp = make_response({"ok": True, "token": token})
        resp.set_cookie(
            COOKIE_NAME_LOGIN_CONFIG_TOKEN,
            value=token,
            max_age=3600,
            httponly=True,
            samesite="Lax",
        )
        return resp


# extend: stop CVE-2025-63387未授权访问


@console_ns.route("/features")
class FeatureApi(Resource):
    @console_ns.doc("get_tenant_features")
    @console_ns.doc(description="Get feature configuration for current tenant")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[FeatureModel.__name__],
    )
    @setup_required
    @login_required
    @account_initialization_required
    @cloud_utm_record
    @with_current_tenant_id
    def get(self, current_tenant_id: str):
        """Get feature configuration for current tenant"""
        payload = FeatureService.get_features(
            current_tenant_id,
            exclude_vector_space=True,
        ).model_dump()
        payload.pop("vector_space", None)
        return payload


@console_ns.route("/features/vector-space")
class FeatureVectorSpaceApi(Resource):
    @console_ns.doc("get_tenant_feature_vector_space")
    @console_ns.doc(description="Get vector-space usage and limit for current tenant")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[LimitationModel.__name__],
    )
    @setup_required
    @login_required
    @account_initialization_required
    @cloud_utm_record
    @with_current_tenant_id
    def get(self, current_tenant_id: str):
        """Get vector-space usage and limit for current tenant"""
        return FeatureService.get_vector_space(current_tenant_id).model_dump()


@console_ns.route("/trial-models")
class TrialModelsApi(Resource):
    @console_ns.doc("get_trial_models")
    @console_ns.doc(description="Get hosted trial model provider configuration")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[TrialModelsResponse.__name__],
    )
    @setup_required
    @login_required
    @account_initialization_required
    def get(self):
        """Get hosted trial model provider configuration for model-provider pages."""
        return dump_response(
            TrialModelsResponse,
            {"trial_models": FeatureService.get_trial_models()},
        )


@console_ns.route("/app-dsl-version")
class AppDslVersionApi(Resource):
    @console_ns.doc("get_app_dsl_version")
    @console_ns.doc(description="Get current app DSL version")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[AppDslVersionResponse.__name__],
    )
    def get(self):
        """Get current app DSL version for workflow clipboard compatibility."""
        return dump_response(
            AppDslVersionResponse,
            {"app_dsl_version": FeatureService.get_app_dsl_version()},
        )


# extend: start CVE-2025-63387未授权访问
@console_ns.route("/login_config")
class LoginConfigApi(Resource):
    """extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的

    仅当请求带有 login_config_bootstrap 写入的 cookie 时才返回登录配置，
    避免未经过控制台入口的扫描直接获取系统配置。
    """

    @console_ns.doc("get_login_config")
    @console_ns.doc(description="Get system-wide login/feature configuration")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[SystemFeatureModel.__name__],
    )
    @console_ns.response(403, "Missing or invalid login_config token")
    def get(self):
        """Get system-wide feature configuration

        NOTE: This endpoint is unauthenticated by design, as it provides system features
        data required for dashboard initialization.

        Authentication would create circular dependency (can't login without dashboard loading).

        Only non-sensitive configuration data should be returned by this endpoint.
        """
        # extend: CVE-2025-63387 支持 Cookie 或 Header 携带 JWT（跨域时 Cookie 可能为 None，用 Header）
        token = request.cookies.get(COOKIE_NAME_LOGIN_CONFIG_TOKEN) or request.headers.get(
            HEADER_NAME_LOGIN_CONFIG_TOKEN
        )
        if not _verify_login_config_token(token):
            raise Forbidden(
                "Missing or invalid login_config token (cookie or X-Login-Config-Token); "
                "call /login_config_bootstrap first."
            )
        # extend: stop CVE-2025-63387未授权访问
        current_user, _ = current_account_with_tenant_optional()
        is_authenticated = current_user is not None
        return FeatureService.get_system_features(is_authenticated=is_authenticated).model_dump()
