"""Independent boundary checks for the permanent avatar UUID guard."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from models.casdoor_avatar_file_guard_extend import (
    CasdoorAvatarFileGuardExtend as Guard,
)
from repositories.casdoor_avatar_file_guard_repository_extend import (
    CasdoorAvatarFileGuardConflict,
    CasdoorAvatarFileGuardRepository,
)
from sqlalchemy.orm import Session


def _engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'guard-independent.sqlite'}")
    Guard.__table__.create(engine)
    return engine


def test_only_the_repository_owner_that_locked_a_parent_can_bind(tmp_path):
    engine = _engine(tmp_path)
    file_id = uuid4()
    with Session(engine) as session, session.begin():
        first_owner = CasdoorAvatarFileGuardRepository(session)
        other_owner = CasdoorAvatarFileGuardRepository(session)
        parent = first_owner.lock_or_create(file_id)

        with pytest.raises(CasdoorAvatarFileGuardConflict):
            other_owner.bind_reservation(parent, intent_id=uuid4(), attempt_id=uuid4())

        bound = first_owner.bind_reservation(
            parent, intent_id=uuid4(), attempt_id=uuid4()
        )
        assert bound.version == 2 and bound.stage == "reserved"

    with engine.connect() as connection:
        row = connection.execute(sa.select(Guard.__table__)).one()
    assert row.file_id == str(file_id)
    assert row.stage == "reserved" and row.version == 2
    engine.dispose()


def test_cross_root_snapshot_and_copy_are_rejected_but_fresh_snapshot_binds(tmp_path):
    engine = _engine(tmp_path)
    file_id = uuid4()
    with Session(engine) as session:
        owner = CasdoorAvatarFileGuardRepository(session)
        with session.begin():
            prior_root = owner.lock_or_create(file_id)

        with session.begin():
            fresh = owner.lock_existing(file_id)
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                owner.bind_reservation(
                    prior_root, intent_id=uuid4(), attempt_id=uuid4()
                )
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                owner.bind_reservation(
                    type(fresh)(**fresh.__dict__), intent_id=uuid4(), attempt_id=uuid4()
                )
            bound = owner.bind_reservation(fresh, intent_id=uuid4(), attempt_id=uuid4())
            assert bound.version == 2

    engine.dispose()


def test_sql_failure_poisoning_is_shared_by_guard_owners_until_rollback(tmp_path):
    engine = _engine(tmp_path)
    file_id = uuid4()
    injected = False

    def fail_parent_insert(
        connection, cursor, statement, parameters, context, executemany
    ):
        nonlocal injected
        if not injected and statement.lstrip().upper().startswith(
            "INSERT INTO CASDOOR_AVATAR_FILE_GUARD_EXTEND"
        ):
            injected = True
            raise sa.exc.OperationalError(
                statement, parameters, RuntimeError("injected SQL failure")
            )

    sa.event.listen(engine, "before_cursor_execute", fail_parent_insert)
    try:
        with Session(engine) as session:
            with session.begin():
                first_owner = CasdoorAvatarFileGuardRepository(session)
                with pytest.raises(CasdoorAvatarFileGuardConflict):
                    first_owner.lock_or_create(file_id)
                with pytest.raises(CasdoorAvatarFileGuardConflict):
                    CasdoorAvatarFileGuardRepository(session).lock_or_create(file_id)
                session.rollback()

            with session.begin():
                parent = CasdoorAvatarFileGuardRepository(session).lock_or_create(
                    file_id
                )
                assert parent.file_id == file_id

        with engine.connect() as connection:
            rows = connection.execute(sa.select(Guard.__table__)).all()
        assert injected and len(rows) == 1 and rows[0].file_id == str(file_id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail_parent_insert)
        engine.dispose()


def test_fresh_cas_detects_same_root_interference_and_rejects_further_guard_use(
    tmp_path,
):
    engine = _engine(tmp_path)
    file_id = uuid4()
    with Session(engine) as session:
        with session.begin():
            owner = CasdoorAvatarFileGuardRepository(session)
            parent = owner.lock_or_create(file_id)
            session.execute(
                sa.update(Guard.__table__)
                .where(Guard.__table__.c.file_id == str(file_id))
                .values(version=parent.version + 1)
            )
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                owner.bind_reservation(parent, intent_id=uuid4(), attempt_id=uuid4())
            with pytest.raises(CasdoorAvatarFileGuardConflict):
                CasdoorAvatarFileGuardRepository(session).lock_existing(file_id)
            session.rollback()

    with engine.connect() as connection:
        assert connection.execute(sa.select(Guard.__table__)).all() == []
    engine.dispose()
