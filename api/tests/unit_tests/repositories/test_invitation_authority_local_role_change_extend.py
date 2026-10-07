"""Offline sequential LOCAL role events; SQLite cannot prove concurrent lock safety."""

import json
import socket
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from core.casdoor.local_roles import LocalRoleOutcome
from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json, roles_fingerprint
from models.account import TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories import invitation_authority_repository_extend as authority_module
from repositories.casdoor_local_role_repository_extend import CasdoorLocalRoleConflict
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from tests.unit_tests.repositories.test_casdoor_local_role_repository_extend import apply, target
from tests.unit_tests.repositories.test_casdoor_local_role_repository_extend import storage as role_storage_fixture

storage = role_storage_fixture


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    attempts = []

    def deny(*_args, **_kwargs):
        attempts.append(1)
        raise AssertionError("offline network denied")

    for name in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    yield
    assert attempts == []


def scope(s):
    return dict(account_id=str(s.version.plan.context.account_id), workspace_id=str(s.target.workspace_id))


def lifecycle(s):
    return InvitationAuthorityRepository().get_lifecycle(s.session, **scope(s))


def role_rows(s):
    return (
        tuple(s.session.execute(sa.select(*TenantAccountJoin.__table__.columns)).all()),
        tuple(s.session.execute(sa.select(*History.__table__.columns)).all()),
    )


def seed_lifecycle(s, prior, *, epoch=11):
    with s.session.begin():
        s.session.execute(sa.delete(Lifecycle))
        if prior:
            record = InvitationAuthorityRepository().set_lifecycle_state(s.session, **scope(s), state=prior)
            row = s.session.get(Lifecycle, record.lifecycle_id)
            row.epoch = epoch
            row.updated_at = datetime(2020, 1, 1)
        protected = InvitationAuthorityRepository().set_lifecycle_state(
            s.session, account_id=str(uuid4()), workspace_id=str(uuid4()), state="withdrawn"
        )
    return protected


def set_initial_role(s, role):
    role = TenantAccountRole(role)
    observation = MembershipObservation(
        s.target.workspace_id, s.version.plan.context.account_id, UUID(s.join.id), role, MembershipBackend.LOCAL
    )
    with s.session.begin():
        s.session.execute(sa.update(TenantAccountJoin).values(role=role))
        s.session.execute(
            sa.update(History).values(
                last_applied_roles_json=role_baseline_json(observation),
                last_applied_fingerprint=roles_fingerprint(observation),
            )
        )


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("old,new", [("normal", "admin"), ("admin", "normal")])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_actual_upgrade_downgrade_advance_once_after_join_and_history_in_caller_root(
    storage, monkeypatch, prior, old, new, finish
):
    s = storage
    set_initial_role(s, old)
    target(s, new, fallback=new == "normal")
    protected = seed_lifecycle(s, prior)
    s.session.begin()
    root = s.session.get_transaction()
    before_rows, before = role_rows(s), lifecycle(s)
    calls, flushes = [], []
    original = InvitationAuthorityRepository.record_local_role_change
    original_flush = s.session.flush

    def observed(repo, session, **kwargs):
        assert session is s.session and kwargs == scope(s)
        assert session.get_transaction() is root
        assert not session.new and not session.dirty and not session.deleted
        join_rows, history_rows = role_rows(s)
        assert join_rows[0].role.value == new
        assert json.loads(history_rows[0].last_applied_roles_json)["join_role"] == new
        assert json.loads(history_rows[0].desired_roles_json)["target_role"] == new
        assert history_rows[0].last_applied_fingerprint != before_rows[1][0].last_applied_fingerprint
        calls.append(1)
        return original(repo, session, **kwargs)

    def flush(objects=None):
        if objects is not None:
            flushes.append(list(objects))
        return original_flush(objects)

    monkeypatch.setattr(InvitationAuthorityRepository, "record_local_role_change", observed)
    monkeypatch.setattr(s.session, "flush", flush)
    with monkeypatch.context() as guard:
        for name in ("begin", "begin_nested", "commit", "rollback", "close"):
            guard.setattr(s.session, name, Mock(side_effect=AssertionError(name)))
        receipt = apply(s)
        assert receipt.applied and receipt.role_changed and receipt.metadata_changed
        assert receipt.prior_role.value == old and receipt.current_role.value == new
        assert calls == [1]
        assert len(flushes) == 1 and len(flushes[0]) == 1 and isinstance(flushes[0][0], Lifecycle)
        after = lifecycle(s)
        assert after.epoch == (12 if before else 1)
        assert after.state == (prior or "active")
        assert UUID(after.lifecycle_id).version == 4
        if before:
            assert (after.lifecycle_id, after.state, after.created_at) == (
                before.lifecycle_id,
                before.state,
                before.created_at,
            )
            assert after.updated_at > before.updated_at
        assert apply(s).outcome is LocalRoleOutcome.NOOP
        assert calls == [1] and lifecycle(s) == after
        assert s.session.get_transaction() is root
    getattr(s.session, finish)()
    with s.session.begin():
        assert lifecycle(s) == (after if finish == "commit" else before)
        if finish == "rollback":
            assert role_rows(s) == before_rows
        else:
            assert role_rows(s)[0][0].role.value == new
        assert (
            InvitationAuthorityRepository().get_lifecycle(
                s.session, account_id=protected.account_id, workspace_id=protected.workspace_id
            )
            == protected
        )


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize(
    "kind",
    [
        "noop",
        "metadata_generation",
        "unchanged_role",
        "owner",
        "dataset",
        "unmanaged",
        "released",
        "override",
        "tombstone",
        "drift",
        "absent",
        "intent",
    ],
)
def test_unchanged_protected_pending_and_metadata_paths_never_emit(storage, monkeypatch, prior, kind):
    s = storage
    if kind in ("noop", "metadata_generation"):
        with s.session.begin():
            apply(s)
        if kind == "metadata_generation":
            with s.session.begin():
                s.identity.sync_generation = 2
            s.version = replace(s.version, generation=2)
    elif kind == "unchanged_role":
        target(s, "normal")
    else:
        with s.session.begin():
            if kind in ("owner", "dataset"):
                s.join.role = TenantAccountRole.OWNER if kind == "owner" else TenantAccountRole.DATASET_OPERATOR
            elif kind == "unmanaged":
                s.session.delete(s.history)
            elif kind in ("released", "override"):
                s.history.ownership = (
                    CasdoorMembershipOwnership.RELEASED
                    if kind == "released"
                    else CasdoorMembershipOwnership.LOCAL_OVERRIDE
                )
            elif kind == "tombstone":
                s.history.tombstone = True
            elif kind == "drift":
                s.history.last_applied_fingerprint = "f" * 64
            elif kind == "absent":
                s.session.delete(s.join)
            else:
                s.session.add(
                    Intent(
                        namespace_id=s.namespace.id,
                        identity_id=s.identity.id,
                        account_id=s.account.id,
                        workspace_id=s.workspace.id,
                        membership_id=s.history.id,
                        revision_id=s.revision.id,
                        generation=1,
                        ownership_epoch=7,
                        fence_epoch=0,
                        kind=CasdoorIntentKind.ROLE_REPLACE,
                        scope_digest="a" * 64,
                        idempotency_key="b" * 64,
                        desired_json="{}",
                        operation_state=CasdoorOperationState.APPLIED,
                        termination_state=CasdoorTerminationState.CONFIRMED,
                    )
                )
    seed_lifecycle(s, prior, epoch=MAX_LIFECYCLE_EPOCH)
    event = Mock(side_effect=AssertionError("no actual role change"))
    monkeypatch.setattr(InvitationAuthorityRepository, "record_local_role_change", event)
    with s.session.begin():
        before, before_rows = lifecycle(s), role_rows(s)
        receipt = apply(s)
        assert not receipt.role_changed
        if kind in ("metadata_generation", "unchanged_role"):
            assert receipt.applied and receipt.metadata_changed
        elif kind == "noop":
            assert receipt.outcome is LocalRoleOutcome.NOOP
        else:
            assert receipt.outcome in (LocalRoleOutcome.PENDING, LocalRoleOutcome.PRESERVED)
            assert role_rows(s) == before_rows
        assert lifecycle(s) == before
    event.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["epoch_active", "epoch_withdrawn", "unique", "insert_after_write", "update_after_write", "schema"]
)
def test_lifecycle_failure_propagates_and_caller_restores_join_and_entire_history(storage, monkeypatch, failure):
    s = storage
    prior = (
        "withdrawn"
        if failure == "epoch_withdrawn"
        else "active"
        if failure in ("epoch_active", "update_after_write")
        else None
    )
    protected = seed_lifecycle(s, prior, epoch=MAX_LIFECYCLE_EPOCH if failure.startswith("epoch") else 11)
    with s.session.begin():
        before_rows, before = role_rows(s), lifecycle(s)
    if failure == "unique":
        monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(protected.lifecycle_id))
    if failure == "schema":
        Lifecycle.__table__.drop(s.engine)
    writes, calls = [], []
    original = InvitationAuthorityRepository.record_local_role_change

    def event(repo, session, **kwargs):
        assert role_rows(s)[0][0].role is TenantAccountRole.ADMIN
        assert json.loads(role_rows(s)[1][0].last_applied_roles_json)["join_role"] == "admin"
        calls.append(1)
        return original(repo, session, **kwargs)

    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(
            (
                "UPDATE tenant_account_joins",
                "UPDATE casdoor_managed_membership",
                "INSERT INTO invitation_authority",
                "UPDATE invitation_authority",
            )
        ):
            writes.append(statement)
            if failure.endswith("after_write") and "invitation_authority" in statement:
                raise RuntimeError("lifecycle_write_failed")

    monkeypatch.setattr(InvitationAuthorityRepository, "record_local_role_change", event)
    sa.event.listen(s.engine, "after_cursor_execute", observed)
    expected = (
        InvitationAuthorityConflict
        if failure.startswith("epoch")
        else IntegrityError
        if failure == "unique"
        else OperationalError
        if failure == "schema"
        else RuntimeError
    )
    try:
        with pytest.raises(expected), s.session.begin():
            apply(s)
        assert calls == [1]
        assert writes[0].startswith("UPDATE tenant_account_joins")
        assert writes[1].startswith("UPDATE casdoor_managed_membership")
    finally:
        sa.event.remove(s.engine, "after_cursor_execute", observed)
    with s.session.begin():
        assert role_rows(s) == before_rows
        if failure != "schema":
            assert lifecycle(s) == before
            assert (
                InvitationAuthorityRepository().get_lifecycle(
                    s.session, account_id=protected.account_id, workspace_id=protected.workspace_id
                )
                == protected
            )


@pytest.mark.parametrize("failure", ["join_cas", "readback", "history_cas"])
def test_sql_join_readback_and_history_failures_never_emit_event(storage, monkeypatch, failure):
    s = storage
    seed_lifecycle(s, "active")
    triggers = {
        "join_cas": (
            "CREATE TRIGGER reject_join BEFORE UPDATE OF role ON tenant_account_joins "
            "BEGIN SELECT RAISE(IGNORE); END"
        ),
        "readback": (
            "CREATE TRIGGER drift_join AFTER UPDATE OF role ON tenant_account_joins "
            "BEGIN UPDATE tenant_account_joins SET role = 'normal' WHERE id = NEW.id; END"
        ),
        "history_cas": (
            "CREATE TRIGGER reject_history BEFORE UPDATE ON casdoor_managed_membership_extend "
            "BEGIN SELECT RAISE(IGNORE); END"
        ),
    }
    with s.session.begin():
        before_rows, before = role_rows(s), lifecycle(s)
        s.session.execute(sa.text(triggers[failure]))
    event = Mock(side_effect=AssertionError("event before successful CAS/readback"))
    monkeypatch.setattr(InvitationAuthorityRepository, "record_local_role_change", event)
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)
    with s.session.begin():
        assert role_rows(s) == before_rows and lifecycle(s) == before
    event.assert_not_called()


@pytest.mark.parametrize("consumed", [False, True])
def test_actual_role_change_fences_old_issued_and_consumed_receipt(storage, consumed):
    s = storage
    Issuance.__table__.create(s.engine)
    seed_lifecycle(s, "active")
    authority = InvitationAuthorityRepository()
    with s.session.begin():
        before = lifecycle(s)
        facts = dict(
            schema_version=1,
            issuance_id=str(uuid4()),
            lifecycle_id=before.lifecycle_id,
            lifecycle_epoch=before.epoch,
            token_digest="a" * 64,
            join_id_at_issue=s.join.id,
        )
        issued = authority.record_issuance(
            s.session,
            payload_json=json.dumps(
                dict(
                    **scope(s),
                    email=s.account.email,
                    role="normal",
                    requires_setup=False,
                    invitation_authority=facts,
                )
            ),
        )
        receipt = json.dumps(
            dict(
                **facts,
                **scope(s),
                status="consumed",
                operation_id=str(uuid4()),
                payload_digest=issued.payload_digest,
                key_digest="b" * 64,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        if consumed:
            issued = authority.record_consumption(s.session, receipt_json=receipt)
    with s.session.begin():
        assert apply(s).role_changed
    with pytest.raises(InvitationAuthorityConflict, match="invitation_lifecycle_conflict"), s.session.begin():
        authority.record_consumption(s.session, receipt_json=receipt)
    with s.session.begin():
        assert lifecycle(s).epoch == before.epoch + 1
        assert authority.get_issuance(s.session, issuance_id=issued.issuance_id) == issued
        assert s.session.scalar(sa.select(sa.func.count()).select_from(Issuance)) == 1
