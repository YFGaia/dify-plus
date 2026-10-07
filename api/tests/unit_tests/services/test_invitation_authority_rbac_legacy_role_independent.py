"""Independent checks of invitation fencing in the legacy RBAC fallback."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
)
from services.enterprise import rbac_service


@pytest.fixture(autouse=True)
def legacy_defaults(config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)


def build_memberships(session, *, previous_state="active", member_state="active", owner_exists=True):
    tenant = Tenant(name="Independent legacy role transaction")
    current_owner = Account(name="Current owner", email="i28-p3h-owner@example.test", status=AccountStatus.ACTIVE)
    member = Account(name="Member", email="i28-p3h-member@example.test", status=AccountStatus.ACTIVE)
    bystander = Account(name="Bystander", email="i28-p3h-bystander@example.test", status=AccountStatus.ACTIVE)
    session.add_all([tenant, current_owner, member, bystander])
    session.flush()
    if owner_exists:
        session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=current_owner.id, role=TenantAccountRole.OWNER))
    session.add_all(
        [
            TenantAccountJoin(tenant_id=tenant.id, account_id=member.id, role=TenantAccountRole.NORMAL),
            TenantAccountJoin(tenant_id=tenant.id, account_id=bystander.id, role=TenantAccountRole.NORMAL),
        ]
    )
    authority = InvitationAuthorityRepository()
    for account, state, epoch in (
        (current_owner, previous_state, 8),
        (member, member_state, 13),
        (bystander, "active", 4),
    ):
        if state is None:
            continue
        result = authority.set_lifecycle_state(session, account_id=account.id, workspace_id=tenant.id, state=state)
        persisted = session.get(InvitationAuthorityLifecycleExtend, result.lifecycle_id)
        persisted.epoch = epoch
        persisted.updated_at = datetime(2022, 4, 5)
    session.commit()
    return SimpleNamespace(
        tenant=tenant,
        owner=current_owner,
        member=member,
        bystander=bystander,
        workspace_id=tenant.id,
        authority=authority,
    )


def recorded_lifecycle(session, world, account):
    return world.authority.get_lifecycle(session, account_id=account.id, workspace_id=world.workspace_id)


def persisted_roles(session, world):
    return dict(
        session.execute(
            sa.select(TenantAccountJoin.account_id, TenantAccountJoin.role)
            .where(TenantAccountJoin.tenant_id == world.workspace_id)
            .order_by(TenantAccountJoin.account_id)
        ).all()
    )


def snapshot(session, world):
    rows = tuple(
        session.execute(
            sa.select(TenantAccountJoin.id, TenantAccountJoin.account_id, TenantAccountJoin.role)
            .where(TenantAccountJoin.tenant_id == world.workspace_id)
            .order_by(TenantAccountJoin.account_id)
        ).all()
    )
    facts = tuple(
        recorded_lifecycle(session, world, account) for account in (world.owner, world.member, world.bystander)
    )
    return rows, facts


def assert_increment(original, updated, old_state, epoch):
    assert updated.state == (old_state or "active")
    assert updated.epoch == (epoch + 1 if original else 1)
    assert UUID(updated.lifecycle_id).version == 4
    if original:
        assert (updated.lifecycle_id, updated.created_at) == (original.lifecycle_id, original.created_at)
        assert updated.updated_at > original.updated_at


def replace_member(session, world, role, member_id=None, *, account_id=None, role_ids=None):
    return rbac_service.RBACService.MemberRoles.replace(
        str(world.workspace_id),
        account_id or str(world.owner.id),
        member_id or str(world.member.id),
        role_ids if role_ids is not None else [role],
        session=session,
    )


@pytest.mark.parametrize("new_role", ["admin", "editor"])
@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_single_legacy_role_edit_is_fenced_before_commit_with_unmodified_response(
    sqlite_session_factory, monkeypatch, new_role, prior
):
    with sqlite_session_factory() as session:
        world = build_memberships(session, member_state=prior)
        before = snapshot(session, world)
        transaction = session.get_transaction()
        explicit_lists, sequence = [], []
        real_flush = session.flush
        record_change = InvitationAuthorityRepository.record_membership_role_change

        def track_flush(objects=None):
            if objects is not None:
                explicit_lists.append(tuple(join.account_id for join in objects))
            return real_flush(objects)

        def track_fence(repository, supplied, **scope):
            assert supplied is session and supplied.get_transaction() is transaction
            assert scope == {"account_id": world.member.id, "workspace_id": world.workspace_id}
            assert explicit_lists[0] == (world.member.id,)
            assert persisted_roles(session, world)[world.member.id] is TenantAccountRole(new_role)
            assert not session.new and not session.dirty
            with sqlite_session_factory() as observer:
                assert snapshot(observer, world) == before
            sequence.append("fence")
            return record_change(repository, supplied, **scope)

        monkeypatch.setattr(session, "flush", track_flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", track_fence)

        def remote_not_allowed(*_args, **_kwargs):
            pytest.fail("legacy path made remote call")

        monkeypatch.setattr(rbac_service, "_inner_call", remote_not_allowed)
        sa.event.listen(session, "after_commit", lambda _session: sequence.append("committed"))

        result = replace_member(session, world, new_role)

        assert result.account_id == str(world.member.id)
        assert result.roles[0].id == new_role
        assert sequence == ["fence", "committed"]
        with sqlite_session_factory() as observer:
            assert_increment(before[1][1], recorded_lifecycle(observer, world, world.member), prior, 13)
            assert recorded_lifecycle(observer, world, world.owner) == before[1][0]
            assert recorded_lifecycle(observer, world, world.bystander) == before[1][2]


@pytest.mark.parametrize("former_owner_state", [None, "active", "withdrawn"])
@pytest.mark.parametrize("incoming_owner_state", [None, "active", "withdrawn"])
def test_owner_handoff_fences_both_accounts_only_after_one_complete_join_flush(
    sqlite_session_factory, monkeypatch, former_owner_state, incoming_owner_state
):
    with sqlite_session_factory() as session:
        world = build_memberships(session, previous_state=former_owner_state, member_state=incoming_owner_state)
        before = snapshot(session, world)
        transaction = session.get_transaction()
        flushed, sent = [], []
        real_flush = session.flush
        record_change = InvitationAuthorityRepository.record_membership_role_change

        def collect_flush(objects=None):
            if objects is not None:
                flushed.append(tuple(join.account_id for join in objects))
            return real_flush(objects)

        def check_event(repository, supplied, **scope):
            assert supplied is session and supplied.get_transaction() is transaction
            assert flushed[0] == (world.owner.id, world.member.id)
            live = persisted_roles(session, world)
            assert live[world.owner.id] is TenantAccountRole.NORMAL
            assert live[world.member.id] is TenantAccountRole.OWNER
            assert not session.dirty and not session.new
            with sqlite_session_factory() as observer:
                assert snapshot(observer, world) == before
            sent.append(scope["account_id"])
            return record_change(repository, supplied, **scope)

        monkeypatch.setattr(session, "flush", collect_flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", check_event)
        sa.event.listen(session, "after_commit", lambda _session: sent.append("committed"))

        result = replace_member(session, world, "owner")

        assert result.roles[0].id == "owner"
        assert sent == [world.owner.id, world.member.id, "committed"]
        with sqlite_session_factory() as observer:
            assert_increment(before[1][0], recorded_lifecycle(observer, world, world.owner), former_owner_state, 8)
            assert_increment(before[1][1], recorded_lifecycle(observer, world, world.member), incoming_owner_state, 13)
            assert recorded_lifecycle(observer, world, world.bystander) == before[1][2]


@pytest.mark.parametrize("fail_on", [1, 2])
def test_owner_event_exception_prevents_commit_and_external_rollback_reverts_partial_fences(
    sqlite_session_factory, monkeypatch, fail_on
):
    with sqlite_session_factory() as session:
        world = build_memberships(session, previous_state="active", member_state="withdrawn")
        before = snapshot(session, world)
        dispatched = []
        real_event = InvitationAuthorityRepository.record_membership_role_change

        def fail_after_write(repository, supplied, **scope):
            dispatched.append(scope["account_id"])
            record = real_event(repository, supplied, **scope)
            if len(dispatched) == fail_on:
                raise RuntimeError("legacy lifecycle event failed")
            return record

        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(session, "commit", commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", fail_after_write)

        with pytest.raises(RuntimeError, match="legacy lifecycle event failed"):
            replace_member(session, world, "owner")

        expected = [world.owner.id] if fail_on == 1 else [world.owner.id, world.member.id]
        assert dispatched == expected
        commit.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as observer:
            assert snapshot(observer, world) == before


@pytest.mark.parametrize("who,role", [("member", "normal"), ("owner", "owner")])
def test_reassigning_current_role_commits_legacy_response_without_a_fence(
    sqlite_session_factory, monkeypatch, who, role
):
    with sqlite_session_factory() as session:
        world = build_memberships(session, member_state="withdrawn", previous_state="active")
        before = snapshot(session, world)
        target = world.member if who == "member" else world.owner
        event = Mock(side_effect=AssertionError("unchanged role has no invitation lifecycle change"))
        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)

        result = replace_member(session, world, role, member_id=str(target.id))

        assert result.account_id == str(target.id) and result.roles[0].id == role
        event.assert_not_called()
        commit.assert_called_once_with()
        with sqlite_session_factory() as observer:
            assert snapshot(observer, world) == before


def test_owner_promotion_without_existing_owner_advances_target_only(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        world = build_memberships(session, previous_state="withdrawn", member_state=None, owner_exists=False)
        before = snapshot(session, world)
        observed = []
        actual = InvitationAuthorityRepository.record_membership_role_change

        def record_only_target(repository, supplied, **scope):
            observed.append((supplied, scope))
            assert persisted_roles(session, world)[world.member.id] is TenantAccountRole.OWNER
            return actual(repository, supplied, **scope)

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", record_only_target)

        response = replace_member(session, world, "owner")

        assert response.roles[0].id == "owner"
        assert len(observed) == 1 and observed[0][0] is session
        assert observed[0][1] == {"account_id": world.member.id, "workspace_id": world.workspace_id}
        with sqlite_session_factory() as observer:
            assert_increment(before[1][1], recorded_lifecycle(observer, world, world.member), None, 13)
            assert recorded_lifecycle(observer, world, world.owner) == before[1][0]


def test_failed_bulk_join_flush_has_no_fence_or_commit(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        world = build_memberships(session)
        before = snapshot(session, world)
        original_flush = session.flush
        event, commit = Mock(), Mock(wraps=session.commit)

        def reject_changed_joins(objects=None):
            if objects is not None:
                assert tuple(join.account_id for join in objects) == (world.owner.id, world.member.id)
                raise RuntimeError("bulk Join flush failed")
            return original_flush(objects)

        monkeypatch.setattr(session, "flush", reject_changed_joins)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)

        with pytest.raises(RuntimeError, match="bulk Join flush failed"):
            replace_member(session, world, "owner")

        event.assert_not_called()
        commit.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as observer:
            assert snapshot(observer, world) == before


@pytest.mark.parametrize("candidate_state", [None, "active", "withdrawn"])
def test_remote_rbac_replace_leaves_local_lifecycle_and_shadow_join_untouched(
    sqlite_session_factory, monkeypatch, config_overrides, candidate_state
):
    config_overrides(RBAC_ENABLED=True)
    with sqlite_session_factory() as session:
        world = build_memberships(session, member_state=candidate_state)
        before = snapshot(session, world)
        fence = Mock(side_effect=AssertionError("remote roles do not receive local lifecycle events"))
        commit = Mock(wraps=session.commit)
        remote = Mock(return_value={"account_id": world.member.id, "roles": []})
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", fence)
        monkeypatch.setattr(rbac_service, "_inner_call", remote)
        monkeypatch.setattr(session, "commit", commit)

        result = replace_member(session, world, "ignored", role_ids=["remote-editor", "remote-auditor"])

        assert result.account_id == world.member.id and result.roles == []
        remote.assert_called_once_with(
            "PUT",
            f"{rbac_service._INNER_PREFIX}/members/rbac-roles",
            tenant_id=str(world.workspace_id),
            account_id=str(world.owner.id),
            params={"account_id": str(world.member.id)},
            json={"role_ids": ["remote-editor", "remote-auditor"]},
        )
        fence.assert_not_called()
        commit.assert_not_called()
        assert snapshot(session, world) == before


@pytest.mark.parametrize("role_ids", [[], ["normal", "admin"], ["bad-role"]])
def test_malformed_legacy_role_request_is_rejected_before_writes(sqlite_session_factory, monkeypatch, role_ids):
    with sqlite_session_factory() as session:
        world = build_memberships(session)
        before = snapshot(session, world)
        event, commit = Mock(), Mock(wraps=session.commit)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        monkeypatch.setattr(session, "commit", commit)

        with pytest.raises(ValueError):
            replace_member(session, world, "ignored", role_ids=role_ids)

        event.assert_not_called()
        commit.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as observer:
            assert snapshot(observer, world) == before
