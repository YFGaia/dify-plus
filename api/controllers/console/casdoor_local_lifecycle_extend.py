"""Closed instance-manager LOCAL maintenance; target IDs are DB routing hints."""

from flask import request

from controllers.common.schema import query_params_from_model, register_response_schema_models, register_schema_models
from controllers.console import console_ns
from controllers.console.auth.casdoor_extend import _diagnostic_account, _direct_ip
from controllers.console.casdoor_config_extend import _configuration_response
from controllers.console.casdoor_schemas_extend import (
    CasdoorConfigurationResponse,
    CasdoorLocalMembershipInspectionResponse,
    CasdoorLocalMembershipListQuery,
    CasdoorLocalMembershipListResponse,
    CasdoorLocalMembershipMutationPayload,
    CasdoorLocalMembershipMutationResponse,
    CasdoorLocalMembershipReviewPayload,
    CasdoorLocalMembershipReviewResponse,
    CasdoorLocalMembershipTarget,
    CasdoorNamespaceResetMutationPayload,
    CasdoorNamespaceResetReviewResponse,
    CasdoorResetNamespacePayload,
)
from controllers.console.workspace.casdoor_identity_extend import CasdoorIdentityActionResource
from core.casdoor.auth_transactions import AuthTransactionError
from extensions.ext_application_services import application_services
from libs.helper import dump_response
from libs.token import extract_refresh_token

PREFIX = "/system-manage-extend/integration/casdoor/local-membership"
register_schema_models(
    console_ns,
    CasdoorLocalMembershipTarget,
    CasdoorLocalMembershipReviewPayload,
    CasdoorLocalMembershipMutationPayload,
    CasdoorLocalMembershipListQuery,
)
register_response_schema_models(
    console_ns,
    CasdoorLocalMembershipInspectionResponse,
    CasdoorLocalMembershipReviewResponse,
    CasdoorLocalMembershipMutationResponse,
    CasdoorLocalMembershipListResponse,
)


class LocalLifecycleResource(CasdoorIdentityActionResource):
    @staticmethod
    def arguments():
        return {"refresh_token": extract_refresh_token(request), "server_ip": _direct_ip()}

    @staticmethod
    def query(model):
        if request.get_data(cache=False) or any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise AuthTransactionError()
        values = dict(request.args)
        if "limit" in values:
            text = values["limit"]
            if not text.isascii() or not text.isdecimal():
                raise AuthTransactionError()
            values["limit"] = int(text)
        return model.model_validate(values)


@console_ns.route(PREFIX + "/targets")
class CasdoorLocalMembershipListApi(LocalLifecycleResource):
    @console_ns.doc(params=query_params_from_model(CasdoorLocalMembershipListQuery))
    @console_ns.response(
        200, "Manager target navigation", console_ns.models[CasdoorLocalMembershipListResponse.__name__]
    )
    def get(self):
        query = self.query(CasdoorLocalMembershipListQuery)
        result = application_services().casdoor_local_lifecycle.list_memberships(
            _diagnostic_account(), **query.model_dump(), **self.arguments()
        )
        return dump_response(CasdoorLocalMembershipListResponse, result)


@console_ns.route(PREFIX)
class CasdoorLocalMembershipInspectApi(LocalLifecycleResource):
    @console_ns.doc(params=query_params_from_model(CasdoorLocalMembershipTarget))
    @console_ns.response(
        200, "Exact local membership inspection", console_ns.models[CasdoorLocalMembershipInspectionResponse.__name__]
    )
    def get(self):
        query = self.query(CasdoorLocalMembershipTarget)
        result = application_services().casdoor_local_lifecycle.inspect_membership(
            _diagnostic_account(), **query.model_dump(), **self.arguments()
        )
        return dump_response(CasdoorLocalMembershipInspectionResponse, result)


@console_ns.route(PREFIX + "/review")
class CasdoorLocalMembershipReviewApi(LocalLifecycleResource):
    @console_ns.expect(console_ns.models[CasdoorLocalMembershipReviewPayload.__name__])
    @console_ns.response(
        200, "One-use source-bound review", console_ns.models[CasdoorLocalMembershipReviewResponse.__name__]
    )
    def post(self):
        payload = self._input(CasdoorLocalMembershipReviewPayload)
        result = application_services().casdoor_local_lifecycle.review_membership(
            _diagnostic_account(), **payload.model_dump(), **self.arguments()
        )
        return dump_response(CasdoorLocalMembershipReviewResponse, result)


@console_ns.route(PREFIX + "/release")
class CasdoorLocalMembershipReleaseApi(LocalLifecycleResource):
    @console_ns.expect(console_ns.models[CasdoorLocalMembershipMutationPayload.__name__])
    @console_ns.response(
        200,
        "Permissions retained; management released",
        console_ns.models[CasdoorLocalMembershipMutationResponse.__name__],
    )
    def post(self):
        payload = self._input(CasdoorLocalMembershipMutationPayload)
        result = application_services().casdoor_local_lifecycle.release_membership(
            _diagnostic_account(), **payload.model_dump(), **self.arguments()
        )
        return dump_response(CasdoorLocalMembershipMutationResponse, result)


@console_ns.route(PREFIX + "/adopt")
class CasdoorLocalMembershipAdoptApi(LocalLifecycleResource):
    @console_ns.expect(console_ns.models[CasdoorLocalMembershipMutationPayload.__name__])
    @console_ns.response(
        200,
        "Reviewed current target applied and managed",
        console_ns.models[CasdoorLocalMembershipMutationResponse.__name__],
    )
    def post(self):
        payload = self._input(CasdoorLocalMembershipMutationPayload)
        result = application_services().casdoor_local_lifecycle.adopt_membership(
            _diagnostic_account(), **payload.model_dump(), **self.arguments()
        )
        return dump_response(CasdoorLocalMembershipMutationResponse, result)


register_schema_models(console_ns, CasdoorResetNamespacePayload, CasdoorNamespaceResetMutationPayload)
register_response_schema_models(console_ns, CasdoorNamespaceResetReviewResponse)


@console_ns.route("/system-manage-extend/integration/casdoor/reset-namespace/review")
class CasdoorNamespaceResetReviewApi(LocalLifecycleResource):
    @console_ns.expect(console_ns.models[CasdoorResetNamespacePayload.__name__])
    @console_ns.response(
        200,
        "One-use clean LOCAL review; credential format only",
        console_ns.models[CasdoorNamespaceResetReviewResponse.__name__],
    )
    def post(self):
        payload = self._input(CasdoorResetNamespacePayload)
        result = application_services().casdoor_local_lifecycle.review_namespace_reset(
            _diagnostic_account(), **payload.model_dump(), **self.arguments()
        )
        return dump_response(CasdoorNamespaceResetReviewResponse, result)


@console_ns.route("/system-manage-extend/integration/casdoor/reset-namespace")
class CasdoorNamespaceResetApi(LocalLifecycleResource):
    @console_ns.expect(console_ns.models[CasdoorNamespaceResetMutationPayload.__name__])
    @console_ns.response(
        200, "Archived old namespace; fresh disabled draft", console_ns.models[CasdoorConfigurationResponse.__name__]
    )
    def post(self):
        payload = self._input(CasdoorNamespaceResetMutationPayload)
        result = application_services().casdoor_local_lifecycle.reset_namespace(
            _diagnostic_account(), **payload.model_dump(), **self.arguments()
        )
        return _configuration_response(result)
