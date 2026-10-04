"""Independent transaction-order checks for local member role changes."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityRepository,
)
from services.account_service import AccountService, RBACService, TenantService
from services.errors.account import RoleAlreadyAssignedError


@pytest.fixture(autouse=True)
def community_mode(config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)


def role_case(session, *, previous_owner_state="active", candidate_state="active"):
    tenant = Tenant(name="Independent local role writer")
    owner = Account(name="Owner", email="p3g-owner@example.test", status=AccountStatus.ACTIVE)
    candidate = Account(name="Candidate", email="p3g-candidate@example.test", status=AccountStatus.ACTIVE)
    witness = Account(name="Witness", email="p3g-witness@example.test", status=AccountStatus.ACTIVE)
    session.add_all([tenant, owner, candidate, witness])
    session.flush()
    session.add_all(
        [
            TenantAccountJoin(tenant_id=tenant.id, account_id=owner.id, role=TenantAccountRole.OWNER),
            TenantAccountJoin(tenant_id=tenant.id, account_id=candidate.id, role=TenantAccountRole.NORMAL),
            TenantAccountJoin(tenant_id=tenant.id, account_id=witness.id, role=TenantAccountRole.NORMAL),
        ]
    )
    repo = InvitationAuthorityRepository()
    for account, state, epoch in (
        (owner, previous_owner_state, 12),
        (candidate, candidate_state, 21),
        (witness, "active", 5),
    ):
        if state is None:
            continue
        row_record = repo.set_lifecycle_state(session, account_id=account.id, workspace_id=tenant.id, state=state)
        row = session.get(Lifecycle, row_record.lifecycle_id)
        row.epoch = epoch
        row.updated_at = datetime(2021, 2, 3)
    session.commit()
    return SimpleNamespace(
        tenant=tenant,
        operator=owner,
        candidate=candidate,
        witness=witness,
        repository=repo,
        workspace_id=tenant.id,
    )


def join_roles(session, case):
    return dict(
        session.execute(
            sa.select(TenantAccountJoin.account_id, TenantAccountJoin.role)
            .where(TenantAccountJoin.tenant_id == case.workspace_id)
            .order_by(TenantAccountJoin.account_id)
        ).all()
    )


def life(session, case, account):
    return case.repository.get_lifecycle(session, account_id=account.id, workspace_id=case.workspace_id)


def durable_state(session, case):
    roles = tuple(
        session.execute(
            sa.select(TenantAccountJoin.account_id, TenantAccountJoin.role)
            .where(TenantAccountJoin.tenant_id == case.workspace_id)
            .order_by(TenantAccountJoin.account_id)
        ).all()
    )
    records = tuple(life(session, case, account) for account in (case.operator, case.candidate, case.witness))
    return roles, records


def check_advanced(before, after, prior_state, old_epoch):
    assert after.state == (prior_state or "active")
    assert after.epoch == (old_epoch + 1 if before else 1)
    if before:
        assert (after.lifecycle_id, after.created_at) == (before.lifecycle_id, before.created_at)
        assert after.updated_at > before.updated_at


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_local_non_owner_write_is_flushed_before_its_only_lifecycle_event(sqlite_session_factory, monkeypatch, prior):
    with sqlite_session_factory() as session:
        case = role_case(session, candidate_state=prior)
        before = durable_state(session, case)
        transaction = session.get_transaction()
        visible_flushes, order = [], []
        real_flush = session.flush
        original_event = InvitationAuthorityRepository.record_membership_role_change

        def watch_flush(objects=None):
            if objects is not None:
                visible_flushes.append(tuple(row.account_id for row in objects))
            return real_flush(objects)

        def watch_event(repository, supplied, **scope):
            assert supplied is session and supplied.get_transaction() is transaction
            assert scope == {"account_id": case.candidate.id, "workspace_id": case.workspace_id}
            assert visible_flushes == [(case.candidate.id,)]
            assert join_roles(session, case)[case.candidate.id] is TenantAccountRole.ADMIN
            assert not session.new and not session.dirty
            with sqlite_session_factory() as reader:
                assert durable_state(reader, case) == before
            order.append(("event", scope["account_id"]))
            return original_event(repository, supplied, **scope)

        monkeypatch.setattr(session, "flush", watch_flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", watch_event)
        sa.event.listen(session, "after_commit", lambda _session: order.append(("commit", None)))

        result = TenantService.update_member_role(case.tenant, case.candidate, "admin", case.operator, session=session)

        assert result is None and order == [("event", case.candidate.id), ("commit", None)]
        with sqlite_session_factory() as reader:
            assert join_roles(reader, case) == {
                case.operator.id: TenantAccountRole.OWNER,
                case.candidate.id: TenantAccountRole.ADMIN,
                case.witness.id: TenantAccountRole.NORMAL,
            }
            check_advanced(before[1][1], life(reader, case, case.candidate), prior, 21)
            assert life(reader, case, case.operator) == before[1][0]
            assert life(reader, case, case.witness) == before[1][2]


@pytest.mark.parametrize("owner_state", [None, "active", "withdrawn"])
@pytest.mark.parametrize("candidate_state", [None, "active", "withdrawn"])
def test_local_owner_transfer_flushes_both_memberships_before_ordered_events(
    sqlite_session_factory, monkeypatch, owner_state, candidate_state
):
    with sqlite_session_factory() as session:
        case = role_case(session, previous_owner_state=owner_state, candidate_state=candidate_state)
        before = durable_state(session, case)
        transaction = session.get_transaction()
        flushed_accounts, sequence = [], []
        real_flush = session.flush
        write_lifecycle = InvitationAuthorityRepository.record_membership_role_change

        def observe_flush(objects=None):
            if objects is not None:
                flushed_accounts.append(tuple(row.account_id for row in objects))
            return real_flush(objects)

        def observe_lifecycle(repository, supplied, **scope):
            assert supplied is session and supplied.get_transaction() is transaction
            assert flushed_accounts[0] == (case.operator.id, case.candidate.id)
            updated = join_roles(session, case)
            assert updated[case.operator.id] is TenantAccountRole.NORMAL
            assert updated[case.candidate.id] is TenantAccountRole.OWNER
            assert not session.new and not session.dirty
            with sqlite_session_factory() as reader:
                assert durable_state(reader, case) == before
            sequence.append(scope["account_id"])
            return write_lifecycle(repository, supplied, **scope)

        monkeypatch.setattr(session, "flush", observe_flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", observe_lifecycle)
        sa.event.listen(session, "after_commit", lambda _session: sequence.append("committed"))

        result = TenantService.update_member_role(case.tenant, case.candidate, "owner", case.operator, session=session)

        assert result is None
        assert sequence == [case.operator.id, case.candidate.id, "committed"]
        with sqlite_session_factory() as reader:
            check_advanced(before[1][0], life(reader, case, case.operator), owner_state, 12)
            check_advanced(before[1][1], life(reader, case, case.candidate), candidate_state, 21)
            assert life(reader, case, case.witness) == before[1][2]


@pytest.mark.parametrize("failed_pair_index", [0, 1])
def test_owner_transfer_event_error_escapes_before_commit_and_outer_rollback_restores_all_rows(
    sqlite_session_factory, monkeypatch, failed_pair_index
):
    with sqlite_session_factory() as session:
        case = role_case(session, previous_owner_state="active", candidate_state="withdrawn")
        before = durable_state(session, case)
        order = []
        original = InvitationAuthorityRepository.record_membership_role_change

        def fail_after_selected_write(repository, supplied, **scope):
            order.append(scope["account_id"])
            row = original(repository, supplied, **scope)
            if len(order) - 1 == failed_pair_index:
                raise RuntimeError("independent lifecycle event failed")
            return row

        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(session, "commit", commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", fail_after_selected_write)

        with pytest.raises(RuntimeError, match="independent lifecycle event failed"):
            TenantService.update_member_role(case.tenant, case.candidate, "owner", case.operator, session=session)

        expected_order = [case.operator.id] if failed_pair_index == 0 else [case.operator.id, case.candidate.id]
        assert order == expected_order
        commit.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as reader:
            assert durable_state(reader, case) == before


def test_same_role_rejection_precedes_local_lifecycle_and_commit(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        case = role_case(session, candidate_state="active")
        row = life(session, case, case.candidate)
        session.get(Lifecycle, row.lifecycle_id).epoch = MAX_LIFECYCLE_EPOCH
        session.commit()
        before = durable_state(session, case)
        event = Mock(side_effect=AssertionError("unchanged role must not emit lifecycle"))
        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)

        with pytest.raises(RoleAlreadyAssignedError):
            TenantService.update_member_role(case.tenant, case.candidate, "normal", case.operator, session=session)

        event.assert_not_called()
        commit.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as reader:
            assert durable_state(reader, case) == before


def test_rbac_enabled_remote_role_only_write_does_not_advance_local_lifecycle(
    sqlite_session_factory, monkeypatch, config_overrides
):
    config_overrides(RBAC_ENABLED=True)
    with sqlite_session_factory() as session:
        case = role_case(session, candidate_state="withdrawn")
        before = durable_state(session, case)
        monkeypatch.setattr(TenantService, "check_member_permission", Mock())
        monkeypatch.setattr(
            AccountService, "get_workspace_permission_keys", Mock(return_value={"workspace.role.manage"})
        )
        monkeypatch.setattr(AccountService, "_resolve_legacy_role_id", Mock(return_value="editor-role-id"))
        event = Mock(side_effect=AssertionError("remote role effects have no lifecycle claim here"))
        remote = Mock()
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(RBACService.MemberRoles, "replace", remote)

        TenantService.update_member_role(case.tenant, case.candidate, "editor", case.operator, session=session)

        event.assert_not_called()
        remote.assert_called_once_with(
            tenant_id=str(case.workspace_id),
            account_id=case.operator.id,
            member_account_id=case.candidate.id,
            role_ids=["editor-role-id"],
            session=session,
        )
        with sqlite_session_factory() as reader:
            assert durable_state(reader, case) == before
