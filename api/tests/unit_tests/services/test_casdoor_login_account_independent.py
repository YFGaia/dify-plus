"""Independent SQLite composition counterexamples for ordinary Casdoor LOGIN."""

from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from models.account import Account
from models.account_money_extend import AccountMoneyExtend as Quota
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from repositories.casdoor_account_preflight_repository_extend import AccountPreflightConflict
from services.casdoor_login_account_service_extend import _PreparedLoginAccount
from sqlalchemy.orm import Session
from test_casdoor_login_account_service_extend import counts, persist, prepare

pytest_plugins = ("test_casdoor_login_account_service_extend",)


def test_forged_same_field_preparation_rejected_without_consuming_authentic(env):
    session, context, key, service, *_ = env
    authentic = prepare(env)
    forged = _PreparedLoginAccount(
        authentic.plan,
        authentic.preflight,
        authentic.key,
        authentic.account_id,
        authentic.deadline,
        authentic._shared,
        service,
    )

    with session.begin(), pytest.raises(AccountPreflightConflict):
        service.persist_login_account(forged, session=session, context=context, key=key)

    assert not authentic._consumed
    assert counts(session.bind) == (0, 0, 0)
    with session.begin():
        result = persist(env, authentic)
    assert result.account_id == authentic.account_id
    assert counts(session.bind) == (1, 1, 1)


def test_real_identity_insert_trigger_failure_requires_full_outer_rollback(env, monkeypatch):
    session, context, key, service, *_ = env
    prepared = prepare(env)
    setup_owner = Mock(wraps=SQLAlchemyAccountActivationRepository.persist_account_setup)
    monkeypatch.setattr(SQLAlchemyAccountActivationRepository, "persist_account_setup", setup_owner)
    engine = session.bind
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER reject_casdoor_login_identity BEFORE INSERT ON {Identity.__tablename__} "
            "BEGIN SELECT RAISE(ABORT, 'controlled identity insert rejection'); END"
        )

    # A prior caller-owned write is already flushed and the root is clean when
    # the helper begins; its failure must still abort the complete caller UoW.
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement.lower())

    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(sa.exc.IntegrityError):
            with session.begin():
                session.add(Account(name="Earlier caller write", email="earlier@example.test"))
                session.flush()
                service.persist_login_account(prepared, session=session, context=context, key=key)
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER IF EXISTS reject_casdoor_login_identity")

    account_inserts = [
        index
        for index, statement in enumerate(statements)
        if f"insert into {Account.__tablename__.lower()}" in statement
    ]
    quota_insert = next(
        index for index, statement in enumerate(statements) if f"insert into {Quota.__tablename__.lower()}" in statement
    )
    identity_insert = next(
        index
        for index, statement in enumerate(statements)
        if f"insert into {Identity.__tablename__.lower()}" in statement
    )
    assert len(account_inserts) >= 2  # earlier caller row, then prepared NEW account
    assert account_inserts[1] < quota_insert < identity_insert
    assert setup_owner.call_count == 1

    with Session(engine) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(Account)) == 0
        assert reader.scalar(sa.select(sa.func.count()).select_from(Quota)) == 0
        assert reader.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
    assert prepared._consumed
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(env, prepared)
    fresh = prepare(env)
    assert fresh.account_id != prepared.account_id
    with session.begin():
        persist(env, fresh)
    assert counts(engine) == (1, 1, 1)
