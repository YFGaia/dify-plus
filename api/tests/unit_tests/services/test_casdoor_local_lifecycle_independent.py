"""Independent mounted LOCAL lifecycle regressions using the frozen owner fixtures."""

import sqlalchemy as sa
import test_casdoor_identity_action_flow_extend as actions_owner
import test_casdoor_local_lifecycle_flow_extend as lifecycle_owner

from models.account import TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorIdentityExtend,
    CasdoorIntentKind,
    CasdoorManagedMembershipExtend,
    CasdoorMembershipOwnership,
    CasdoorOperationState,
    CasdoorSyncIntentExtend,
    CasdoorTerminationState,
)

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


def test_final_original_refresh_get_revocation_rolls_back_mounted_release(lifecycle, monkeypatch):
    d = lifecycle
    proof = lifecycle_owner.review(d, lifecycle_owner.managed(d), "release")
    original_get = d.f.redis.get
    inside_uow_reads = []

    def revoke_before_final_read(key):
        if d.f.opened:
            inside_uow_reads.append(key)
            d.source.clear()
        return original_get(key)

    monkeypatch.setattr(d.f.redis, "get", revoke_before_final_read, raising=False)
    response = lifecycle_owner.send(d, lifecycle_owner.PATH + "/release", method="POST", json=proof)

    assert response.status_code != 200, response.json
    assert inside_uow_reads == ["refresh_token:" + d.token]
    with d.f.service._session_factory() as session:
        assert session.scalar(sa.select(CasdoorManagedMembershipExtend.ownership)) is CasdoorMembershipOwnership.MANAGED
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(CasdoorAuditExtend)
                .where(CasdoorAuditExtend.action == "local_membership_release_v1")
            )
            == 0
        )


def test_mounted_review_rejects_terminal_historical_avatar_intent(lifecycle):
    d = lifecycle
    target = lifecycle_owner.managed(d)
    with d.f.service._session_factory() as session, session.begin():
        identity = session.scalar(
            sa.select(CasdoorIdentityExtend).where(CasdoorIdentityExtend.account_id == d.actor.id)
        )
        integration = d.services.casdoor_configuration._repository(session)._integration()
        revision = d.services.casdoor_configuration._repository(session)._revision(
            integration.id, integration.active_revision_id
        )
        session.add(
            CasdoorSyncIntentExtend(
                namespace_id=identity.namespace_id,
                identity_id=identity.id,
                account_id=d.actor.id,
                workspace_id=target["workspace_id"],
                membership_id=None,
                revision_id=revision.id,
                generation=identity.sync_generation,
                ownership_epoch=0,
                fence_epoch=0,
                kind=CasdoorIntentKind.PROFILE_AVATAR,
                scope_digest="c" * 64,
                idempotency_key="d" * 64,
                desired_json="{}",
                operation_state=CasdoorOperationState.FAILED,
                termination_state=CasdoorTerminationState.CONFIRMED,
            )
        )

    response = lifecycle_owner.send(d, lifecycle_owner.PATH, query_string=target)
    assert response.status_code != 200, response.json
    assert not d.review_records
    with d.f.service._session_factory() as session:
        assert session.scalar(sa.select(CasdoorManagedMembershipExtend.ownership)) is CasdoorMembershipOwnership.MANAGED


def test_actual_reauth_unlink_rejects_release_receipt_tampered_after_success(lifecycle, monkeypatch):
    d = lifecycle
    actions_owner.enable_unlink(d, monkeypatch)
    target = lifecycle_owner.managed(d)
    proof = lifecycle_owner.review(d, target, "release")
    released = lifecycle_owner.send(d, lifecycle_owner.PATH + "/release", method="POST", json=proof)
    assert released.status_code == 200, released.json

    # Establish a real recent-reauth proof first, then corrupt the persisted
    # receipt before the actual mounted unlink route consumes that proof.
    state, _ = actions_owner.begin(d, "reauthenticate")
    assert actions_owner.complete(d, state).status_code == 302

    with d.f.service._session_factory() as session, session.begin():
        receipt = session.scalar(
            sa.select(CasdoorAuditExtend)
            .where(CasdoorAuditExtend.action == "local_membership_release_v1")
            .with_for_update()
        )
        assert receipt is not None
        receipt.summary_json = "{}"
    result = lifecycle_owner.send(d, "/console/api/account/casdoor-identity/unlink", method="POST", json={})

    assert result.status_code != 200, result.json
    with d.f.service._session_factory() as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.id).where(CasdoorIdentityExtend.account_id == d.actor.id))
        assert (
            session.scalar(sa.select(TenantAccountJoin.role).where(TenantAccountJoin.account_id == d.actor.id))
            is TenantAccountRole.NORMAL
        )
        assert (
            session.scalar(sa.select(CasdoorManagedMembershipExtend.ownership)) is CasdoorMembershipOwnership.RELEASED
        )
