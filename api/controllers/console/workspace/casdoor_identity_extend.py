"""Private, account-context-only Casdoor observations with closed wire schemas."""

import re
from typing import Literal
from uuid import UUID

from flask import Response, request
from flask_restx import Resource
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from controllers.common.schema import (
    query_params_from_model,
    register_response_schema_models,
)
from controllers.console import bp, console_ns
from controllers.console.flask_admission import console_account_admission
from extensions.ext_application_services import application_services
from fields.base import ResponseModel
from libs.helper import dump_response
from machinery.context import RequestContext
from repositories.casdoor_self_identity_repository_extend import CasdoorSelfReadConflict

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
    avatar_status: Literal["unknown"]


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
        response.vary.add("Cookie")
        response.vary.add("Authorization")
        if response.status_code >= 400:
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
