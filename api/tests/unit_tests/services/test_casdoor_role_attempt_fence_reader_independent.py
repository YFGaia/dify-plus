"""Independent actual-SQLite lifecycle checks for the I16-B observation API."""

from dataclasses import FrozenInstanceError, fields
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.casdoor_extend import CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_attempt_fence_reader_repository_extend import (
    RoleAttemptFenceReadObservation,
)
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict
from services.casdoor_role_attempt_fence_reader_service_extend import (
    CasdoorRoleAttemptFenceReaderService,
)
from sqlalchemy.orm import Session

from tests.unit_tests.repositories import (
    test_casdoor_role_attempt_dispatch_fence_extend as fence,
)
from tests.unit_tests.services import (
    test_casdoor_role_attempt_fence_reader_extend as author,
)


@pytest.fixture
def storage(monkeypatch):
    # Reuse the accepted synthetic actual-SQLite chain setup. Fixture
    # implementations are called directly because pytest does not discover
    # fixtures simply by importing their test module.
    yield from author.storage.__wrapped__(monkeypatch)


@pytest.fixture
def staged(storage):
    return author.staged.__wrapped__(storage)


@pytest.fixture
def reserved(staged):
    return author.reserved.__wrapped__(staged)


@pytest.fixture
def armed(reserved):
    with reserved.session.begin():
        fence.arm(reserved)
    return reserved


class LifecycleSession(Session):
    """Capture lifecycle ordering and inject failures after real SQLite work."""

    def __init__(self, bind, events, fail=None):
        super().__init__(bind, expire_on_commit=False)
        self.events = events
        self.fail = fail
        self.closed_for_test = False

    def begin(self, *args, **kwargs):
        self.events.append("begin")
        return super().begin(*args, **kwargs)

    def rollback(self):
        self.events.append("rollback")
        if self.fail == "rollback_before":
            raise RuntimeError("injected rollback refusal")
        super().rollback()
        if self.fail == "rollback_after":
            raise RuntimeError("injected rollback acknowledgement loss")

    def close(self):
        self.events.append("close")
        super().close()
        self.closed_for_test = True
        if self.fail == "close_after":
            raise RuntimeError("injected close acknowledgement loss")


def make_reader(s, *, fail=None, session_factory=None):
    events, sessions = [], []

    def factory():
        if session_factory is not None:
            return session_factory()
        session = LifecycleSession(s.engine, events, fail=fail)
        sessions.append(session)
        return session

    return CasdoorRoleAttemptFenceReaderService(session_factory=factory), events, sessions


def observe(s, service, *, intent_id=None, reservation_id=None):
    return service.observe_dispatch_fence(
        s.version,
        s.target,
        intent_id=s.receipt.intent_id if intent_id is None else intent_id,
        reservation_id=s.reservation_id if reservation_id is None else reservation_id,
    )


def read_row(session):
    return dict(session.execute(sa.select(*Intent.__table__.columns)).one()._mapping)


def trace(s):
    statements = []
    sa.event.listen(
        s.engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )
    return statements


def dml(statements):
    return [
        statement for statement in statements if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


def test_committed_lost_ack_is_observed_only_after_new_root_rollback_and_close(armed):
    s = armed
    s.session.begin()
    fence.arm(s)
    original_commit = s.session.commit

    def commit_then_lose_ack():
        original_commit()
        raise OSError("lost fixture acknowledgement")

    with pytest.raises(OSError, match="lost fixture acknowledgement"):
        commit_then_lose_ack()

    events, sessions = [], []

    def factory():
        session = LifecycleSession(s.engine, events)
        sessions.append(session)
        return session

    sql = trace(s)
    result = CasdoorRoleAttemptFenceReaderService(session_factory=factory).observe_dispatch_fence(
        s.version,
        s.target,
        intent_id=s.receipt.intent_id,
        reservation_id=s.reservation_id,
    )
    assert events == ["begin", "rollback", "close"]
    assert sessions[0] is not s.session and sessions[0].closed_for_test
    assert not sessions[0].in_transaction()
    assert type(result) is RoleAttemptFenceReadObservation
    assert {field.name for field in fields(result)} == {
        "intent_id",
        "reservation_id",
        "membership_id",
        "join_id",
        "desired_payload_digest",
        "scope_digest",
    }
    assert result.intent_id == s.receipt.intent_id and result.reservation_id == s.reservation_id
    assert result.membership_id == s.receipt.membership_id and result.join_id == s.receipt.join_id
    assert result.scope_digest == s.receipt.scope_digest
    assert result.desired_payload_digest == s.receipt.desired_payload_digest
    assert str(result.reservation_id) not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.reservation_id = uuid4()
    assert not dml(sql)
    assert not any("SAVEPOINT" in statement.upper() for statement in sql)
    with s.session.begin():
        row = read_row(s.session)
    assert row["operation_state"] is CasdoorOperationState.IN_FLIGHT
    assert row["termination_state"] is CasdoorTerminationState.UNCONFIRMED
    assert row["attempt_count"] == 1 and row["attempt_id"] == str(s.reservation_id)


@pytest.mark.parametrize(
    "case",
    ["reserved", "missing", "different_id", "different_reservation", "string_reservation"],
    ids=["unarmed-reservation", "missing-row", "foreign-intent-id", "wrong-attempt-uuid", "noncanonical-uuid"],
)
def test_unobserved_states_fail_with_fixed_conflict_and_zero_dml(reserved, case):
    s = reserved
    kwargs = {}
    with s.session.begin():
        if case == "missing":
            s.session.execute(sa.delete(Intent))
        elif case in ("different_id", "different_reservation", "string_reservation"):
            fence.arm(s)
            if case == "different_id":
                kwargs["intent_id"] = UUID("71cc4734-335e-44f4-9800-69a320061a31")
            elif case == "different_reservation":
                kwargs["reservation_id"] = UUID("d7db120d-bfeb-4d2e-b995-3df345086620")
            else:
                kwargs["reservation_id"] = str(s.reservation_id)

    service, events, sessions = make_reader(s)
    sql = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict) as error:
        observe(s, service, **kwargs)
    assert str(error.value) == "authorization_pending"
    if case == "string_reservation":
        assert events == [] and sessions == []
    else:
        assert events[-2:] == ["rollback", "close"] and sessions[0].closed_for_test
    assert not dml(sql)
    assert not any("SAVEPOINT" in statement.upper() for statement in sql)


@pytest.mark.parametrize(
    "failure",
    ["rollback_before", "rollback_after", "close_after"],
    ids=["rollback-before-cleanup", "rollback-ack-lost", "close-ack-lost"],
)
def test_cleanup_failure_suppresses_success_after_valid_sql_read(armed, failure):
    s = armed
    events, sessions = [], []

    def factory():
        session = LifecycleSession(s.engine, events, fail=failure)
        sessions.append(session)
        return session

    sql = trace(s)
    service = CasdoorRoleAttemptFenceReaderService(session_factory=factory)
    with pytest.raises(CasdoorRoleIntentConflict) as error:
        observe(s, service)
    assert str(error.value) == "authorization_pending"
    assert events == ["begin", "rollback", "close"]
    assert sessions[0].closed_for_test
    assert not dml(sql)
    assert not any("SAVEPOINT" in statement.upper() for statement in sql)


def test_factory_failure_has_same_conflict_and_never_creates_partial_result(armed):
    def broken_factory():
        raise RuntimeError("factory unavailable")

    service, events, sessions = make_reader(armed, session_factory=broken_factory)
    with pytest.raises(CasdoorRoleIntentConflict) as error:
        observe(armed, service)
    assert str(error.value) == "authorization_pending"
    assert events == [] and sessions == []
