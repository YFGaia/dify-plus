"""Actual SQLite local reservation tests; no fixture confers remote authority."""

import ast
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from models.account import Account, AccountStatus, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_attempt_repository_extend import CasdoorRoleAttemptRepository
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict
from sqlalchemy.dialects import mysql, postgresql

from tests.unit_tests.repositories import test_casdoor_role_intent_repository_extend as staging

storage = staging.storage


@pytest.fixture
def staged(storage):
    s = storage
    with s.session.begin():
        s.receipt = staging.enqueue(s)
    s.attempts = CasdoorRoleAttemptRepository(s.session)
    s.reservation_id = uuid4()
    return s


def reserve(s, **kwargs):
    return s.attempts.reserve(
        s.version,
        s.target,
        intent_id=kwargs.get("intent_id", s.receipt.intent_id),
        reservation_id=kwargs.get("reservation_id", s.reservation_id),
    )


def snapshot(s):
    return {
        model.__tablename__: s.session.execute(sa.select(*model.__table__.columns)).all()
        for model in (Intent, staging.History, TenantAccountJoin, staging.Identity)
    }


def trace(s):
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    return statements


def dml(statements):
    return [stmt for stmt in statements if stmt.startswith(("INSERT", "UPDATE", "DELETE"))]


def test_real_enqueue_reserve_exact_shape_readonly_replay_and_outer_rollback(staged):
    s = staged
    with s.session.begin():
        before = snapshot(s)
    statements = trace(s)
    with pytest.raises(RuntimeError, match="later"), s.session.begin():
        first = reserve(s)
        assert first.intent_id == s.receipt.intent_id
        assert first.scope_digest == s.receipt.scope_digest
        assert first.desired_payload_digest == s.receipt.desired_payload_digest
        assert first.membership_id == s.receipt.membership_id and first.join_id == s.receipt.join_id
        after = snapshot(s)
        for table in before.keys() - {Intent.__tablename__}:
            assert before[table] == after[table]
        original, changed = (dict(value[Intent.__tablename__][0]._mapping) for value in (before, after))
        assert changed.pop("attempt_count") == 1
        assert changed.pop("attempt_id") == str(s.reservation_id)
        assert original.pop("attempt_count") == 0 and original.pop("attempt_id") is None
        original.pop("updated_at")
        changed.pop("updated_at")
        assert original == changed
        assert len(dml(statements)) == 1
        statements.clear()
        assert reserve(s) == first
        assert not dml(statements)
        with pytest.raises(FrozenInstanceError):
            first.reservation_id = uuid4()
        assert str(first.intent_id) not in repr(first)
        raise RuntimeError("later caller failure")
    with s.session.begin():
        assert snapshot(s) == before


def test_committed_reservation_replay_cannot_restage_or_change_id(staged):
    s = staged
    with s.session.begin():
        first = reserve(s)
    statements = trace(s)
    with s.session.begin():
        assert reserve(s) == first
    assert not dml(statements)
    for operation in (lambda: reserve(s, reservation_id=uuid4()), lambda: staging.enqueue(s)):
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            operation()
    assert not dml(statements)


@pytest.mark.parametrize("interference", ["count", "scope", "payload", "proof", "epoch"])
def test_full_cas_interference_fails_and_whole_uow_rolls_back(staged, interference):
    s = staged
    with s.session.begin():
        before = snapshot(s)
    changed = False

    @sa.event.listens_for(s.session, "do_orm_execute")
    def race(state):
        nonlocal changed
        if state.is_update and not changed:
            changed = True
            values = {
                "count": {"attempt_count": 2},
                "scope": {"scope_digest": "f" * 64},
                "payload": {"desired_json": "{}"},
                "proof": {"proof_ref": "untrusted"},
                "epoch": {"ownership_epoch": 8},
            }[interference]
            s.session.connection().execute(sa.update(Intent).values(**values))

    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        # An earlier caller write must roll back too, not just the failed CAS.
        s.session.connection().execute(sa.update(staging.Identity).values(profile_sync_json='{"caller":1}'))
        reserve(s)
    assert changed
    with s.session.begin():
        assert snapshot(s) == before


@pytest.mark.parametrize(
    "values",
    [
        {"attempt_count": 1},
        {"attempt_id": str(uuid4())},
        {"attempt_count": 2},
        {"operation_state": CasdoorOperationState.UNKNOWN},
        {"operation_state": CasdoorOperationState.IN_FLIGHT},
        {"operation_state": CasdoorOperationState.APPLIED, "termination_state": CasdoorTerminationState.CONFIRMED},
        {"termination_state": CasdoorTerminationState.UNCONFIRMED},
        {"resource_type": "workspace", "resource_id": str(uuid4())},
        {"desired_json": "{}"},
        {"scope_digest": "f" * 64},
        {"idempotency_key": "e" * 64},
        {"ownership_epoch": 8},
        {"generation": 2},
        {"fence_epoch": 1},
    ]
    + [
        {field: datetime(2000, 1, 1)}
        for field in ("lease_expires_at", "sent_at", "acknowledged_at", "readback_at", "terminated_at", "retry_at")
    ]
    + [{field: "untrusted"} for field in ("lease_owner", "termination_proof_kind", "proof_ref", "error_code")],
)
@pytest.mark.parametrize("already_reserved", [False, True])
def test_any_scope_or_effect_drift_blocks_fresh_and_reentry(staged, values, already_reserved):
    s = staged
    if already_reserved:
        with s.session.begin():
            reserve(s)
        if values == {"attempt_count": 1}:
            values = {"attempt_count": 0}
    with s.session.begin():
        s.session.execute(sa.update(Intent).values(**values))
    statements = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reserve(s)
    assert not dml(statements)


@pytest.mark.parametrize("kind", list(CasdoorIntentKind))
@pytest.mark.parametrize("association", ["scope", "workspace_null", "membership"])
@pytest.mark.parametrize("state", [CasdoorOperationState.UNKNOWN, CasdoorOperationState.APPLIED])
def test_foreign_related_intent_blocks_except_unrelated_avatar(staged, kind, association, state):
    s = staged
    with s.session.begin():
        foreign = staging.Namespace(
            integration_id=s.integration.id,
            expected_issuer=s.namespace.expected_issuer,
            organization="Other",
            application="App",
            client_id="Client",
            core_fingerprint="d" * 64,
        )
        s.session.add(foreign)
        s.session.flush()
        s.session.add(
            Intent(
                namespace_id=foreign.id,
                identity_id=str(uuid4()),
                account_id=s.account.id if association != "membership" else str(uuid4()),
                workspace_id=s.workspace.id if association == "scope" else None,
                membership_id=s.history.id if association == "membership" else None,
                revision_id=s.revision.id,
                generation=0,
                ownership_epoch=0,
                fence_epoch=0,
                kind=kind,
                scope_digest="d" * 64,
                idempotency_key="e" * 64,
                desired_json="{}",
                operation_state=state,
                termination_state=CasdoorTerminationState.CONFIRMED,
            )
        )
    if kind is CasdoorIntentKind.PROFILE_AVATAR:
        with s.session.begin():
            reserve(s)
    else:
        statements = trace(s)
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            reserve(s)
        assert not dml(statements)


@pytest.mark.parametrize(
    "field", ["desired_json", "desired_roles_json", "baseline_json", "mappings_json", "policy_json"]
)
def test_unicode_bytes_guard_before_materialization(staged, field):
    s = staged
    model = (
        Intent
        if field == "desired_json"
        else staging.Revision
        if field.endswith("mappings_json") or field == "policy_json"
        else staging.History
    )
    with s.session.begin():
        s.session.execute(sa.update(model).values({field: "汉" * (180000 if field == "mappings_json" else 22000)}))
    statements = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reserve(s)
    assert any("CAST(" in stmt and "BLOB" in stmt for stmt in statements)
    table = model.__tablename__
    assert not any(
        stmt.startswith("SELECT") and f"{table}.{field}" in stmt and "CAST(" not in stmt for stmt in statements
    )
    assert not dml(statements)


@pytest.mark.parametrize(
    "pending", ["new", "dirty", "deleted", "nested", "no_root", "rbac_off", "uuid", "reservation_uuid"]
)
def test_clean_root_and_uuid_refuse_before_sql(staged, monkeypatch, pending):
    s = staged
    if pending != "no_root":
        s.session.begin()
    if pending == "new":
        s.session.add(Account(name="Pending", email="pending@example.test"))
    elif pending == "dirty":
        s.account.name = "Pending"
    elif pending == "deleted":
        s.session.delete(s.account)
    elif pending == "nested":
        s.session.begin_nested()
    elif pending == "rbac_off":
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    statements = trace(s)
    kwargs = (
        {"intent_id": str(s.receipt.intent_id)}
        if pending == "uuid"
        else {"reservation_id": True}
        if pending == "reservation_uuid"
        else {}
    )
    with pytest.raises((CasdoorRoleIntentConflict, RuntimeError)):
        reserve(s, **kwargs)
    assert not statements
    s.session.rollback()


@pytest.mark.parametrize("already_reserved", [False, True])
@pytest.mark.parametrize(
    "model,field,value",
    [
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
        ("history", "last_applied_fingerprint", "f" * 64),
        ("history", "ownership", CasdoorMembershipOwnership.LOCAL_OVERRIDE),
        ("history", "finalization", CasdoorFinalizationState.FINALIZED),
        ("history", "tombstone", True),
        ("history", "desired_roles_json", "{}"),
    ],
)
def test_actual_current_chain_required_on_fresh_and_reentry(staged, already_reserved, model, field, value):
    s = staged
    if already_reserved:
        with s.session.begin():
            reserve(s)
    row = getattr(s, model)
    with s.session.begin():
        s.session.execute(sa.update(type(row)).where(type(row).id == row.id).values({field: value}))
    statements = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reserve(s)
    assert not dml(statements)


def test_lock_order_full_cas_dialect_shape_and_no_remote_api(staged):
    s = staged
    statements = []
    sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
    with s.session.begin():
        reserve(s)
    locked = [stmt for stmt in statements if isinstance(stmt, sa.sql.Select) and stmt._for_update_arg is not None]
    tables = [next(iter(stmt.get_final_froms())).name for stmt in locked]
    assert tables == [
        model.__tablename__
        for model in (
            staging.Integration,
            staging.Namespace,
            Account,
            staging.Identity,
            staging.Tenant,
            TenantAccountJoin,
            staging.History,
            staging.History,
            Intent,
            Intent,
        )
    ]
    updates = [stmt for stmt in statements if isinstance(stmt, sa.sql.dml.Update)]
    assert len(updates) == 1
    for dialect in (postgresql.dialect(), mysql.dialect()):
        assert all("FOR UPDATE" in str(stmt.compile(dialect=dialect)) for stmt in locked)
        sql = str(updates[0].compile(dialect=dialect))
        where = sql.split(" WHERE ")[1]
        for column in Intent.__table__.columns:
            if column.name not in ("created_at", "updated_at"):
                assert f".{column.name} " in where
    tree = ast.parse(
        Path(__file__).parents[3].joinpath("repositories/casdoor_role_attempt_repository_extend.py").read_text()
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"enqueue", "flush", "commit", "rollback", "begin_nested", "send_request", "replace_rbac"}
        for node in ast.walk(tree)
    )
    assert not any(
        hasattr(s.attempts, name) for name in ("dispatch", "retry", "cancel", "mark_applied", "mark_confirmed")
    )


@pytest.mark.parametrize("change", ["missing", "avatar_collision", "wrong_id"])
def test_exact_requested_intent_must_be_present_and_role_kind(staged, change):
    s = staged
    with s.session.begin():
        if change == "missing":
            s.session.execute(sa.delete(Intent))
        elif change == "avatar_collision":
            s.session.execute(sa.update(Intent).values(kind=CasdoorIntentKind.PROFILE_AVATAR))
    statements = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reserve(s, intent_id=uuid4()) if change == "wrong_id" else reserve(s)
    assert not dml(statements)


@pytest.mark.parametrize("bad", ["owner", "builtin_long", "generation_bool", "fence_bool"])
def test_bad_plan_refuses_before_sql(staged, bad):
    from dataclasses import replace

    s = staged
    if bad in ("owner", "builtin_long"):
        s.target = replace(s.target, **({"target_role": "owner"} if bad == "owner" else {"builtin_id": "汉" * 86}))
        s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,)))
    elif bad == "generation_bool":
        s.version = replace(s.version, generation=True)
    else:
        s.version = replace(s.version, fence_epoch=False)
    statements = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reserve(s)
    assert not statements


def test_reserved_reentry_rechecks_new_related_generation(staged):
    s = staged
    with s.session.begin():
        reserve(s)
    with s.session.begin():
        values = dict(staging.stored(s)[0]._mapping)
        values.update(
            id=str(uuid4()),
            generation=2,
            scope_digest="f" * 64,
            idempotency_key="e" * 64,
            operation_state=CasdoorOperationState.UNKNOWN,
            termination_state=CasdoorTerminationState.UNCONFIRMED,
        )
        s.session.execute(sa.insert(Intent).values(**values))
    statements = trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        reserve(s)
    assert not dml(statements)


def test_real_or_copied_reservation_cannot_open_production_gates(staged):
    from copy import copy
    from dataclasses import asdict

    from core.casdoor.rbac_reader import RbacReadError, read_rbac
    from core.casdoor.rbac_replace import RbacReplaceError, replace_rbac

    s = staged
    with s.session.begin():
        receipt = reserve(s)
    for candidate in (receipt, copy(receipt), asdict(receipt), True):
        with pytest.raises(RbacReadError, match="authorization_pending"):
            read_rbac(capability=candidate)
        with pytest.raises(RbacReplaceError, match="authorization_pending"):
            replace_rbac(capability=candidate)
