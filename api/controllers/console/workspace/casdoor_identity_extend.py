"""Private, account-context-only Casdoor observations with closed wire schemas."""

import re
from typing import Literal
from uuid import UUID, uuid4

from core.casdoor.auth_transactions import (
    SCOPE_COOKIE_NAME,
    AuthMode,
    AuthTransactionError,
    source_initialization_cookie_name,
)
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.request_safety import (
    LocalReferences,
    RequestAction,
    SafetyEvent,
    SafetyResultCode,
    format_public_error,
    record_safety_event,
)
from extensions.ext_application_services import application_services
from fields.base import ResponseModel
from flask import Response, jsonify, make_response, request
from flask_restx import Resource
from libs.helper import dump_response
from libs.login import login_required
from libs.token import extract_refresh_token
from machinery.context import RequestContext
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from repositories.casdoor_self_identity_repository_extend import CasdoorSelfReadConflict
from services.casdoor_identity_action_service_extend import PROOF_COOKIE_NAME, PROOF_SCOPE_COOKIE_NAME

from controllers.common.schema import (
    query_params_from_model,
    register_response_schema_models,
    register_schema_models,
)
from controllers.console import bp, console_ns
from controllers.console.auth.casdoor_extend import (
    _apply_cookies,
    _diagnostic_account,
    _direct_ip,
    _exact_directives,
    _security_cookie,
)
from controllers.console.casdoor_schemas_extend import (
    CasdoorIdentityActionsResponse,
    CasdoorIdentityUnlinkedResponse,
    CasdoorLinkIdentityPayload,
    CasdoorNavigationResponse,
    CasdoorResultResponse,
    CasdoorRevisionPayload,
    CasdoorUnlinkIdentityPayload,
)
from controllers.console.flask_admission import console_account_admission

PATH = "/console/api/account/casdoor-identity"


class CasdoorSelfIdentityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    identity_after: UUID | None = None
    membership_after: UUID | None = None
    current_membership_after: UUID | None = None
    limit: StrictInt = Field(default=20, ge=1, le=50)


class ClosedResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CasdoorSelfNameResponse(ClosedResponse):
    last_status: Literal["applied", "unchanged", "skipped", "local_override", "disabled"] | None
    last_reason: (
        Literal[
            "disabled",
            "empty_remote_name",
            "filled_empty",
            "created_baseline",
            "managed_update",
            "same_name",
            "local_name_present",
            "unowned_local_name",
            "local_override",
            "stale_profile_attempt",
            "ambiguous_profile_attempt",
        ]
        | None
    )
    last_sync_at: str | None
    recorded_generation: int | None
    baseline_generation: int | None
    current_local_differs_from_last_applied: bool | None


class CasdoorSelfEmailResponse(ClosedResponse):
    current_differs: bool | None
    verified: bool | None
    last_status: Literal["same", "different", "invalid", "unavailable"] | None
    last_differs: bool | None


class CasdoorSelfIdentityResponse(ClosedResponse):
    id: str | None
    namespace_id: str | None
    organization: str | None = Field(max_length=255)
    masked_identifier: Literal["********"]
    activity: Literal["active", "inactive", "unknown"]
    lifecycle: Literal["active", "fencing", "archived", "unknown"]
    sync_generation: int | None
    profile_consistency: Literal["consistent", "historical", "unknown"]
    name: CasdoorSelfNameResponse
    email: CasdoorSelfEmailResponse
    avatar_status: Literal[
        "off",
        "no_record",
        "pending",
        "source_expired",
        "in_flight",
        "unknown",
        "failed_before_storage",
        "failed_storage_cleaned",
        "local_attachment_recorded",
        "local_override",
        "historical",
    ]
    avatar_recorded_at: str | None = Field(
        max_length=40,
        description=(
            "UTC operation record time. DB attachment readback does not prove physical storage synchronization."
        ),
    )
    avatar_last_reason: (
        Literal[
            "fetch_rejected",
            "fetch_failed",
            "fetch_cancelled",
            "fetch_unknown",
            "image_rejected",
            "storage_unknown",
            "attachment_lost",
            "lease_expired",
            "commit_unknown",
        ]
        | None
    )
    avatar_recorded_generation: int | None = Field(ge=0, le=2**63 - 1)
    avatar_consistency: Literal["current", "historical", "unknown"]
    avatar_current_local_differs_from_last_applied: bool | None


class CasdoorSelfCurrentMembershipResponse(ClosedResponse):
    id: str | None
    workspace_id: str | None
    join_presence: Literal["present", "absent", "unknown"]
    local_role: Literal["owner", "admin", "editor", "normal", "dataset_operator"] | None
    state: Literal["unmanaged", "history_present", "unknown"]
    remote_actual_state: Literal["unknown"]


class CasdoorSelfMembershipResponse(ClosedResponse):
    id: str | None
    workspace_id: str | None
    namespace_id: str | None
    identity_id: str | None
    recorded_ownership: Literal["managed", "released", "local_override"] | None
    recorded_source: Literal["mapping", "fallback", "adopt"] | None
    recorded_finalization: Literal["pending", "finalized", "manual_recovery"] | None
    tombstone: bool | None
    join_presence: Literal["present", "absent", "unknown"]
    local_role: Literal["owner", "admin", "editor", "normal", "dataset_operator"] | None
    state: Literal[
        "unknown",
        "tombstone",
        "local_override",
        "unmanaged",
        "controlled_withdrawal",
        "absent_unknown",
        "historical",
        "recorded_managed",
    ]
    consistency: Literal["unknown", "stale", "historical"]
    remote_actual_state: Literal["unknown"]


class CasdoorSelfActionsResponse(ClosedResponse):
    link: Literal[False]
    unlink: Literal[False]
    reauthenticate: Literal[False]
    logout: Literal[False]
    adopt: Literal[False]
    release: Literal[False]
    retry: Literal[False]


class CasdoorSelfIdentityStatusResponse(ClosedResponse):
    binding: Literal["linked", "unlinked"]
    identities: list[CasdoorSelfIdentityResponse]
    memberships: list[CasdoorSelfMembershipResponse]
    current_memberships: list[CasdoorSelfCurrentMembershipResponse]
    identity_has_more: bool
    identity_next: str | None
    membership_has_more: bool
    membership_next: str | None
    current_membership_has_more: bool
    current_membership_next: str | None
    actions: CasdoorSelfActionsResponse


class CasdoorSelfErrorResponse(ClosedResponse):
    code: Literal[
        "casdoor_self_invalid_query",
        "casdoor_self_read_conflict",
        "casdoor_self_unavailable",
        "casdoor_self_request_rejected",
    ]


register_response_schema_models(console_ns, CasdoorSelfIdentityStatusResponse, CasdoorSelfErrorResponse)


@bp.after_app_request
def _self_private_response(response: Response) -> Response:
    """Applies to native auth/routing errors without changing their status codes."""
    if request.path == PATH or request.path.startswith(PATH + "/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.vary.add("Cookie")
        response.vary.add("Authorization")
        if response.status_code >= 400 and not getattr(response, "_casdoor_action_error", False):
            # Replace native descriptions/input, including routing failures, with
            # a fixed public code. Do not inspect or read an error response body.
            code = getattr(response, "_casdoor_self_code", "casdoor_self_request_rejected")
            response.set_data('{"code":"' + code + '"}')
            response.content_type = "application/json"
    return response


def _error(code, status):
    response = Response(status=status, mimetype="application/json")
    response._casdoor_self_code = code
    return response


def _query():
    values = {}
    for key, items in request.args.lists():
        if key not in CasdoorSelfIdentityQuery.model_fields or len(items) != 1:
            raise ValueError()
        raw = items[0]
        if key == "limit":
            if not re.fullmatch(r"[1-9][0-9]?", raw):
                raise ValueError()
            values[key] = int(raw)
        else:
            if len(raw) != 36 or str(UUID(raw)) != raw:
                raise ValueError()
            values[key] = UUID(raw)
    return CasdoorSelfIdentityQuery.model_validate(values)


@console_ns.route("/account/casdoor-identity", provide_automatic_options=False)
class CasdoorSelfIdentityApi(Resource):
    @console_ns.doc(params=query_params_from_model(CasdoorSelfIdentityQuery))
    @console_ns.response(
        200, "Private local observations", console_ns.models[CasdoorSelfIdentityStatusResponse.__name__]
    )
    @console_ns.response(400, "Invalid query", console_ns.models[CasdoorSelfErrorResponse.__name__])
    @console_ns.response(409, "Read conflict", console_ns.models[CasdoorSelfErrorResponse.__name__])
    @console_ns.response(503, "Read unavailable", console_ns.models[CasdoorSelfErrorResponse.__name__])
    @console_account_admission(require_valid_enterprise_license=True)
    def get(self, request_context: RequestContext):
        try:
            query = _query()
        except (ValueError, TypeError):
            return _error("casdoor_self_invalid_query", 400)
        try:
            result = application_services().casdoor_self_identity.get(request_context, **query.model_dump())
            return dump_response(CasdoorSelfIdentityStatusResponse, result)
        except CasdoorSelfReadConflict:
            return _error("casdoor_self_read_conflict", 409)
        except Exception:
            return _error("casdoor_self_unavailable", 503)

    @console_ns.doc(False)
    def head(self):
        response = _error("casdoor_self_request_rejected", 405)
        response.headers["Allow"] = "GET, OPTIONS"
        return response

    @console_ns.doc(False)
    def options(self):
        return Response(status=204, headers={"Allow": "GET, OPTIONS"})


register_schema_models(console_ns, CasdoorLinkIdentityPayload, CasdoorUnlinkIdentityPayload, CasdoorRevisionPayload)
register_response_schema_models(
    console_ns,
    CasdoorIdentityActionsResponse,
    CasdoorIdentityUnlinkedResponse,
    CasdoorNavigationResponse,
    CasdoorResultResponse,
)


class CasdoorIdentityActionResource(Resource):
    method_decorators = [login_required]
    safety_action = RequestAction.START

    def dispatch_request(self, *args, **kwargs):
        correlation = uuid4()
        references = LocalReferences()
        try:
            response = make_response(super().dispatch_request(*args, **kwargs))
            references = getattr(self, "_source_references", references)
            code = SafetyResultCode.SUCCESS if response.status_code < 400 else CasdoorErrorCode.INVALID_TRANSACTION
        except Exception as error:
            public = format_public_error(
                CasdoorErrorCode.INVALID_TRANSACTION
                if isinstance(error, (AuthTransactionError, ValueError))
                else error,
                correlation_id=correlation,
            )
            response = jsonify(dump_response(CasdoorResultResponse, public.payload()))
            response.status_code = public.status
            response._casdoor_action_error = True
            if public.retry_after_seconds is not None:
                response.headers["Retry-After"] = str(public.retry_after_seconds)
            code = public.code
        try:
            record_safety_event(SafetyEvent(self.safety_action, code, correlation, references=references))
        except Exception:
            # Logging cannot change the authorization/transport result; never
            # log the original exception, request values or provider data.
            response.headers["Cache-Control"] = "no-store"
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _input(model):
        if request.args:
            raise AuthTransactionError()
        return model.model_validate(request.get_json())

    @staticmethod
    def _arguments(*, proof=False):
        return {
            "refresh_token": extract_refresh_token(request),
            "browser_scope": _security_cookie(PROOF_SCOPE_COOKIE_NAME if proof else SCOPE_COOKIE_NAME),
            "server_ip": _direct_ip(),
        }

    def _prepare(self, mode, **extra):
        service = application_services().casdoor_identity_action
        account = _diagnostic_account()
        result = service.prepare_action(account, mode=mode, **self._arguments(), **extra)
        self._source_references = LocalReferences(account_id=UUID(account.id), actor_account_id=UUID(account.id))
        response = make_response(dump_response(CasdoorNavigationResponse, {"handoff_path": result.path}))
        # The account POST does not overwrite the auth-path shared scope.
        prefix = "/console/api/auth/casdoor/identity/"
        if not result.path.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", result.path[len(prefix) :]):
            raise AuthTransactionError()
        handle = result.path[len(prefix) :]
        _exact_directives(
            service, result.cookies, ((source_initialization_cookie_name(handle), None, 60, result.path),)
        )
        return _apply_cookies(response, result.cookies)


@console_ns.route("/account/casdoor-identity/actions")
class CasdoorIdentityActionsApi(CasdoorIdentityActionResource):
    @console_ns.doc(False)
    def head(self):
        return Response(status=405, headers={"Allow": "GET, OPTIONS"})

    @console_ns.response(
        200, "Current source-session identity actions", console_ns.models[CasdoorIdentityActionsResponse.__name__]
    )
    def get(self):
        if request.args or request.get_data(cache=False):
            raise AuthTransactionError()
        result = application_services().casdoor_identity_action.action_status(
            _diagnostic_account(), proof=_security_cookie(PROOF_COOKIE_NAME), **self._arguments(proof=True)
        )
        return dump_response(CasdoorIdentityActionsResponse, result)


@console_ns.route("/account/casdoor-identity/link")
class CasdoorIdentityLinkApi(CasdoorIdentityActionResource):
    safety_action = RequestAction.LINK

    @console_ns.expect(console_ns.models[CasdoorLinkIdentityPayload.__name__])
    @console_ns.response(200, "Local identity handoff", console_ns.models[CasdoorNavigationResponse.__name__])
    def post(self):
        self._input(CasdoorLinkIdentityPayload)
        return self._prepare(AuthMode.LINK)


@console_ns.route("/account/casdoor-identity/reauthenticate")
class CasdoorIdentityReauthenticateApi(CasdoorIdentityActionResource):
    safety_action = RequestAction.REAUTH

    @console_ns.expect(console_ns.models[CasdoorUnlinkIdentityPayload.__name__])
    @console_ns.response(200, "Local reauthentication handoff", console_ns.models[CasdoorNavigationResponse.__name__])
    def post(self):
        self._input(CasdoorUnlinkIdentityPayload)
        return self._prepare(AuthMode.REAUTH_UNLINK)


@console_ns.route("/account/casdoor-identity/unlink")
class CasdoorIdentityUnlinkApi(CasdoorIdentityActionResource):
    safety_action = RequestAction.REAUTH

    @console_ns.expect(console_ns.models[CasdoorUnlinkIdentityPayload.__name__])
    @console_ns.response(
        200,
        "Identity unlinked; original session preserved",
        console_ns.models[CasdoorIdentityUnlinkedResponse.__name__],
    )
    def post(self):
        self._input(CasdoorUnlinkIdentityPayload)
        service = application_services().casdoor_identity_action
        clear = service.unlink_action(
            _diagnostic_account(), proof=_security_cookie(PROOF_COOKIE_NAME), **self._arguments(proof=True)
        )
        _exact_directives(service, clear, ((PROOF_COOKIE_NAME, "", 0, PATH), (PROOF_SCOPE_COOKIE_NAME, "", 0, PATH)))
        return _apply_cookies(
            make_response(dump_response(CasdoorIdentityUnlinkedResponse, {"status": "unlinked"})), clear
        )


@console_ns.route("/system-manage-extend/integration/casdoor/test-reauth")
class CasdoorReauthenticationDiagnosticApi(CasdoorIdentityActionResource):
    safety_action = RequestAction.DIAGNOSTIC

    @console_ns.expect(console_ns.models[CasdoorRevisionPayload.__name__])
    @console_ns.response(
        200, "Local reviewed reauthentication diagnostic handoff", console_ns.models[CasdoorNavigationResponse.__name__]
    )
    def post(self):
        payload = self._input(CasdoorRevisionPayload)
        return self._prepare(AuthMode.DIAGNOSTIC, **payload.model_dump())
