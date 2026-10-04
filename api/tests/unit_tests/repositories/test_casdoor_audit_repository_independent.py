"""Independent SQLite transaction-boundary tests for Casdoor audit append."""

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


class AuditIndependentBase(DeclarativeBase):
    pass


class BusinessRow(AuditIndependentBase):
    __tablename__ = "i07b_independent_business"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[int] = mapped_column(default=1)


class MustBePositive(AuditIndependentBase):
    __tablename__ = "i07b_independent_constraint"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[int] = mapped_column(sa.CheckConstraint("value > 0"))


@pytest.fixture
def sqlite_session():
    engine = sa.create_engine("sqlite://")
    CasdoorAuditExtend.__table__.create(engine)
    AuditIndependentBase.metadata.create_all(engine)
    statements = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    with Session(engine) as session:
        yield engine, session, statements
    engine.dispose()


def make_event(**kwargs):
    return SafetyEvent(RequestAction.CALLBACK, SafetyResultCode.SUCCESS, uuid4(), **kwargs)


def test_active_clean_root_transaction_appends_and_outer_rollback_removes_audit(sqlite_session):
    engine, session, statements = sqlite_session
    with session.begin():
        session.add(BusinessRow(id=1, value=8))

    session.begin()
    account_id = uuid4()
    row = CasdoorAuditRepository(session).append(
        make_event(
            references=LocalReferences(account_id=account_id),
            summary=AuditSummary(workspace_count=2, role_count=3),
        )
    )
    assert row.account_id == str(account_id)
    assert any("INSERT INTO casdoor_audit_extend" in sql for sql in statements)
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1

    session.rollback()
    with Session(engine) as read_session:
        assert read_session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 0
        assert read_session.get(BusinessRow, 1).value == 8


def test_nested_transaction_is_rejected_before_repository_sql(sqlite_session):
    _, session, statements = sqlite_session
    session.begin()
    with session.begin_nested():
        statements.clear()
        with pytest.raises(RuntimeError, match="root transaction"):
            CasdoorAuditRepository(session).append(make_event())
        assert statements == []
        assert not session.new
    session.rollback()


def test_no_transaction_is_rejected_before_sql(sqlite_session):
    _, session, statements = sqlite_session
    with pytest.raises(RuntimeError, match="root transaction"):
        CasdoorAuditRepository(session).append(make_event())
    assert statements == []
    assert not session.new


def test_inactive_failed_root_transaction_is_rejected_without_staging_audit(sqlite_session):
    """A failed flush leaves in_transaction true but makes the Session unusable."""
    _, session, statements = sqlite_session
    session.begin()
    session.add(MustBePositive(id=1, value=-1))
    with pytest.raises(sa.exc.IntegrityError):
        session.flush()
    assert session.in_transaction()
    assert not session.is_active
    statements.clear()

    with pytest.raises(RuntimeError, match="active") as exc_info:
        CasdoorAuditRepository(session).append(make_event())
    assert not isinstance(exc_info.value, sa.exc.PendingRollbackError)
    assert statements == []
    assert not session.new
    session.rollback()


@pytest.mark.parametrize(
    "event",
    [
        {"profile": "syntheticProfile"},
        make_event(summary={"token": "syntheticToken"}),
        make_event(references=LocalReferences(account_id="syntheticCode")),
        make_event(summary=AuditSummary(role_count=True)),
        make_event(summary=AuditSummary(intent_count=-1)),
    ],
)
def test_malformed_reference_or_count_is_rejected_before_sql(sqlite_session, event):
    _, session, statements = sqlite_session
    session.begin()
    with pytest.raises((ValueError, RequestSafetyError)):
        CasdoorAuditRepository(session).append(event)
    assert statements == []
    assert not session.new
    session.rollback()
