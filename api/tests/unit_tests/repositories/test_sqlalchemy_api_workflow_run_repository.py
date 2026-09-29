from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import TextClause

from core.workflow.nodes.human_input.entities import FormDefinition, ParagraphInputConfig, UserActionConfig
from core.workflow.nodes.human_input.enums import FormInputType
from core.workflow.nodes.human_input.pause_reason import HumanInputRequired
from graphon.entities.pause_reason import HitlRequired, PauseReasonType
from graphon.enums import WorkflowExecutionStatus, WorkflowType
from models import Message
from models.enums import ConversationFromSource, CreatorUserRole, WorkflowRunTriggeredFrom
from models.human_input import HumanInputForm, HumanInputFormRecipient, RecipientType
from models.model import App, AppMode
from models.workflow import WorkflowPause, WorkflowPauseReason, WorkflowRun
from models.workflow_account_extend import WorkflowRunAccountExtend
from repositories import sqlalchemy_api_workflow_run_repository as statistic_repository_module
from repositories.sqlalchemy_api_workflow_run_repository import (
    DifyAPISQLAlchemyWorkflowRunRepository,
    WorkflowRunMessageRef,
    WorkflowRunPauseRecord,
    _build_human_input_required_reason,
    _PrivateWorkflowPauseEntity,
)


def _build_form_model() -> HumanInputForm:
    expiration_time = datetime(2024, 1, 1, tzinfo=UTC)
    definition = FormDefinition(
        form_content="content",
        inputs=[ParagraphInputConfig(type=FormInputType.PARAGRAPH, output_variable_name="name")],
        user_actions=[UserActionConfig(id="approve", title="Approve")],
        rendered_content="rendered",
        expiration_time=expiration_time,
        default_values={"name": "Alice"},
        node_title="Ask Name",
        display_in_ui=True,
    )
    form = HumanInputForm(
        tenant_id="tenant-1",
        app_id="app-1",
        workflow_run_id="run-1",
        node_id="node-1",
        form_definition=definition.model_dump_json(),
        rendered_content="rendered",
        expiration_time=expiration_time,
    )
    form.id = "form-1"
    return form


def _build_reason_model() -> WorkflowPauseReason:
    return WorkflowPauseReason(
        pause_id="pause-1",
        type_=PauseReasonType.HITL_REQUIRED,
        form_id="form-1",
        node_id="node-1",
    )


def _recipient(recipient_type: RecipientType, access_token: str) -> HumanInputFormRecipient:
    return HumanInputFormRecipient(
        form_id="form-1",
        delivery_id=f"delivery-{recipient_type.value}",
        recipient_type=recipient_type,
        recipient_payload="{}",
        access_token=access_token,
    )


def test_build_human_input_required_reason_prefers_standalone_web_app_token() -> None:
    reason = _build_human_input_required_reason(
        _build_reason_model(),
        _build_form_model(),
        [
            _recipient(RecipientType.BACKSTAGE, "btok"),
            _recipient(RecipientType.CONSOLE, "ctok"),
            _recipient(RecipientType.STANDALONE_WEB_APP, "wtok"),
        ],
    )

    assert reason.node_title == "Ask Name"
    assert reason.form_content == "rendered"
    assert reason.resolved_default_values == {"name": "Alice"}
    assert not hasattr(reason, "form_token")


def test_build_human_input_required_reason_falls_back_to_console_token() -> None:
    reason = _build_human_input_required_reason(
        _build_reason_model(),
        _build_form_model(),
        [
            _recipient(RecipientType.BACKSTAGE, "btok"),
            _recipient(RecipientType.CONSOLE, "ctok"),
        ],
    )

    assert reason.node_id == "node-1"
    assert reason.actions[0].id == "approve"
    assert not hasattr(reason, "form_token")


def test_workflow_pause_reason_from_entity_persists_hitl_type_for_dify_human_input() -> None:
    reason_model = WorkflowPauseReason.from_entity(
        pause_id="pause-1",
        pause_reason=HumanInputRequired(
            form_id="form-1",
            form_content="content",
            inputs=[],
            actions=[],
            node_id="node-1",
            node_title="Ask Name",
        ),
    )

    assert reason_model.type_ == PauseReasonType.HITL_REQUIRED
    assert reason_model.form_id == "form-1"
    assert reason_model.node_id == "node-1"


def test_workflow_pause_reason_to_entity_restores_graphon_hitl_reason() -> None:
    reason_model = WorkflowPauseReason(
        pause_id="pause-1",
        type_=PauseReasonType.HITL_REQUIRED,
        form_id="form-1",
        node_id="node-1",
    )

    reason = reason_model.to_entity()

    assert isinstance(reason, HitlRequired)
    assert reason.TYPE == PauseReasonType.HITL_REQUIRED
    assert reason.session_id == "form-1"
    assert reason.node_id == "node-1"


def test_private_workflow_pause_entity_preserves_list_shaped_pause_reasons() -> None:
    pause_reasons = [
        HumanInputRequired(
            form_id="form-1",
            form_content="content",
            inputs=[],
            actions=[],
            node_id="node-1",
            node_title="Ask Name",
        )
    ]
    pause_model = WorkflowPause(
        workflow_id="workflow-1",
        workflow_run_id="run-1",
        state_object_key="pause-state",
    )
    pause_model.id = "pause-1"
    entity = _PrivateWorkflowPauseEntity(
        pause_model=pause_model,
        reason_models=[],
        pause_reasons=pause_reasons,
    )

    result = entity.get_pause_reasons()

    assert isinstance(result, list)
    assert result == pause_reasons


def _message(*, message_id: str, app_id: str, workflow_run_id: str, conversation_id: str) -> Message:
    message = Message(
        app_id=app_id,
        conversation_id=conversation_id,
        query="query",
        message={"role": "user", "content": "query"},
        answer="answer",
        message_unit_price=Decimal("0.0001"),
        answer_unit_price=Decimal("0.0001"),
        currency="USD",
        from_source=ConversationFromSource.API,
    )
    message.id = message_id
    message._inputs = {}
    message.workflow_run_id = workflow_run_id
    return message


def _workflow_run(*, run_id: str, tenant_id: str, status: WorkflowExecutionStatus) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        tenant_id=tenant_id,
        app_id="app-1",
        workflow_id="workflow-1",
        type=WorkflowType.WORKFLOW,
        triggered_from=WorkflowRunTriggeredFrom.DEBUGGING,
        version="1",
        graph="{}",
        inputs="{}",
        status=status,
        created_by_role=CreatorUserRole.ACCOUNT,
        created_by="account-1",
    )


def test_get_message_refs_filters_by_app_and_returns_lightweight_records(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    sqlite_session.add_all(
        [
            _message(message_id="msg-1", app_id="app-1", workflow_run_id="run-1", conversation_id="conv-1"),
            _message(message_id="msg-2", app_id="app-2", workflow_run_id="run-2", conversation_id="conv-2"),
        ]
    )
    sqlite_session.commit()
    repository = DifyAPISQLAlchemyWorkflowRunRepository(session_maker=sqlite_session_factory)

    result = repository.get_message_refs(
        app_id="app-1",
        workflow_run_ids=["run-1", "run-2"],
    )

    assert result == {
        "run-1": WorkflowRunMessageRef(message_id="msg-1", conversation_id="conv-1"),
    }


def test_get_pause_record_scopes_the_workflow_run_to_the_workspace(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    sqlite_session.add(
        _workflow_run(
            run_id="run-1",
            tenant_id="tenant-1",
            status=WorkflowExecutionStatus.SUCCEEDED,
        )
    )
    sqlite_session.commit()
    repository = DifyAPISQLAlchemyWorkflowRunRepository(session_maker=sqlite_session_factory)

    assert repository.get_pause_record(workspace_id="tenant-2", workflow_run_id="run-1") is None
    assert repository.get_pause_record(
        workspace_id="tenant-1",
        workflow_run_id="run-1",
    ) == WorkflowRunPauseRecord(
        status=WorkflowExecutionStatus.SUCCEEDED,
        paused_at=None,
        reasons=(),
        form_tokens={},
    )


def test_get_pause_record_loads_reasons_and_tokens_in_one_repository_call(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    workflow_run = _workflow_run(
        run_id="run-1",
        tenant_id="tenant-1",
        status=WorkflowExecutionStatus.PAUSED,
    )
    pause = WorkflowPause(
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.id,
        state_object_key="pause-state",
    )
    pause.id = "pause-1"
    reason = WorkflowPauseReason(
        pause_id=pause.id,
        type_=PauseReasonType.HITL_REQUIRED,
        form_id="form-1",
        node_id="node-1",
    )
    recipient = HumanInputFormRecipient(
        form_id="form-1",
        delivery_id="delivery-1",
        recipient_type=RecipientType.CONSOLE,
        recipient_payload="{}",
        access_token="form-token",
    )
    sqlite_session.add_all([workflow_run, pause, reason, recipient])
    sqlite_session.commit()
    repository = DifyAPISQLAlchemyWorkflowRunRepository(session_maker=sqlite_session_factory)

    result = repository.get_pause_record(workspace_id="tenant-1", workflow_run_id="run-1")

    assert result is not None
    assert result.status == WorkflowExecutionStatus.PAUSED
    assert result.paused_at == pause.created_at
    assert len(result.reasons) == 1
    assert isinstance(result.reasons[0], HumanInputRequired)
    assert result.reasons[0].form_id == "form-1"
    assert result.form_tokens == {"form-1": "form-token"}


def test_delete_pause_model_deletes_record_when_state_object_delete_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pause_model = WorkflowPause(
        workflow_id="workflow-1",
        workflow_run_id="run-1",
        state_object_key="workflow-state.json",
    )
    pause_model.id = "pause-1"
    session = Mock(spec=Session)

    with (
        patch(
            "repositories.sqlalchemy_api_workflow_run_repository.storage.delete",
            side_effect=PermissionError("DeleteObject denied"),
        ) as delete_state_object,
        caplog.at_level(logging.ERROR, logger="repositories.sqlalchemy_api_workflow_run_repository"),
    ):
        DifyAPISQLAlchemyWorkflowRunRepository._delete_pause_model(session, pause_model)

    delete_state_object.assert_called_once_with(pause_model.state_object_key)
    session.delete.assert_called_once_with(pause_model)
    assert "pause_id=pause-1" in caplog.text
    assert "workflow_run_id=run-1" in caplog.text
    assert "object_key=workflow-state.json" in caplog.text
    assert caplog.records[-1].exc_info is not None


def seed_workflow_account_statistics(session_factory: sessionmaker[Session]) -> None:
    """Include legacy/unowned runs and intentionally mismatched ownership scopes."""
    with session_factory() as session:
        session.add_all(
            [
                App(
                    id="app-1",
                    tenant_id="tenant-1",
                    name="Workflow statistics",
                    mode=AppMode.WORKFLOW,
                    enable_site=True,
                    enable_api=True,
                ),
                App(
                    id="app-2",
                    tenant_id="tenant-2",
                    name="Other workspace",
                    mode=AppMode.WORKFLOW,
                    enable_site=True,
                    enable_api=True,
                ),
            ]
        )
        cases = [
            # id, actor, terminal, tokens, run tenant/app, owner tenant/app, day, source
            ("a1", "account-1", "shared-user", 10, "tenant-1", "app-1", "tenant-1", "app-1", 2, "app-run"),
            ("a2", "account-1", "shared-user", 10, "tenant-1", "app-1", "tenant-1", "app-1", 2, "app-run"),
            ("b", "account-2", "shared-user", 30, "tenant-1", "app-1", "tenant-1", "app-1", 2, "app-run"),
            ("missing", None, "legacy", 40, "tenant-1", "app-1", "tenant-1", "app-1", 2, "app-run"),
            ("null", None, "anonymous", 50, "tenant-1", "app-1", "tenant-1", "app-1", 2, "app-run"),
            ("bad-tenant", "account-1", "wrong-tenant", 60, "tenant-1", "app-1", "tenant-2", "app-1", 2, "app-run"),
            ("bad-app", "account-1", "wrong-app", 70, "tenant-1", "app-1", "tenant-1", "app-2", 2, "app-run"),
            ("other-tenant", "account-1", "other", 80, "tenant-2", "app-1", "tenant-2", "app-1", 2, "app-run"),
            ("other-app", "account-1", "other", 90, "tenant-1", "app-2", "tenant-1", "app-2", 2, "app-run"),
            ("debug", "account-1", "debug", 100, "tenant-1", "app-1", "tenant-1", "app-1", 2, "debugging"),
            ("before", "account-1", "shared-user", 110, "tenant-1", "app-1", "tenant-1", "app-1", 1, "app-run"),
            ("end", "account-1", "shared-user", 120, "tenant-1", "app-1", "tenant-1", "app-1", 3, "app-run"),
        ]
        for run_id, actor, terminal, tokens, tenant, app_id, owner_tenant, owner_app, day, source in cases:
            run = _workflow_run(run_id=run_id, tenant_id=tenant, status=WorkflowExecutionStatus.SUCCEEDED)
            run.app_id = app_id
            run.created_by_role = CreatorUserRole.END_USER
            run.created_by = terminal
            run.created_at = datetime(2024, 1, day)
            run.finished_at = None if run_id == "a2" else datetime(2024, 1, day, 1)
            run.total_tokens = tokens
            run.triggered_from = WorkflowRunTriggeredFrom(source)
            session.add(run)
            if run_id != "missing":
                session.add(
                    WorkflowRunAccountExtend(
                        workflow_run_id=run_id, tenant_id=owner_tenant, app_id=owner_app, from_account_id=actor
                    )
                )
        session.commit()


def sqlite_workflow_statistic_repository(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> DifyAPISQLAlchemyWorkflowRunRepository:
    """Adapt only SQLite date/AVG result types; execute production filtering SQL."""
    monkeypatch.setattr(statistic_repository_module, "convert_datetime_to_date", lambda field: f"DATE({field})")

    @contextmanager
    def statistic_session() -> Generator[SimpleNamespace, None, None]:
        with session_factory() as session:

            def execute(statement: TextClause, parameters: dict[str, object]) -> list[SimpleNamespace]:
                result = session.execute(statement, parameters)
                if "AVG(sub.interactions)" in str(statement):
                    return [
                        SimpleNamespace(date=row.date, interactions=Decimal(str(row.interactions))) for row in result
                    ]
                return [SimpleNamespace(**{str(key): value for key, value in row._mapping.items()}) for row in result]

            yield SimpleNamespace(execute=execute)

    # This test adapter implements the context-manager/execute subset used by
    # these raw SQL methods and preserves the production PostgreSQL AVG type.
    return DifyAPISQLAlchemyWorkflowRunRepository(session_maker=cast(sessionmaker[Session], statistic_session))


@pytest.mark.parametrize(
    ("method", "metric", "expected_a", "expected_b", "expected_all"),
    [
        ("get_daily_runs_statistics", "runs", 2, 1, 7),
        ("get_daily_token_cost_statistics", "token_count", 20, 30, 270),
        ("get_average_app_interaction_statistics", "interactions", 2.0, 1.0, 1.4),
    ],
)
def test_workflow_statistics_account_scope(
    sqlite_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    metric: str,
    expected_a: float,
    expected_b: float,
    expected_all: float,
) -> None:
    seed_workflow_account_statistics(sqlite_session_factory)
    repository = sqlite_workflow_statistic_repository(sqlite_session_factory, monkeypatch)
    query = getattr(repository, method)
    arguments = {
        "tenant_id": "tenant-1",
        "app_id": "app-1",
        "triggered_from": WorkflowRunTriggeredFrom.APP_RUN,
        "start_date": datetime(2024, 1, 2),
        "end_date": datetime(2024, 1, 3),
    }
    for actor, expected in [("account-1", expected_a), ("account-2", expected_b), (None, expected_all)]:
        assert query(**arguments, account_id=actor) == [{"date": "2024-01-02", metric: expected}]
    assert query(**arguments) == [{"date": "2024-01-02", metric: expected_all}]
    assert query(**arguments, account_id="unknown") == []
    assert query(**arguments, account_id="account-1' OR '1'='1") == []
    arguments.pop("start_date")
    arguments.pop("end_date")
    assert sorted(row["date"] for row in query(**arguments, account_id="account-1")) == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]
    assert [row["date"] for row in query(**arguments, account_id="account-2")] == ["2024-01-02"]


def test_workflow_daily_terminals_remain_app_wide(
    sqlite_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_workflow_account_statistics(sqlite_session_factory)
    repository = sqlite_workflow_statistic_repository(sqlite_session_factory, monkeypatch)
    assert repository.get_daily_terminals_statistics(
        tenant_id="tenant-1",
        app_id="app-1",
        triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        start_date=datetime(2024, 1, 2),
        end_date=datetime(2024, 1, 3),
    ) == [{"date": "2024-01-02", "terminal_count": 5}]
