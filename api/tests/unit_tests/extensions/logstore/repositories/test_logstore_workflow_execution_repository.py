from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from extensions.logstore.repositories.logstore_workflow_execution_repository import (
    LogstoreWorkflowExecutionRepository,
)
from models.account import Account
from models.enums import WorkflowRunTriggeredFrom


def test_repository_uses_typed_logstore_migration_flags(
    config_overrides: Callable[..., None],
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    config_overrides(
        LOGSTORE_DUAL_WRITE_ENABLED=True,
        LOGSTORE_ENABLE_PUT_GRAPH_FIELD=False,
    )
    with (
        patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore"),
        patch(
            "extensions.logstore.repositories.logstore_workflow_execution_repository."
            "SQLAlchemyWorkflowExecutionRepository"
        ),
    ):
        repository = LogstoreWorkflowExecutionRepository(
            session_factory=sqlite_session_factory,
            tenant_id="tenant-1",
            user=cast(Account, SimpleNamespace(id="account-1")),
            app_id="app-1",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        )

    assert repository._enable_dual_write is True
    assert repository._enable_put_graph_field is False


import pytest

from graphon.entities import WorkflowExecution
from graphon.enums import WorkflowType
from libs.datetime_utils import naive_utc_now
from models import EndUser


def _execution() -> WorkflowExecution:
    return WorkflowExecution.new(
        id_="run",
        workflow_id="workflow",
        workflow_type=WorkflowType.WORKFLOW,
        workflow_version="1",
        graph={},
        inputs={},
        started_at=naive_utc_now(),
    )


@pytest.mark.parametrize("pg", [False, True])
@pytest.mark.parametrize("original", [None, "account-a", "account-b", "missing"])
@pytest.mark.parametrize("resumer", [None, "retry-account"])
def test_logstore_recovery_keeps_first_actor(
    config_overrides: Callable[..., None],
    sqlite_session_factory: sessionmaker[Session],
    pg: bool,
    original: str | None,
    resumer: str | None,
) -> None:
    config_overrides(LOGSTORE_DUAL_WRITE_ENABLED=False)
    first = {"id": "run", "log_version": "1", "tenant_id": "tenant-1", "app_id": "app-1"}
    if original != "missing":
        first["from_account_id"] = original or ""
    later = {**first, "log_version": "2", "from_account_id": "incorrect-later-actor"}
    with patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore") as store:
        client = store.return_value
        client.supports_pg_protocol = pg
        client.get_logs.return_value = [later, first]
        repository = LogstoreWorkflowExecutionRepository(
            session_factory=sqlite_session_factory,
            tenant_id="tenant-1",
            user=EndUser(id="end-user", external_user_id="untrusted"),
            app_id="app-1",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            from_account_id=resumer,
        )
        repository.save(_execution())
        repository.save(_execution())
        for call in client.put_log.call_args_list:
            fields = dict(call.args[1])
            assert fields["from_account_id"] == ("" if original in (None, "missing") else original)
            assert fields["created_by"] == "end-user"
            assert fields["created_by_role"] == "end_user"
        reader = client.get_logs
        reader.assert_called_once()
        query = reader.call_args.kwargs["query"]
        assert "run" in query


@pytest.mark.parametrize("actor", [None, "account-a", "account-b"])
def test_logstore_initial_write_and_dual_write_forward_actor(
    config_overrides: Callable[..., None], sqlite_session_factory: sessionmaker[Session], actor: str | None
) -> None:
    config_overrides(LOGSTORE_DUAL_WRITE_ENABLED=True)
    with (
        patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore") as store,
        patch(
            "extensions.logstore.repositories.logstore_workflow_execution_repository.SQLAlchemyWorkflowExecutionRepository"
        ) as sql,
    ):
        store.return_value.supports_pg_protocol = True
        store.return_value.get_logs.return_value = list[dict[str, str]]()
        repository = LogstoreWorkflowExecutionRepository(
            session_factory=sqlite_session_factory,
            tenant_id="tenant-1",
            user=EndUser(id="end-user"),
            app_id="app-1",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            from_account_id=actor,
        )
        repository.save(_execution())
        assert dict(store.return_value.put_log.call_args.args[1])["from_account_id"] == (actor or "")
        assert sql.call_args.kwargs["from_account_id"] == actor
        sql.return_value.save.assert_called_once()


def test_logstore_actor_lookup_failure_does_not_append_guess(
    config_overrides: Callable[..., None], sqlite_session_factory: sessionmaker[Session]
) -> None:
    config_overrides(LOGSTORE_DUAL_WRITE_ENABLED=False)
    with patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore") as store:
        store.return_value.supports_pg_protocol = True
        store.return_value.get_logs.side_effect = RuntimeError("lookup unavailable")
        repository = LogstoreWorkflowExecutionRepository(
            session_factory=sqlite_session_factory,
            tenant_id="tenant-1",
            user=EndUser(id="end-user"),
            app_id="app-1",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            from_account_id="retry-account",
        )
        with pytest.raises(RuntimeError, match="lookup unavailable"):
            repository.save(_execution())
        store.return_value.put_log.assert_not_called()


@pytest.mark.parametrize("pg", [False, True])
@pytest.mark.parametrize("scope", [{"tenant_id": "other-tenant"}, {"app_id": "other-app"}])
def test_logstore_rejects_an_existing_run_in_another_scope(
    config_overrides: Callable[..., None],
    sqlite_session_factory: sessionmaker[Session],
    pg: bool,
    scope: dict[str, str],
) -> None:
    config_overrides(LOGSTORE_DUAL_WRITE_ENABLED=True)
    with patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore") as store:
        client = store.return_value
        client.supports_pg_protocol = pg
        rows = [{"id": "run", "tenant_id": "tenant-1", "app_id": "app-1", "from_account_id": "original"} | scope]
        client.get_logs.return_value = rows
        repository = LogstoreWorkflowExecutionRepository(
            session_factory=sqlite_session_factory,
            tenant_id="tenant-1",
            user=EndUser(id="end-user"),
            app_id="app-1",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            from_account_id="retry-account",
        )
        with pytest.raises(ValueError, match="Unauthorized"):
            repository.save(_execution())
        client.put_log.assert_not_called()


@pytest.mark.parametrize("original", [None, "account-a"])
@pytest.mark.parametrize("sql_copy_exists", [False, True])
def test_logstore_dual_write_preserves_stored_actor(
    config_overrides: Callable[..., None],
    sqlite_session_factory: sessionmaker[Session],
    original: str | None,
    sql_copy_exists: bool,
) -> None:
    from core.repositories.sqlalchemy_workflow_execution_repository import SQLAlchemyWorkflowExecutionRepository
    from models import WorkflowRun
    from models.workflow_account_extend import WorkflowRunAccountExtend

    config_overrides(LOGSTORE_DUAL_WRITE_ENABLED=True)
    kwargs = {
        "session_factory": sqlite_session_factory,
        "tenant_id": "tenant-1",
        "user": EndUser(id="end-user"),
        "app_id": "app-1",
        "triggered_from": WorkflowRunTriggeredFrom.APP_RUN,
    }
    if sql_copy_exists:
        SQLAlchemyWorkflowExecutionRepository(**kwargs, from_account_id=original).save(_execution())
    with patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore") as store:
        client = store.return_value
        client.supports_pg_protocol = True
        client.get_logs.return_value = [
            {"id": "run", "tenant_id": "tenant-1", "app_id": "app-1", "from_account_id": original or ""}
        ]
        repository = LogstoreWorkflowExecutionRepository(**kwargs, from_account_id="retry-account")
        repository.save(_execution())
        assert dict(client.put_log.call_args.args[1])["from_account_id"] == (original or "")
    with sqlite_session_factory() as session:
        assert session.get(WorkflowRun, "run") is not None
        attribution = session.get(WorkflowRunAccountExtend, "run")
        assert attribution is not None
        assert attribution.from_account_id == original


@pytest.mark.parametrize("dual_write", [False, True])
def test_logstore_does_not_require_sql_for_ownership(
    config_overrides: Callable[..., None], sqlite_session_factory: sessionmaker[Session], dual_write: bool
) -> None:
    config_overrides(LOGSTORE_DUAL_WRITE_ENABLED=dual_write)
    with (
        patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore") as store,
        patch.object(Session, "get", side_effect=AssertionError("SQL lookup forbidden")),
    ):
        store.return_value.supports_pg_protocol = True
        store.return_value.get_logs.return_value = [{"id": "run", "tenant_id": "tenant-1", "app_id": "app-1"}]
        repository = LogstoreWorkflowExecutionRepository(
            session_factory=sqlite_session_factory,
            tenant_id="tenant-1",
            user=EndUser(id="end-user"),
            app_id="app-1",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            from_account_id="retry-account",
        )
        with patch.object(repository.sql_repository, "save", side_effect=RuntimeError("SQL unavailable")) as sql_save:
            repository.save(_execution())
            assert sql_save.call_count == int(dual_write)
        assert dict(store.return_value.put_log.call_args.args[1])["from_account_id"] == ""


@pytest.mark.parametrize("foreign_field", ["tenant_id", "app_id"])
@pytest.mark.parametrize("association_only", [False, True])
def test_dual_write_rejects_foreign_sql_scope_before_log_append(
    config_overrides: Callable[..., None],
    sqlite_session_factory: sessionmaker[Session],
    foreign_field: str,
    association_only: bool,
) -> None:
    from core.repositories.sqlalchemy_workflow_execution_repository import SQLAlchemyWorkflowExecutionRepository
    from models import WorkflowRun

    config_overrides(LOGSTORE_DUAL_WRITE_ENABLED=True)
    foreign_tenant = "foreign" if foreign_field == "tenant_id" else "tenant-1"
    foreign_app = "foreign" if foreign_field == "app_id" else "app-1"
    SQLAlchemyWorkflowExecutionRepository(
        session_factory=sqlite_session_factory,
        tenant_id=foreign_tenant,
        user=EndUser(id="end-user"),
        app_id=foreign_app,
        triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        from_account_id="original",
    ).save(_execution())
    if association_only:
        with sqlite_session_factory() as session:
            row = session.get(WorkflowRun, "run")
            assert row is not None
            session.delete(row)
            session.commit()
    with patch("extensions.logstore.repositories.logstore_workflow_execution_repository.AliyunLogStore") as store:
        store.return_value.get_logs.return_value = list[dict[str, str]]()
        repository = LogstoreWorkflowExecutionRepository(
            session_factory=sqlite_session_factory,
            tenant_id="tenant-1",
            user=EndUser(id="end-user"),
            app_id="app-1",
            triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
            from_account_id="retry-account",
        )
        with pytest.raises(ValueError, match="Unauthorized"):
            repository.save(_execution())
        store.return_value.put_log.assert_not_called()


def test_logstore_actor_reads_raw_fields_across_pages() -> None:
    from unittest.mock import MagicMock

    from extensions.logstore.repositories.logstore_workflow_execution_repository import get_logstore_account_actor

    client = MagicMock()
    client.supports_pg_protocol = True
    later = {"id": "run", "tenant_id": "tenant", "app_id": "app", "log_version": "20", "from_account_id": "later"}
    # Ignore tokenized search hits for another exact ID, including foreign scopes.
    unrelated = {"id": "other-run", "tenant_id": "foreign"}
    first = {**later, "log_version": "1", "from_account_id": "original"}
    client.get_logs.side_effect = [[later] * 99 + [unrelated], [first]]
    assert get_logstore_account_actor(client, execution_id="run", tenant_id="tenant", app_id="app") == (
        True,
        "original",
    )
    assert [call.kwargs["offset"] for call in client.get_logs.call_args_list] == [0, 100]
    client.execute_sql.assert_not_called()
