"""DingTalk public navigation; every callback requires the same safe transaction."""

from urllib.parse import urlencode

from configs import dify_config
from flask import make_response, redirect, request
from flask_restx import Resource
from libs.helper import dump_response
from libs.login import current_user
from libs.token import (
    extract_refresh_token,
    set_access_token_to_cookie,
    set_csrf_token_to_cookie,
    set_refresh_token_to_cookie,
)
from pydantic import ValidationError
from services.dingtalk_auth_service_extend import DingTalkAuthError, DingTalkAuthService

from controllers.common.schema import query_params_from_model, register_response_schema_models
from controllers.console.dingtalk_schemas_extend import DingTalkCallbackQuery, DingTalkDisplayResponse
from controllers.console.wraps import setup_required

from .. import api

register_response_schema_models(api, DingTalkDisplayResponse)


class DingTalkDisplay(Resource):
    @api.response(200, "Public login display", api.models["DingTalkDisplayResponse"])
    def get(self):
        try:
            data = DingTalkAuthService().display()
        except Exception:
            data = {"enabled": False, "button_text": "钉钉", "start_path": "/console/api/auth/dingtalk/start"}
        response = make_response(dump_response(DingTalkDisplayResponse, data))
        response.headers["Cache-Control"] = "no-store"
        return response


class DingTalkStart(Resource):
    @setup_required
    @api.response(302, "Navigate to DingTalk authorization")
    def get(self):
        try:
            service = DingTalkAuthService()
            uri, state, cookie = service.authorization_start(server_ip=request.remote_addr or "")
            response = redirect(uri)
            response.set_cookie(
                service.cookie_name(state),
                cookie,
                max_age=300,
                httponly=True,
                secure=service.callback_url().startswith("https://"),
                samesite="Lax",
                path="/console/api",
            )
        except DingTalkAuthError as error:
            response = _error_redirect(error.code)
        except Exception:
            response = _error_redirect("dingtalk_operation_failed")
        response.headers["Cache-Control"] = "no-store"
        return response


def _error_redirect(code, diagnostic=False):
    root = dify_config.CONSOLE_WEB_URL.rstrip("/")
    path = "/system-manage-extend/system-integration" if diagnostic else "/signin"
    query = {"tab": "dingtalk", "dingtalk_result": code} if diagnostic else {"dingtalk_error": code}
    return redirect(root + path + "?" + urlencode(query))


class DingTalk(Resource):
    @setup_required
    @api.doc(params=query_params_from_model(DingTalkCallbackQuery))
    @api.response(302, "Complete browser-bound login or diagnostic")
    def get(self):
        service = DingTalkAuthService()
        state = request.args.get("state", "")
        diagnostic = False
        try:
            transaction = service.consume(state, request.cookies.get(service.cookie_name(state)))
            diagnostic = bool(transaction["source"])
            code = request.args.get("authCode") or request.args.get("code", "")
            query = DingTalkCallbackQuery.model_validate({"state": state, "authCode": code})
            pair = service.complete(
                transaction,
                query.authCode,
                account=current_user if diagnostic else None,
                refresh_token=extract_refresh_token(request) if diagnostic else None,
                ip_address=request.remote_addr,
            )
            if diagnostic:
                response = _error_redirect("success", diagnostic=True)
            else:
                response = redirect(dify_config.CONSOLE_WEB_URL.rstrip("/") + "/explore/apps-center-extend")
                set_access_token_to_cookie(request, response, pair.access_token)
                set_refresh_token_to_cookie(request, response, pair.refresh_token)
                set_csrf_token_to_cookie(request, response, pair.csrf_token)
        except DingTalkAuthError as error:
            response = _error_redirect(error.code, diagnostic)
        except ValidationError:
            response = _error_redirect("invalid_authorization_code", diagnostic)
        except Exception:
            response = _error_redirect("dingtalk_operation_failed", diagnostic)
        if len(state) == 43:
            response.delete_cookie(
                service.cookie_name(state),
                path="/console/api",
                secure=service.callback_url().startswith("https://"),
                httponly=True,
                samesite="Lax",
            )
        response.headers["Cache-Control"] = "no-store"
        return response


# Legacy URLs route to the safe callback; neither permits code-only login.
DingTalkThirdParty = DingTalk
api.add_resource(DingTalkDisplay, "/auth/dingtalk/display")
api.add_resource(DingTalkStart, "/auth/dingtalk/start")
api.add_resource(DingTalk, "/auth/dingtalk/callback", "/ding-talk/login", "/ding-talk/third-party/login")
