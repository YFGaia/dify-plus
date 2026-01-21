from flask_login import current_user
from flask_restx import Resource, marshal_with
from werkzeug.exceptions import Forbidden

from controllers.console import api
from controllers.console.app.wraps import get_app_model
from controllers.console.wraps import (
    account_initialization_required,
    setup_required,
)
from fields.app_fields_extend import (
    recommended_app_list_fields,
)
from libs.login import login_required
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
class MessageContextApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    def get(self):
        """Message Context"""
        from flask import request
        conversation_id = request.args.get("conversation_id")
        if not conversation_id:
            from werkzeug.exceptions import BadRequest
            raise BadRequest("conversation_id is required")
        app_service = RecommendedAppService()

        return app_service.message_context(conversation_id)

    @setup_required
    @login_required
    @account_initialization_required
    def delete(self):
        """Message Context"""
        from flask import request
        message_id = request.args.get("message_id")
        conversation_id = request.args.get("conversation_id")
        if not message_id or not conversation_id:
            from werkzeug.exceptions import BadRequest
            raise BadRequest("message_id and conversation_id are required")
        app_service = RecommendedAppService()

        return app_service.delete_message_context(conversation_id, message_id)
# Extend: stop messages context handling


# ----------------start sync app------------------------
api.add_resource(AppSyncApi, "/apps/<uuid:app_id>/sync")
api.add_resource(InstalledSyncAppApi, "/installed/apps")
api.add_resource(MessageContextApi, "/message/context")
# ---------------- stop sync app ------------------------
