"""Anonymous browser-bound RP logout navigation; never a login endpoint.

The original local logout already succeeded before a normal workflow exists.
Only HttpOnly browser ownership and single-use private state can continue it.
The provider target (with its native hint) is delivered only as a 303 Location.
"""

import re
from urllib.parse import parse_qs, urlsplit

from configs import dify_config
from controllers.common.schema import query_params_from_model, register_response_schema_models, register_schema_models
from controllers.console import console_ns
from controllers.console.auth.casdoor_extend import _GetOnlyResource
from controllers.console.casdoor_schemas_extend import CasdoorLogoutResponse, CasdoorPayload
from core.casdoor.auth_transactions import CookieDirective, CookiePolicy
from extensions.ext_application_services import application_services
from flask import jsonify, make_response, redirect, request
from flask_restx import Resource
from libs.helper import dump_response
from pydantic import Field, StrictStr
from repositories.casdoor_rp_logout_repository_extend import canonical_opaque
from services.casdoor_rp_logout_service_extend import (
    RP_CALLBACK_PATH,
    RP_HANDOFF_PATH,
    RP_RETRY_PATH,
    RPLogoutHandoff,
    RPLogoutNavigation,
    RPLogoutReturn,
    RPLogoutUnavailable,
    RPProtocolObservation,
)


class CasdoorRPLogoutCallbackQuery(CasdoorPayload):
    state: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)
    error: StrictStr | None = Field(default=None, max_length=1024, repr=False)
    error_description: StrictStr | None = Field(default=None, max_length=2048, repr=False)
    error_uri: StrictStr | None = Field(default=None, max_length=2048, repr=False)


register_schema_models(console_ns, CasdoorRPLogoutCallbackQuery)
register_response_schema_models(console_ns, CasdoorLogoutResponse)


def _cookie(name):
    values = request.cookies.getlist(name)
    if len(values) != 1:
        raise RPLogoutUnavailable()
    return canonical_opaque(values[0])


def apply_rp_cookies(response, owner, cookies):
    """Bounded host-only directives from the private workflow owner."""
    secure = owner._cookie_policy().secure
    seen = set()
    if type(cookies) is not tuple or not 1 <= len(cookies) <= 4:
        raise RPLogoutUnavailable()
    for cookie in cookies:
        if (
            type(cookie) is not CookieDirective
            or cookie.name in seen
            or not re.fullmatch(r"(?:__Secure-)?casdoor_rp_(?:handoff|state|retry)_[a-z0-9_]+", cookie.name)
            or cookie.path not in (RP_CALLBACK_PATH, RP_RETRY_PATH)
            and not re.fullmatch(re.escape(RP_HANDOFF_PATH) + r"[A-Za-z0-9_-]{43}", cookie.path)
            or type(cookie.max_age) is not int
            or not 0 <= cookie.max_age <= 300
            or cookie.secure is not secure
            or cookie.httponly is not True
            or cookie.samesite != "Lax"
            or cookie.domain is not None
            or type(cookie.value) is not str
            or (cookie.value != "" if cookie.max_age == 0 else canonical_opaque(cookie.value) != cookie.value)
        ):
            raise RPLogoutUnavailable()
        seen.add(cookie.name)
        response.set_cookie(
            cookie.name,
            cookie.value,
            max_age=cookie.max_age,
            path=cookie.path,
            secure=cookie.secure,
            httponly=True,
            samesite="Lax",
        )
    return response


def _web_origin():
    return CookiePolicy(
        dify_config.CONSOLE_WEB_URL,
        allow_loopback_http=dify_config.DEPLOY_ENV == "DEVELOPMENT",
    ).backend_origin.rstrip("/")


def _result_url(completed=False):
    return _web_origin() + "/signin/casdoor-logout?status=" + ("returned" if completed else "unavailable")


def _private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _retry_origin():
    allowed = {
        _web_origin(),
        CookiePolicy(
            dify_config.CONSOLE_API_URL, allow_loopback_http=dify_config.DEPLOY_ENV == "DEVELOPMENT"
        ).backend_origin.rstrip("/"),
    }
    if (
        request.headers.get("Origin") not in allowed
        or request.args
        or request.get_data(cache=True).strip() not in (b"", b"{}")
    ):
        raise RPLogoutUnavailable()


@console_ns.route("/auth/casdoor/logout/retry")
class CasdoorRPLogoutRetryApi(Resource):
    @console_ns.response(
        200, "Anonymous browser-bound logout continuation", console_ns.models[CasdoorLogoutResponse.__name__]
    )
    def post(self):
        try:
            _retry_origin()
            owner = application_services().casdoor_rp_logout
            if owner is None:
                raise RPLogoutUnavailable()
            secure = owner._cookie_policy().secure
            handle_name = ("__Secure-" if secure else "") + "casdoor_rp_retry_handle"
            handle = _cookie(handle_name)
            _, browser_name = owner.retry_cookie_names(handle)
            result = owner.retry(opaque=handle, browser_cookie=_cookie(browser_name))
            if type(result) is not RPLogoutHandoff:
                raise RPLogoutUnavailable()
            response = make_response(
                jsonify(
                    dump_response(
                        CasdoorLogoutResponse,
                        {
                            "status": "handoff_ready",
                            "handoff": {"handoff_path": result.handoff_path},
                        },
                    )
                )
            )
            return _private_response(apply_rp_cookies(response, owner, result.cookies))
        except Exception:
            return _private_response(
                make_response(jsonify(dump_response(CasdoorLogoutResponse, {"status": "local_only"})))
            )


@console_ns.route("/auth/casdoor/logout/<string:handle>")
class CasdoorRPLogoutHandoffApi(_GetOnlyResource):
    @console_ns.response(303, "Reviewed provider logout navigation")
    def get(self, handle):
        try:
            canonical_opaque(handle)
            if request.args or request.get_data(cache=False):
                raise RPLogoutUnavailable()
            owner = application_services().casdoor_rp_logout
            if owner is None:
                raise RPLogoutUnavailable()
            result = owner.navigate(opaque=handle, browser_cookie=_cookie(owner.handoff_cookie_name(handle)))
            if type(result) is not RPLogoutNavigation:
                raise RPLogoutUnavailable()
            target = urlsplit(result.location)
            params = parse_qs(target.query, strict_parsing=True)
            if (
                target.scheme != "https"
                or target.username
                or target.password
                or target.fragment
                or set(params) != {"id_token_hint", "post_logout_redirect_uri", "state"}
                or any(len(value) != 1 for value in params.values())
            ):
                raise RPLogoutUnavailable()
            return _private_response(apply_rp_cookies(redirect(result.location, code=303), owner, result.cookies))
        except Exception:
            return _private_response(redirect(_result_url(), code=303))


@console_ns.route("/auth/casdoor/logout/callback")
class CasdoorRPLogoutCallbackApi(_GetOnlyResource):
    @console_ns.doc(params=query_params_from_model(CasdoorRPLogoutCallbackQuery))
    @console_ns.response(303, "Fixed logout result; no new Dify session")
    def get(self):
        owner = None
        state = None
        try:
            data = {}
            for key, values in request.args.lists():
                if key not in CasdoorRPLogoutCallbackQuery.model_fields or len(values) != 1:
                    raise RPLogoutUnavailable()
                data[key] = values[0]
            query = CasdoorRPLogoutCallbackQuery.model_validate(data)
            state = canonical_opaque(query.state)
            owner = application_services().casdoor_rp_logout
            if owner is None or request.get_data(cache=False):
                raise RPLogoutUnavailable()
            result = owner.complete_callback(
                state=state,
                browser_cookie=_cookie(owner.callback_cookie_name(state)),
                provider_error=query.error is not None
                or query.error_description is not None
                or query.error_uri is not None,
            )
            if type(result) is RPProtocolObservation:
                target = _web_origin() + "/system-manage-extend/system-integration?tab=casdoor"
                response = redirect(target, code=303)
                return _private_response(apply_rp_cookies(response, owner, (owner.clear_callback_cookie(state),)))
            if type(result) is RPLogoutReturn:
                return _private_response(
                    apply_rp_cookies(redirect(_result_url(result.completed), code=303), owner, result.cookies)
                )
            raise RPLogoutUnavailable()
        except Exception:
            response = redirect(_result_url(), code=303)
            if owner is not None and state is not None:
                try:
                    apply_rp_cookies(response, owner, (owner.clear_callback_cookie(state),))
                except Exception:
                    pass
            return _private_response(response)
