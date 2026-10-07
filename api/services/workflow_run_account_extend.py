"""Queries for Dify Plus workflow-run ownership metadata."""

from sqlalchemy.orm import Session
from werkzeug.utils import import_string

from configs import dify_config
from extensions.logstore.aliyun_logstore import AliyunLogStore
from extensions.logstore.repositories.logstore_workflow_execution_repository import (
    LogstoreWorkflowExecutionRepository,
    get_logstore_account_actor,
)
from models.workflow_account_extend import WorkflowRunAccountExtend


def get_workflow_run_account_id(
    session: Session,
    *,
    workflow_run_id: str,
    tenant_id: str,
    app_id: str,
) -> str | None:
    """Return the original actor for a run after validating its tenant and app scope.

    Missing rows represent legacy runs whose ownership is unknown. The caller must
    never fill that gap from the user who is resuming the workflow.
    """
    repository_class = import_string(dify_config.CORE_WORKFLOW_EXECUTION_REPOSITORY)
    if issubclass(repository_class, LogstoreWorkflowExecutionRepository):
        _, actor = get_logstore_account_actor(
            AliyunLogStore(), execution_id=workflow_run_id, tenant_id=tenant_id, app_id=app_id
        )
        return actor
    attribution = session.get(WorkflowRunAccountExtend, workflow_run_id)
    if attribution is None:
        return None
    if attribution.tenant_id != tenant_id or attribution.app_id != app_id:
        raise ValueError("Unauthorized access to workflow run")
    return attribution.from_account_id
