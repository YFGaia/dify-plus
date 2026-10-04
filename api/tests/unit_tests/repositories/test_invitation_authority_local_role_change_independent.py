"""Independent sequential checks for LOCAL role-change lifecycle events."""

import json
import socket
from datetime import datetime
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from core.casdoor.local_roles import LocalRoleOutcome
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from repositories import invitation_authority_repository_extend as authority_module
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from tests.unit_tests.repositories.test_casdoor_local_role_repository_extend import (
    apply,
    target,
)
from tests.unit_tests.repositories.test_casdoor_local_role_repository_extend import (
    storage as role_storage_fixture,
)

storage = role_storage_fixture


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    attempts = []

    def reject(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("network access denied for local role verification")

    for method in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, method, reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)
    yield
    assert attempts == []


def scope(s):
    return {"account_id": str(s.account.id), "workspace_id": str(s.workspace.id)}


def lifecycle(s):
    return InvitationAuthorityRepository().get_lifecycle(s.session, **scope(s))


def persisted_rows(s):
    return (
        tuple(s.session.execute(sa.select(*TenantAccountJoin.__table__.columns)).all()),
        tuple(s.session.execute(sa.select(*History.__table__.columns)).all()),
    )


def seed_lifecycle(s, state=None, *, epoch=9):
    authority = InvitationAuthorityRepository()
    with s.session.begin():
        s.session.execute(sa.delete(Lifecycle))
        prior = None
        if state is not None:
            prior = authority.set_lifecycle_state(s.session, **scope(s), state=state)
            row = s.session.get(Lifecycle, prior.lifecycle_id)
            row.epoch = epoch
            row.updated_at = datetime(2021, 1, 1)
            prior = authority.get_lifecycle(s.session, **scope(s))
        unrelated = authority.set_lifecycle_state(
            s.session,
            account_id=str(uuid4()),
            workspace_id=str(uuid4()),
            state="withdrawn",
        )
    return prior, unrelated


def initialize_current_role(s, role):
    role = TenantAccountRole(role)
    observation = MembershipObservation(
        s.target.workspace_id,
        s.version.plan.context.account_id,
        UUID(s.join.id),
        role,
        MembershipBackend.LOCAL,
    )
    with s.session.begin():
        s.session.execute(sa.update(TenantAccountJoin).values(role=role))
        s.session.execute(
            sa.update(History).values(
                last_applied_roles_json=role_baseline_json(observation),
                last_applied_fingerprint=roles_fingerprint(observation),
            )
        )


@pytest.mark.parametrize("old_role,new_role", [("normal", "admin"), ("admin", "normal")])
@pytest.mark.parametrize("prior_state", [None, "active", "withdrawn"])
def test_real_role_change_emits_event_after_join_and_history_cas(storage, monkeypatch, old_role, new_role, prior_state):
    s = storage
    if old_role != "normal":
        initialize_current_role(s, old_role)
    target(s, new_role, fallback=new_role == "normal")
    before, unrelated = seed_lifecycle(s, prior_state)
    original = InvitationAuthorityRepository.record_local_role_change
    session = s.session
    calls, flushes = [], []
    real_flush = session.flush

    def observe_event(repository, supplied, **identity):
        assert supplied is session
        assert identity == scope(s)
        assert supplied.get_transaction() is root_transaction
        assert not supplied.new and not supplied.dirty and not supplied.deleted
        persisted_join = supplied.execute(
            sa.select(TenantAccountJoin.role).where(TenantAccountJoin.id == s.join.id)
        ).scalar_one()
        history = supplied.execute(
            sa.select(History.last_applied_roles_json, History.desired_roles_json).where(History.id == s.history.id)
        ).one()
        assert persisted_join is TenantAccountRole(new_role)
        assert json.loads(history.last_applied_roles_json)["join_role"] == new_role
        assert json.loads(history.desired_roles_json)["target_role"] == new_role
        calls.append(True)
        record = original(repository, supplied, **identity)
        assert len(flushes) == 1 and len(flushes[0]) == 1
        assert isinstance(flushes[0][0], Lifecycle)
        return record

    def track_flush(objects=None):
        if objects is not None:
            flushes.append(list(objects))
        return real_flush(objects)

    monkeypatch.setattr(InvitationAuthorityRepository, "record_local_role_change", observe_event)
    monkeypatch.setattr(session, "flush", track_flush)
    with session.begin():
        root_transaction = session.get_transaction()
        previous_rows = persisted_rows(s)
        receipt = apply(s)
        assert receipt.applied and receipt.role_changed
        assert receipt.prior_role is TenantAccountRole(old_role)
        assert receipt.current_role is TenantAccountRole(new_role)
        assert calls == [True]
        current = lifecycle(s)
        assert current.epoch == (before.epoch + 1 if before else 1)
        assert current.state == (prior_state or "active")
        if before:
            assert (current.lifecycle_id, current.state, current.created_at) == (
                before.lifecycle_id,
                before.state,
                before.created_at,
            )
            assert current.updated_at > before.updated_at
        again = apply(s)
        assert again.outcome is LocalRoleOutcome.NOOP and not again.role_changed
        assert calls == [True] and lifecycle(s) == current
        assert session.get_transaction() is root_transaction
        assert persisted_rows(s) != previous_rows
        assert (
            InvitationAuthorityRepository().get_lifecycle(
                session,
                account_id=unrelated.account_id,
                workspace_id=unrelated.workspace_id,
            )
            == unrelated
        )
    with s.session.begin():
        assert lifecycle(s) == current


@pytest.mark.parametrize("blocked", ["metadata_only", "protected_owner"])
def test_role_unchanged_or_protected_membership_never_emits_event(storage, monkeypatch, blocked):
    s = storage
    target(s, "normal", fallback=True)
    if blocked == "protected_owner":
        with s.session.begin():
            s.session.execute(sa.update(TenantAccountJoin).values(role=TenantAccountRole.OWNER))
    previous, _unrelated = seed_lifecycle(s, "withdrawn", epoch=MAX_LIFECYCLE_EPOCH)
    event = Mock(side_effect=AssertionError("no real managed role change"))
    monkeypatch.setattr(InvitationAuthorityRepository, "record_local_role_change", event)
    with s.session.begin():
        assert lifecycle(s) == previous
        outcome = apply(s)
        if blocked == "metadata_only":
            assert outcome.applied and not outcome.role_changed and outcome.metadata_changed
            noop = apply(s)
            assert noop.outcome is LocalRoleOutcome.NOOP
        else:
            assert outcome.outcome in (LocalRoleOutcome.PENDING, LocalRoleOutcome.PRESERVED)
        assert lifecycle(s) == previous
    event.assert_not_called()


@pytest.mark.parametrize("problem", ["event_write", "epoch_limit", "unique_id"])
def test_lifecycle_failure_leaves_join_history_for_caller_rollback(storage, monkeypatch, problem):
    s = storage
    prior_state = "withdrawn" if problem == "event_write" else "active" if problem == "epoch_limit" else None
    prior_epoch = MAX_LIFECYCLE_EPOCH if problem == "epoch_limit" else 9
    previous, protected = seed_lifecycle(s, prior_state, epoch=prior_epoch)
    with s.session.begin():
        before_rows = persisted_rows(s)
    original = InvitationAuthorityRepository.record_local_role_change
    calls = []

    def verify_cas_then_record(repository, supplied, **identity):
        assert supplied is s.session and identity == scope(s)
        role = supplied.execute(sa.select(TenantAccountJoin.role).where(TenantAccountJoin.id == s.join.id)).scalar_one()
        history = supplied.execute(
            sa.select(History.last_applied_roles_json).where(History.id == s.history.id)
        ).scalar_one()
        assert role is TenantAccountRole.ADMIN
        assert json.loads(history)["join_role"] == "admin"
        calls.append(True)
        if problem == "event_write":
            original(repository, supplied, **identity)
            raise RuntimeError("failure after lifecycle write")
        return original(repository, supplied, **identity)

    monkeypatch.setattr(InvitationAuthorityRepository, "record_local_role_change", verify_cas_then_record)
    if problem == "unique_id":
        monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(protected.lifecycle_id))
        expected = IntegrityError
    elif problem == "epoch_limit":
        expected = InvitationAuthorityConflict
    else:
        expected = RuntimeError
    with pytest.raises(expected):
        with s.session.begin():
            assert apply(s).role_changed
    assert calls == [True]
    with s.session.begin():
        assert persisted_rows(s) == before_rows
        assert lifecycle(s) == previous
        assert (
            InvitationAuthorityRepository().get_lifecycle(
                s.session,
                account_id=protected.account_id,
                workspace_id=protected.workspace_id,
            )
            == protected
        )


def test_old_issuance_receipt_is_fenced_after_committed_role_change(storage):
    s = storage
    Issuance.__table__.create(s.engine)
    before, _ = seed_lifecycle(s, "active")
    authority = InvitationAuthorityRepository()
    with s.session.begin():
        facts = {
            "schema_version": 1,
            "issuance_id": str(uuid4()),
            "lifecycle_id": before.lifecycle_id,
            "lifecycle_epoch": before.epoch,
            "token_digest": "e" * 64,
            "join_id_at_issue": s.join.id,
        }
        issuance = authority.record_issuance(
            s.session,
            payload_json=json.dumps(
                {
                    **scope(s),
                    "email": s.account.email,
                    "role": "admin",
                    "requires_setup": False,
                    "invitation_authority": facts,
                }
            ),
        )
        receipt_json = json.dumps(
            {
                **facts,
                **scope(s),
                "status": "consumed",
                "operation_id": str(uuid4()),
                "payload_digest": issuance.payload_digest,
                "key_digest": "f" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    target(s, "admin")
    with s.session.begin():
        assert apply(s).role_changed
    with pytest.raises(InvitationAuthorityConflict, match="invitation_lifecycle_conflict"):
        with s.session.begin():
            authority.record_consumption(s.session, receipt_json=receipt_json)
    with s.session.begin():
        assert lifecycle(s).epoch == before.epoch + 1
        assert authority.get_issuance(s.session, issuance_id=issuance.issuance_id) == issuance
        assert s.session.scalar(sa.select(sa.func.count()).select_from(Issuance)) == 1
