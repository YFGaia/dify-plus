"""
Unit tests for CeleryWorkflowExecutionRepository.

These tests verify the Celery-based asynchronous storage functionality
for workflow execution data.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from core.repositories.celery_workflow_execution_repository import CeleryWorkflowExecutionRepository
from graphon.entities import WorkflowExecution
from graphon.enums import WorkflowType
from libs.datetime_utils import naive_utc_now
from models import Account, EndUser, Tenant
from models.enums import WorkflowRunTriggeredFrom

RESOURCE_TENANT_ID = "resource-tenant-id"


@pytest.fixture
def mock_session_factory():
    """Mock SQLAlchemy session factory."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # Create a real sessionmaker with in-memory SQLite for testing
    engine = create_engine("sqlite:///:memory:")
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_account():
    """Mock Account user."""
    account = Account(name="Test Account", email="test@example.com")
    account.id = str(uuid4())
    account._current_tenant = Tenant(name="Test Tenant")
    account._current_tenant.id = str(uuid4())
    return account


@pytest.fixture
def mock_end_user():
    """Mock EndUser."""
    user = EndUser(
        id=str(uuid4()),
        tenant_id=str(uuid4()),
    )
    return user


@pytest.fixture
def sample_workflow_execution():
    """Sample WorkflowExecution for testing."""
    return WorkflowExecution.new(
        id_=str(uuid4()),
        workflow_id=str(uuid4()),
        workflow_type=WorkflowType.WORKFLOW,
        workflow_version="1.0",
        graph={"nodes": [], "edges": []},
        inputs={"input1": "value1"},
        started_at=naive_utc_now(),
    )


class TestCeleryWorkflowExecutionRepository:
    """Test cases for CeleryWorkflowExecutionRepository."""

    def test_init_with_sessionmaker(self, mock_session_factory, mock_account):
        """Test repository initialization with sessionmaker."""
        app_id = "test-app-id"
        triggered_from = WorkflowRunTriggeredFrom.APP_RUN

        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=mock_account,
            app_id=app_id,
            triggered_from=triggered_from,
        )

        assert repo._tenant_id == RESOURCE_TENANT_ID
        assert repo._app_id == app_id
        assert repo._triggered_from == triggered_from
        assert repo._creator_user_id == mock_account.id
        assert repo._creator_user_role is not None

    def test_init_basic_functionality(self, mock_session_factory, mock_account):
        """Test repository initialization basic functionality."""
        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=mock_account,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.DEBUGGING,
        )

        # Verify basic initialization
        assert repo._tenant_id == RESOURCE_TENANT_ID
        assert repo._app_id == "test-app"
        assert repo._triggered_from == WorkflowRunTriggeredFrom.DEBUGGING

    def test_init_with_end_user(self, mock_session_factory, mock_end_user):
        """Test repository initialization with EndUser."""
        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=mock_end_user,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

        assert repo._tenant_id == RESOURCE_TENANT_ID

    def test_init_without_tenant_id_raises_error(self, mock_session_factory):
        """Test that initialization fails without tenant_id."""
        # Create an Account with no tenant_id.
        user = Account(name="Test Account", email="test@example.com")
        user.id = str(uuid4())

        with pytest.raises(ValueError, match="tenant_id is required"):
            CeleryWorkflowExecutionRepository(
                session_factory=mock_session_factory,
                tenant_id="",
                user=user,
                app_id="test-app",
                triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            )

    def test_init_uses_resource_tenant_when_account_has_no_current_tenant(self, mock_session_factory):
        user = Account(name="Test Account", email="test@example.com")
        user.id = str(uuid4())

        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=user,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

        assert repo._tenant_id == RESOURCE_TENANT_ID
        assert repo._creator_user_id == user.id

    @patch("core.repositories.celery_workflow_execution_repository.save_workflow_execution_task")
    def test_save_queues_celery_task(self, mock_task, mock_session_factory, mock_account, sample_workflow_execution):
        """Test that save operation queues a Celery task without tracking."""
        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=mock_account,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

        repo.save(sample_workflow_execution)

        # Verify Celery task was queued with correct parameters
        mock_task.delay.assert_called_once()
        call_args = mock_task.delay.call_args[1]

        assert call_args["execution_data"] == sample_workflow_execution.model_dump()
        assert call_args["tenant_id"] == RESOURCE_TENANT_ID
        assert call_args["app_id"] == "test-app"
        assert call_args["triggered_from"] == WorkflowRunTriggeredFrom.APP_RUN
        assert call_args["creator_user_id"] == mock_account.id

        # Verify no task tracking occurs (no _pending_saves attribute)
        assert not hasattr(repo, "_pending_saves")

    @patch("core.repositories.celery_workflow_execution_repository.save_workflow_execution_task")
    def test_save_handles_celery_failure(
        self, mock_task, mock_session_factory, mock_account, sample_workflow_execution
    ):
        """Test that save operation handles Celery task failures."""
        mock_task.delay.side_effect = Exception("Celery is down")

        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=mock_account,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

        with pytest.raises(Exception, match="Celery is down"):
            repo.save(sample_workflow_execution)

    @patch("core.repositories.celery_workflow_execution_repository.save_workflow_execution_task")
    def test_save_operation_fire_and_forget(
        self, mock_task, mock_session_factory, mock_account, sample_workflow_execution
    ):
        """Test that save operation works in fire-and-forget mode."""
        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=mock_account,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

        # Test that save doesn't block or maintain state
        repo.save(sample_workflow_execution)

        # Verify no pending saves are tracked (no _pending_saves attribute)
        assert not hasattr(repo, "_pending_saves")

    @patch("core.repositories.celery_workflow_execution_repository.save_workflow_execution_task")
    def test_multiple_save_operations(self, mock_task, mock_session_factory, mock_account):
        """Test multiple save operations work correctly."""
        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=RESOURCE_TENANT_ID,
            user=mock_account,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

        # Create multiple executions
        exec1 = WorkflowExecution.new(
            id_=str(uuid4()),
            workflow_id=str(uuid4()),
            workflow_type=WorkflowType.WORKFLOW,
            workflow_version="1.0",
            graph={"nodes": [], "edges": []},
            inputs={"input1": "value1"},
            started_at=naive_utc_now(),
        )
        exec2 = WorkflowExecution.new(
            id_=str(uuid4()),
            workflow_id=str(uuid4()),
            workflow_type=WorkflowType.WORKFLOW,
            workflow_version="1.0",
            graph={"nodes": [], "edges": []},
            inputs={"input2": "value2"},
            started_at=naive_utc_now(),
        )

        # Save both executions
        repo.save(exec1)
        repo.save(exec2)

        # Should work without issues and not maintain state (no _pending_saves attribute)
        assert not hasattr(repo, "_pending_saves")

    @patch("core.repositories.celery_workflow_execution_repository.save_workflow_execution_task")
    def test_save_with_different_user_types(self, mock_task, mock_session_factory, mock_end_user):
        """Test save operation with different user types."""
        repo = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id=mock_end_user.tenant_id,
            user=mock_end_user,
            app_id="test-app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

        execution = WorkflowExecution.new(
            id_=str(uuid4()),
            workflow_id=str(uuid4()),
            workflow_type=WorkflowType.WORKFLOW,
            workflow_version="1.0",
            graph={"nodes": [], "edges": []},
            inputs={"input1": "value1"},
            started_at=naive_utc_now(),
        )

        repo.save(execution)

        # Verify task was called with EndUser context
        mock_task.delay.assert_called_once()
        call_args = mock_task.delay.call_args[1]
        assert call_args["tenant_id"] == mock_end_user.tenant_id
        assert call_args["creator_user_id"] == mock_end_user.id


@pytest.mark.parametrize("actor", [None, "account-a", "account-b"])
def test_celery_serializes_actor_without_changing_creator(
    mock_session_factory: sessionmaker[Session],
    mock_end_user: EndUser,
    sample_workflow_execution: WorkflowExecution,
    actor: str | None,
) -> None:
    mock_end_user.external_user_id = "untrusted-binding"
    with patch("core.repositories.celery_workflow_execution_repository.save_workflow_execution_task") as task:
        repository = CeleryWorkflowExecutionRepository(
            session_factory=mock_session_factory,
            tenant_id="tenant",
            user=mock_end_user,
            app_id="app",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            from_account_id=actor,
        )
        repository.save(sample_workflow_execution)
    kwargs = task.delay.call_args.kwargs
    assert kwargs["from_account_id"] == actor
    assert kwargs["creator_user_id"] == mock_end_user.id
    assert kwargs["creator_user_role"] == "end_user"


@pytest.mark.parametrize("actor", [None, "account-a", "account-b"])
@pytest.mark.parametrize("retry_actor", [None, "retry-account"])
def test_celery_storage_preserves_first_actor(
    sqlite_session_factory: sessionmaker[Session],
    sample_workflow_execution: WorkflowExecution,
    actor: str | None,
    retry_actor: str | None,
) -> None:
    from models import WorkflowRun
    from models.workflow_account_extend import WorkflowRunAccountExtend
    from tasks.workflow_execution_tasks import save_workflow_execution_task

    with patch("tasks.workflow_execution_tasks.session_factory.create_session", sqlite_session_factory):
        kwargs = {
            "execution_data": sample_workflow_execution.model_dump(mode="json"),
            "tenant_id": "tenant",
            "app_id": "app",
            "triggered_from": "app-run",
            "creator_user_id": "end-user",
            "creator_user_role": "end_user",
        }
        assert save_workflow_execution_task.run(**kwargs, from_account_id=actor)
        assert save_workflow_execution_task.run(**kwargs, from_account_id=retry_actor)
        # Older queued tasks can omit the added optional parameter.
        assert save_workflow_execution_task.run(**kwargs)
    with sqlite_session_factory() as session:
        row = session.get(WorkflowRun, sample_workflow_execution.id_)
        assert row is not None
        attribution = session.get(WorkflowRunAccountExtend, row.id)
        assert attribution is not None
        assert attribution.from_account_id == actor
        assert row.created_by == "end-user"


@pytest.mark.parametrize("scope", [{"tenant_id": "other-tenant"}, {"app_id": "other-app"}])
def test_celery_storage_rejects_another_owner_scope(
    sqlite_session_factory: sessionmaker[Session], sample_workflow_execution: WorkflowExecution, scope: dict[str, str]
) -> None:
    from models import WorkflowRun
    from models.workflow_account_extend import WorkflowRunAccountExtend
    from tasks.workflow_execution_tasks import save_workflow_execution_task

    kwargs = {
        "execution_data": sample_workflow_execution.model_dump(mode="json"),
        "tenant_id": "tenant",
        "app_id": "app",
        "triggered_from": "app-run",
        "creator_user_id": "end-user",
        "creator_user_role": "end_user",
        "from_account_id": "account-a",
    }
    with patch("tasks.workflow_execution_tasks.session_factory.create_session", sqlite_session_factory):
        assert save_workflow_execution_task.run(**kwargs)
        with pytest.raises(ValueError, match="Unauthorized"):
            save_workflow_execution_task.run(
                execution_data=sample_workflow_execution.model_dump(mode="json"),
                tenant_id=scope.get("tenant_id", "tenant"),
                app_id=scope.get("app_id", "app"),
                triggered_from="app-run",
                creator_user_id="end-user",
                creator_user_role="end_user",
                from_account_id="retry-account",
            )
    with sqlite_session_factory() as session:
        row = session.get(WorkflowRun, sample_workflow_execution.id_)
        assert row is not None
        assert (row.tenant_id, row.app_id) == ("tenant", "app")
        attribution = session.get(WorkflowRunAccountExtend, row.id)
        assert attribution is not None
        assert attribution.from_account_id == "account-a"
