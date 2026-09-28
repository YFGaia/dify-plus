from flask import request
from flask_login import current_user
from flask_restx import Resource, marshal_with
from pydantic import BaseModel, Field, RootModel
from werkzeug.exceptions import BadRequest, Forbidden

from configs import dify_config
from controllers.common.rbac import AgentBehindApp, PlainApp, RBACCheck, RBACPermission, enforce_rbac_checks
from controllers.common.schema import query_params_from_model, register_response_schema_models
from controllers.console import api, console_ns
from controllers.console.app.wraps import get_app_model
from controllers.console.wraps import (
    account_initialization_required,
    setup_required,
)
from fields.app_fields_extend import (
    recommended_app_list_fields,
)
from libs.login import current_account_with_tenant, login_required
from services.account_service_extend import TenantExtendService
from services.recommended_app_service_extend import RecommendedAppService


# ---------------- start sync app to
class InstalledSyncAppApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @marshal_with(recommended_app_list_fields)
    def get(self):
        """Installed app"""

        app_service = RecommendedAppService()

        return app_service.installed_app_list(current_user.current_tenant_id)


class AppSyncApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @get_app_model
    def put(self, app_model):
        """Sync app"""

        # The role of the current user in the ta table must be admin or owner
        tenant_extend_service = TenantExtendService
        super_admin_id = tenant_extend_service.get_super_admin_id().id
        if super_admin_id != current_user.id:
            raise Forbidden()

        app_service = RecommendedAppService()

        appId = app_service.sync_recommended_app(app_model.id)

        return appId, 200

    @setup_required
    @login_required
    @account_initialization_required
    @get_app_model
    def delete(self, app_model):
        """Delete sync app"""
        # The role of the current user in the ta table must be admin or owner
        tenant_extend_service = TenantExtendService
        super_admin_id = tenant_extend_service.get_super_admin_id().id
        if super_admin_id != current_user.id:
            raise Forbidden()

        app_service = RecommendedAppService()

        app_service.delete_sync_recommended_app(app_model.id)

        return "", 200


# Extend: start messages context handling
class MessageContextQuery(BaseModel):
    conversation_id: str = Field(min_length=1)


class DeleteMessageContextQuery(MessageContextQuery):
    message_id: str = Field(min_length=1)


class MessageContextResponse(RootModel[list[str]]):
    pass


class DeleteMessageContextResponse(RootModel[str]):
    pass


register_response_schema_models(console_ns, MessageContextResponse, DeleteMessageContextResponse)


def _authorize_message_context(conversation_id: str, *, write: bool) -> tuple[str, str]:
    """Reconstruct the app owner before applying the Console conversation policy."""
    account, tenant_id = current_account_with_tenant()
    app = RecommendedAppService.message_context_app(tenant_id=tenant_id, conversation_id=conversation_id)
    # Match adjacent Console conversation routes when enterprise RBAC is disabled.
    if not dify_config.RBAC_ENABLED and not account.has_edit_permission:
        raise Forbidden()
    enforce_rbac_checks(
        tenant_id=tenant_id,
        account_id=account.id,
        checks=[
            RBACCheck(RBACPermission.APP_EDIT if write else RBACPermission.APP_VIEW_LAYOUT, PlainApp()),
            RBACCheck(RBACPermission.AGENT_EDIT if write else RBACPermission.AGENT_TEST_AND_RUN, AgentBehindApp()),
        ],
        path_args={"app_id": app.id},
    )
    return tenant_id, app.id


@console_ns.route("/message/context")
class MessageContextApi(Resource):
    @console_ns.doc(params=query_params_from_model(MessageContextQuery))
    @console_ns.response(200, "Message context IDs", console_ns.models[MessageContextResponse.__name__])
    @setup_required
    @login_required
    @account_initialization_required
    def get(self) -> list[str]:
        """Return context markers only for an authorized conversation in the active tenant."""
        if not request.args.get("conversation_id"):
            raise BadRequest("conversation_id is required")
        query = MessageContextQuery.model_validate(request.args.to_dict(flat=True))
        tenant_id, app_id = _authorize_message_context(query.conversation_id, write=False)
        result = RecommendedAppService.message_context(
            tenant_id=tenant_id, app_id=app_id, conversation_id=query.conversation_id
        )
        return MessageContextResponse.model_validate(result).model_dump(mode="json")

    @console_ns.doc(params=query_params_from_model(DeleteMessageContextQuery))
    @console_ns.response(200, "Context marker removed", console_ns.models[DeleteMessageContextResponse.__name__])
    @setup_required
    @login_required
    @account_initialization_required
    def delete(self) -> str:
        """Remove one marker after checking the conversation's app edit permission."""
        if not request.args.get("message_id") or not request.args.get("conversation_id"):
            raise BadRequest("message_id and conversation_id are required")
        query = DeleteMessageContextQuery.model_validate(request.args.to_dict(flat=True))
        tenant_id, app_id = _authorize_message_context(query.conversation_id, write=True)
        result = RecommendedAppService.delete_message_context(
            tenant_id=tenant_id, app_id=app_id, conversation_id=query.conversation_id, message_id=query.message_id
        )
        return DeleteMessageContextResponse.model_validate(result).model_dump(mode="json")


# Extend: stop messages context handling


# ----------------start sync app------------------------
api.add_resource(AppSyncApi, "/apps/<uuid:app_id>/sync")
api.add_resource(InstalledSyncAppApi, "/installed/apps")
# ---------------- stop sync app ------------------------
