from importlib import import_module

from flask import Blueprint
from flask_restx import Namespace

from libs.external_api import ExternalApi

bp = Blueprint("console", __name__, url_prefix="/console/api")

api = ExternalApi(
    bp,
    version="1.0",
    title="Console API",
    description="Console management APIs for app configuration, monitoring, and administration",
)

console_ns = Namespace("console", description="Console management API operations", path="/")

RESOURCE_MODULES = (
    "controllers.console.app.app_import",
    "controllers.console.explore.audio",
    "controllers.console.explore.completion",
    "controllers.console.explore.conversation",
    "controllers.console.explore.message",
    "controllers.console.explore.workflow",
    "controllers.console.files",
    "controllers.console.remote_files",
)

for module_name in RESOURCE_MODULES:
    import_module(module_name)

# Ensure resource modules are imported so route decorators are evaluated.
# Import other controllers
from . import (
    apikey,
    extension,
    feature,
    human_input_form,
    init_validate,
    notification,
    ping,
    setup,
    spec,
    system_manage_extend,  # Extend: 系统管理功能迁移
    version,
    workflow_run_archive,
)
from .agent import composer as agent_composer
from .agent import roster as agent_roster

# Import app controllers
from .app import (
    advanced_prompt_template,
    agent,
    agent_app_access,
    agent_app_feature,
    agent_app_sandbox,
    agent_config_inspector,
    agent_drive_inspector,
    ai_draw_extnd,  # Extend: The backend implements direct proxy forwarding of the API
    annotation,
    app,
    app_extend,  # 二开部分：新增同步应用到模版中心
    audio,
    completion,
    conversation,
    conversation_variables,
    ding_talk_extend,  # Extend: DingTalk Related APIs
    generator,
    mcp_server,
    message,
    model_config,
    ops_trace,
    passport_extend,  # 二开部分: 新增passport_extend(额度限制，应用web计费)
    site,
    statistic,
    workflow,
    workflow_app_log,
    workflow_comment,
    workflow_draft_variable,
    workflow_node_output_inspector,
    workflow_run,
    workflow_statistic,
    workflow_trigger,
)

# Import auth controllers
from .auth import (
    activate,
    data_source_bearer_auth,
    data_source_oauth,
    email_register,
    forgot_password,
    login,
    oauth,
    oauth_server,
)

# Import billing controllers
from .billing import billing, compliance

# Import datasets controllers
from .datasets import (
    data_source,
    datasets,
    datasets_document,
    datasets_segments,
    external,
    hit_testing,
    metadata,
    website,
)
from .datasets.rag_pipeline import (
    datasource_auth,
    datasource_content_preview,
    rag_pipeline,
    rag_pipeline_datasets,
    rag_pipeline_draft_variable,
    rag_pipeline_import,
    rag_pipeline_workflow,
)

# Import explore controllers
from .explore import (
    banner,
    installed_app,
    parameter,
    recommended_app,
    saved_message,
    trial,
)
from .snippets import snippet_workflow, snippet_workflow_draft_variable
from .socketio import workflow as socketio_workflow

# Import tag controllers
from .tag import tags

# Import workspace controllers
from .workspace import (
    account,
    account_extend,  # 二开部分：新增account_extend
    agent_providers,
    endpoint,
    load_balancing_config,
    members,
    model_providers,
    models,
    plugin,
    rbac,
    snippets,
    tool_providers,
    trigger_providers,
    workspace,
)

api.add_namespace(console_ns)

__all__ = [
    "account",
    "account_extend",  # 二开部分：新增account_extend
    "activate",
    "advanced_prompt_template",
    "agent",
    "agent_app_access",
    "agent_app_feature",
    "agent_app_sandbox",
    "agent_composer",
    "agent_config_inspector",
    "agent_drive_inspector",
    "agent_providers",
    "agent_roster",
    "ai_draw_extnd",
    "annotation",
    "api",
    "apikey",
    "app",
    "app_extend",
    "audio",
    "banner",
    "billing",
    "bp",
    "completion",
    "compliance",
    "console_ns",
    "conversation",
    "conversation_variables",
    "data_source",
    "data_source_bearer_auth",
    "data_source_oauth",
    "datasets",
    "datasets_document",
    "datasets_segments",
    "datasource_auth",
    "datasource_content_preview",
    "ding_talk_extend",
    "email_register",
    "endpoint",
    "extension",
    "external",
    "feature",
    "forgot_password",
    "generator",
    "hit_testing",
    "human_input_form",
    "init_validate",
    "installed_app",
    "load_balancing_config",
    "login",
    "mcp_server",
    "members",
    "message",
    "metadata",
    "model_config",
    "model_providers",
    "models",
    "notification",
    "oauth",
    "oauth_server",
    "ops_trace",
    "parameter",
    "passport_extend",
    "ping",
    "plugin",
    "rag_pipeline",
    "rag_pipeline_datasets",
    "rag_pipeline_draft_variable",
    "rag_pipeline_import",
    "rag_pipeline_workflow",
    "rbac",
    "recommended_app",
    "saved_message",
    "setup",
    "site",
    "snippet_workflow",
    "snippet_workflow_draft_variable",
    "snippets",
    "socketio_workflow",
    "spec",
    "statistic",
    "system_manage_extend",  # extend: 二开
    "tags",
    "tool_providers",
    "trial",
    "trigger_providers",
    "version",
    "website",
    "workflow",
    "workflow_app_log",
    "workflow_comment",
    "workflow_draft_variable",
    "workflow_node_output_inspector",
    "workflow_run",
    "workflow_run_archive",
    "workflow_statistic",
    "workflow_trigger",
    "workspace",
]
