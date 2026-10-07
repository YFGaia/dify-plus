"""Authenticated, uncached global management admission for Console entry points."""

from uuid import UUID

from flask import make_response
from flask_restx import Resource
from pydantic import ConfigDict, StrictBool

from controllers.common.schema import register_response_schema_models
from controllers.console import console_ns
from controllers.console.wraps import account_initialization_required, setup_required
from extensions.ext_database import db
from fields.base import ResponseModel
from libs.helper import dump_response
from libs.login import current_user, login_required
from services.system_management_access_service_extend import SystemManagementAccessService


class SystemManagementPermissionsResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")

    can_manage_system: StrictBool
    workspace_id: UUID | None
    account_id: UUID


register_response_schema_models(console_ns, SystemManagementPermissionsResponse)


@console_ns.route("/system-manage-extend/permissions")
class SystemManagementPermissionsExtend(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @console_ns.response(
        200, "Current global management permission", console_ns.models[SystemManagementPermissionsResponse.__name__]
    )
    def get(self):
        account = current_user._get_current_object()
        response = make_response(
            dump_response(
                SystemManagementPermissionsResponse,
                {
                    "can_manage_system": SystemManagementAccessService.can_manage(account, session=db.session),
                    "workspace_id": account.current_tenant_id,
                    "account_id": account.id,
                },
            ),
            200,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
