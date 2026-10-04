"""Independent boundary checks for shared membership role persistence."""

from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityRepository,
)
from services.account_service import TenantService


@pytest.fixture(autouse=True)
def community_without_rbac(config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)


def workspace_member(session, *, role="normal", prior=None):
    workspace = Tenant(name="P3F independent role boundary")
    member = Account(name="P3F independent member", email="p3f-role-boundary@example.test")
    session.add_all([workspace, member])
    session.flush()
    session.add(TenantAccountJoin(tenant_id=workspace.id, account_id=member.id, role=TenantAccountRole(role)))
    repo = InvitationAuthorityRepository()
    if prior is not None:
        record = repo.set_lifecycle_state(session, account_id=member.id, workspace_id=workspace.id, state=prior)
        row = session.get(Lifecycle, record.lifecycle_id)
        row.epoch = 6
        row.updated_at = datetime(2020, 1, 1)
    session.commit()
    return SimpleNamespace(workspace=workspace, member=member, repo=repo)


def lifecycle(session, state):
    return state.repo.get_lifecycle(session, account_id=state.member.id, workspace_id=state.workspace.id)


def member_join(session, state):
    return session.scalar(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.tenant_id == state.workspace.id,
            TenantAccountJoin.account_id == state.member.id,
        )
    )


@pytest.mark.parametrize("prior", [None, "withdrawn"])
def test_role_change_advances_only_its_workspace_marker_and_retains_withdrawal_history(sqlite_session_factory, prior):
    with sqlite_session_factory() as session:
        state = workspace_member(session, prior=prior)
        before = lifecycle(session, state)
        original_join = member_join(session, state)
        outer = session.get_transaction()

        persisted = TenantService.persist_tenant_member(state.workspace, state.member, session, "admin")

        after = lifecycle(session, state)
        assert persisted.join is original_join and not persisted.membership_created
        assert persisted.join.role is TenantAccountRole.ADMIN
        assert after.epoch == (7 if prior else 1)
        assert after.state == (prior or "active")
        assert outer is session.get_transaction()
        if before:
            assert (after.lifecycle_id, after.created_at) == (before.lifecycle_id, before.created_at)
            assert after.updated_at > before.updated_at
        with sqlite_session_factory() as reader:
            assert lifecycle(reader, state) == before
        session.rollback()

        with sqlite_session_factory() as reader:
            assert member_join(reader, state).role is TenantAccountRole.NORMAL
            assert lifecycle(reader, state) == before


def test_changed_role_with_missing_marker_creates_active_epoch_one(sqlite_session_factory):
    with sqlite_session_factory() as session:
        state = workspace_member(session)
        assert lifecycle(session, state) is None
        TenantService.persist_tenant_member(state.workspace, state.member, session, "editor")
        marker = lifecycle(session, state)
        assert marker.state == "active" and marker.epoch == 1
        assert member_join(session, state).role is TenantAccountRole.EDITOR
        session.commit()


def test_same_role_at_epoch_ceiling_is_inert(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = workspace_member(session, role="admin", prior="active")
        marker = lifecycle(session, state)
        session.get(Lifecycle, marker.lifecycle_id).epoch = MAX_LIFECYCLE_EPOCH
        session.commit()
        before = lifecycle(session, state)
        monkeypatch.setattr(
            InvitationAuthorityRepository,
            "record_membership_role_change",
            lambda *_args, **_kwargs: pytest.fail("same-role persistence emitted role-change event"),
        )

        result = TenantService.persist_tenant_member(state.workspace, state.member, session, "admin")

        assert not result.membership_created and result.join.role is TenantAccountRole.ADMIN
        assert lifecycle(session, state) == before


def test_local_role_seam_keeps_the_supplied_session_and_scope(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        calls = []

        def record(repo, supplied, **scope):
            calls.append((repo, supplied, scope))
            return "recorded"

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", record)
        result = InvitationAuthorityRepository().record_local_role_change(
            session, account_id="acct-independent", workspace_id="workspace-independent"
        )

        assert result == "recorded"
        assert len(calls) == 1
        assert calls[0][1] is session
        assert calls[0][2] == {
            "account_id": "acct-independent",
            "workspace_id": "workspace-independent",
        }
