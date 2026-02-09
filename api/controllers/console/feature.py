from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from flask import make_response, request
from flask_restx import Resource, fields
from werkzeug.exceptions import Forbidden, Unauthorized

from configs import dify_config
from constants import COOKIE_NAME_LOGIN_CONFIG_TOKEN, HEADER_NAME_LOGIN_CONFIG_TOKEN
from libs.helper import extract_remote_ip
from libs.login import current_account_with_tenant, current_user, login_required
from services.feature_service import FeatureService

from . import console_ns
from .wraps import account_initialization_required, cloud_utm_record, setup_required


def _issue_login_config_jwt(ip: str) -> str:
    """extend: CVE-2025-63387 签发 JWT，payload 含 ip 与 1h 过期。"""
    payload = {
        "ip": ip,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, dify_config.SECRET_KEY, algorithm="HS256")


def _verify_login_config_token(token: Optional[str]) -> bool:
    """extend: CVE-2025-63387 校验 JWT 签名、过期时间，以及当前请求 IP 与 payload.ip 一致。"""
    if not token:
        return False
    try:
        payload = jwt.decode(token, dify_config.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return payload.get("ip") == extract_remote_ip(request)


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
        console_ns.model("FeatureResponse", {"features": fields.Raw(description="Feature configuration object")}),
    )
    @setup_required
    @login_required
    @account_initialization_required
    @cloud_utm_record
    def get(self):
        """Get feature configuration for current tenant"""
        _, current_tenant_id = current_account_with_tenant()

        return FeatureService.get_features(current_tenant_id).model_dump()



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
        console_ns.model(
            "LoginConfigResponse", {"features": fields.Raw(description="System feature configuration object")}
        ),
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
        try:
            is_authenticated = current_user.is_authenticated
        except Unauthorized:
            is_authenticated = False
        return FeatureService.get_system_features(is_authenticated=is_authenticated).model_dump()
