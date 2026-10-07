from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from inspect import unwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import BadRequest, Forbidden

from controllers.console import flask_admission
from controllers.console.app import statistic as statistic_module
from controllers.console.app import wraps as app_wraps
from libs.login import AccountWithTenant
from machinery.context import RequestContext
from models.model import App, AppMode
from repositories import app_statistic_query_repository as repository_module
from services.app_statistic_query import (
    AppStatisticQuery,
    AverageResponseTimeStatisticRecord,
    AverageSessionInteractionStatisticRecord,
    DailyConversationStatisticRecord,
    DailyMessageStatisticRecord,
    DailyTerminalStatisticRecord,
    DailyTokenCostStatisticRecord,
    TokensPerSecondStatisticRecord,
    UserSatisfactionRateStatisticRecord,
)
from tests.unit_tests.repositories.test_app_statistic_query_repository import (
    SQLiteStatisticRepository,
    seed_account_statistics,
)

ACCOUNT_RESOURCES = (
    statistic_module.DailyConversationStatistic,
    statistic_module.DailyTokenCostStatistic,
    statistic_module.AverageSessionInteractionStatistic,
)


def _request_context() -> RequestContext:
    return RequestContext(
        request_id="request-1",
        trace_id="trace-1",
        account_id="account-1",
        active_workspace_id="tenant-1",
    )


def _app_model() -> App:
    return App(id="app-1", tenant_id="tenant-1", name="Statistics App")


def _install_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    statistics: MagicMock,
    *,
    time_range: tuple[datetime | None, datetime | None] = (None, None),
) -> None:
    monkeypatch.setattr(
        statistic_module,
        "application_services",
        lambda: SimpleNamespace(app_statistics=statistics),
    )
    monkeypatch.setattr(
        statistic_module,
        "current_account_with_tenant",
        lambda: SimpleNamespace(account=SimpleNamespace(timezone="UTC")),
    )
    monkeypatch.setattr(statistic_module, "parse_time_range", lambda *_args, **_kwargs: time_range)


def _invoke(
    app: Flask,
    resource_type: type,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    resource = resource_type()
    query_type = (
        statistic_module.AccountStatisticTimeRangeQuery
        if resource_type in ACCOUNT_RESOURCES
        else statistic_module.StatisticTimeRangeQuery
    )
    method = unwrap(resource.get)
    with app.test_request_context("/console/api/apps/app-1/statistics", method="GET"):
        response = method(
            resource,
            query_type(start=start, end=end),
            _request_context(),
            app_model=_app_model(),
        )
    return response if isinstance(response, dict) else response.get_json()


@pytest.mark.parametrize(
    ("resource_type", "query_call_getter", "record", "expected"),
    [
        pytest.param(
            statistic_module.DailyMessageStatistic,
            lambda query: query.get_daily_messages,
            DailyMessageStatisticRecord(date="2024-01-01", message_count=3),
            {"date": "2024-01-01", "message_count": 3},
            id="daily-messages",
        ),
        pytest.param(
            statistic_module.DailyConversationStatistic,
            lambda query: query.get_daily_conversations,
            DailyConversationStatisticRecord(date="2024-01-02", conversation_count=5),
            {"date": "2024-01-02", "conversation_count": 5},
            id="daily-conversations",
        ),
        pytest.param(
            statistic_module.DailyTerminalsStatistic,
            lambda query: query.get_daily_terminals,
            DailyTerminalStatisticRecord(date="2024-01-03", terminal_count=7),
            {"date": "2024-01-03", "terminal_count": 7},
            id="daily-terminals",
        ),
        pytest.param(
            statistic_module.DailyTokenCostStatistic,
            lambda query: query.get_daily_token_costs,
            DailyTokenCostStatisticRecord(
                date="2024-01-04",
                token_count=10,
                total_price=Decimal("0.25"),
                currency="USD",
            ),
            {"date": "2024-01-04", "token_count": 10, "total_price": "0.25", "currency": "USD"},
            id="daily-token-costs",
        ),
        pytest.param(
            statistic_module.AverageSessionInteractionStatistic,
            lambda query: query.get_average_session_interactions,
            AverageSessionInteractionStatisticRecord(date="2024-01-05", interactions=2.5),
            {"date": "2024-01-05", "interactions": 2.5},
            id="average-session-interactions",
        ),
        pytest.param(
            statistic_module.UserSatisfactionRateStatistic,
            lambda query: query.get_user_satisfaction_rates,
            UserSatisfactionRateStatisticRecord(date="2024-01-06", rate=100.0),
            {"date": "2024-01-06", "rate": 100.0},
            id="user-satisfaction-rate",
        ),
        pytest.param(
            statistic_module.AverageResponseTimeStatistic,
            lambda query: query.get_average_response_times,
            AverageResponseTimeStatisticRecord(date="2024-01-07", latency=1234.0),
            {"date": "2024-01-07", "latency": 1234.0},
            id="average-response-time",
        ),
        pytest.param(
            statistic_module.TokensPerSecondStatistic,
            lambda query: query.get_tokens_per_second,
            TokensPerSecondStatisticRecord(date="2024-01-08", tps=15.5),
            {"date": "2024-01-08", "tps": 15.5},
            id="tokens-per-second",
        ),
    ],
)
def test_statistic_endpoint_delegates_to_statistic_query(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    resource_type: type,
    query_call_getter: Callable[[MagicMock], MagicMock],
    record: tuple,
    expected: dict[str, Any],
) -> None:
    statistics = MagicMock(spec=AppStatisticQuery)
    query_call = query_call_getter(statistics)
    query_call.return_value = [record]
    _install_dependencies(monkeypatch, statistics)

    assert _invoke(app, resource_type) == {"data": [expected]}
    query_call.assert_called_once_with(
        app_id="app-1",
        start_date=None,
        end_date=None,
        timezone="UTC",
        **({"account_id": None} if resource_type in ACCOUNT_RESOURCES else {}),
    )


def test_statistic_endpoint_passes_time_range(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    statistics = MagicMock(spec=AppStatisticQuery)
    statistics.get_daily_messages.return_value = []
    start_date = datetime(2024, 1, 1, tzinfo=UTC)
    end_date = datetime(2024, 1, 2, tzinfo=UTC)
    _install_dependencies(monkeypatch, statistics, time_range=(start_date, end_date))

    assert _invoke(app, statistic_module.DailyMessageStatistic, start="start", end="end") == {"data": []}
    statistics.get_daily_messages.assert_called_once_with(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="UTC",
    )


def test_statistic_endpoint_rejects_invalid_time_range(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    statistics = MagicMock(spec=AppStatisticQuery)
    _install_dependencies(monkeypatch, statistics)
    monkeypatch.setattr(
        statistic_module,
        "parse_time_range",
        MagicMock(side_effect=ValueError("Invalid time range")),
    )

    with pytest.raises(BadRequest, match="Invalid time range"):
        _invoke(app, statistic_module.DailyMessageStatistic)

    statistics.get_daily_messages.assert_not_called()


@pytest.mark.parametrize(
    ("resource_type", "metric", "expected_a", "expected_b", "expected_all"),
    [
        (statistic_module.DailyConversationStatistic, "conversation_count", 1, 1, 3),
        (statistic_module.DailyTokenCostStatistic, "token_count", 6, 12, 36),
        (statistic_module.AverageSessionInteractionStatistic, "interactions", 2.0, 4.0, 4.0),
    ],
)
def test_account_statistics_http_isolation(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_session_factory: sessionmaker[Session],
    resource_type: type,
    metric: str,
    expected_a: float,
    expected_b: float,
    expected_all: float,
) -> None:
    """Exercise admission context, app/type guard, GET validation, SQL and serialization.

    The signed-in account is supplied at the authentication boundary; login/setup
    wrappers are outside this unit test. Caller query IDs cannot override it.
    """
    seed_account_statistics(sqlite_session_factory)
    repository = SQLiteStatisticRepository(session_factory=sqlite_session_factory)
    monkeypatch.setattr(repository_module, "convert_datetime_to_date", lambda field: f"DATE({field})")
    monkeypatch.setattr(statistic_module, "application_services", lambda: SimpleNamespace(app_statistics=repository))
    account = SimpleNamespace(id="account-1", timezone="UTC")
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
    date_range = {"start": "2024-01-01 00:00", "end": "2024-01-02 00:00"}

    for account_id, expected in [("account-1", expected_a), ("account-2", expected_b)]:
        account.id = account_id
        response = client.get(
            "/statistics/app-1",
            query_string={
                **date_range,
                "account": "true",
                "account_id": "account-2" if account_id == "account-1" else "account-1",
                "from_account_id": "caller-selected",
                "external_user_id": "enterpriseuser",
            },
        )
        assert response.status_code == 200
        assert response.json["data"][0][metric] == expected
        assert len(response.json["data"]) == 1
        assert rbac.call_args.kwargs["account_id"] == account_id
        assert rbac.call_args.kwargs["tenant_id"] == "tenant-1"
        assert rbac.call_args.kwargs["checks"][0].scene == statistic_module.RBACPermission.APP_MONITOR

        for query in [date_range, {**date_range, "account": "false"}]:
            response = client.get("/statistics/app-1", query_string=query)
            assert response.status_code == 200
            assert response.json["data"][0][metric] == expected_all

    account.id = "account-1"
    response = client.get("/statistics/app-1", query_string={"account": "true"})
    assert [row["date"] for row in response.json["data"]] == ["2024-01-01", "2024-01-02"]
    account.id = "account-2"
    response = client.get("/statistics/app-1", query_string={"account": "true"})
    assert [row["date"] for row in response.json["data"]] == ["2024-01-01"]
    assert client.get("/statistics/app-1?account=invalid").status_code == 422
    assert client.get("/statistics/app-2?account=true").status_code == 404
    rbac.side_effect = Forbidden()
    assert client.get("/statistics/app-1?account=true").status_code == 403
    rbac.side_effect = None
    if resource_type is statistic_module.AverageSessionInteractionStatistic:
        with sqlite_session_factory() as session:
            session.query(App).filter_by(id="app-1").update({"mode": AppMode.COMPLETION})
            session.commit()
        assert client.get("/statistics/app-1?account=true").status_code == 404


def test_account_query_documentation_is_limited_to_supported_charts() -> None:
    assert "account" not in statistic_module.StatisticTimeRangeQuery.model_fields
    for resource_type in [
        *ACCOUNT_RESOURCES,
        statistic_module.DailyMessageStatistic,
        statistic_module.DailyTerminalsStatistic,
        statistic_module.UserSatisfactionRateStatistic,
        statistic_module.AverageResponseTimeStatistic,
        statistic_module.TokensPerSecondStatistic,
    ]:
        params = resource_type.get.__apidoc__["params"]
        assert ("account" in params) == (resource_type in ACCOUNT_RESOURCES)
        if resource_type in ACCOUNT_RESOURCES:
            assert params["account"]["in"] == "query"
            assert params["account"]["type"] == "boolean"
