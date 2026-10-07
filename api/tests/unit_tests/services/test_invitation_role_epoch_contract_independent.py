"""Independent transaction rollback proof for lifecycle epoch exhaustion."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services.account_service import TenantService


@pytest.fixture(autouse=True)
def community_without_rbac(config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)


def _seed_ceiling_membership(session):
    workspace = Tenant(name="P3F-R1 original workspace")
    account = Account(name="P3F-R1 member", email="p3fr1-member@example.test")
    session.add_all([workspace, account])
    session.flush()
    join = TenantAccountJoin(tenant_id=workspace.id, account_id=account.id, role=TenantAccountRole.NORMAL)
    session.add(join)
    repo = InvitationAuthorityRepository()
    record = repo.set_lifecycle_state(session, account_id=account.id, workspace_id=workspace.id, state="active")
    lifecycle = session.get(InvitationAuthorityLifecycleExtend, record.lifecycle_id)
    lifecycle.epoch = MAX_LIFECYCLE_EPOCH
    session.commit()
    return SimpleNamespace(workspace=workspace, account=account, join_id=join.id, lifecycle_id=lifecycle.lifecycle_id)


def _lifecycle_columns(row):
    return tuple(
        (column.key, getattr(row, column.key))
        for column in sa.inspect(InvitationAuthorityLifecycleExtend).mapper.column_attrs
    )


def test_epoch_exhaustion_rolls_back_role_and_unrelated_caller_write(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = _seed_ceiling_membership(session)
        session.begin()
        original_name = state.workspace.name
        before_lifecycle = session.get(InvitationAuthorityLifecycleExtend, state.lifecycle_id)
        before_lifecycle_record = _lifecycle_columns(before_lifecycle)

        caller_root = session.get_transaction()
        state.workspace.name = "P3F-R1 uncommitted caller mutation"
        session.flush([state.workspace])
        assert session.get_transaction() is caller_root

        actual_record = InvitationAuthorityRepository.record_membership_role_change
        observations = []

        def observe_then_delegate(repo, supplied_session, **scope):
            assert supplied_session is session
            assert session.get_transaction() is caller_root
            assert scope == {"account_id": state.account.id, "workspace_id": state.workspace.id}
            flushed_role = session.scalar(
                sa.select(TenantAccountJoin.role).where(TenantAccountJoin.id == state.join_id)
            )
            assert flushed_role is TenantAccountRole.EDITOR
            observations.append((repo, supplied_session, scope))
            return actual_record(repo, supplied_session, **scope)

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", observe_then_delegate)
        with pytest.raises(InvitationAuthorityConflict, match="^invitation_lifecycle_epoch_exhausted$"):
            TenantService.persist_tenant_member(state.workspace, state.account, session, role="editor")

        assert len(observations) == 1
        assert session.get_transaction() is caller_root
        session.rollback()

        with sqlite_session_factory() as reader:
            workspace = reader.get(Tenant, state.workspace.id)
            assert workspace.name == original_name
            joins = reader.scalars(
                sa.select(TenantAccountJoin).where(
                    TenantAccountJoin.tenant_id == state.workspace.id,
                    TenantAccountJoin.account_id == state.account.id,
                )
            ).all()
            assert len(joins) == 1
            assert joins[0].id == state.join_id
            assert joins[0].role is TenantAccountRole.NORMAL
            lifecycle = reader.get(InvitationAuthorityLifecycleExtend, state.lifecycle_id)
            assert _lifecycle_columns(lifecycle) == before_lifecycle_record
