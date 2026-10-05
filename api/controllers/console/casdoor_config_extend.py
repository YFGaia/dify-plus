"""Casdoor management transport; local static validation is not activation proof.

Current Account identity comes from the existing login/CSRF owner. Instance
authorization belongs to the deployment allowlist service, with no workspace-role
or IdP claim fallback. Errors never serialize request values, validation locations,
provider messages, encrypted envelopes or credentials.
"""

import re
from datetime import UTC
from typing import Any
from uuid import uuid4

from core.casdoor.auth_transactions import (
    COOKIE_PATH,
    SCOPE_COOKIE_NAME,
    AuthTransactionError,
    CookieDirective,
    diagnostic_initialization_cookie_name,
)
from core.casdoor.crypto import CryptoError
from core.casdoor.permissions import CasdoorManagementForbiddenError
from extensions.ext_application_services import application_services
from flask import Response, jsonify, make_response, request
from flask_restx import Resource
from libs.helper import dump_response
from libs.login import current_user, login_required
from libs.token import extract_access_token, extract_refresh_token, is_admin_api_key_request
from models.account import Account
from models.casdoor_extend import CasdoorValidationKind
from pydantic import ValidationError
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
    ConfigurationSnapshot,
    RevisionSnapshot,
)
from werkzeug.exceptions import HTTPException

from controllers.common.schema import query_params_from_model, register_response_schema_models, register_schema_models
from controllers.console import bp, console_ns
from controllers.console.casdoor_schemas_extend import (
    CasdoorClearSecretPayload,
    CasdoorConfigurationResponse,
    CasdoorDisablePayload,
    CasdoorDisableResponse,
    CasdoorManagementErrorResponse,
    CasdoorPermissionsResponse,
    CasdoorRevisionPayload,
    CasdoorRPLogoutDiagnosticResponse,
    CasdoorRPLogoutStatusQuery,
    CasdoorSaveConfigurationPayload,
    CasdoorStaticValidationResponse,
    CasdoorTestLoginResponse,
    CasdoorWorkspacesQuery,
    CasdoorWorkspacesResponse,
)

PREFIX = "/system-manage-extend/integration/casdoor"


@bp.after_app_request
def _casdoor_no_store(response: Response) -> Response:
    """Include routing errors, which have no matched Blueprint/Resource yet."""
    path = "/console/api" + PREFIX
    if request.path == path or request.path.startswith(path + "/"):
        response.headers["Cache-Control"] = "no-store"
    return response


register_schema_models(
    console_ns,
    CasdoorSaveConfigurationPayload,
    CasdoorClearSecretPayload,
    CasdoorDisablePayload,
    CasdoorRevisionPayload,
    CasdoorWorkspacesQuery,
    CasdoorRPLogoutStatusQuery,
)
register_response_schema_models(
    console_ns,
    CasdoorPermissionsResponse,
    CasdoorRPLogoutDiagnosticResponse,
    CasdoorConfigurationResponse,
    CasdoorDisableResponse,
    CasdoorStaticValidationResponse,
    CasdoorTestLoginResponse,
    CasdoorWorkspacesResponse,
    CasdoorManagementErrorResponse,
)


def _account() -> Account | None:
    account = current_user._get_current_object()
    return account if isinstance(account, Account) else None


def _configuration_response(snapshot: ConfigurationSnapshot) -> dict[str, Any]:
    def revision(value: RevisionSnapshot | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "revision_id": value.revision_id,
            "namespace_id": value.namespace_id,
            "configuration": value.configuration,
            "secret_configured": value.secret_configured,
            "validation": _validation_summaries(value),
            "diagnostic": application_services().casdoor_configuration.diagnostic_preview(value.revision_id),
        }

    return dump_response(
        CasdoorConfigurationResponse,
        {
            "enabled": snapshot.enabled,
            "etag": snapshot.etag,
            "active_revision_id": snapshot.active_revision_id,
            "draft_revision_id": snapshot.draft_revision_id,
            "active": revision(snapshot.active),
            "draft": revision(snapshot.draft),
        },
    )


def _validation_summaries(revision: RevisionSnapshot):
    if not revision.validation:
        return []
    rows = {row.kind: row for row in revision.validation}
    summaries = []
    for label, kinds in (
        (
            "validation",
            (CasdoorValidationKind.STATIC, CasdoorValidationKind.DEPLOYMENT, CasdoorValidationKind.PROTOCOL),
        ),
        ("diagnostic", (CasdoorValidationKind.DIAGNOSTIC,)),
    ):
        selected = [rows.get(kind) for kind in kinds]
        statuses = {row.status if row is not None else "not_run" for row in selected}
        if "pending" in statuses:
            statuses.discard("pending")
            statuses.add("not_run")
        status = next(item for item in ("failed", "unknown", "expired", "not_run", "passed") if item in statuses)
        checked = [row.checked_at for row in selected if row is not None and row.checked_at is not None]
        expires = [row.expires_at for row in selected if row is not None and row.expires_at is not None]
        summaries.append(
            {
                "revision_id": revision.revision_id,
                "kind": label,
                "status": status,
                "checked_at": max(checked).replace(tzinfo=UTC) if checked else None,
                "expires_at": min(expires).replace(tzinfo=UTC) if expires else None,
                "correlation_id": selected[0].correlation_id if label == "diagnostic" and selected[0] else None,
            }
        )
    return summaries


@console_ns.response(400, "Invalid management request", console_ns.models[CasdoorManagementErrorResponse.__name__])
@console_ns.response(403, "Instance management denied", console_ns.models[CasdoorManagementErrorResponse.__name__])
@console_ns.response(409, "Configuration conflict", console_ns.models[CasdoorManagementErrorResponse.__name__])
@console_ns.response(500, "Management request failed", console_ns.models[CasdoorManagementErrorResponse.__name__])
class CasdoorManagementResource(Resource):
    method_decorators = [login_required]

    def dispatch_request(self, *args: Any, **kwargs: Any) -> Response:
        try:
            response = make_response(super().dispatch_request(*args, **kwargs))
        except CasdoorManagementForbiddenError:
            response = self._error("casdoor_management_forbidden", 403)
        except CasdoorConfigurationError as error:
            status = 409 if error.code.value == "config_conflict" else 400
            safe_reason = error.reason if error.reason in {"deployment_proof_missing", "live_test_not_wired"} else None
            response = self._error(error.code.value, status, reason=safe_reason)
        except ValidationError as error:
            # Never return msg/loc either: custom messages and extra-field names
            # can contain user text even after Pydantic drops input/context.
            error.errors(include_input=False, include_context=False)
            response = self._error("invalid_transaction", 400)
        except CryptoError:
            response = self._error("config_conflict", 400)
        except AuthTransactionError:
            response = self._error("invalid_transaction", 400)
        except HTTPException as error:
            code = "unauthorized" if error.code == 401 else "invalid_transaction"
            response = self._error(code, error.code or 500)
        except Exception:
            # A final transport privacy boundary: never serialize or log raw
            # driver/validation exceptions, whose values may include credentials.
            response = self._error("internal_server_error", 500)
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _error(code: str, status: int, *, reason: str | None = None) -> Response:
        response = jsonify(
            dump_response(
                CasdoorManagementErrorResponse,
                {"code": code, "reason": reason, "correlation_id": uuid4()},
            )
        )
        response.status_code = status
        return response


@console_ns.route(PREFIX + "/permissions")
class CasdoorPermissionsApi(CasdoorManagementResource):
    @console_ns.response(200, "Current account permission", console_ns.models[CasdoorPermissionsResponse.__name__])
    def get(self):
        return dump_response(
            CasdoorPermissionsResponse,
            {"can_manage_casdoor": application_services().casdoor_configuration.can_manage(_account())},
        )


@console_ns.route(PREFIX)
class CasdoorConfigurationApi(CasdoorManagementResource):
    @console_ns.response(200, "Configuration", console_ns.models[CasdoorConfigurationResponse.__name__])
    def get(self):
        return _configuration_response(application_services().casdoor_configuration.get(_account()))

    @console_ns.expect(console_ns.models[CasdoorSaveConfigurationPayload.__name__])
    @console_ns.response(200, "New immutable draft", console_ns.models[CasdoorConfigurationResponse.__name__])
    def put(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        payload = CasdoorSaveConfigurationPayload.model_validate(request.get_json())
        return _configuration_response(
            service.save(account, configuration=payload.configuration, etag=payload.etag, secret=payload.secret)
        )


@console_ns.route(PREFIX + "/clear-secret")
class CasdoorClearSecretApi(CasdoorManagementResource):
    @console_ns.expect(console_ns.models[CasdoorClearSecretPayload.__name__])
    @console_ns.response(200, "Draft Secret cleared", console_ns.models[CasdoorConfigurationResponse.__name__])
    def post(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        payload = CasdoorClearSecretPayload.model_validate(request.get_json())
        return _configuration_response(
            service.clear_secret(account, etag=payload.etag, revision_id=payload.revision_id)
        )


@console_ns.route(PREFIX + "/validate")
class CasdoorStaticValidationApi(CasdoorManagementResource):
    @console_ns.expect(console_ns.models[CasdoorRevisionPayload.__name__])
    @console_ns.response(200, "Local static checks only", console_ns.models[CasdoorStaticValidationResponse.__name__])
    def post(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        payload = CasdoorRevisionPayload.model_validate(request.get_json())
        result = service.validate_static(account, etag=payload.etag, revision_id=payload.revision_id)
        return dump_response(
            CasdoorStaticValidationResponse,
            {
                "revision_id": result.revision_id,
                "etag": result.etag,
                "checked_at": result.checked_at,
                "certificate_summaries": [
                    {
                        "fingerprint": pin.fingerprint,
                        "kid": pin.kid,
                        "not_before": pin.not_before,
                        "accept_until": pin.accept_until,
                    }
                    for pin in result.certificates
                ],
            },
        )


@console_ns.route(PREFIX + "/disable")
class CasdoorDisableApi(CasdoorManagementResource):
    @console_ns.expect(console_ns.models[CasdoorDisablePayload.__name__])
    @console_ns.response(
        200, "Casdoor disabled and namespace fenced", console_ns.models[CasdoorDisableResponse.__name__]
    )
    def post(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        payload = CasdoorDisablePayload.model_validate(request.get_json())
        result = service.disable(account, etag=payload.etag)
        return dump_response(
            CasdoorDisableResponse,
            {
                "configuration": _configuration_response(result.configuration),
                "reconciliation_required": result.reconciliation_required,
            },
        )


@console_ns.route(PREFIX + "/activate")
class CasdoorActivateApi(CasdoorManagementResource):
    @console_ns.expect(console_ns.models[CasdoorRevisionPayload.__name__])
    @console_ns.response(200, "Activated configuration", console_ns.models[CasdoorConfigurationResponse.__name__])
    def post(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        payload = CasdoorRevisionPayload.model_validate(request.get_json())
        return _configuration_response(service.activate(account, etag=payload.etag, revision_id=payload.revision_id))


@console_ns.route(PREFIX + "/test-login")
class CasdoorTestLoginApi(CasdoorManagementResource):
    def _prepare(self, service, account, **kwargs):
        return service.prepare(account, **kwargs)

    @console_ns.expect(console_ns.models[CasdoorRevisionPayload.__name__])
    @console_ns.response(
        200,
        "Draft diagnostic browser navigation or deployment block",
        console_ns.models[CasdoorTestLoginResponse.__name__],
    )
    def post(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        payload = CasdoorRevisionPayload.model_validate(request.get_json())
        # Refresh extraction remains with the existing Console Cookie owner.
        if is_admin_api_key_request(request) or not extract_access_token(request):
            raise AuthTransactionError("source_session_invalid")
        result = self._prepare(
            application_services().casdoor_diagnostic,
            account,
            refresh_token=extract_refresh_token(request),
            etag=payload.etag,
            revision_id=payload.revision_id,
            browser_scope=_single_scope(),
            server_ip=request.remote_addr,
        )
        if result.navigation is None:
            return dump_response(CasdoorTestLoginResponse, {"status": "blocked", "reason": result.reason})
        navigation = result.navigation
        response = make_response(
            jsonify(
                dump_response(
                    CasdoorTestLoginResponse, {"status": "started", "handoff": {"handoff_path": navigation.redirect}}
                )
            ),
            200,
        )
        policy = application_services().casdoor_diagnostic._policy()
        if type(navigation.cookies) is not tuple or len(navigation.cookies) != 1:
            raise AuthTransactionError()
        cookie = navigation.cookies[0]
        prefix = COOKIE_PATH + "/diagnostic/"
        if not navigation.redirect.startswith(prefix) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", navigation.redirect[len(prefix) :]
        ):
            raise AuthTransactionError()
        handle = navigation.redirect[len(prefix) :]
        if (
            type(cookie) is not CookieDirective
            or cookie.name != diagnostic_initialization_cookie_name(handle)
            or cookie.path != navigation.redirect
            or cookie.max_age != 60
            or type(cookie.value) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie.value)
            or cookie.secure != policy.secure
            or cookie.domain is not None
            or not cookie.httponly
            or cookie.samesite != "Lax"
        ):
            raise AuthTransactionError()
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


@console_ns.route(PREFIX + "/test-rp-logout")
class CasdoorTestRPLogoutApi(CasdoorTestLoginApi):
    def _prepare(self, service, account, **kwargs):
        return service.prepare_rp_logout(account, **kwargs)


@console_ns.route(PREFIX + "/rp-logout-status")
class CasdoorRPLogoutStatusApi(CasdoorManagementResource):
    @console_ns.doc(params=query_params_from_model(CasdoorRPLogoutStatusQuery))
    @console_ns.response(
        200, "Actual optional RP protocol observation", console_ns.models[CasdoorRPLogoutDiagnosticResponse.__name__]
    )
    def get(self):
        account = _account()
        application_services().casdoor_configuration.require_management(account)
        if is_admin_api_key_request(request) or not extract_access_token(request):
            raise AuthTransactionError("source_session_invalid")
        data = {}
        for key, values in request.args.lists():
            if key not in CasdoorRPLogoutStatusQuery.model_fields or len(values) != 1:
                raise AuthTransactionError()
            data[key] = values[0]
        query = CasdoorRPLogoutStatusQuery.model_validate(data)
        result = application_services().casdoor_diagnostic.rp_logout_status(
            account,
            refresh_token=extract_refresh_token(request),
            revision_id=query.revision_id,
            server_ip=request.remote_addr,
        )
        return dump_response(CasdoorRPLogoutDiagnosticResponse, result)


def _single_scope():
    values = request.cookies.getlist(SCOPE_COOKIE_NAME)
    if len(values) > 1:
        raise AuthTransactionError()
    return values[0] if values else None


@console_ns.route(PREFIX + "/workspaces")
class CasdoorWorkspacesApi(CasdoorManagementResource):
    @console_ns.doc(params=query_params_from_model(CasdoorWorkspacesQuery))
    @console_ns.response(200, "Global workspace selection", console_ns.models[CasdoorWorkspacesResponse.__name__])
    def get(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        query = CasdoorWorkspacesQuery.model_validate(request.args.to_dict(flat=True))
        return dump_response(CasdoorWorkspacesResponse, service.workspaces(account, page=query.page, limit=query.limit))
