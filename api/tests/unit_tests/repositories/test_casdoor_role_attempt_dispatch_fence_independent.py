"""Independent adversarial checks for the conservative ROLE_REPLACE fence."""

from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.rbac_replace import RbacReplaceError, replace_rbac
from models.casdoor_extend import CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_attempt_repository_extend import (
    CasdoorRoleAttemptRepository,
)
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict
from sqlalchemy.orm import Session

from tests.unit_tests.repositories import (
    test_casdoor_role_intent_repository_extend as staging,
)


@pytest.fixture
def storage(monkeypatch):
    # Reuse the accepted synthetic actual-SQLite fixture builder directly;
    # pytest does not discover fixtures merely because their test module is imported.
    yield from staging.storage.__wrapped__(monkeypatch)


@pytest.fixture
def reserved(storage):
    s = storage
    with s.session.begin():
        s.receipt = staging.enqueue(s)
    s.attempts = CasdoorRoleAttemptRepository(s.session)
    s.reservation_id = UUID("789a9d71-3e03-46af-8db9-5717b8f5074f")
    with s.session.begin():
        s.reservation = s.attempts.reserve(
            s.version, s.target, intent_id=s.receipt.intent_id, reservation_id=s.reservation_id
        )
    return s


def fence(s, *, intent_id=None, reservation_id=None):
    return s.attempts.mark_dispatch_unconfirmed(
        s.version,
        s.target,
        intent_id=intent_id or s.receipt.intent_id,
        reservation_id=reservation_id or s.reservation_id,
    )


def intent_row(session):
    return dict(session.execute(sa.select(*Intent.__table__.columns)).one()._mapping)


def snapshot(s):
    return {
        model.__tablename__: s.session.execute(sa.select(*model.__table__.columns)).all()
        for model in (Intent, staging.History, staging.TenantAccountJoin, staging.Identity)
    }


def trace(s):
    statements = []
    sa.event.listen(
        s.engine, "before_cursor_execute", lambda _c, _u, statement, _p, _x, _m: statements.append(statement)
    )
    return statements


def dml(statements):
    return [statement for statement in statements if statement.startswith(("INSERT", "UPDATE", "DELETE"))]


def test_unknown_commit_ack_cannot_rearm_reservation_or_create_send_authority(reserved):
    s = reserved
    s.session.begin()
    observed = fence(s)
    original_commit = s.session.commit
    commit_calls = 0

    def lose_ack_after_commit():
        nonlocal commit_calls
        commit_calls += 1
        original_commit()
        raise OSError("simulated acknowledgement loss")

    s.session.commit = lose_ack_after_commit
    with pytest.raises(OSError, match="acknowledgement loss"):
        s.session.commit()
    assert commit_calls == 1

    sql = trace(s)
    with Session(s.engine) as fresh:
        owner = CasdoorRoleAttemptRepository(fresh)
        with fresh.begin():
            replay = owner.mark_dispatch_unconfirmed(
                s.version, s.target, intent_id=s.receipt.intent_id, reservation_id=s.reservation_id
            )
            row = intent_row(fresh)
        assert replay == observed
        assert row["operation_state"] is CasdoorOperationState.IN_FLIGHT
        assert row["termination_state"] is CasdoorTerminationState.UNCONFIRMED
        assert row["attempt_id"] == str(s.reservation_id) and row["attempt_count"] == 1
        assert all(
            row[name] is None
            for name in (
                "lease_owner",
                "lease_expires_at",
                "sent_at",
                "acknowledged_at",
                "readback_at",
                "terminated_at",
                "termination_proof_kind",
                "proof_ref",
                "retry_at",
                "error_code",
            )
        )
        with pytest.raises(CasdoorRoleIntentConflict), fresh.begin():
            owner.reserve(s.version, s.target, intent_id=s.receipt.intent_id, reservation_id=s.reservation_id)
    assert not dml(sql)
    assert not any(hasattr(observed, field) for field in ("dispatch", "capability", "authorized", "session"))
    with pytest.raises(RbacReplaceError):
        replace_rbac(capability=observed)


@pytest.mark.parametrize("invalid_root", ["dirty", "nested", "inactive"])
def test_root_preflight_refusals_have_zero_driver_sql(reserved, monkeypatch, invalid_root):
    s = reserved
    s.session.begin()
    if invalid_root == "dirty":
        s.account.name = "unflushed mutation"
    elif invalid_root == "nested":
        s.session.begin_nested()
    elif invalid_root == "inactive":
        monkeypatch.setattr(Session, "is_active", property(lambda _session: False))

    sql = trace(s)
    with pytest.raises((CasdoorRoleIntentConflict, RuntimeError)):
        fence(s)
    assert sql == []
    s.session.rollback()


def test_two_foreign_related_non_avatar_rows_are_ambiguous(reserved):
    s = reserved
    with s.session.begin():
        original = intent_row(s.session)
        competing = []
        for marker in ("b", "c"):
            clone = dict(original)
            clone.update(
                id=str(uuid4()),
                # Same account/workspace relationship, but independently
                # keyed foreign intents; the bounded query must reject the
                # pair as ambiguous before arming the caller-selected row.
                identity_id=str(uuid4()),
                scope_digest=marker * 64,
                idempotency_key=marker * 64,
                created_at=datetime(2001, 1, 1),
            )
            competing.append(clone)
        s.session.execute(sa.insert(Intent), competing)
    sql = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        fence(s)
    assert not dml(sql)


@pytest.mark.parametrize(
    "trigger_sql",
    [
        "SELECT RAISE(IGNORE);",
        "UPDATE casdoor_sync_intent_extend SET retry_at='2001-01-01 00:00:00' WHERE id=NEW.id;",
        "UPDATE casdoor_sync_intent_extend SET attempt_id='00000000-0000-0000-0000-000000000001' WHERE id=NEW.id;",
    ],
)
def test_sqlite_trigger_suppression_or_effect_injection_aborts_prior_root_write(reserved, trigger_sql):
    s = reserved
    with s.session.begin():
        before = snapshot(s)
        s.session.execute(
            sa.text(
                "CREATE TRIGGER hostile_fence BEFORE UPDATE OF operation_state "
                "ON casdoor_sync_intent_extend WHEN NEW.operation_state='in_flight' "
                f"BEGIN {trigger_sql} END"
            )
        )
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        s.session.connection().execute(
            sa.update(staging.Identity).values(profile_sync_json='{"root":"must roll back"}')
        )
        fence(s)
    with s.session.begin():
        assert snapshot(s) == before
