"""Actual bounded SQLite coordination; other dialects compile only, no storage I/O."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.casdoor_avatar_file_guard_extend import (
    CasdoorAvatarFileGuardExtend as Guard,
)
from repositories.casdoor_avatar_file_guard_repository_extend import (
    CasdoorAvatarFileGuardConflict,
    CasdoorAvatarFileGuardRepository,
    _insert_parent,
)


@pytest.fixture
def guard_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'guard.sqlite'}", connect_args={"timeout": 0.1})
    Guard.__table__.create(engine)
    yield engine
    engine.dispose()


def persisted(engine):
    with engine.connect() as connection:
        return list(connection.execute(sa.select(*Guard.__table__.columns)).mappings())


def test_parent_is_permanent_unique_and_binding_is_same_root(guard_engine):
    file_id, intent_id, attempt_id = uuid4(), uuid4(), uuid4()
    with Session(guard_engine) as session, session.begin():
        owner = CasdoorAvatarFileGuardRepository(session)
        original = owner.lock_or_create(file_id)
        assert original.stage == "unbound" and original.version == 1
        bound = owner.bind_reservation(original, intent_id=intent_id, attempt_id=attempt_id)
        assert bound.stage == "reserved" and bound.version == 2
        assert not session.new and not session.dirty and not session.deleted
    with Session(guard_engine) as session, session.begin():
        fresh = CasdoorAvatarFileGuardRepository(session).lock_or_create(file_id)
        assert fresh == bound
    assert len(persisted(guard_engine)) == 1


def test_caller_rollback_removes_parent_and_binding(guard_engine):
    with Session(guard_engine) as session:
        with pytest.raises(RuntimeError), session.begin():
            owner = CasdoorAvatarFileGuardRepository(session)
            parent = owner.lock_or_create(uuid4())
            owner.bind_reservation(parent, intent_id=uuid4(), attempt_id=uuid4())
            raise RuntimeError("actual caller root fails")
    assert persisted(guard_engine) == []


def test_existing_absence_does_not_create_parent(guard_engine):
    with Session(guard_engine) as session:
        with pytest.raises(CasdoorAvatarFileGuardConflict), session.begin():
            CasdoorAvatarFileGuardRepository(session).lock_existing(uuid4())
    assert persisted(guard_engine) == []


@pytest.mark.parametrize("mode", ["no_root", "autobegin", "nested", "new", "dirty", "deleted"])
def test_refuses_incomplete_caller_root_before_coordination(guard_engine, mode):
    file_id = uuid4()
    with Session(guard_engine) as session:
        owner = CasdoorAvatarFileGuardRepository(session)
        if mode == "no_root":
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                owner.lock_or_create(file_id)
            return
        if mode == "autobegin":
            session.execute(sa.select(sa.literal(1)))
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                owner.lock_or_create(file_id)
            session.rollback()
            return
        with session.begin():
            if mode == "nested":
                with session.begin_nested(), pytest.raises(CasdoorAvatarFileGuardConflict):
                    owner.lock_or_create(file_id)
            else:
                row = Guard(file_id=str(uuid4()), version=1, stage="unbound")
                session.add(row)
                if mode != "new":
                    session.flush()
                    if mode == "dirty":
                        row.version = 2
                    else:
                        session.delete(row)
                with pytest.raises(CasdoorAvatarFileGuardConflict):
                    owner.lock_or_create(file_id)
                session.rollback()
    assert all(row["file_id"] != str(file_id) for row in persisted(guard_engine))


def test_copied_or_prior_transaction_snapshot_cannot_bind(guard_engine):
    file_id = uuid4()
    with Session(guard_engine) as session:
        owner = CasdoorAvatarFileGuardRepository(session)
        with session.begin():
            old = owner.lock_or_create(file_id)
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                owner.bind_reservation(replace(old), intent_id=uuid4(), attempt_id=uuid4())
        with session.begin(), pytest.raises(CasdoorAvatarFileGuardConflict):
            owner.bind_reservation(old, intent_id=uuid4(), attempt_id=uuid4())
    assert persisted(guard_engine)[0]["stage"] == "unbound"


def test_actual_cas_mismatch_rolls_back_whole_binding_root(guard_engine):
    file_id = uuid4()
    with Session(guard_engine) as session:
        with pytest.raises(CasdoorAvatarFileGuardConflict), session.begin():
            owner = CasdoorAvatarFileGuardRepository(session)
            parent = owner.lock_or_create(file_id)
            session.execute(sa.update(Guard.__table__).values(version=2))
            owner.bind_reservation(parent, intent_id=uuid4(), attempt_id=uuid4())
    assert persisted(guard_engine) == []


def test_failed_root_cannot_be_resumed_by_another_guard_owner(guard_engine):
    file_id = uuid4()
    with Session(guard_engine) as session:
        with session.begin():
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                CasdoorAvatarFileGuardRepository(session).lock_existing(file_id)
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                CasdoorAvatarFileGuardRepository(session).lock_or_create(file_id)
            session.rollback()
        with session.begin():
            assert CasdoorAvatarFileGuardRepository(session).lock_or_create(file_id).stage == "unbound"
    assert len(persisted(guard_engine)) == 1


@pytest.mark.parametrize("stage", ["reserved", "cleanup_pending", "cleanup_complete"])
def test_bound_or_terminal_parent_cannot_rebind(guard_engine, stage):
    file_id = uuid4()
    with guard_engine.begin() as connection:
        connection.execute(
            sa.insert(Guard.__table__).values(
                file_id=str(file_id), version=2, stage=stage, intent_id=str(uuid4()), attempt_id=str(uuid4())
            )
        )
    before = persisted(guard_engine)
    with Session(guard_engine) as session:
        with pytest.raises(CasdoorAvatarFileGuardConflict), session.begin():
            owner = CasdoorAvatarFileGuardRepository(session)
            owner.bind_reservation(owner.lock_existing(file_id), intent_id=uuid4(), attempt_id=uuid4())
    assert persisted(guard_engine) == before


@pytest.mark.parametrize(
    "bad", [{"version": 0}, {"version": 9007199254740992}, {"stage": "unknown"}, {"stage": "reserved"}]
)
def test_database_checks_reject_invalid_shapes(guard_engine, bad):
    values = dict(file_id=str(uuid4()), version=1, stage="unbound", intent_id=None, attempt_id=None)
    values.update(bad)
    with pytest.raises(IntegrityError), guard_engine.begin() as connection:
        connection.execute(sa.insert(Guard.__table__).values(**values))
    assert persisted(guard_engine) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 0},
        {"stage": "unknown"},
        {"stage": "reserved", "intent_id": str(uuid4()), "attempt_id": "not-uuid"},
    ],
)
def test_corrupted_persisted_parent_fails_closed(guard_engine, bad):
    file_id = uuid4()
    values = dict(file_id=str(file_id), version=1, stage="unbound", intent_id=None, attempt_id=None)
    values.update(bad)
    with guard_engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(sa.insert(Guard.__table__).values(**values))
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    with Session(guard_engine) as session:
        with pytest.raises(CasdoorAvatarFileGuardConflict), session.begin():
            CasdoorAvatarFileGuardRepository(session).lock_existing(file_id)


def test_invalid_uuid_and_unsupported_dialect_issue_no_statement(guard_engine, monkeypatch):
    statements = []
    sa.event.listen(guard_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    with Session(guard_engine) as session, session.begin():
        owner = CasdoorAvatarFileGuardRepository(session)
        with pytest.raises(CasdoorAvatarFileGuardConflict):
            owner.lock_or_create(str(uuid4()))
        monkeypatch.setattr(guard_engine.dialect, "name", "oracle")
        with pytest.raises(CasdoorAvatarFileGuardConflict):
            owner.lock_or_create(uuid4())
    assert statements == []


@pytest.mark.parametrize("existing", [False, True])
def test_sqlite_second_writer_refuses_while_parent_root_is_open(guard_engine, existing):
    file_id = uuid4()
    started = Event()
    if existing:
        with Session(guard_engine) as seed, seed.begin():
            CasdoorAvatarFileGuardRepository(seed).lock_or_create(file_id)

    def contender():
        with Session(guard_engine) as other:
            with pytest.raises(CasdoorAvatarFileGuardConflict), other.begin():
                started.set()
                other_owner = CasdoorAvatarFileGuardRepository(other)
                (other_owner.lock_existing if existing else other_owner.lock_or_create)(file_id)

    with Session(guard_engine) as first, first.begin():
        owner = CasdoorAvatarFileGuardRepository(first)
        owner.lock_or_create(file_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contender)
            assert started.wait(2)
            future.result(timeout=3)
        assert owner.lock_existing(file_id).stage == "unbound"
    assert len(persisted(guard_engine)) == 1


def test_binding_version_exhaustion_preserves_parent(guard_engine):
    file_id = uuid4()
    with guard_engine.begin() as connection:
        connection.execute(
            sa.insert(Guard.__table__).values(file_id=str(file_id), version=9007199254740991, stage="unbound")
        )
    with Session(guard_engine) as session:
        with pytest.raises(CasdoorAvatarFileGuardConflict), session.begin():
            owner = CasdoorAvatarFileGuardRepository(session)
            owner.bind_reservation(owner.lock_existing(file_id), intent_id=uuid4(), attempt_id=uuid4())
    assert persisted(guard_engine)[0]["version"] == 9007199254740991


def test_sqlite_competing_insert_observes_committed_binding(guard_engine):
    file_id, intent_id, attempt_id = uuid4(), uuid4(), uuid4()
    started = Event()
    # A longer bounded DB wait allows the genuine first root to commit.
    other_engine = sa.create_engine(guard_engine.url, connect_args={"timeout": 2})

    def contender():
        with Session(other_engine) as other, other.begin():
            started.set()
            return CasdoorAvatarFileGuardRepository(other).lock_or_create(file_id)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool, Session(guard_engine) as first:
            with first.begin():
                owner = CasdoorAvatarFileGuardRepository(first)
                bound = owner.bind_reservation(
                    owner.lock_or_create(file_id), intent_id=intent_id, attempt_id=attempt_id
                )
                future = pool.submit(contender)
                assert started.wait(2)
            assert future.result(timeout=3) == bound
        assert len(persisted(guard_engine)) == 1
    finally:
        other_engine.dispose()


@pytest.mark.parametrize("dialect", [postgresql.dialect(), mysql.dialect(), sqlite.dialect()])
def test_actual_dialect_statements_compile_without_starting_service(dialect):
    insert_sql = str(_insert_parent(dialect.name, uuid4()).compile(dialect=dialect)).upper()
    assert "INSERT INTO CASDOOR_AVATAR_FILE_GUARD_EXTEND" in insert_sql
    if dialect.name == "mysql":
        assert "ON DUPLICATE KEY UPDATE" in insert_sql
    else:
        assert "ON CONFLICT (FILE_ID) DO NOTHING" in insert_sql
    read_sql = str(sa.select(*Guard.__table__.columns).with_for_update().compile(dialect=dialect)).upper()
    assert ("FOR UPDATE" in read_sql) == (dialect.name != "sqlite")


def test_registration_has_only_content_free_permanent_parent():
    from models import CasdoorAvatarFileGuardExtend

    assert CasdoorAvatarFileGuardExtend is Guard
    assert tuple(Guard.__table__.columns.keys()) == ("file_id", "version", "stage", "intent_id", "attempt_id")
    assert not Guard.__table__.foreign_keys and not Guard.__table__.indexes
