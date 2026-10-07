"""Anonymous Casdoor display metadata; enabled does not establish auth readiness."""

from flask import Response, jsonify, make_response, request
from flask_restx import Resource

from controllers.common.schema import query_params_from_model, register_response_schema_models
from controllers.console import console_ns
from controllers.console.casdoor_schemas_extend import CasdoorDisplayResponse, CasdoorPayload, CasdoorResultResponse
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.request_safety import format_public_error
from extensions.ext_application_services import application_services
from libs.helper import dump_response
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError


class CasdoorDisplayQuery(CasdoorPayload):
    """Display accepts no query parameters."""


register_response_schema_models(console_ns, CasdoorDisplayResponse, CasdoorResultResponse)


def _error(error: CasdoorErrorCode | Exception, *, status: int | None = None) -> Response:
    public = format_public_error(error.code if type(error) is CasdoorConfigurationError else error)
    return make_response(jsonify(dump_response(CasdoorResultResponse, public.payload())), status or public.status)


@console_ns.route("/auth/casdoor/display", provide_automatic_options=False)
@console_ns.doc(security=[])
class CasdoorDisplayApi(Resource):
    @console_ns.doc(params=query_params_from_model(CasdoorDisplayQuery))
    @console_ns.response(200, "Public display metadata", console_ns.models[CasdoorDisplayResponse.__name__])
    @console_ns.response(400, "Invalid display request", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(409, "Configuration conflict", console_ns.models[CasdoorResultResponse.__name__])
    @console_ns.response(503, "Display unavailable", console_ns.models[CasdoorResultResponse.__name__])
    def get(self):
        try:
            if any(request.args.lists()):
                return _error(CasdoorErrorCode.INVALID_TRANSACTION)
            CasdoorDisplayQuery.model_validate({})
            # No body is valid. Bound reads even for chunked requests without a
            # Content-Length and reject parser/stream errors without logging input.
            request.max_content_length = 1
            if request.get_data(cache=False, parse_form_data=False):
                return _error(CasdoorErrorCode.INVALID_TRANSACTION)
        except Exception:
            return _error(CasdoorErrorCode.INVALID_TRANSACTION)
        try:
            metadata = application_services().casdoor_configuration.display()
            return dump_response(
                CasdoorDisplayResponse,
                {
                    "enabled": metadata.enabled,
                    "button_text": metadata.button_text,
                    "start_path": "/console/api/auth/casdoor/login",
                },
            )
        except Exception as error:
            return _error(error)

    @console_ns.response(405, "Method not allowed", console_ns.models[CasdoorResultResponse.__name__])
    def head(self):
        return _error(CasdoorErrorCode.INVALID_TRANSACTION, status=405)

    @console_ns.doc(False)
    @console_ns.response(204, "No content")
    def options(self):
        return "", 204
