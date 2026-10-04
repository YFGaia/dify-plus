"""Ordinary LOCAL Casdoor transport; authorization remains with the actual A2 owner.

Only delivered responses can expire browser cookies. A propagated cancellation
has no response: its original safe A2 metadata is not cookie delivery evidence.
"""

import re
from ipaddress import ip_address
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from controllers.common.schema import (
    query_params_from_model,
    register_response_schema_models,
    register_schema_models,
)
from controllers.console import bp, console_ns
from controllers.console.casdoor_schemas_extend import (
    CasdoorCallbackQuery,
    CasdoorPayload,
    CasdoorRestrictedResultResponse,
    CasdoorResultQuery,
    CasdoorResultResponse,
)
from core.casdoor.auth_transactions import (
    COOKIE_PATH,
    SCOPE_COOKIE_NAME,
    TTL_SECONDS,
    AuthTransactionError,
    CookieDirective,
    CookiePolicy,
    RestrictedResultPayload,
    result_cookie_name,
    transaction_cookie_name,
)
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.request_safety import (
    PublicError,
    RequestAction,
    SafetyEvent,
    format_public_error,
    record_safety_event,
)
from extensions.ext_application_services import application_services
from flask import Response, jsonify, make_response, redirect, request
from flask_restx import Resource
from libs.helper import dump_response
from libs.token import (
    set_access_token_to_cookie,
    set_csrf_token_to_cookie,
    set_refresh_token_to_cookie,
)
from pydantic import Field, StrictStr, ValidationError, model_validator
from services.casdoor_local_http_service_extend import (
    _CancelledNavigation,
    _CompleteResult,
    _RestrictedNavigation,
    _RestrictedResult,
    _StartResult,
)
from services.entities.account_login_entities import AuthTokenPair


class CasdoorLoginQuery(CasdoorPayload):
    init: StrictStr | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)
    return_path: StrictStr | None = Field(default=None, max_length=2048)
    locale: StrictStr | None = Field(default=None, max_length=64)
    timezone: StrictStr | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def initialization_only(self) -> "CasdoorLoginQuery":
        if self.init is not None and self.model_fields_set != {"init"}:
            raise ValueError("initialization accepts no navigation fields")
        return self


register_schema_models(console_ns, CasdoorLoginQuery, CasdoorCallbackQuery, CasdoorResultQuery)
register_response_schema_models(console_ns, CasdoorResultResponse, CasdoorRestrictedResultResponse)


@bp.after_app_request
def _casdoor_auth_headers(response: Response) -> Response:
    """Routing failures and future descendants have the same privacy boundary."""
    if request.path == COOKIE_PATH or request.path.startswith(COOKIE_PATH + "/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _query(model):
    values = {}
    for key, items in request.args.lists():
        if key not in model.model_fields or len(items) != 1:
            raise AuthTransactionError()
        values[key] = items[0]
    return model.model_validate(values)


def _direct_ip() -> str:
    value = request.remote_addr
    try:
        if type(value) is not str:
            raise ValueError()
        ip_address(value)
    except ValueError:
        raise AuthTransactionError() from None
    return value


def _security_cookie(name: str) -> str | None:
    values = request.cookies.getlist(name)
    if len(values) > 1:
        raise AuthTransactionError()
    return values[0] if values else None


def _error_response(error: PublicError) -> Response:
    response = make_response(jsonify(error.payload()), error.status)
    if error.retry_after_seconds is not None:
        response.headers["Retry-After"] = str(error.retry_after_seconds)
    return response


def _transport_error(error: Exception | CasdoorErrorCode, correlation: UUID, action: RequestAction) -> PublicError:
    """Log only the closed public projection, preserving cancellation identity."""
    public = format_public_error(error, correlation_id=correlation)
    try:
        record_safety_event(SafetyEvent(action, public.code, correlation))
    except Exception:
        pass
    return public


def _validate_cookies(service, cookies, *, status: int, state: str | None = None):
    """Accept only the original branch's exact host-only directive set."""
    if type(cookies) is not tuple:
        raise AuthTransactionError()
    if not cookies:
        if status in (302, 303):
            raise AuthTransactionError()
        return cookies
    secure = service._policy().secure  # Pure original configuration policy, no I/O.
    if state is not None:
        expected = {transaction_cookie_name(state): ("", 0, COOKIE_PATH + "/callback")}
    elif status == 303:
        expected = {SCOPE_COOKIE_NAME: (None, TTL_SECONDS, COOKIE_PATH)}
    elif status == 302 and len(cookies) == 2:
        # The transaction name is checked against the actual result's state by
        # the caller; cookie presence itself is never authorization evidence.
        expected = {
            SCOPE_COOKIE_NAME: (None, TTL_SECONDS, COOKIE_PATH),
            cookies[0].name: (None, TTL_SECONDS, COOKIE_PATH + "/callback"),
        }
    else:
        raise AuthTransactionError()
    if len(cookies) != len(expected):
        raise AuthTransactionError()
    seen = set()
    for cookie in cookies:
        if type(cookie) is not CookieDirective or cookie.name not in expected or cookie.name in seen:
            raise AuthTransactionError()
        seen.add(cookie.name)
        value, max_age, path = expected[cookie.name]
        if (
            type(cookie.max_age) is not int
            or cookie.max_age != max_age
            or cookie.path != path
            or type(cookie.secure) is not bool
            or cookie.secure != secure
            or cookie.httponly is not True
            or cookie.samesite != "Lax"
            or cookie.domain is not None
            or type(cookie.value) is not str
            or (
                cookie.value != value if value is not None else re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie.value) is None
            )
        ):
            raise AuthTransactionError()
    return cookies


def _apply_cookies(response: Response, cookies) -> Response:
    for cookie in cookies:
        response.set_cookie(
            cookie.name,
            cookie.value,
            max_age=cookie.max_age,
            path=cookie.path,
            secure=cookie.secure,
            httponly=cookie.httponly,
            samesite=cookie.samesite,
            domain=cookie.domain,
        )
    return response


def _safe_parse_clear(service):
    """Malformed queries can expire only a single canonical, unique state."""
    states = request.args.getlist("state")
    if len(states) != 1:
        return ()
    try:
        name = transaction_cookie_name(states[0])
        cookie = CookieDirective(name, "", 0, COOKIE_PATH + "/callback", service._policy().secure)
        return _validate_cookies(service, (cookie,), status=400, state=states[0])
    except Exception:
        return ()


def _exact_directives(service, cookies, expected):
    """Ordered branch-specific host-only directives, never cookie authority."""
    if type(cookies) is not tuple or len(cookies) != len(expected):
        raise AuthTransactionError()
    secure = service._policy().secure
    for cookie, (name, value, age, path) in zip(cookies, expected, strict=True):
        if (
            type(cookie) is not CookieDirective
            or cookie.name != name
            or type(cookie.value) is not str
            or (
                cookie.value != value if value is not None else re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie.value) is None
            )
            or type(cookie.max_age) is not int
            or cookie.max_age != age
            or cookie.path != path
            or type(cookie.secure) is not bool
            or cookie.secure != secure
            or cookie.httponly is not True
            or cookie.samesite != "Lax"
            or cookie.domain is not None
        ):
            raise AuthTransactionError()
    return cookies


def _navigation_cookies(service, result, state, browser_scope):
    """Validate against pure trusted settings; never read another active snapshot."""
    if type(result.correlation_id) is not UUID:
        raise AuthTransactionError()
    policy = service._policy()
    web = CookiePolicy(service._settings.CONSOLE_WEB_URL, allow_loopback_http=policy.allow_loopback_http)
    target = web.backend_origin.rstrip("/") + "/signin"
    expected = [(transaction_cookie_name(state), "", 0, COOKIE_PATH + "/callback")]
    if type(result) is _RestrictedNavigation:
        name = result_cookie_name(result.handoff)
        if type(result.handoff) is not str or type(browser_scope) is not str:
            raise AuthTransactionError()
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", browser_scope) is None:
            raise AuthTransactionError()
        target += "/casdoor-result?handoff=" + result.handoff
        expected.extend(
            [
                (name, None, TTL_SECONDS, COOKIE_PATH + "/result"),
                (SCOPE_COOKIE_NAME, browser_scope, TTL_SECONDS, COOKIE_PATH),
            ]
        )
    if type(result.redirect) is not str or result.redirect != target:
        raise AuthTransactionError()
    return _exact_directives(service, result.cookies, expected)


def _result_clear(service, cookies, handoff):
    return _exact_directives(service, cookies, [(result_cookie_name(handoff), "", 0, COOKIE_PATH + "/result")])


def _safe_result_clear(service):
    handles = request.args.getlist("handoff")
    if len(handles) != 1:
        return ()
    try:
        cookie = CookieDirective(
            result_cookie_name(handles[0]), "", 0, COOKIE_PATH + "/result", service._policy().secure
        )
        return _result_clear(service, (cookie,), handles[0])
    except Exception:
        return ()


class _GetOnlyResource(Resource):
    @console_ns.doc(False)
    def head(self):
        # RESTX otherwise dispatches automatic HEAD to get(), spending state.
        response = make_response("", 405)
        response.headers["Allow"] = "GET, OPTIONS"
        return response

    @console_ns.doc(False)
    def options(self):
        response = make_response("", 204)
        response.headers["Allow"] = "GET, OPTIONS"
        return response


@console_ns.route("/auth/casdoor/login")
class CasdoorLoginApi(_GetOnlyResource):
    @console_ns.doc(security=[], params=query_params_from_model(CasdoorLoginQuery))
    @console_ns.response(302, "Authorization navigation")
    @console_ns.response(303, "Confirm browser scope")
    @console_ns.response(400, "Invalid transaction", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(409, "Configuration conflict", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(429, "Rate limited", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(503, "Unavailable", console_ns.models[CasdoorResultResponse.__name__])
    def get(self):
        if request.method != "GET":
            return self.head()
        correlation = uuid4()
        try:
            query = _query(CasdoorLoginQuery)
            direct_ip = _direct_ip()
            service = application_services().casdoor_local_http
            result = service.start(
                browser_scope=_security_cookie(SCOPE_COOKIE_NAME),
                init_handle=query.init,
                return_path=query.return_path,
                locale=query.locale,
                timezone=query.timezone,
                server_ip=direct_ip,
            )
            if type(result) is not _StartResult or type(result.correlation_id) is not UUID:
                raise AuthTransactionError()
            correlation = result.correlation_id
            cookies = _validate_cookies(service, result.cookies, status=result.status)
            if result.error is not None:
                return _apply_cookies(_error_response(result.error), cookies)
            if result.status == 302:
                states = parse_qs(urlsplit(result.redirect).query).get("state", [])
                if len(states) != 1 or cookies[0].name != transaction_cookie_name(states[0]):
                    raise AuthTransactionError()
            return _apply_cookies(redirect(result.redirect, code=result.status), cookies)
        except (ValidationError, AuthTransactionError):
            return _error_response(
                _transport_error(CasdoorErrorCode.INVALID_TRANSACTION, correlation, RequestAction.START)
            )
        except Exception as error:
            return _error_response(_transport_error(error, correlation, RequestAction.START))


@console_ns.route("/auth/casdoor/callback")
class CasdoorCallbackApi(_GetOnlyResource):
    @console_ns.doc(security=[], params=query_params_from_model(CasdoorCallbackQuery))
    @console_ns.response(302, "Completed Console login")
    @console_ns.response(400, "Invalid transaction", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(403, "Authorization denied", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(409, "Authorization pending or conflict", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(429, "Rate limited", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(503, "Unavailable", console_ns.models[CasdoorResultResponse.__name__])
    def get(self):
        if request.method != "GET":
            return self.head()
        correlation = uuid4()
        cookies = ()
        try:
            service = application_services().casdoor_local_http
            cookies = _safe_parse_clear(service)
            query = _query(CasdoorCallbackQuery)
            name = transaction_cookie_name(query.state)
            browser_scope = _security_cookie(SCOPE_COOKIE_NAME)
            result = service.complete(
                state=query.state,
                transaction_cookie=_security_cookie(name),
                browser_scope=browser_scope,
                code=query.code,
                provider_error=query.error,
                server_ip=_direct_ip(),
            )
            if type(result) not in (_CompleteResult, _CancelledNavigation, _RestrictedNavigation):
                raise AuthTransactionError()
            if type(result.correlation_id) is not UUID:
                raise AuthTransactionError()
            correlation = result.correlation_id
            if type(result) in (_CancelledNavigation, _RestrictedNavigation):
                directives = _navigation_cookies(service, result, query.state, browser_scope)
                return _apply_cookies(redirect(result.redirect, code=302), directives)
            if type(result) is not _CompleteResult:
                raise AuthTransactionError()
            directives = _validate_cookies(service, result.cookies, status=result.status, state=query.state)
            if result.error is not None:
                return _apply_cookies(_error_response(result.error), directives)
            if result.status != 302 or type(result.tokens) is not AuthTokenPair:
                raise AuthTransactionError()
            response = redirect(result.redirect, code=result.status)
            # Only the actual A2 normal return carries a genuine completed pair.
            set_access_token_to_cookie(request, response, result.tokens.access_token)
            set_refresh_token_to_cookie(request, response, result.tokens.refresh_token)
            set_csrf_token_to_cookie(request, response, result.tokens.csrf_token)
            return _apply_cookies(response, directives)
        except (ValidationError, AuthTransactionError):
            public = _transport_error(CasdoorErrorCode.INVALID_TRANSACTION, correlation, RequestAction.CALLBACK)
            return _apply_cookies(_error_response(public), cookies)
        except Exception as error:
            return _apply_cookies(
                _error_response(_transport_error(error, correlation, RequestAction.CALLBACK)), cookies
            )
        except BaseException:
            # No deliverable Response exists. Preserve the primary identity and
            # original A2 metadata; the browser's cookie can remain until TTL.
            raise


@console_ns.route("/auth/casdoor/result")
class CasdoorRestrictedResultApi(_GetOnlyResource):
    @console_ns.doc(security=[], params=query_params_from_model(CasdoorResultQuery))
    @console_ns.response(200, "Consumed display result", console_ns.models[CasdoorRestrictedResultResponse.__name__])
    @console_ns.response(400, "Invalid transaction", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(403, "Authorization denied", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(409, "Configuration conflict", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(429, "Rate limited", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(503, "Unavailable", console_ns.models[CasdoorResultResponse.__name__])
    def get(self):
        if request.method != "GET":
            return self.head()
        correlation = uuid4()
        cookies = ()
        try:
            service = application_services().casdoor_local_http
            cookies = _safe_result_clear(service)
            if request.get_data(cache=False):
                raise AuthTransactionError()
            query = _query(CasdoorResultQuery)
            result = service.restricted_result(
                handoff=query.handoff,
                browser_scope=_security_cookie(SCOPE_COOKIE_NAME),
                result_cookie=_security_cookie(result_cookie_name(query.handoff)),
                server_ip=_direct_ip(),
            )
            if type(result) is not _RestrictedResult or type(result.correlation_id) is not UUID:
                raise AuthTransactionError()
            correlation = result.correlation_id
            if type(result.status) is not int:
                raise AuthTransactionError()
            if result.status == 200:
                if type(result.payload) is not RestrictedResultPayload or result.error is not None:
                    raise AuthTransactionError()
                projection = result.payload.public_projection()
                if projection["correlation_id"] != str(result.correlation_id):
                    raise AuthTransactionError()
                directives = _result_clear(service, result.cookies, query.handoff)
                response = make_response(jsonify(dump_response(CasdoorRestrictedResultResponse, projection)), 200)
            else:
                if (
                    result.payload is not None
                    or type(result.error) is not PublicError
                    or result.status != result.error.status
                    or result.status not in (400, 403, 409, 429, 503)
                    or result.error.correlation_id != result.correlation_id
                ):
                    raise AuthTransactionError()
                directives = result.cookies
                if type(directives) is not tuple:
                    raise AuthTransactionError()
                if directives:
                    _result_clear(service, directives, query.handoff)
                response = _error_response(result.error)
            return _apply_cookies(response, directives)
        except (ValidationError, AuthTransactionError):
            public = _transport_error(CasdoorErrorCode.INVALID_TRANSACTION, correlation, RequestAction.CALLBACK)
            return _apply_cookies(_error_response(public), cookies)
        except Exception as error:
            return _apply_cookies(
                _error_response(_transport_error(error, correlation, RequestAction.CALLBACK)), cookies
            )
