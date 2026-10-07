from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import Forbidden

from controllers.console import flask_admission
from controllers.console.app import workflow_statistic as statistic_module
from controllers.console.app import wraps as app_wraps
from libs.login import AccountWithTenant
from models.account import Account
from models.model import App, AppMode
from services.workflow_statistic_query_service import WorkflowStatisticQueryService
from tests.unit_tests.repositories.test_sqlalchemy_api_workflow_run_repository import (
    seed_workflow_account_statistics,
    sqlite_workflow_statistic_repository,
)

ACCOUNT_RESOURCES = (
    statistic_module.WorkflowDailyRunsStatistic,
    statistic_module.WorkflowDailyTokenCostStatistic,
    statistic_module.WorkflowAverageAppInteractionStatistic,
)


@pytest.mark.parametrize(
    ("resource_type", "metric", "expected_a", "expected_b", "expected_all"),
    [
        (statistic_module.WorkflowDailyRunsStatistic, "runs", 2, 1, 7),
        (statistic_module.WorkflowDailyTokenCostStatistic, "token_count", 20, 30, 270),
        (statistic_module.WorkflowAverageAppInteractionStatistic, "interactions", 2.0, 1.0, 1.4),
        (statistic_module.WorkflowDailyTerminalsStatistic, "terminal_count", 5, 5, 5),
    ],
)
def test_workflow_statistics_http_account_isolation(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_session_factory: sessionmaker[Session],
    resource_type: type,
    metric: str,
    expected_a: float,
    expected_b: float,
    expected_all: float,
) -> None:
    """Exercise admission, app guard, parsing, service, SQL and serialization.

    Supply the signed-in actor at the authentication boundary. Setup, login and
    CSRF wrappers are outside this unit test; context injection and RBAC dispatch
    remain active, with the RBAC decision supplied by a mock.
    """
    seed_workflow_account_statistics(sqlite_session_factory)
    repository = sqlite_workflow_statistic_repository(sqlite_session_factory, monkeypatch)
    statistics = WorkflowStatisticQueryService(workflow_runs=repository)
    monkeypatch.setattr(
        statistic_module, "application_services", lambda: SimpleNamespace(workflow_statistics=statistics)
    )
    account = Account(name="Statistics user", email="statistics@example.test", timezone="UTC")
    account.id = "account-1"
    identity = AccountWithTenant(account=account, tenant_id="tenant-1")
    monkeypatch.setattr(flask_admission, "current_account_with_tenant", lambda: identity)
    monkeypatch.setattr(statistic_module, "current_account_with_tenant", lambda: identity)
    monkeypatch.setattr(app_wraps, "current_account_with_tenant", lambda: identity)
    rbac = MagicMock()
    monkeypatch.setattr(flask_admission, "enforce_rbac_checks", rbac)

    def load_app(app_id: str) -> App | None:
        with sqlite_session_factory() as session:
            return app_wraps._load_app_model(session, app_id)

    monkeypatch.setattr(app_wraps, "_load_app_model_from_scoped_session", load_app)
    method = resource_type.get
    while method.__code__.co_name != "inject_request_context":
        method = method.__wrapped__
    http_app = Flask(__name__)
    http_app.add_url_rule("/statistics/<app_id>", view_func=lambda app_id: method(resource_type(), app_id=app_id))
    client = http_app.test_client()
    date_range = {"start": "2024-01-02 00:00", "end": "2024-01-03 00:00"}

    for account_id, expected in [("account-1", expected_a), ("account-2", expected_b)]:
        account.id = account_id
        response = client.get("/statistics/app-1", query_string={**date_range, "account": "true"})
        assert response.status_code == 200
        assert response.json == {"data": [{"date": "2024-01-02", metric: expected}]}
        assert rbac.call_args.kwargs["account_id"] == account_id
        assert rbac.call_args.kwargs["tenant_id"] == "tenant-1"
        assert rbac.call_args.kwargs["checks"][0].scene == statistic_module.RBACPermission.APP_MONITOR

        for query in [date_range, {**date_range, "account": "false"}]:
            response = client.get("/statistics/app-1", query_string=query)
            assert response.status_code == 200
            assert response.json == {"data": [{"date": "2024-01-02", metric: expected_all}]}

    if resource_type in ACCOUNT_RESOURCES:
        scopes: list[dict[str, str]] = [{}, {"account": "false"}, {"account": "true"}]
        for forged_field in ["account_id", "from_account_id"]:
            for scope in scopes:
                response = client.get("/statistics/app-1", query_string={**scope, forged_field: "account-1"})
                assert response.status_code == 422
        assert client.get("/statistics/app-1?account=invalid").status_code == 422
        account.id = "unknown"
        assert client.get("/statistics/app-1?account=true").json == {"data": []}

    assert client.get("/statistics/app-1?start=invalid").status_code == 400
    assert client.get("/statistics/app-2?account=true").status_code == 404
    assert client.get("/statistics/missing?account=true").status_code == 404
    rbac.side_effect = Forbidden()
    assert client.get("/statistics/app-1?account=true").status_code == 403
    rbac.side_effect = None
    if resource_type is statistic_module.WorkflowAverageAppInteractionStatistic:
        with sqlite_session_factory() as session:
            session.query(App).filter_by(id="app-1").update({"mode": AppMode.COMPLETION})
            session.commit()
        assert client.get("/statistics/app-1?account=true").status_code == 404


def test_account_query_documentation_is_limited_to_supported_charts() -> None:
    assert "account" not in statistic_module.WorkflowStatisticQuery.model_fields
    for resource_type in [*ACCOUNT_RESOURCES, statistic_module.WorkflowDailyTerminalsStatistic]:
        params = resource_type.get.__apidoc__["params"]
        assert ("account" in params) == (resource_type in ACCOUNT_RESOURCES)
        assert "account_id" not in params
        assert "from_account_id" not in params
        if resource_type in ACCOUNT_RESOURCES:
            assert params["account"]["in"] == "query"
            assert params["account"]["type"] == "boolean"
