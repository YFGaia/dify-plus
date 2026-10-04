"""Casdoor management transport; local static validation is not activation proof.

Current Account identity comes from the existing login/CSRF owner. Instance
authorization belongs to the deployment allowlist service, with no workspace-role
or IdP claim fallback. Errors never serialize request values, validation locations,
provider messages, encrypted envelopes or credentials.
"""

from typing import Any
from uuid import uuid4

from flask import Response, jsonify, make_response, request
from flask_restx import Resource
from pydantic import ValidationError
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
    CasdoorSaveConfigurationPayload,
    CasdoorStaticValidationResponse,
    CasdoorTestLoginResponse,
    CasdoorWorkspacesQuery,
    CasdoorWorkspacesResponse,
)
from core.casdoor.crypto import CryptoError
from core.casdoor.permissions import CasdoorManagementForbiddenError
from extensions.ext_application_services import application_services
from libs.helper import dump_response
from libs.login import current_user, login_required
from models.account import Account
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
    ConfigurationSnapshot,
    RevisionSnapshot,
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
)
register_response_schema_models(
    console_ns,
    CasdoorPermissionsResponse,
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
            # I24-B owns aggregation of all four proof kinds. Local static checks
            # cannot stand in for protocol/deployment/diagnostic verification.
            "validation": (),
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
    @console_ns.expect(console_ns.models[CasdoorRevisionPayload.__name__])
    @console_ns.response(
        200,
        "Draft login test blocked by missing real proof",
        console_ns.models[CasdoorTestLoginResponse.__name__],
    )
    def post(self):
        service = application_services().casdoor_configuration
        account = _account()
        service.require_management(account)
        payload = CasdoorRevisionPayload.model_validate(request.get_json())
        reason = service.test_login(account, etag=payload.etag, revision_id=payload.revision_id)
        return dump_response(CasdoorTestLoginResponse, {"status": "blocked", "reason": reason})


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
