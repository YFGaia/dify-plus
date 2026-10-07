"""Independent counterexamples for local reservation CAS and replay guards."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict

from tests.unit_tests.repositories import test_casdoor_role_attempt_repository_extend as reservation


@pytest.fixture
def storage(monkeypatch):
    yield from reservation.staging.storage.__wrapped__(monkeypatch)


@pytest.fixture
def staged(storage):
    s = storage
    with s.session.begin():
        s.receipt = reservation.staging.enqueue(s)
    s.attempts = reservation.CasdoorRoleAttemptRepository(s.session)
    s.reservation_id = uuid4()
    return s


def test_sqlite_trigger_suppressed_cas_rolls_back_entire_caller_uow(staged):
    s = staged
    with s.session.begin():
        before = reservation.snapshot(s)
        s.session.execute(
            sa.text(
                "CREATE TRIGGER suppress_reservation BEFORE UPDATE OF attempt_count ON "
                f"{Intent.__tablename__} WHEN NEW.attempt_count = 1 BEGIN SELECT RAISE(IGNORE); END"
            )
        )

    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        s.session.execute(sa.update(reservation.staging.Identity).values(profile_sync_json='{"caller":1}'))
        reservation.reserve(s)

    with s.session.begin():
        assert reservation.snapshot(s) == before
        s.session.execute(sa.text("DROP TRIGGER suppress_reservation"))


def test_replay_rejects_actual_changed_historical_roles_without_writes(staged):
    s = staged
    with s.session.begin():
        reservation.reserve(s)
    with s.session.begin():
        s.session.execute(
            sa.update(History)
            .where(History.id == s.history.id)
            .values(last_applied_roles_json='{"independent_change":true}')
        )
        changed = reservation.snapshot(s)

    statements = reservation.trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reservation.reserve(s)
    assert not reservation.dml(statements)
    with s.session.begin():
        assert reservation.snapshot(s) == changed
