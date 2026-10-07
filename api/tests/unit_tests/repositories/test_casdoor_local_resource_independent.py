from unittest.mock import Mock
from uuid import UUID

import pytest
import repositories.casdoor_local_resource_repository_extend as subject
import sqlalchemy as sa
import tasks.initialize_created_app_rbac_access_task as shared
from sqlalchemy.orm import Session

WORKSPACE = UUID(int=100000)


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

        def seed(kind, count):
            with engine.begin() as conn:
                conn.execute(
                    tables[kind].insert(),
                    [dict(id=str(UUID(int=i)), tenant_id=str(WORKSPACE)) for i in range(1, count + 1)],
                )

        yield session, seed, queries
    engine.dispose()


def test_replaced_caller_root_during_query_is_rejected_without_result(db, monkeypatch):
    session, seed, queries = db
    kind = shared._WHITELIST_RESOURCE_KINDS[0].resource_type
    seed(kind, 1)
    queries.clear()
    session.begin()
    original_scalars = session.scalars
    previous_root = session.get_transaction()

    def replace_root(statement):
        result = original_scalars(statement)
        session.rollback()
        session.begin()
        return result

    monkeypatch.setattr(session, "scalars", replace_root)
    returned = []
    with pytest.raises(subject.CasdoorLocalResourceScanError) as caught:
        returned.append(subject.CasdoorLocalResourceRepository(session).enumerate_ids(WORKSPACE, deadline=12.0))

    assert caught.value.reason == subject.LocalResourceScanFailure.INVALID_SESSION
    assert returned == []
    assert len(queries) == 1
    assert session.get_transaction() is not previous_root
    assert session.in_transaction()


def test_database_failure_after_real_page_never_returns_partial_inventory(db, monkeypatch):
    session, seed, queries = db
    kind = shared._WHITELIST_RESOURCE_KINDS[0].resource_type
    seed(kind, 2)
    queries.clear()
    session.begin()
    original_scalars = session.scalars
    calls = 0

    def fail_on_second_query(statement):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("private database detail")
        return original_scalars(statement)

    monkeypatch.setattr(session, "scalars", fail_on_second_query)
    returned = []
    with pytest.raises(subject.CasdoorLocalResourceScanError) as caught:
        returned.append(subject.CasdoorLocalResourceRepository(session).enumerate_ids(WORKSPACE, deadline=12.0))

    assert caught.value.reason == subject.LocalResourceScanFailure.DATABASE
    assert str(caught.value) == "local_resource_scan_database"
    assert "private database detail" not in str(caught.value)
    assert returned == []
    assert calls == 2
    assert len(queries) == 1
