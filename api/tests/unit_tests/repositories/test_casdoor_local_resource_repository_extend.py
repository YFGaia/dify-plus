from unittest.mock import Mock
from uuid import UUID

import pytest
import repositories.casdoor_local_resource_repository_extend as subject
import sqlalchemy as sa
import tasks.initialize_created_app_rbac_access_task as shared
from models import App
from sqlalchemy.orm import Session

WORKSPACE = UUID(int=100000)
OTHER = UUID(int=200000)


@pytest.fixture
def db(monkeypatch):
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    tables = {}
    for kind in shared._WHITELIST_RESOURCE_KINDS:
        model = kind.model
        tables[kind.resource_type] = sa.Table(
            model.__tablename__,
            metadata,
            sa.Column("id", model.__table__.c.id.type, primary_key=True),
            sa.Column("tenant_id", model.__table__.c.tenant_id.type),
        )
    metadata.create_all(engine)
    queries = []
    sa.event.listen(engine, "before_cursor_execute", lambda c, cu, stmt, p, ctx, many: queries.append(stmt))
    monkeypatch.setattr(shared.db, "session", Mock(side_effect=AssertionError("ambient session")))
    monkeypatch.setattr(subject.time, "monotonic", lambda: 10.0)
    with Session(engine) as session:

        def seed(kind, count, tenant=WORKSPACE, offset=1):
            with engine.begin() as conn:
                conn.execute(
                    tables[kind].insert(),
                    [dict(id=str(UUID(int=i)), tenant_id=str(tenant)) for i in range(offset, offset + count)],
                )

        yield session, seed, queries
    engine.dispose()


def scan(session):
    return subject.CasdoorLocalResourceRepository(session).enumerate_ids(WORKSPACE, deadline=12.0)


def assert_failure(reason, callback):
    with pytest.raises(subject.CasdoorLocalResourceScanError) as caught:
        callback()
    assert caught.value.reason == reason
    assert str(caught.value) == "local_resource_scan_" + reason.value


def test_real_keyset_all_kinds_exact_tenant_order_and_caller_lifecycle(db, monkeypatch):
    session, seed, queries = db
    expected = []
    for kind in shared._WHITELIST_RESOURCE_KINDS:
        seed(kind.resource_type, 501)
        seed(kind.resource_type, 2, OTHER, offset=1000)
        expected.extend((kind.resource_type, UUID(int=i)) for i in range(1, 502))
    session.begin()
    transaction = session.get_transaction()
    queries.clear()
    for name in ("flush", "commit", "rollback", "close", "begin", "begin_nested"):
        monkeypatch.setattr(session, name, Mock(side_effect=AssertionError(name)))
    result = scan(session)
    assert result.resources == tuple(expected)
    assert result.status == "LOCAL_SCAN_EXHAUSTED"
    assert result.workspace_id == WORKSPACE
    assert result.attempted_queries == len(queries) == 9
    assert all(sql.startswith("SELECT ") and " LIMIT " in sql for sql in queries)
    assert session.get_transaction() is transaction and transaction.is_active
    monkeypatch.undo()
    session.rollback()
    assert not session.in_transaction()


def test_empty_requires_three_terminal_probes(db):
    session, _, queries = db
    session.begin()
    result = scan(session)
    assert result.resources == ()
    assert result.attempted_queries == len(queries) == 3


@pytest.mark.parametrize("count", [4095, 4096, 4097])
def test_aggregate_cap_with_real_sql(db, count):
    session, seed, queries = db
    seed(shared._WHITELIST_RESOURCE_KINDS[0].resource_type, count)
    session.begin()
    queries.clear()
    if count > 4096:
        assert_failure(subject.LocalResourceScanFailure.RESOURCE_LIMIT, lambda: scan(session))
        assert len(queries) == 9
    else:
        result = scan(session)
        assert len(result.resources) == count
        assert result.attempted_queries == len(queries) == 12


def test_overflow_in_later_kind_after_exact_cap(db):
    session, seed, queries = db
    seed(shared._WHITELIST_RESOURCE_KINDS[0].resource_type, 4096)
    seed(shared._WHITELIST_RESOURCE_KINDS[2].resource_type, 1)
    session.begin()
    queries.clear()
    assert_failure(subject.LocalResourceScanFailure.RESOURCE_LIMIT, lambda: scan(session))
    assert len(queries) == 12


@pytest.mark.parametrize(
    "workspace,deadline",
    [
        ("bad", 12),
        (str(WORKSPACE), 12),
        (WORKSPACE, True),
        (WORKSPACE, float("nan")),
        (WORKSPACE, float("inf")),
        (WORKSPACE, 10),
        (WORKSPACE, 12.1),
    ],
)
def test_invalid_inputs_never_query(db, workspace, deadline):
    session, _, queries = db
    session.begin()
    assert_failure(
        subject.LocalResourceScanFailure.INVALID_INPUT,
        lambda: subject.CasdoorLocalResourceRepository(session).enumerate_ids(workspace, deadline=deadline),
    )
    assert queries == []


@pytest.mark.parametrize("state", ["none", "nested", "new", "dirty", "deleted", "inactive"])
def test_unclean_session_never_queries(db, state):
    session, _, queries = db
    if state != "none":
        session.begin()
    if state == "nested":
        session.begin_nested()
    if state in ("new", "dirty", "deleted"):
        app = App(id=str(UUID(int=1)), tenant_id=str(WORKSPACE))
        if state == "new":
            session.add(app)
        else:
            from sqlalchemy.orm import make_transient_to_detached

            make_transient_to_detached(app)
            session.add(app)
            if state == "dirty":
                app.tenant_id = str(OTHER)
            else:
                session.delete(app)
    if state == "inactive":
        from sqlalchemy.orm.session import SessionTransactionState

        session.get_transaction()._state = SessionTransactionState.DEACTIVE
    assert_failure(subject.LocalResourceScanFailure.INVALID_SESSION, lambda: scan(session))
    assert queries == []


@pytest.mark.parametrize("value", ["bad", "00000000-0000-0000-0000-00000000000A", "0" * 32])
def test_invalid_database_ids_are_safe_errors(db, value):
    session, _, queries = db
    session.execute(
        sa.text("INSERT INTO apps (id, tenant_id) VALUES (:id, :tenant)"), dict(id=value, tenant=str(WORKSPACE))
    )
    queries.clear()
    assert_failure(subject.LocalResourceScanFailure.INVALID_ROW, lambda: scan(session))
    assert len(queries) == 1


@pytest.mark.parametrize("moments,query_count", [([10, 12], 0), ([10, 10, 12], 1), ([10, 10, 10, 12], 1)])
def test_deadline_checks_before_after_and_terminal_probe(db, monkeypatch, moments, query_count):
    session, _, queries = db
    session.begin()
    clock = iter(moments)
    monkeypatch.setattr(subject.time, "monotonic", lambda: next(clock))
    assert_failure(subject.LocalResourceScanFailure.DEADLINE, lambda: scan(session))
    assert len(queries) == query_count


def test_query_guard_counts_empty_probes(db, monkeypatch):
    session, _, queries = db
    session.begin()
    monkeypatch.setattr(subject, "MAX_QUERY_ATTEMPTS", 2)
    assert_failure(subject.LocalResourceScanFailure.QUERY_LIMIT, lambda: scan(session))
    assert len(queries) == 2


def test_database_failure_has_no_raw_error(db, monkeypatch):
    session, _, _ = db
    session.begin()
    monkeypatch.setattr(session, "scalars", Mock(side_effect=RuntimeError("sensitive DB details")))
    assert_failure(subject.LocalResourceScanFailure.DATABASE, lambda: scan(session))


def test_session_rechecked_before_next_query(db, monkeypatch):
    session, _, queries = db
    session.begin()
    original = session.scalars

    def dirty_after_query(*args, **kwargs):
        result = original(*args, **kwargs)
        session.add(App(id=str(UUID(int=1)), tenant_id=str(WORKSPACE)))
        return result

    monkeypatch.setattr(session, "scalars", dirty_after_query)
    assert_failure(subject.LocalResourceScanFailure.INVALID_SESSION, lambda: scan(session))
    assert len(queries) == 1


def test_sixteen_attempt_budget_with_real_queries(db, monkeypatch):
    session, seed, queries = db
    seed(shared._WHITELIST_RESOURCE_KINDS[0].resource_type, 20)
    session.begin()
    queries.clear()
    # Smaller test pages reach the independent attempt bound before the ID cap.
    monkeypatch.setattr(subject, "APP_RBAC_RESOURCE_CONFIG_BATCH_SIZE", 1)
    assert_failure(subject.LocalResourceScanFailure.QUERY_LIMIT, lambda: scan(session))
    assert len(queries) == 16


def test_additional_page_sentinel_rejects_entire_scan(db, monkeypatch):
    session, seed, queries = db
    seed(shared._WHITELIST_RESOURCE_KINDS[0].resource_type, 4097)
    session.begin()
    queries.clear()
    monkeypatch.setattr(subject, "APP_RBAC_RESOURCE_CONFIG_BATCH_SIZE", 512)
    assert_failure(subject.LocalResourceScanFailure.RESOURCE_LIMIT, lambda: scan(session))
    assert len(queries) == 9


@pytest.mark.parametrize(
    "pages",
    [
        [["00000000-0000-0000-0000-000000000002", "00000000-0000-0000-0000-000000000001"]],
        [["00000000-0000-0000-0000-000000000001"], ["00000000-0000-0000-0000-000000000001"]],
    ],
)
def test_invalid_order_or_duplicate_across_pages_fails_closed(db, monkeypatch, pages):
    session, _, _ = db
    session.begin()
    results = []
    for page in pages:
        result = Mock()
        result.all.return_value = page
        results.append(result)
    monkeypatch.setattr(session, "scalars", Mock(side_effect=results))
    assert_failure(subject.LocalResourceScanFailure.INVALID_ROW, lambda: scan(session))


def test_oversized_integer_deadline_is_stable_invalid_input(db):
    session, _, queries = db
    session.begin()
    assert_failure(
        subject.LocalResourceScanFailure.INVALID_INPUT,
        lambda: subject.CasdoorLocalResourceRepository(session).enumerate_ids(WORKSPACE, deadline=10**1000),
    )
    assert queries == []
