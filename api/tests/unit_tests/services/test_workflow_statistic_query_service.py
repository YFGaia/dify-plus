from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from machinery.context import RequestContext
from models.enums import WorkflowRunTriggeredFrom
from repositories.api_workflow_run_repository import APIWorkflowRunRepository
from services.workflow_statistic_query_service import WorkflowStatisticQueryService


def _request_context() -> RequestContext:
    return RequestContext(
        request_id="request-1",
        trace_id="trace-1",
        account_id="account-1",
        active_workspace_id="workspace-1",
    )


def test_workflow_statistic_queries_delegate_to_workflow_run_repository() -> None:
    workflow_runs = MagicMock(spec=APIWorkflowRunRepository)
    workflow_runs.get_daily_runs_statistics.return_value = [{"date": "2024-01-01", "runs": 2}]
    workflow_runs.get_daily_terminals_statistics.return_value = [{"date": "2024-01-01", "terminal_count": 3}]
    workflow_runs.get_daily_token_cost_statistics.return_value = [{"date": "2024-01-01", "token_count": 4}]
    workflow_runs.get_average_app_interaction_statistics.return_value = [{"date": "2024-01-01", "interactions": 2.5}]
    service = WorkflowStatisticQueryService(workflow_runs=workflow_runs)
    context = _request_context()
    start_date = datetime(2024, 1, 1, tzinfo=UTC)
    end_date = datetime(2024, 1, 2, tzinfo=UTC)

    assert service.get_daily_runs(
        context,
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == [{"date": "2024-01-01", "runs": 2}]
    assert service.get_daily_terminals(
        context,
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == [{"date": "2024-01-01", "terminal_count": 3}]
    assert service.get_daily_token_costs(
        context,
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == [{"date": "2024-01-01", "token_count": 4}]
    assert service.get_average_app_interactions(
        context,
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == [{"date": "2024-01-01", "interactions": 2.5}]

    expected_arguments = {
        "tenant_id": "workspace-1",
        "app_id": "app-1",
        "triggered_from": WorkflowRunTriggeredFrom.APP_RUN,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": "Asia/Shanghai",
    }
    workflow_runs.get_daily_runs_statistics.assert_called_once_with(**expected_arguments, account_id=None)
    workflow_runs.get_daily_terminals_statistics.assert_called_once_with(**expected_arguments)
    workflow_runs.get_daily_token_cost_statistics.assert_called_once_with(**expected_arguments, account_id=None)
    workflow_runs.get_average_app_interaction_statistics.assert_called_once_with(**expected_arguments, account_id=None)


@pytest.mark.parametrize("account_id", ["account-1", "account-2"])
@pytest.mark.parametrize("account", [True, False])
@pytest.mark.parametrize(
    ("method", "repository_method"),
    [
        ("get_daily_runs", "get_daily_runs_statistics"),
        ("get_daily_token_costs", "get_daily_token_cost_statistics"),
        ("get_average_app_interactions", "get_average_app_interaction_statistics"),
    ],
)
def test_account_scope_uses_context(method: str, repository_method: str, account: bool, account_id: str) -> None:
    repository = MagicMock(spec=APIWorkflowRunRepository)
    context = _request_context()._replace(account_id=account_id)
    service = WorkflowStatisticQueryService(workflow_runs=repository)
    getattr(service, method)(context, app_id="app-1", start_date=None, end_date=None, timezone="UTC", account=account)
    getattr(repository, repository_method).assert_called_once_with(
        tenant_id="workspace-1",
        app_id="app-1",
        triggered_from=WorkflowRunTriggeredFrom.APP_RUN,
        start_date=None,
        end_date=None,
        timezone="UTC",
        account_id=account_id if account else None,
    )
