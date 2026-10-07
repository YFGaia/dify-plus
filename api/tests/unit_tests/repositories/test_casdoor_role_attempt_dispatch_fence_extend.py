"""Actual SQLite conservative fence; no observation enables remote dispatch."""

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.rbac_replace import RbacReplaceError, replace_rbac
from models.account import Account, AccountStatus, TenantAccountRole
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_attempt_repository_extend import (
    CasdoorRoleAttemptRepository,
)
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import Session

from tests.unit_tests.repositories import (
    test_casdoor_role_attempt_repository_extend as reservation,
)

storage = reservation.storage
staged = reservation.staged


@pytest.fixture
def reserved(staged):
    with staged.session.begin():
        reservation.reserve(staged)
    return staged


def arm(s, **kwargs):
    return s.attempts.mark_dispatch_unconfirmed(
        s.version,
        s.target,
        intent_id=kwargs.get("intent_id", s.receipt.intent_id),
        reservation_id=kwargs.get("reservation_id", s.reservation_id),
    )


def row(s):
    return dict(s.session.execute(sa.select(*Intent.__table__.columns)).one()._mapping)


def test_actual_chain_changes_only_two_states_and_fresh_root_replay_is_readonly(reserved):
    s = reserved
    with s.session.begin():
        before = reservation.snapshot(s)
    statements = reservation.trace(s)
    with s.session.begin():
        result = arm(s)
        after = reservation.snapshot(s)
        assert result.intent_id == s.receipt.intent_id
        assert result.reservation_id == s.reservation_id
        assert result.scope_digest == s.receipt.scope_digest
        assert result.desired_payload_digest == s.receipt.desired_payload_digest
        assert result.membership_id == s.receipt.membership_id and result.join_id == s.receipt.join_id
        for table in before.keys() - {Intent.__tablename__}:
            assert after[table] == before[table]
        old, new = (dict(x[Intent.__tablename__][0]._mapping) for x in (before, after))
        assert new.pop("operation_state") is CasdoorOperationState.IN_FLIGHT
        assert new.pop("termination_state") is CasdoorTerminationState.UNCONFIRMED
        assert old.pop("operation_state") is CasdoorOperationState.PENDING
        assert old.pop("termination_state") is CasdoorTerminationState.NOT_STARTED
        old.pop("updated_at")
        new.pop("updated_at")
        assert new == old
    assert len(reservation.dml(statements)) == 1
    statements.clear()
    with Session(s.engine) as fresh, fresh.begin():
        fresh_owner = CasdoorRoleAttemptRepository(fresh)
        assert (
            fresh_owner.mark_dispatch_unconfirmed(
                s.version, s.target, intent_id=s.receipt.intent_id, reservation_id=s.reservation_id
            )
            == result
        )
    assert not reservation.dml(statements)
    with pytest.raises(FrozenInstanceError):
        result.reservation_id = uuid4()
    assert str(result.intent_id) not in repr(result)
    assert not any(hasattr(result, name) for name in ("capability", "dispatch", "authorized", "retry", "created"))
    with pytest.raises(RbacReplaceError):
        replace_rbac(capability=result)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reservation.reserve(s)


@pytest.mark.parametrize("field", ["intent_id", "reservation_id"])
def test_uuid_subclasses_are_not_canonical_input(reserved, field):
    class OtherUUID(UUID):
        pass

    s = reserved
    value = s.receipt.intent_id if field == "intent_id" else s.reservation_id
    statements = reservation.trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        arm(s, **{field: OtherUUID(str(value))})
    assert not statements


def test_inactive_session_rejects_before_sql(reserved, monkeypatch):
    s = reserved
    s.session.begin()
    statements = reservation.trace(s)
    monkeypatch.setattr(Session, "is_active", property(lambda _session: False))
    with pytest.raises(RuntimeError):
        arm(s)
    assert not statements
    s.session.rollback()


@pytest.mark.parametrize("change", ["no_reservation", "wrong_reservation", "wrong_id", "missing", "avatar_kind"])
def test_absent_wrong_or_non_role_reservation_refuses_without_dml(staged, change):
    s = staged
    if change != "no_reservation":
        with s.session.begin():
            reservation.reserve(s)
            if change == "missing":
                s.session.execute(sa.delete(Intent))
            elif change == "avatar_kind":
                s.session.execute(sa.update(Intent).values(kind=CasdoorIntentKind.PROFILE_AVATAR))
    statements = reservation.trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        arm(
            s,
            **(
                {"reservation_id": uuid4()}
                if change == "wrong_reservation"
                else {"intent_id": uuid4()}
                if change == "wrong_id"
                else {}
            ),
        )
    assert not reservation.dml(statements)


_EFFECTS = [
    {name: datetime(2000, 1, 1)}
    for name in ("lease_expires_at", "sent_at", "acknowledged_at", "readback_at", "terminated_at", "retry_at")
] + [{name: "untrusted"} for name in ("lease_owner", "termination_proof_kind", "proof_ref", "error_code")]


@pytest.mark.parametrize("already_armed", [False, True])
@pytest.mark.parametrize(
    "values",
    _EFFECTS
    + [
        {"attempt_count": 0},
        {"attempt_count": 2},
        {"attempt_id": None},
        {"attempt_id": str(uuid4())},
        {"operation_state": CasdoorOperationState.UNKNOWN},
        {"operation_state": CasdoorOperationState.APPLIED},
        {"operation_state": CasdoorOperationState.FAILED},
        {"operation_state": CasdoorOperationState.CANCELLED},
        {"termination_state": CasdoorTerminationState.CONFIRMED},
        {"termination_state": CasdoorTerminationState.MANUAL_RECOVERY},
        {"operation_state": CasdoorOperationState.IN_FLIGHT, "termination_state": CasdoorTerminationState.NOT_STARTED},
        {"operation_state": CasdoorOperationState.PENDING, "termination_state": CasdoorTerminationState.UNCONFIRMED},
        {"resource_type": "app", "resource_id": str(uuid4())},
        {"desired_json": "{}"},
        {"scope_digest": "f" * 64},
        {"idempotency_key": "e" * 64},
        {"generation": 2},
        {"fence_epoch": 1},
        {"ownership_epoch": 8},
    ],
)
def test_every_scope_state_or_effect_drift_refuses_arm_and_replay(reserved, already_armed, values):
    s = reserved
    if already_armed:
        with s.session.begin():
            arm(s)
    with s.session.begin():
        s.session.execute(sa.update(Intent).values(**values))
    statements = reservation.trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        arm(s)
    assert not reservation.dml(statements)


_CHAIN = [
    ("identity", "sync_generation", 2),
    ("identity", "subject", "Changed"),
    ("identity", "account_id", str(uuid4())),
    ("namespace", "fence_epoch", 1),
    ("revision", "config_digest", "f" * 64),
    ("revision", "mappings_json", "[]"),
    ("integration", "enabled", False),
    ("integration", "active_revision_id", None),
    ("account", "status", AccountStatus.BANNED),
    ("join", "role", TenantAccountRole.OWNER),
    ("history", "join_id", str(uuid4())),
    ("history", "desired_generation", 2),
    ("history", "ownership_epoch", 8),
    ("history", "ownership_epoch", 2**63 - 1),
    ("history", "last_applied_fingerprint", "f" * 64),
    ("history", "ownership", CasdoorMembershipOwnership.LOCAL_OVERRIDE),
    ("history", "finalization", CasdoorFinalizationState.FINALIZED),
    ("history", "tombstone", True),
    ("history", "desired_roles_json", "{}"),
    ("history", "baseline_json", "{}"),
    ("history", "baseline_json", '{"schema_version":2}'),
]


@pytest.mark.parametrize("already_armed", [False, True])
@pytest.mark.parametrize(
    "model,field,value", _CHAIN, ids=[f"{model}-{field}-{index}" for index, (model, field, _value) in enumerate(_CHAIN)]
)
def test_actual_chain_drift_rejected_before_write(reserved, already_armed, model, field, value):
    s = reserved
    if already_armed:
        with s.session.begin():
            arm(s)
    obj = getattr(s, model)
    with s.session.begin():
        s.session.execute(sa.update(type(obj)).where(type(obj).id == obj.id).values({field: value}))
    statements = reservation.trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        arm(s)
    assert not reservation.dml(statements)


@pytest.mark.parametrize("kind", list(CasdoorIntentKind))
@pytest.mark.parametrize("association", ["scope", "workspace_null", "membership"])
@pytest.mark.parametrize("already_armed", [False, True])
def test_foreign_related_intent_blocks_all_non_avatar_kinds(reserved, kind, association, already_armed):
    s = reserved
    if already_armed:
        with s.session.begin():
            arm(s)
    with s.session.begin():
        values = row(s)
        values.update(
            id=str(uuid4()),
            identity_id=str(uuid4()),
            generation=2,
            scope_digest="f" * 64,
            idempotency_key="e" * 64,
            kind=kind,
            operation_state=CasdoorOperationState.UNKNOWN,
            termination_state=CasdoorTerminationState.UNCONFIRMED,
            account_id=str(uuid4()) if association == "membership" else s.account.id,
            workspace_id=s.workspace.id if association == "scope" else None,
            membership_id=s.history.id if association == "membership" else None,
        )
        s.session.execute(sa.insert(Intent).values(**values))
    statements = reservation.trace(s)
    if kind is CasdoorIntentKind.PROFILE_AVATAR:
        with s.session.begin():
            arm(s)
        assert len(reservation.dml(statements)) == (0 if already_armed else 1)
    else:
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            arm(s)
        assert not reservation.dml(statements)


@pytest.mark.parametrize(
    "invalid",
    [
        "new",
        "dirty",
        "deleted",
        "nested",
        "no_root",
        "rbac_off",
        "id_string",
        "id_bool",
        "reservation_string",
        "reservation_bool",
        "generation_bool",
        "generation_overflow",
        "fence_bool",
        "fence_overflow",
        "target_owner",
    ],
)
def test_invalid_input_or_root_has_zero_sql(reserved, monkeypatch, invalid):
    s = reserved
    if invalid != "no_root":
        s.session.begin()
    if invalid == "new":
        s.session.add(Account(name="Pending", email="pending@example.test"))
    elif invalid == "dirty":
        s.account.name = "Pending"
    elif invalid == "deleted":
        s.session.delete(s.account)
    elif invalid == "nested":
        s.session.begin_nested()
    elif invalid == "rbac_off":
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    elif invalid.startswith("generation_"):
        s.version = replace(s.version, generation=True if invalid.endswith("bool") else 2**63)
    elif invalid.startswith("fence_"):
        s.version = replace(s.version, fence_epoch=False if invalid.endswith("bool") else 2**63)
    elif invalid == "target_owner":
        s.target = replace(s.target, target_role="owner")
        s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,)))
    kwargs = {}
    if invalid in ("id_string", "id_bool"):
        kwargs["intent_id"] = str(s.receipt.intent_id) if invalid.endswith("string") else True
    elif invalid in ("reservation_string", "reservation_bool"):
        kwargs["reservation_id"] = str(s.reservation_id) if invalid.endswith("string") else False
    statements = reservation.trace(s)
    with pytest.raises((CasdoorRoleIntentConflict, RuntimeError)):
        arm(s, **kwargs)
    assert not statements
    s.session.rollback()


@pytest.mark.parametrize("interference", ["count", "payload", "proof", "epoch", "timestamp"])
def test_full_cas_race_rolls_back_whole_caller_root(reserved, interference):
    s = reserved
    with s.session.begin():
        before = reservation.snapshot(s)
    changed = False

    @sa.event.listens_for(s.session, "do_orm_execute")
    def race(state):
        nonlocal changed
        if state.is_update and not changed:
            changed = True
            values = {
                "count": {"attempt_count": 2},
                "payload": {"desired_json": "{}"},
                "proof": {"proof_ref": "untrusted"},
                "epoch": {"ownership_epoch": 8},
                "timestamp": {"created_at": datetime(2000, 1, 1)},
            }[interference]
            s.session.connection().execute(sa.update(Intent).values(**values))

    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        s.session.connection().execute(sa.update(reservation.staging.Identity).values(profile_sync_json='{"caller":1}'))
        arm(s)
    assert changed
    with s.session.begin():
        assert reservation.snapshot(s) == before


@pytest.mark.parametrize("interference", ["suppress", "effect", "parent", "timestamp"])
def test_trigger_interference_and_postwrite_scope_reread_abort_everything(reserved, interference):
    s = reserved
    with s.session.begin():
        before = reservation.snapshot(s)
        action = {
            "suppress": "SELECT RAISE(IGNORE);",
            "effect": f"UPDATE {Intent.__tablename__} SET proof_ref='untrusted' WHERE id=NEW.id;",
            "parent": f"UPDATE {reservation.staging.Identity.__tablename__} SET sync_generation=2;",
            "timestamp": f"UPDATE {Intent.__tablename__} SET created_at='2000-01-01 00:00:00' WHERE id=NEW.id;",
        }[interference]
        timing = "BEFORE" if interference == "suppress" else "AFTER"
        s.session.execute(
            sa.text(
                f"CREATE TRIGGER corrupt_fence {timing} UPDATE OF operation_state ON {Intent.__tablename__} WHEN NEW.operation_state='in_flight' BEGIN {action} END"
            )
        )
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        s.session.connection().execute(sa.update(reservation.staging.Identity).values(profile_sync_json='{"caller":1}'))
        arm(s)
    with s.session.begin():
        assert reservation.snapshot(s) == before


def test_postwrite_sql_exception_propagates_and_outer_root_rollback_restores_prior_write(reserved, monkeypatch):
    s = reserved
    with s.session.begin():
        before = reservation.snapshot(s)
    original = s.attempts._read_attempt_scope
    count = 0

    def fail_later(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("later SQL read failure")
        return original(*args)

    monkeypatch.setattr(s.attempts, "_read_attempt_scope", fail_later)
    with pytest.raises(RuntimeError, match="later SQL"), s.session.begin():
        s.session.connection().execute(sa.update(reservation.staging.Identity).values(profile_sync_json='{"caller":1}'))
        arm(s)
    with s.session.begin():
        assert reservation.snapshot(s) == before


def test_fixture_commit_failure_rolls_back_without_repository_retry(reserved):
    s = reserved
    with s.session.begin():
        before = reservation.snapshot(s)

    def reject_commit(session):
        raise RuntimeError("fixture commit failed")

    sa.event.listen(s.session, "before_commit", reject_commit)
    try:
        with pytest.raises(RuntimeError, match="fixture commit failed"), s.session.begin():
            arm(s)
    finally:
        sa.event.remove(s.session, "before_commit", reject_commit)
        s.session.rollback()
    with s.session.begin():
        assert reservation.snapshot(s) == before


def test_simulated_lost_ack_keeps_durable_quarantine_and_never_rearms(reserved):
    s = reserved
    original_commit = s.session.commit
    calls = 0

    def commit_then_lose_ack():
        nonlocal calls
        calls += 1
        original_commit()
        raise RuntimeError("fixture lost commit ack")

    s.session.begin()
    arm(s)
    with pytest.raises(RuntimeError, match="lost commit ack"):
        commit_then_lose_ack()
    assert calls == 1
    statements = reservation.trace(s)
    with Session(s.engine) as fresh, fresh.begin():
        observation = CasdoorRoleAttemptRepository(fresh).mark_dispatch_unconfirmed(
            s.version, s.target, intent_id=s.receipt.intent_id, reservation_id=s.reservation_id
        )
        actual = dict(fresh.execute(sa.select(*Intent.__table__.columns)).one()._mapping)
        assert actual["operation_state"] is CasdoorOperationState.IN_FLIGHT
        assert actual["termination_state"] is CasdoorTerminationState.UNCONFIRMED
        assert actual["attempt_count"] == 1 and actual["sent_at"] is None
    assert not reservation.dml(statements)
    with pytest.raises(RbacReplaceError):
        replace_rbac(capability=observation)


def test_cas_contains_every_column_and_source_has_no_io_or_transaction_owner(reserved):
    s = reserved
    statements = []
    sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
    with s.session.begin():
        arm(s)
    updates = [stmt for stmt in statements if isinstance(stmt, sa.sql.dml.Update)]
    assert len(updates) == 1
    for dialect in (postgresql.dialect(), mysql.dialect()):
        where = str(updates[0].compile(dialect=dialect)).split(" WHERE ")[1]
        assert all(f".{column.name} " in where for column in Intent.__table__.columns)
    tree = ast.parse(
        Path(__file__).parents[3].joinpath("repositories/casdoor_role_attempt_repository_extend.py").read_text()
    )
    banned = {
        "flush",
        "commit",
        "rollback",
        "begin",
        "begin_nested",
        "enqueue",
        "ensure_owned",
        "send_request",
        "replace_rbac",
        "delay",
        "publish",
        "issue",
    }
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in banned
        for node in ast.walk(tree)
    )
