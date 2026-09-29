import json
import logging
import time
from typing import override

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from configs import dify_config
from core.repositories.factory import WorkflowExecutionRepository
from core.repositories.sqlalchemy_workflow_execution_repository import SQLAlchemyWorkflowExecutionRepository
from extensions.logstore.aliyun_logstore import AliyunLogStore
from extensions.logstore.sql_escape import escape_logstore_query_value
from graphon.entities import WorkflowExecution
from graphon.workflow_type_encoder import WorkflowRuntimeTypeConverter
from models import (
    Account,
    CreatorUserRole,
    EndUser,
)
from models.enums import WorkflowRunTriggeredFrom

logger = logging.getLogger(__name__)


def get_logstore_account_actor(
    client: AliyunLogStore, *, execution_id: str, tenant_id: str, app_id: str | None
) -> tuple[bool, str | None]:
    """Read immutable ownership from the earliest Logstore version, without SQL I/O.

    Missing/empty fields mean unknown ownership. A lookup failure must propagate;
    the caller must not treat it as a new run and append guessed ownership.
    """
    # Raw SDK reads include unindexed event fields, even when PG mode is enabled.
    # The upstream WorkflowRun-derived SQL index does not contain this fork field.
    first = None
    offset = 0
    to_time = int(time.time()) + 1
    while True:
        rows = client.get_logs(
            logstore=AliyunLogStore.workflow_execution_logstore,
            from_time=0,
            to_time=to_time,
            query=f"id:{escape_logstore_query_value(execution_id)}",
            line=100,
            offset=offset,
            reverse=False,
        )
        # Search indexes may tokenize IDs. Only exact run IDs can supply ownership.
        # Check scope after lookup so a foreign run cannot appear to be a new run.
        for row in rows:
            if row.get("id") != execution_id:
                continue
            if row.get("tenant_id") != tenant_id or row.get("app_id") != (app_id or ""):
                raise ValueError("Unauthorized access to workflow run")
            if first is None or int(row.get("log_version") or 0) < int(first.get("log_version") or 0):
                first = row
        if len(rows) < 100:
            break
        offset += len(rows)
    return (False, None) if first is None else (True, first.get("from_account_id") or None)


class LogstoreWorkflowExecutionRepository(WorkflowExecutionRepository):
    def __init__(
        self,
        session_factory: sessionmaker | Engine,
        tenant_id: str,
        user: Account | EndUser,
        app_id: str | None,
        triggered_from: WorkflowRunTriggeredFrom | None,
        from_account_id: str | None = None,
    ):
        """
        Initialize the repository with a SQLAlchemy sessionmaker or engine and context information.

        Args:
            session_factory: SQLAlchemy sessionmaker or engine for creating sessions
            tenant_id: Tenant that owns the workflow execution
            user: Account or EndUser used for creator attribution
            app_id: App ID for filtering by application (can be None)
            triggered_from: Source of the execution trigger (DEBUGGING or APP_RUN)
            from_account_id: Validated WebApp Console actor; ignored for already persisted ownership
        """
        logger.debug(
            "LogstoreWorkflowExecutionRepository.__init__: app_id=%s, triggered_from=%s", app_id, triggered_from
        )
        # Initialize LogStore client
        # Note: Project/logstore/index initialization is done at app startup via ext_logstore
        self.logstore_client = AliyunLogStore()

        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._tenant_id = tenant_id

        # Store app context
        self._app_id = app_id
        self._from_account_id = from_account_id
        self._account_actors: dict[str, str | None] = {}

        # Extract user context
        self._triggered_from = triggered_from
        self._creator_user_id = user.id

        # Determine user role based on user type
        self._creator_user_role = CreatorUserRole.ACCOUNT if isinstance(user, Account) else CreatorUserRole.END_USER

        # Initialize SQL repository for dual-write support
        self.sql_repository = SQLAlchemyWorkflowExecutionRepository(
            session_factory=session_factory,
            tenant_id=tenant_id,
            user=user,
            app_id=app_id,
            triggered_from=triggered_from,
            from_account_id=from_account_id,
        )

        self._enable_dual_write = dify_config.LOGSTORE_DUAL_WRITE_ENABLED

        # Control flag for whether to write the `graph` field to LogStore.
        # If LOGSTORE_ENABLE_PUT_GRAPH_FIELD is "true", write the full `graph` field;
        # otherwise write an empty {} instead. Defaults to writing the `graph` field.
        self._enable_put_graph_field = dify_config.LOGSTORE_ENABLE_PUT_GRAPH_FIELD

    def _to_logstore_model(self, domain_model: WorkflowExecution) -> list[tuple[str, str]]:
        """
        Convert a domain model to a logstore model (List[Tuple[str, str]]).

        Args:
            domain_model: The domain model to convert

        Returns:
            The logstore model as a list of key-value tuples
        """
        logger.debug(
            "_to_logstore_model: id=%s, workflow_id=%s, status=%s",
            domain_model.id_,
            domain_model.workflow_id,
            domain_model.status.value,
        )
        # Use values from constructor if provided
        if not self._triggered_from:
            raise ValueError("triggered_from is required in repository constructor")
        if not self._creator_user_id:
            raise ValueError("created_by is required in repository constructor")
        if not self._creator_user_role:
            raise ValueError("created_by_role is required in repository constructor")

        # Generate log_version as nanosecond timestamp for record versioning
        log_version = str(time.time_ns())

        # Use WorkflowRuntimeTypeConverter to handle complex types (Segment, File, etc.)
        json_converter = WorkflowRuntimeTypeConverter()

        logstore_model = [
            ("id", domain_model.id_),
            ("log_version", log_version),  # Add log_version field for append-only writes
            ("tenant_id", self._tenant_id),
            ("app_id", self._app_id or ""),
            ("workflow_id", domain_model.workflow_id),
            (
                "triggered_from",
                self._triggered_from.value if hasattr(self._triggered_from, "value") else str(self._triggered_from),
            ),
            ("type", domain_model.workflow_type.value),
            ("version", domain_model.workflow_version),
            (
                "graph",
                json.dumps(json_converter.to_json_encodable(domain_model.graph), ensure_ascii=False)
                if domain_model.graph and self._enable_put_graph_field
                else "{}",
            ),
            (
                "inputs",
                json.dumps(json_converter.to_json_encodable(domain_model.inputs), ensure_ascii=False)
                if domain_model.inputs
                else "{}",
            ),
            (
                "outputs",
                json.dumps(json_converter.to_json_encodable(domain_model.outputs), ensure_ascii=False)
                if domain_model.outputs
                else "{}",
            ),
            ("status", domain_model.status.value),
            ("error_message", domain_model.error_message or ""),
            ("total_tokens", str(domain_model.total_tokens)),
            ("total_steps", str(domain_model.total_steps)),
            ("exceptions_count", str(domain_model.exceptions_count)),
            (
                "created_by_role",
                self._creator_user_role.value
                if hasattr(self._creator_user_role, "value")
                else str(self._creator_user_role),
            ),
            ("created_by", self._creator_user_id),
            ("from_account_id", self._account_actors.get(domain_model.id_, self._from_account_id) or ""),
            ("started_at", domain_model.started_at.isoformat() if domain_model.started_at else ""),
            ("finished_at", domain_model.finished_at.isoformat() if domain_model.finished_at else ""),
        ]

        return logstore_model

    @override
    def save(self, execution: WorkflowExecution) -> None:
        """
        Save or update a WorkflowExecution domain entity to the logstore.

        This method serves as a domain-to-logstore adapter that:
        1. Converts the domain entity to its logstore representation
        2. Persists the logstore model using Aliyun SLS
        3. Maintains proper multi-tenancy by including tenant context during conversion
        4. Optionally writes to SQL database for dual-write support (controlled by LOGSTORE_DUAL_WRITE_ENABLED)

        Args:
            execution: The WorkflowExecution domain entity to persist
        """
        logger.debug(
            "save: id=%s, workflow_id=%s, status=%s", execution.id_, execution.workflow_id, execution.status.value
        )
        try:
            if self._enable_dual_write:
                # Reject an existing SQL run in a foreign scope before appending
                # a Logstore version. SQL availability remains best-effort.
                try:
                    self.sql_repository.validate_scope(execution.id_)
                except ValueError:
                    raise
                except Exception:
                    logger.exception("Failed to check SQL workflow run scope: id=%s", execution.id_)
            if execution.id_ not in self._account_actors:
                exists, actor = get_logstore_account_actor(
                    self.logstore_client,
                    execution_id=execution.id_,
                    tenant_id=self._tenant_id,
                    app_id=self._app_id,
                )
                self._account_actors[execution.id_] = actor if exists else self._from_account_id
            logstore_model = self._to_logstore_model(execution)
            self.logstore_client.put_log(AliyunLogStore.workflow_execution_logstore, logstore_model)

            logger.debug("Saved workflow execution to logstore: id=%s", execution.id_)
        except Exception:
            logger.exception("Failed to save workflow execution to logstore: id=%s", execution.id_)
            raise

        # Dual-write to SQL database if enabled (for safe migration)
        if self._enable_dual_write:
            try:
                # A SQL copy first created after Logstore recovery uses that run's
                # original actor, including NULL for historical Logstore records.
                self.sql_repository._from_account_id = self._account_actors[execution.id_]
                self.sql_repository.save(execution)
                logger.debug("Dual-write: saved workflow execution to SQL database: id=%s", execution.id_)
            except ValueError:
                raise
            except Exception:
                logger.exception("Failed to dual-write workflow execution to SQL database: id=%s", execution.id_)
                # Don't raise - LogStore write succeeded, SQL is just a backup
