from datetime import UTC, datetime
from decimal import Decimal
from typing import cast, override

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session, sessionmaker

from core.app.entities.app_invoke_entities import InvokeFrom
from models.enums import ConversationFromSource
from models.model import App, AppMode, Conversation, Message
from repositories import app_statistic_query_repository as repository_module
from repositories.app_statistic_query_repository import AppStatisticQueryRepository
from services.app_statistic_query import (
    AverageResponseTimeStatisticRecord,
    AverageSessionInteractionStatisticRecord,
    DailyConversationStatisticRecord,
    DailyMessageStatisticRecord,
    DailyTerminalStatisticRecord,
    DailyTokenCostStatisticRecord,
    TokensPerSecondStatisticRecord,
    UserSatisfactionRateStatisticRecord,
)


class _RecordingRepository(AppStatisticQueryRepository):
    def __init__(self) -> None:
        super().__init__(session_factory=cast(sessionmaker[Session], object()))
        self.rows: tuple[RowMapping, ...] = ()
        self.calls: list[tuple[str, dict[str, object]]] = []

    @override
    def _execute(self, sql_query: str, parameters: dict[str, object]) -> tuple[RowMapping, ...]:
        self.calls.append((sql_query, parameters.copy()))
        return self.rows


def _row(**values: object) -> RowMapping:
    return cast(RowMapping, values)


def test_app_statistic_repository_maps_results_and_preserves_query_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "convert_datetime_to_date", lambda field: field)
    repository = _RecordingRepository()
    start_date = datetime(2024, 1, 1, tzinfo=UTC)
    end_date = datetime(2024, 1, 2, tzinfo=UTC)

    repository.rows = (_row(date="2024-01-01", message_count=2),)
    assert repository.get_daily_messages(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (DailyMessageStatisticRecord(date="2024-01-01", message_count=2),)

    repository.rows = (_row(date="2024-01-01", conversation_count=3),)
    assert repository.get_daily_conversations(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (DailyConversationStatisticRecord(date="2024-01-01", conversation_count=3),)

    repository.rows = (_row(date="2024-01-01", terminal_count=4),)
    assert repository.get_daily_terminals(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (DailyTerminalStatisticRecord(date="2024-01-01", terminal_count=4),)

    repository.rows = (_row(date="2024-01-01", token_count=Decimal(5), total_price=Decimal("0.25")),)
    assert repository.get_daily_token_costs(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (
        DailyTokenCostStatisticRecord(
            date="2024-01-01",
            token_count=5,
            total_price=Decimal("0.25"),
            currency="USD",
        ),
    )

    repository.rows = (_row(date="2024-01-01", interactions=Decimal("2.345")),)
    assert repository.get_average_session_interactions(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (AverageSessionInteractionStatisticRecord(date="2024-01-01", interactions=2.34),)

    repository.rows = (_row(date="2024-01-01", message_count=10, feedback_count=1),)
    assert repository.get_user_satisfaction_rates(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (UserSatisfactionRateStatisticRecord(date="2024-01-01", rate=100.0),)

    repository.rows = (_row(date="2024-01-01", latency=1.234),)
    assert repository.get_average_response_times(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (AverageResponseTimeStatisticRecord(date="2024-01-01", latency=1234.0),)

    repository.rows = (_row(date="2024-01-01", tokens_per_second=15.55555),)
    assert repository.get_tokens_per_second(
        app_id="app-1",
        start_date=start_date,
        end_date=end_date,
        timezone="Asia/Shanghai",
    ) == (TokensPerSecondStatisticRecord(date="2024-01-01", tps=15.5556),)

    assert len(repository.calls) == 8
    for sql_query, parameters in repository.calls:
        assert "created_at >= :start_date" in sql_query
        assert "created_at < :end_date" in sql_query
        assert parameters == {
            "tz": "Asia/Shanghai",
            "app_id": "app-1",
            "excluded_invoke_from": InvokeFrom.DEBUGGER,
            "start_date": start_date,
            "end_date": end_date,
        }


def test_daily_messages_omit_time_range_when_not_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "convert_datetime_to_date", lambda field: field)
    repository = _RecordingRepository()

    assert (
        repository.get_daily_messages(
            app_id="app-1",
            start_date=None,
            end_date=None,
            timezone="Asia/Shanghai",
        )
        == ()
    )

    sql_query, parameters = repository.calls[0]
    assert ":start_date" not in sql_query
    assert ":end_date" not in sql_query
    assert parameters == {
        "tz": "Asia/Shanghai",
        "app_id": "app-1",
        "excluded_invoke_from": InvokeFrom.DEBUGGER,
    }


class SQLiteStatisticRepository(AppStatisticQueryRepository):
    """Execute production SQL with decimal result types matching PostgreSQL's aggregates."""

    @override
    def _execute(self, sql_query: str, parameters: dict[str, object]) -> tuple[RowMapping, ...]:
        statement = sa.text(sql_query)
        if "AS interactions" in sql_query:
            statement = statement.columns(interactions=sa.Numeric())
        elif "AS total_price" in sql_query:
            statement = statement.columns(total_price=sa.Numeric())
        with self._session_factory() as session:
            return tuple(session.execute(statement, parameters).mappings())


def seed_account_statistics(session_factory: sessionmaker[Session]) -> None:
    """Two accounts, unattributed history, debugger traffic, another app and another day."""
    with session_factory() as session:
        session.add_all(
            App(
                id=app_id,
                tenant_id=tenant_id,
                name=app_id,
                mode=AppMode.CHAT,
                enable_site=True,
                enable_api=True,
            )
            for app_id, tenant_id in [("app-1", "tenant-1"), ("app-2", "tenant-2")]
        )
        for label, account_id, count, app_id, invoke_from, day in [
            ("a", "account-1", 2, "app-1", InvokeFrom.EXPLORE, 1),
            ("b", "account-2", 4, "app-1", InvokeFrom.EXPLORE, 1),
            ("history", None, 6, "app-1", InvokeFrom.WEB_APP, 1),
            ("debug", "account-1", 8, "app-1", InvokeFrom.DEBUGGER, 1),
            ("foreign", "account-1", 10, "app-2", InvokeFrom.EXPLORE, 1),
            ("later", "account-1", 1, "app-1", InvokeFrom.EXPLORE, 2),
        ]:
            created_at = datetime(2024, 1, day)
            session.add(
                Conversation(
                    id=label,
                    app_id=app_id,
                    mode=AppMode.CHAT,
                    name=label,
                    inputs={},
                    from_source=ConversationFromSource.CONSOLE,
                    from_account_id=account_id,
                    invoke_from=invoke_from,
                    created_at=created_at,
                )
            )
            session.flush()
            for index in range(count):
                session.add(
                    Message(
                        id=f"{label}-{index}",
                        app_id=app_id,
                        conversation_id=label,
                        inputs={},
                        query="question",
                        message={},
                        answer="answer",
                        message_tokens=1,
                        answer_tokens=2,
                        message_unit_price=Decimal(0),
                        answer_unit_price=Decimal(0),
                        total_price=Decimal("0.25"),
                        currency="USD",
                        from_source=ConversationFromSource.CONSOLE,
                        from_account_id=account_id,
                        invoke_from=invoke_from,
                        created_at=created_at,
                    )
                )
        session.commit()


@pytest.mark.parametrize(
    ("account_id", "count", "conversations"), [("account-1", 2, 1), ("account-2", 4, 1), (None, 12, 3)]
)
def test_personal_statistics_isolate_accounts_and_preserve_app_wide_results(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_session_factory: sessionmaker[Session],
    account_id: str | None,
    count: int,
    conversations: int,
) -> None:
    monkeypatch.setattr(repository_module, "convert_datetime_to_date", lambda field: f"DATE({field})")
    seed_account_statistics(sqlite_session_factory)
    repository = SQLiteStatisticRepository(session_factory=sqlite_session_factory)
    arguments = {
        "app_id": "app-1",
        "timezone": "UTC",
        "start_date": datetime(2024, 1, 1),
        "end_date": datetime(2024, 1, 2),
        "account_id": account_id,
    }

    assert repository.get_daily_conversations(**arguments) == (
        DailyConversationStatisticRecord("2024-01-01", conversations),
    )
    assert repository.get_daily_token_costs(**arguments) == (
        DailyTokenCostStatisticRecord("2024-01-01", count * 3, Decimal("0.25") * count, "USD"),
    )
    assert repository.get_average_session_interactions(**arguments) == (
        AverageSessionInteractionStatisticRecord("2024-01-01", count / conversations),
    )


@pytest.mark.parametrize(
    "method",
    ["get_daily_conversations", "get_daily_token_costs", "get_average_session_interactions"],
)
def test_personal_statistics_all_time_and_unknown_account(
    monkeypatch: pytest.MonkeyPatch, sqlite_session_factory: sessionmaker[Session], method: str
) -> None:
    monkeypatch.setattr(repository_module, "convert_datetime_to_date", lambda field: f"DATE({field})")
    seed_account_statistics(sqlite_session_factory)
    repository = SQLiteStatisticRepository(session_factory=sqlite_session_factory)
    query = getattr(repository, method)
    arguments = {"app_id": "app-1", "timezone": "UTC", "start_date": None, "end_date": None}

    assert [row.date for row in query(**arguments, account_id="account-1")] == ["2024-01-01", "2024-01-02"]
    assert [row.date for row in query(**arguments, account_id="account-2")] == ["2024-01-01"]
    assert query(**arguments, account_id="unknown") == ()
