"""RBAC-disabled legacy Join writer and real SQLite lifecycle transaction evidence."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories import invitation_authority_repository_extend as authority_module
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services.enterprise import rbac_service as svc


@pytest.fixture(autouse=True)
def local_configuration(config_overrides, monkeypatch):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    monkeypatch.setattr(svc, "_inner_call", Mock(side_effect=AssertionError("disabled branch is local")))


def seed(session, *, old_prior="active", target_prior="active", target_epoch=7):
    tenant = Tenant(name="Local role service")
    operator = Account(name="Owner", email="owner-role-service@example.test", status=AccountStatus.ACTIVE)
    member = Account(name="Member", email="member-role-service@example.test", status=AccountStatus.ACTIVE)
    protected = Account(name="Protected", email="protected-role-service@example.test", status=AccountStatus.ACTIVE)
    session.add_all([tenant, operator, member, protected])
    session.flush()
    for account, role in ((operator, "owner"), (member, "normal"), (protected, "normal")):
        session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole(role)))
    for account, prior, epoch in (
        (operator, old_prior, 7),
        (member, target_prior, target_epoch),
        (protected, "active", 4),
    ):
        if prior:
            record = InvitationAuthorityRepository().set_lifecycle_state(
                session, account_id=account.id, workspace_id=tenant.id, state=prior
            )
            row = session.get(Lifecycle, record.lifecycle_id)
            row.epoch = epoch
            row.updated_at = datetime(2020, 1, 1)
    session.commit()
    return SimpleNamespace(
        tenant=tenant,
        operator=operator,
        member=member,
        workspace_id=tenant.id,
        old_id=operator.id,
        target_id=member.id,
        protected_id=protected.id,
    )


def replace(session, s, role, *, target=None):
    return svc.RBACService.MemberRoles.replace(s.workspace_id, s.old_id, target or s.target_id, [role], session=session)


def lifecycle(session, s, account_id):
    return InvitationAuthorityRepository().get_lifecycle(session, account_id=account_id, workspace_id=s.workspace_id)


def joins(session, s):
    return tuple(
        session.execute(
            sa.select(*TenantAccountJoin.__table__.columns)
            .where(TenantAccountJoin.tenant_id == s.workspace_id)
            .order_by(TenantAccountJoin.account_id)
        ).all()
    )


def roles(session, s):
    return {row.account_id: row.role for row in joins(session, s)}


def snapshot(session, s):
    return joins(session, s), tuple(lifecycle(session, s, key) for key in (s.old_id, s.target_id, s.protected_id))


def assert_advance(before, after, prior):
    assert after.epoch == (8 if before else 1) and after.state == (prior or "active")
    assert UUID(after.lifecycle_id).version == 4
    if before:
        assert (after.lifecycle_id, after.state, after.created_at) == (
            before.lifecycle_id,
            before.state,
            before.created_at,
        )
        assert after.updated_at > before.updated_at


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("new_role", ["normal", "admin", "editor"])
def test_non_owner_role_change_flushes_only_target_then_commits_lifecycle(
    sqlite_session_factory, monkeypatch, prior, new_role
):
    with sqlite_session_factory() as session:
        s = seed(session, target_prior=prior)
        if new_role == "normal":
            session.execute(
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.account_id == s.target_id)
                .values(role=TenantAccountRole.EDITOR)
            )
            session.commit()
        before = snapshot(session, s)
        root = session.get_transaction()
        order, explicit_flushes = [], []
        original = InvitationAuthorityRepository.record_membership_role_change
        real_flush = session.flush

        def flush(objects=None):
            if objects is not None:
                explicit_flushes.append(list(objects))
            return real_flush(objects)

        def event(repo, supplied, **kwargs):
            assert supplied is session and supplied.get_transaction() is root
            assert kwargs == dict(account_id=s.target_id, workspace_id=s.workspace_id)
            assert [row.account_id for row in explicit_flushes[0]] == [s.target_id]
            assert roles(session, s)[s.target_id] is TenantAccountRole(new_role)
            assert not session.dirty and not session.new
            with sqlite_session_factory() as observer:
                assert snapshot(observer, s) == before
            order.append(s.target_id)
            return original(repo, supplied, **kwargs)

        monkeypatch.setattr(session, "flush", flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        sa.event.listen(session, "after_commit", lambda _: order.append("commit"))
        result = replace(session, s, new_role)
        assert result.account_id == s.target_id and result.roles[0].id == new_role
        assert order == [s.target_id, "commit"]
        with sqlite_session_factory() as observer:
            assert roles(observer, s) == {
                s.old_id: TenantAccountRole.OWNER,
                s.target_id: TenantAccountRole(new_role),
                s.protected_id: TenantAccountRole.NORMAL,
            }
            assert_advance(before[1][1], lifecycle(observer, s, s.target_id), prior)
            assert lifecycle(observer, s, s.old_id) == before[1][0]
            assert lifecycle(observer, s, s.protected_id) == before[1][2]


@pytest.mark.parametrize("old_prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("target_prior", [None, "active", "withdrawn"])
def test_owner_transfer_flushes_both_joins_then_advances_old_owner_and_target_once(
    sqlite_session_factory, monkeypatch, old_prior, target_prior
):
    with sqlite_session_factory() as session:
        s = seed(session, old_prior=old_prior, target_prior=target_prior)
        before = snapshot(session, s)
        root = session.get_transaction()
        order, explicit_flushes = [], []
        original = InvitationAuthorityRepository.record_membership_role_change
        real_flush = session.flush

        def flush(objects=None):
            if objects is not None:
                explicit_flushes.append(list(objects))
            return real_flush(objects)

        def event(repo, supplied, **kwargs):
            assert supplied is session and supplied.get_transaction() is root
            assert kwargs["workspace_id"] == s.workspace_id
            assert [row.account_id for row in explicit_flushes[0]] == [s.old_id, s.target_id]
            current = roles(session, s)
            assert current[s.old_id] is TenantAccountRole.NORMAL and current[s.target_id] is TenantAccountRole.OWNER
            assert not session.dirty and not session.new
            with sqlite_session_factory() as observer:
                assert snapshot(observer, s) == before
            order.append(kwargs["account_id"])
            return original(repo, supplied, **kwargs)

        monkeypatch.setattr(session, "flush", flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        sa.event.listen(session, "after_commit", lambda _: order.append("commit"))
        assert replace(session, s, "owner").roles[0].id == "owner"
        assert order == [s.old_id, s.target_id, "commit"]
        with sqlite_session_factory() as observer:
            assert_advance(before[1][0], lifecycle(observer, s, s.old_id), old_prior)
            assert_advance(before[1][1], lifecycle(observer, s, s.target_id), target_prior)
            assert lifecycle(observer, s, s.protected_id) == before[1][2]
            assert roles(observer, s)[s.old_id] is TenantAccountRole.NORMAL
            assert roles(observer, s)[s.target_id] is TenantAccountRole.OWNER


@pytest.mark.parametrize(
    "failure",
    ["first_after_write", "second_before_write", "second_after_write", "target_overflow", "target_unique", "schema"],
)
def test_owner_transfer_event_failure_never_commits_and_caller_restores_both_pairs(
    sqlite_session_factory, monkeypatch, failure
):
    with sqlite_session_factory() as session:
        target_prior = None if failure == "target_unique" else "withdrawn"
        s = seed(
            session,
            old_prior="active",
            target_prior=target_prior,
            target_epoch=MAX_LIFECYCLE_EPOCH if failure == "target_overflow" else 7,
        )
        before = snapshot(session, s)
        session.commit()
        if failure == "target_unique":
            monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(before[1][2].lifecycle_id))
        if failure == "schema":
            Lifecycle.__table__.drop(session.get_bind())
        calls = []
        original = InvitationAuthorityRepository.record_membership_role_change

        def event(repo, supplied, **kwargs):
            assert supplied is session
            current = roles(session, s)
            assert current[s.old_id] is TenantAccountRole.NORMAL and current[s.target_id] is TenantAccountRole.OWNER
            calls.append(kwargs["account_id"])
            if failure == "second_before_write" and len(calls) == 2:
                raise RuntimeError("event_failed")
            value = original(repo, supplied, **kwargs)
            if (failure == "first_after_write" and len(calls) == 1) or (
                failure == "second_after_write" and len(calls) == 2
            ):
                raise RuntimeError("event_failed")
            return value

        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(session, "commit", commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        expected = (
            InvitationAuthorityConflict
            if failure == "target_overflow"
            else IntegrityError
            if failure == "target_unique"
            else OperationalError
            if failure == "schema"
            else RuntimeError
        )
        with pytest.raises(expected):
            replace(session, s, "owner")
        assert calls == ([s.old_id] if failure in ("first_after_write", "schema") else [s.old_id, s.target_id])
        commit.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as observer:
            assert joins(observer, s) == before[0]
            if failure != "schema":
                assert snapshot(observer, s) == before


def test_non_owner_event_failure_preserves_all_rows_on_caller_rollback(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        s = seed(session, target_prior="withdrawn")
        before = snapshot(session, s)
        original = InvitationAuthorityRepository.record_membership_role_change

        def fail_after(repo, supplied, **kwargs):
            original(repo, supplied, **kwargs)
            raise RuntimeError("event_failed")

        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(session, "commit", commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", fail_after)
        with pytest.raises(RuntimeError, match="event_failed"):
            replace(session, s, "admin")
        commit.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as observer:
            assert snapshot(observer, s) == before


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("owner_self", [False, True])
def test_same_role_commits_original_response_without_event(sqlite_session_factory, monkeypatch, prior, owner_self):
    with sqlite_session_factory() as session:
        s = seed(session, old_prior=prior, target_prior=prior, target_epoch=MAX_LIFECYCLE_EPOCH)
        before = snapshot(session, s)
        event = Mock(side_effect=AssertionError("unchanged role"))
        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)
        target, role = (s.old_id, "owner") if owner_self else (s.target_id, "normal")
        result = replace(session, s, role, target=target)
        assert result.account_id == target and result.roles[0].id == role
        event.assert_not_called()
        commit.assert_called_once_with()
        with sqlite_session_factory() as observer:
            assert snapshot(observer, s) == before


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_missing_prior_owner_changes_only_target(sqlite_session_factory, monkeypatch, prior):
    with sqlite_session_factory() as session:
        s = seed(session, target_prior=prior)
        session.execute(sa.delete(TenantAccountJoin).where(TenantAccountJoin.account_id == s.old_id))
        session.commit()
        before = snapshot(session, s)
        original = InvitationAuthorityRepository.record_membership_role_change
        calls = []

        def event(repo, supplied, **kwargs):
            assert supplied is session
            calls.append(kwargs)
            assert roles(session, s)[s.target_id] is TenantAccountRole.OWNER
            return original(repo, supplied, **kwargs)

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        result = replace(session, s, "owner")
        assert result.roles[0].id == "owner"
        assert calls == [dict(account_id=s.target_id, workspace_id=s.workspace_id)]
        with sqlite_session_factory() as observer:
            assert_advance(before[1][1], lifecycle(observer, s, s.target_id), prior)
            assert lifecycle(observer, s, s.old_id) == before[1][0]
            assert lifecycle(observer, s, s.protected_id) == before[1][2]


@pytest.mark.parametrize("role_ids", [[], ["normal", "admin"], ["bad-role"], ["workspace.owner"]])
def test_invalid_roles_have_no_event_commit_or_mutation(sqlite_session_factory, monkeypatch, role_ids):
    with sqlite_session_factory() as session:
        s = seed(session)
        before = snapshot(session, s)
        event, commit = Mock(), Mock(wraps=session.commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(ValueError):
            svc.RBACService.MemberRoles.replace(s.workspace_id, s.old_id, s.target_id, role_ids, session=session)
        event.assert_not_called()
        commit.assert_not_called()
        session.rollback()
        assert snapshot(session, s) == before


def test_missing_target_has_no_event_commit_or_mutation(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        s = seed(session)
        before = snapshot(session, s)
        event, commit = Mock(), Mock(wraps=session.commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(ValueError, match="Member not in tenant"):
            replace(session, s, "owner", target="00000000-0000-4000-8000-000000000299")
        event.assert_not_called()
        commit.assert_not_called()
        session.rollback()
        assert snapshot(session, s) == before


def test_join_flush_failure_never_emits_or_commits(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        s = seed(session)
        before = snapshot(session, s)
        real_flush = session.flush
        event, commit = Mock(), Mock(wraps=session.commit)

        def fail_changed(objects=None):
            if objects:
                assert [row.account_id for row in objects] == [s.old_id, s.target_id]
                raise RuntimeError("join_flush_failed")
            return real_flush(objects)

        monkeypatch.setattr(session, "flush", fail_changed)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(RuntimeError, match="join_flush_failed"):
            replace(session, s, "owner")
        event.assert_not_called()
        commit.assert_not_called()
        session.rollback()
        assert snapshot(session, s) == before


@pytest.mark.parametrize("role_ids", [["remote-owner"], ["remote-admin", "remote-editor"], []])
def test_rbac_enabled_remote_only_preserves_local_shadow_state(
    sqlite_session_factory, monkeypatch, config_overrides, role_ids
):
    config_overrides(RBAC_ENABLED=True)
    with sqlite_session_factory() as session:
        s = seed(session, old_prior="withdrawn", target_prior="withdrawn")
        before = snapshot(session, s)
        event, commit = Mock(), Mock(wraps=session.commit)
        remote = Mock(return_value={"account_id": s.target_id, "roles": []})
        monkeypatch.setattr(svc, "_inner_call", remote)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)
        result = svc.RBACService.MemberRoles.replace(s.workspace_id, s.old_id, s.target_id, role_ids, session=session)
        assert result.account_id == s.target_id and result.roles == []
        remote.assert_called_once_with(
            "PUT",
            f"{svc._INNER_PREFIX}/members/rbac-roles",
            tenant_id=s.workspace_id,
            account_id=s.old_id,
            params={"account_id": s.target_id},
            json={"role_ids": role_ids},
        )
        event.assert_not_called()
        commit.assert_not_called()
        assert snapshot(session, s) == before
