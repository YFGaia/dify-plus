"""Actual SQLite outer rollback and zero-SQL whitelist/dirty-session rejection."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.request_safety import (
    AuditSummary,
    LocalReferences,
    RequestAction,
    RequestSafetyError,
    SafetyEvent,
    SafetyResultCode,
)
from models.casdoor_extend import CasdoorAuditExtend
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class AuditTestBase(DeclarativeBase):
    pass


class OtherRow(AuditTestBase):
    __tablename__ = "i07b_other"
    id: Mapped[int] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(default=1)


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")
    CasdoorAuditExtend.__table__.create(engine)
    OtherRow.__table__.create(engine)
    statements = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    with Session(engine) as session:
        yield engine, session, statements
    engine.dispose()


def event(**kwargs):
    return SafetyEvent(RequestAction.CALLBACK, SafetyResultCode.SUCCESS, uuid4(), **kwargs)


def test_actual_outer_rollback_removes_audit_and_preserves_business_row(storage):
    engine, session, _ = storage
    with session.begin():
        session.add(OtherRow(id=1))
    session.begin()
    reference = uuid4()
    row = CasdoorAuditRepository(session).append(
        event(references=LocalReferences(account_id=reference), summary=AuditSummary(workspace_count=1))
    )
    assert row.account_id == str(reference)
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1
    session.rollback()
    with Session(engine) as read:
        assert read.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 0
        assert read.get(OtherRow, 1).count == 1


@pytest.mark.parametrize("state", ["new", "dirty", "deleted"])
def test_pending_unrelated_state_rejected_before_sql_or_flush(storage, state):
    _, session, statements = storage
    with session.begin():
        session.add(OtherRow(id=1))
    session.begin()
    if state == "new":
        session.add(OtherRow(id=2))
    else:
        row = session.get(OtherRow, 1)
        if state == "dirty":
            row.count = 2
        else:
            session.delete(row)
    statements.clear()
    with pytest.raises(RuntimeError, match="explicitly flushed"):
        CasdoorAuditRepository(session).append(event())
    assert not statements
    assert not any(type(row) is CasdoorAuditExtend for row in session.new)
    session.rollback()
    assert session.get(OtherRow, 1).count == 1
    assert session.get(OtherRow, 2) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"secret": "synthetic_secret"},
        "synthetic_token",
        event(summary={"profile": "synthetic_profile"}),
        event(references=LocalReferences(account_id="synthetic_code")),
        event(summary=AuditSummary(role_count=True)),
    ],
)
def test_whitelist_rejection_zero_sql(storage, bad):
    _, session, statements = storage
    session.begin()
    with pytest.raises((ValueError, RequestSafetyError)):
        CasdoorAuditRepository(session).append(bad)
    assert not statements
    assert not session.new


def test_requires_root_caller_transaction(storage):
    _, session, statements = storage
    repository = CasdoorAuditRepository(session)
    with pytest.raises(RuntimeError, match="root transaction"):
        repository.append(event())
    assert not statements
    with session.begin(), session.begin_nested():
        statements.clear()
        with pytest.raises(RuntimeError, match="root transaction"):
            repository.append(event())
        assert not statements


def test_failed_flush_inactive_session_rejected_without_staging_sql_or_hidden_recovery(storage):
    engine, session, statements = storage
    with session.begin():
        session.add(OtherRow(id=1))
    session.begin()
    session.add(OtherRow(id=1))
    with pytest.raises(sa.exc.IntegrityError):
        session.flush()
    assert session.in_transaction() and not session.is_active
    assert not session.new
    statements.clear()
    with pytest.raises(RuntimeError, match="active Session"):
        CasdoorAuditRepository(session).append(event())
    assert not statements
    assert not session.new
    assert session.in_transaction() and not session.is_active
    session.rollback()  # Only the caller may recover the failed transaction.
    with Session(engine) as read:
        assert read.get(OtherRow, 1).count == 1
        assert read.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 0
