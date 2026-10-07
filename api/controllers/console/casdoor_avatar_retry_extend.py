"""Bounded manager-owned avatar retry routing; target IDs are not capabilities."""

from typing import Literal
from uuid import UUID

from core.casdoor.auth_transactions import AuthTransactionError
from extensions.ext_application_services import application_services
from flask import request
from libs.helper import dump_response
from libs.token import extract_refresh_token
from pydantic import Field, StrictBool, StrictInt

from controllers.common.schema import (
    query_params_from_model,
    register_response_schema_models,
    register_schema_models,
)
from controllers.console import console_ns
from controllers.console.auth.casdoor_extend import _diagnostic_account, _direct_ip
from controllers.console.casdoor_config_extend import CasdoorManagementResource
from controllers.console.casdoor_schemas_extend import (
    CasdoorPayload,
    CasdoorResponse,
    CasdoorRetrySyncPayload,
)
from controllers.console.workspace.casdoor_identity_extend import (
    CasdoorIdentityActionResource,
)


class CasdoorAvatarRetryTargetsQuery(CasdoorPayload):
    after: UUID | None = None
    limit: StrictInt = Field(default=20, ge=1, le=100)


class CasdoorAvatarRetryTarget(CasdoorResponse):
    account_id: UUID
    identity_id: UUID
    intent_id: UUID
    reason: Literal["fetch_rejected", "fetch_failed", "image_rejected"]
    retry_eligible: Literal[True]


class CasdoorAvatarRetryTargetsResponse(CasdoorResponse):
    targets: list[CasdoorAvatarRetryTarget]
    next_after: UUID | None
    has_more: StrictBool


class CasdoorAvatarRetryResponse(CasdoorResponse):
    status: Literal["pending"]
    intent_id: UUID


register_schema_models(
    console_ns, CasdoorAvatarRetryTargetsQuery, CasdoorRetrySyncPayload
)
register_response_schema_models(
    console_ns, CasdoorAvatarRetryTargetsResponse, CasdoorAvatarRetryResponse
)


def _service():
    service = application_services().casdoor_avatar_retry
    if service is None:
        raise AuthTransactionError("retry_unavailable")
    return service


def _source_arguments():
    return {"refresh_token": extract_refresh_token(request), "server_ip": _direct_ip()}


class AvatarRetryResource(CasdoorManagementResource):
    _input = staticmethod(CasdoorIdentityActionResource._input)


@console_ns.route("/system-manage-extend/integration/casdoor/sync/retry-targets")
class CasdoorAvatarRetryTargetsApi(AvatarRetryResource):
    @console_ns.doc(params=query_params_from_model(CasdoorAvatarRetryTargetsQuery))
    @console_ns.response(
        200,
        "Manager retry targets",
        console_ns.models[CasdoorAvatarRetryTargetsResponse.__name__],
    )
    def get(self):
        if request.get_data(cache=False) or any(
            len(request.args.getlist(key)) != 1 for key in request.args
        ):
            raise AuthTransactionError()
        values = dict(request.args)
        if "limit" in values:
            text = values["limit"]
            if not text.isascii() or not text.isdecimal():
                raise AuthTransactionError()
            values["limit"] = int(text)
        query = CasdoorAvatarRetryTargetsQuery.model_validate(values)
        result = _service().list_retry_targets(
            _diagnostic_account(), **query.model_dump(), **_source_arguments()
        )
        return dump_response(CasdoorAvatarRetryTargetsResponse, result)


@console_ns.route("/system-manage-extend/integration/casdoor/sync/retry")
class CasdoorAvatarRetryApi(AvatarRetryResource):
    @console_ns.expect(console_ns.models[CasdoorRetrySyncPayload.__name__])
    @console_ns.response(
        200,
        "Retry pending, no execution authority",
        console_ns.models[CasdoorAvatarRetryResponse.__name__],
    )
    def post(self):
        payload = self._input(CasdoorRetrySyncPayload)
        result = _service().retry_avatar(
            _diagnostic_account(), **payload.model_dump(), **_source_arguments()
        )
        return dump_response(CasdoorAvatarRetryResponse, result)
