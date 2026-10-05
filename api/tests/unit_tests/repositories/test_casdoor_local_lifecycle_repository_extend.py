"""Actual LOCAL SQLite release metadata and all-kind no-intent boundary."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.auth_transactions import AuthTransactionError
from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json, roles_fingerprint
from models.account import TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
)
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository
from test_casdoor_manual_ownership_repository_extend import add_intent
from test_casdoor_manual_ownership_repository_extend import storage as storage


@pytest.fixture
def local(storage):
    s = storage
    CasdoorAuditExtend.__table__.create(s.engine)
    InvitationAuthorityLifecycleExtend.__table__.create(s.engine)
    observation = MembershipObservation(
        UUID(s.workspace.id), UUID(s.account.id), UUID(s.join.id), s.join.role, MembershipBackend.LOCAL
    )
    s.history.baseline_json = role_baseline_json(observation)
    s.history.last_applied_roles_json = role_baseline_json(observation)
    s.history.last_applied_fingerprint = roles_fingerprint(observation)
    s.history.desired_roles_json = "{}"
    s.history.finalization = CasdoorFinalizationState.FINALIZED
    s.session.commit()
    return s


def released(s):
    with s.session.begin():
        owner = CasdoorLocalLifecycleRepository(s.session)
        owner.release(inspect(s, owner), actor_account_id=UUID(s.account.id), correlation_id=uuid4())
    s.session.expire_all()


def test_adopt_role_change_advances_actual_invitation_epoch_only_once_and_preserves_baseline(local):
    s = local
    released(s)
    s.integration.active_revision_id = s.revision.id
    s.session.add(
        InvitationAuthorityLifecycleExtend(
            lifecycle_id=str(uuid4()), account_id=s.account.id, workspace_id=s.workspace.id, epoch=8, state="active"
        )
    )
    s.session.commit()
    baseline = s.history.baseline_json
    s.session.commit()
    with s.session.begin():
        owner = CasdoorLocalLifecycleRepository(s.session)
        owner.adopt(
            inspect(s, owner),
            revision_id=UUID(s.revision.id),
            target_role="editor",
            reason="mapping",
            actor_account_id=UUID(s.account.id),
            correlation_id=uuid4(),
        )
    s.session.expire_all()
    assert s.join.role is TenantAccountRole.EDITOR
    assert s.history.baseline_json == baseline
    assert s.session.scalar(sa.select(InvitationAuthorityLifecycleExtend.epoch)) == 9
    s.session.commit()
    released(s)
    with s.session.begin():
        owner = CasdoorLocalLifecycleRepository(s.session)
        owner.adopt(
            inspect(s, owner),
            revision_id=UUID(s.revision.id),
            target_role="editor",
            reason="mapping",
            actor_account_id=UUID(s.account.id),
            correlation_id=uuid4(),
        )
    assert s.session.scalar(sa.select(InvitationAuthorityLifecycleExtend.epoch)) == 9


def test_adopt_invitation_epoch_overflow_rolls_back_join_history_and_audit(local):
    from repositories.invitation_authority_repository_extend import MAX_LIFECYCLE_EPOCH, InvitationAuthorityConflict

    s = local
    released(s)
    s.integration.active_revision_id = s.revision.id
    s.session.add(
        InvitationAuthorityLifecycleExtend(
            lifecycle_id=str(uuid4()),
            account_id=s.account.id,
            workspace_id=s.workspace.id,
            epoch=MAX_LIFECYCLE_EPOCH,
            state="active",
        )
    )
    s.session.commit()
    with pytest.raises(InvitationAuthorityConflict), s.session.begin():
        owner = CasdoorLocalLifecycleRepository(s.session)
        owner.adopt(
            inspect(s, owner),
            revision_id=UUID(s.revision.id),
            target_role="editor",
            reason="mapping",
            actor_account_id=UUID(s.account.id),
            correlation_id=uuid4(),
        )
    s.session.expire_all()
    assert s.join.role is TenantAccountRole.NORMAL
    assert s.history.ownership is CasdoorMembershipOwnership.RELEASED
    assert s.history.ownership_epoch == 4
    assert s.session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1


@pytest.mark.parametrize("corruption", ["missing_receipt", "changed_row", "manual_released_flag"])
def test_unlink_requires_actual_current_release_receipt_not_released_flag(local, corruption):
    s = local
    if corruption != "manual_released_flag":
        released(s)
    if corruption == "missing_receipt":
        s.session.execute(sa.delete(CasdoorAuditExtend))
    elif corruption == "changed_row":
        s.history.desired_roles_json = '{"changed":true}'
    else:
        s.history.ownership = CasdoorMembershipOwnership.RELEASED
    s.session.commit()
    with s.session.begin(), pytest.raises(AuthTransactionError):
        CasdoorLocalLifecycleRepository(s.session).require_released_for_unlink(UUID(s.account.id), UUID(s.identity.id))


def inspect(s, repository, *, lock=True):
    return repository.inspect(
        UUID(s.identity.id), UUID(s.workspace.id), source_account_id=UUID(s.account.id), lock=lock
    )


def test_release_is_actual_cas_retains_permissions_and_has_durable_exact_receipt(local):
    s = local
    before = (s.history.baseline_json, s.history.last_applied_roles_json, s.history.desired_roles_json)
    with s.session.begin():
        owner = CasdoorLocalLifecycleRepository(s.session)
        scope = inspect(s, owner)
        receipt = owner.release(scope, actor_account_id=UUID(s.account.id), correlation_id=uuid4())
        assert receipt["ownership_epoch"] == 4
        owner.final_join(scope)
    s.session.expire_all()
    assert s.history.ownership is CasdoorMembershipOwnership.RELEASED
    assert before == (s.history.baseline_json, s.history.last_applied_roles_json, s.history.desired_roles_json)
    assert s.join.role is TenantAccountRole.NORMAL
    assert s.session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1


def test_public_snapshot_or_replayed_preparation_cannot_release(local):
    s = local
    with s.session.begin():
        owner = CasdoorLocalLifecycleRepository(s.session)
        scope = inspect(s, owner)
        with pytest.raises(AuthTransactionError):
            owner.release(replace(scope), actor_account_id=UUID(s.account.id), correlation_id=uuid4())
        owner.release(scope, actor_account_id=UUID(s.account.id), correlation_id=uuid4())
        with pytest.raises(AuthTransactionError):
            owner.release(scope, actor_account_id=UUID(s.account.id), correlation_id=uuid4())


@pytest.mark.parametrize("kind", tuple(CasdoorIntentKind))
def test_every_historical_intent_kind_including_avatar_blocks_release(local, kind):
    s = local
    add_intent(s, kind=kind, workspace_id=None, membership_id=None)
    with s.session.begin(), pytest.raises(AuthTransactionError):
        inspect(s, CasdoorLocalLifecycleRepository(s.session))


def test_join_change_after_inspection_rejects_and_rolls_back_all_writes(local):
    s = local
    with pytest.raises(AuthTransactionError), s.session.begin():
        owner = CasdoorLocalLifecycleRepository(s.session)
        scope = inspect(s, owner)
        s.session.execute(
            sa.update(TenantAccountJoin).where(TenantAccountJoin.id == s.join.id).values(role=TenantAccountRole.EDITOR)
        )
        owner.release(scope, actor_account_id=UUID(s.account.id), correlation_id=uuid4())
    s.session.expire_all()
    assert s.history.ownership is CasdoorMembershipOwnership.MANAGED
    assert s.join.role is TenantAccountRole.NORMAL


def test_owner_and_malformed_historical_baseline_are_closed(local):
    s = local
    s.join.role = TenantAccountRole.OWNER
    s.session.commit()
    with s.session.begin(), pytest.raises(AuthTransactionError):
        inspect(s, CasdoorLocalLifecycleRepository(s.session))
    s.join.role = TenantAccountRole.NORMAL
    s.history.baseline_json = "{}"
    s.session.commit()
    with s.session.begin(), pytest.raises(AuthTransactionError):
        inspect(s, CasdoorLocalLifecycleRepository(s.session))
