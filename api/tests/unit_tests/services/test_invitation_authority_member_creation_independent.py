"""Independent checks for new Join lifecycle advancement in caller-owned SQL."""

import socket
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityRepository,
)
from services.account_service import TenantService


@pytest.fixture(autouse=True)
def no_network(monkeypatch, config_overrides):
    attempts = []

    def deny(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("network access denied")

    for method in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, method, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    yield
    assert attempts == []


def seed(session, *, prior_state=None, prior_epoch=4, existing_join=False):
    workspace = Tenant(name="Independent P3B workspace")
    account = Account(name="Independent P3B member", email="independent-p3b@example.test")
    session.add_all([workspace, account])
    session.flush()
    if existing_join:
        session.add(TenantAccountJoin(tenant_id=workspace.id, account_id=account.id, role=TenantAccountRole.NORMAL))
    repo = InvitationAuthorityRepository()
    if prior_state:
        record = repo.set_lifecycle_state(
            session, account_id=account.id, workspace_id=workspace.id, state=prior_state
        )
        row = session.get(InvitationAuthorityLifecycleExtend, record.lifecycle_id)
        row.epoch = prior_epoch
    session.commit()
    return SimpleNamespace(workspace=workspace, account=account, repo=repo)


def get_lifecycle(session, state):
    return state.repo.get_lifecycle(session, account_id=state.account.id, workspace_id=state.workspace.id)


def get_joins(session, state):
    return session.scalars(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.tenant_id == state.workspace.id,
            TenantAccountJoin.account_id == state.account.id,
        )
    ).all()


def test_absent_lifecycle_is_created_after_join_and_commits_as_one_event(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = seed(session)
        session.begin()
        owner_transaction = session.get_transaction()
        original = InvitationAuthorityRepository.record_membership_creation
        event_calls = []

        def inspect_event(repo, supplied, **scope):
            assert supplied is session
            assert scope == {"account_id": state.account.id, "workspace_id": state.workspace.id}
            assert len(get_joins(session, state)) == 1
            assert not session.new
            event_calls.append(True)
            return original(repo, supplied, **scope)

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", inspect_event)
        result = TenantService.persist_tenant_member(state.workspace, state.account, session)
        record = get_lifecycle(session, state)
        assert result.membership_created and len(event_calls) == 1
        assert record.state == "active" and record.epoch == 1
        assert UUID(record.lifecycle_id).version == 4
        assert session.get_transaction() is owner_transaction
        with sqlite_session_factory() as reader:
            assert get_lifecycle(reader, state) is None
            assert get_joins(reader, state) == []

        session.commit()
        with sqlite_session_factory() as reader:
            assert get_lifecycle(reader, state) == record
            assert len(get_joins(reader, state)) == 1


@pytest.mark.parametrize("prior_state", ["active", "withdrawn"])
def test_real_new_join_advances_existing_lifecycle_once(sqlite_session_factory, prior_state):
    with sqlite_session_factory() as session:
        state = seed(session, prior_state=prior_state, prior_epoch=9)
        before = get_lifecycle(session, state)
        result = TenantService.persist_tenant_member(state.workspace, state.account, session)
        after = get_lifecycle(session, state)
        assert result.membership_created
        assert (after.lifecycle_id, after.created_at) == (before.lifecycle_id, before.created_at)
        assert after.state == "active" and after.epoch == 10
        assert after.updated_at > before.updated_at
        session.commit()


@pytest.mark.parametrize("role", ["normal", "editor"])
def test_existing_join_never_advances_lifecycle(sqlite_session_factory, monkeypatch, role):
    from repositories.invitation_authority_repository_extend import InvitationAuthorityConflict

    with sqlite_session_factory() as session:
        state = seed(session, prior_state="active", prior_epoch=MAX_LIFECYCLE_EPOCH, existing_join=True)
        before = get_lifecycle(session, state)
        original_join = get_joins(session, state)[0]
        original_join_id = original_join.id
        event = lambda *_args, **_kwargs: pytest.fail("existing Join emitted creation event")
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)

        if role == "normal":
            result = TenantService.persist_tenant_member(state.workspace, state.account, session, role=role)
            assert not result.membership_created and result.join is original_join
            assert result.join.role == TenantAccountRole.NORMAL
            assert get_lifecycle(session, state) == before
            session.commit()
        else:
            with pytest.raises(InvitationAuthorityConflict, match="^invitation_lifecycle_epoch_exhausted$"):
                TenantService.persist_tenant_member(state.workspace, state.account, session, role=role)
            assert (
                session.scalar(sa.select(TenantAccountJoin.role).where(TenantAccountJoin.id == original_join_id))
                == TenantAccountRole.EDITOR
            )
            assert get_lifecycle(session, state) == before
            session.rollback()
        with sqlite_session_factory() as reader:
            assert get_lifecycle(reader, state) == before
            joins = get_joins(reader, state)
            assert len(joins) == 1
            assert joins[0].id == original_join_id
            assert joins[0].role == TenantAccountRole.NORMAL


def test_lifecycle_write_error_propagates_and_outer_rollback_removes_join_and_lifecycle(
    sqlite_session_factory, monkeypatch
):
    with sqlite_session_factory() as session:
        state = seed(session)
        original = InvitationAuthorityRepository.record_membership_creation

        def fail_after_write(repo, supplied, **scope):
            original(repo, supplied, **scope)
            raise RuntimeError("independent lifecycle failure")

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", fail_after_write)
        with pytest.raises(RuntimeError, match="independent lifecycle failure"):
            TenantService.persist_tenant_member(state.workspace, state.account, session)
        session.rollback()
        with sqlite_session_factory() as reader:
            assert get_lifecycle(reader, state) is None
            assert get_joins(reader, state) == []
