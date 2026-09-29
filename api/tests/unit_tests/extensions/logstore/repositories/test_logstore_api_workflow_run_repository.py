import datetime
import sqlite3
import time
from collections.abc import Generator
from unittest.mock import MagicMock

import pytest

from extensions.logstore.aliyun_logstore import AliyunLogStore
from extensions.logstore.repositories.logstore_api_workflow_run_repository import (
    LogstoreAPIWorkflowRunRepository,
    _dict_to_workflow_run,
)

_START = datetime.datetime(2026, 8, 18, 2, 0, 0, tzinfo=datetime.UTC)
_FINISH = _START + datetime.timedelta(seconds=30)
_EXPECTED_CREATED_AT = _START.replace(tzinfo=None)
_EXPECTED_FINISHED_AT = _FINISH.replace(tzinfo=None)

_BASE: dict[str, object] = {"id": "run-1", "tenant_id": "tenant-1", "app_id": "app-1", "workflow_id": "workflow-1"}


@pytest.fixture
def non_utc_host_timezone(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Run the host clock in UTC+05:30 so local-time conversions become observable."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("both epoch", {"started_at": _START.timestamp(), "finished_at": _FINISH.timestamp()}),
        ("aware iso and epoch", {"started_at": _START.isoformat(), "finished_at": _FINISH.timestamp()}),
        (
            "naive iso and epoch",
            {"started_at": _START.replace(tzinfo=None).isoformat(), "finished_at": _FINISH.timestamp()},
        ),
        ("both datetime", {"started_at": _START, "finished_at": _FINISH}),
    ],
)
@pytest.mark.usefixtures("non_utc_host_timezone")
def test_dict_to_workflow_run_normalizes_timestamps_to_naive_utc(case: str, payload: dict[str, object]) -> None:
    model = _dict_to_workflow_run({**_BASE, **payload})

    assert model.created_at == _EXPECTED_CREATED_AT, case
    assert model.finished_at == _EXPECTED_FINISHED_AT, case
    assert model.elapsed_time == 30.0, case


@pytest.mark.usefixtures("non_utc_host_timezone")
def test_dict_to_workflow_run_defaults_missing_started_at_to_naive_utc_now() -> None:
    model = _dict_to_workflow_run({**_BASE, "finished_at": _FINISH.timestamp()})

    assert model.created_at.tzinfo is None
    # A naive local-time default would sit 5h30m ahead of UTC and drive elapsed_time negative.
    assert abs((model.created_at - datetime.datetime.now(tz=datetime.UTC).replace(tzinfo=None)).total_seconds()) < 60


@pytest.fixture
def statistic_repository() -> Generator[tuple[LogstoreAPIWorkflowRunRepository, MagicMock], None, None]:
    """Execute emitted aggregation SQL locally; only adapt SLS timestamp functions.

    This proves predicate/aggregation behavior, not Aliyun index visibility or
    service dialect compatibility. Missing actor fields materialize as SQL NULL.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "from_unixtime", 1, lambda epoch: datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat()
    )
    connection.create_function("from_iso8601_timestamp", 1, lambda value: value)
    connection.create_function("to_unixtime", 1, lambda value: datetime.datetime.fromisoformat(value).timestamp())
    connection.execute(f"""CREATE TABLE {AliyunLogStore.workflow_execution_logstore} (
        id TEXT, tenant_id TEXT, app_id TEXT, triggered_from TEXT, from_account_id TEXT,
        created_by TEXT, total_tokens INTEGER, __time__ INTEGER, finished_at INTEGER
    )""")
    start = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
    rows = [
        # id, account, terminal, tokens, tenant, app, source, day offset, finished
        ("a1", "account-1", "shared", 10, "tenant-1", "app-1", "app-run", 0, 1),
        ("a2", "account-1", "shared", 10, "tenant-1", "app-1", "app-run", 0, 1),
        ("b", "account-2", "shared", 30, "tenant-1", "app-1", "app-run", 0, 1),
        ("missing", None, "legacy", 40, "tenant-1", "app-1", "app-run", 0, 1),
        ("null", None, "legacy", 50, "tenant-1", "app-1", "app-run", 0, 1),
        ("other-tenant", "account-1", "other", 60, "tenant-2", "app-1", "app-run", 0, 1),
        ("other-app", "account-1", "other", 70, "tenant-1", "app-2", "app-run", 0, 1),
        ("debug", "account-1", "debug", 80, "tenant-1", "app-1", "debugging", 0, 1),
        ("before", "account-1", "shared", 90, "tenant-1", "app-1", "app-run", -1, 1),
        ("end", "account-1", "shared", 100, "tenant-1", "app-1", "app-run", 1, 1),
        ("unfinished", "account-1", "shared", 110, "tenant-1", "app-1", "app-run", 0, None),
    ]
    for run_id, actor, terminal, tokens, tenant, app_id, source, offset, finished in rows:
        connection.execute(
            f"INSERT INTO {AliyunLogStore.workflow_execution_logstore} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                tenant,
                app_id,
                source,
                actor,
                terminal,
                tokens,
                (start + datetime.timedelta(days=offset)).timestamp(),
                finished,
            ),
        )

    def execute_sql(*, sql: str, query: str, logstore: str) -> list[dict[str, object]]:
        assert query == "*"
        assert logstore == AliyunLogStore.workflow_execution_logstore
        return [dict(row) for row in connection.execute(sql)]

    repository = LogstoreAPIWorkflowRunRepository.__new__(LogstoreAPIWorkflowRunRepository)
    client = MagicMock(spec=AliyunLogStore)
    client.execute_sql.side_effect = execute_sql
    repository.logstore_client = client
    try:
        yield repository, client
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("method", "metric", "expected_a", "expected_b", "expected_all"),
    [
        ("get_daily_runs_statistics", "runs", 2, 1, 5),
        ("get_daily_token_cost_statistics", "token_count", 20, 30, 140),
        ("get_average_app_interaction_statistics", "interactions", 2.0, 1.0, 2.5),
    ],
)
def test_account_statistics_filters_before_aggregation(
    statistic_repository: tuple[LogstoreAPIWorkflowRunRepository, MagicMock],
    method: str,
    metric: str,
    expected_a: float,
    expected_b: float,
    expected_all: float,
) -> None:
    repository, client = statistic_repository
    query = getattr(repository, method)
    arguments = {
        "tenant_id": "tenant-1",
        "app_id": "app-1",
        "triggered_from": "app-run",
        "start_date": datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        "end_date": datetime.datetime(2024, 1, 3, tzinfo=datetime.UTC),
    }
    for actor, expected in [("account-1", expected_a), ("account-2", expected_b), (None, expected_all)]:
        assert query(**arguments, account_id=actor) == [{"date": "2024-01-02", metric: expected}]
        sql = client.execute_sql.call_args.kwargs["sql"]
        assert "tenant_id='tenant-1'" in sql
        assert "app_id='app-1'" in sql
        assert "triggered_from='app-run'" in sql
        assert "finished_at IS NOT NULL" in sql
        if actor is None:
            assert "from_account_id" not in sql
        else:
            predicate = f"from_account_id='{actor}'"
            assert sql.count(predicate) == 1
            assert sql.index("WHERE") < sql.index(predicate) < sql.index("GROUP BY")

    assert query(**arguments) == [{"date": "2024-01-02", metric: expected_all}]
    assert "from_account_id" not in client.execute_sql.call_args.kwargs["sql"]
    assert query(**arguments, account_id="unknown") == []
    assert query(**arguments, account_id="account-1' OR '1'='1") == []
    sql = client.execute_sql.call_args.kwargs["sql"]
    assert sql.count("from_account_id='account-1'' OR ''1''=''1'") == 1
    arguments.pop("start_date")
    arguments.pop("end_date")
    assert sorted(row["date"] for row in query(**arguments, account_id="account-1")) == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]


def test_daily_terminals_remain_app_wide(
    statistic_repository: tuple[LogstoreAPIWorkflowRunRepository, MagicMock],
) -> None:
    repository, client = statistic_repository
    assert repository.get_daily_terminals_statistics(
        tenant_id="tenant-1",
        app_id="app-1",
        triggered_from="app-run",
        start_date=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2024, 1, 3, tzinfo=datetime.UTC),
    ) == [{"date": "2024-01-02", "terminal_count": 2}]
    assert "from_account_id" not in client.execute_sql.call_args.kwargs["sql"]
