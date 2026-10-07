"""Signed LOCAL login regression for explicit role mapping after local removal."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.configuration import WorkspaceRoleMapping
from core.casdoor.gateway import GatewayOperation
from core.casdoor.manual_ownership import ManualMutationKind
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    role_baseline_json,
)
from models.account import Account, TenantAccountJoin
from models.account import TenantAccountRole as Role
from models.account_money_extend import AccountMoneyExtend as Quota
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorMembershipOwnership as Ownership,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.casdoor_extend import (
    CasdoorTerminationState as Termination,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from services.casdoor_manual_member_mutation_service_extend import (
    mark_local_manual_member_mutation,
)
from sqlalchemy.orm import Session
from test_casdoor_local_login_finalization_service_extend import new_authorization

pytest_plugins = ["test_casdoor_local_login_finalization_service_extend"]


def _remove_locally(chain, workspace_id):
    """Use the production metadata bridge and apply the original writer's deletion."""
    account_id = chain.prepared[0].account_id
    with Session(chain.local.engine) as session, session.begin():
        mark_local_manual_member_mutation(
            session,
            workspace_id=str(workspace_id),
            account_ids=(str(account_id),),
            kind=ManualMutationKind.MEMBER_REMOVE,
        )
        join = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.tenant_id == str(workspace_id),
                TenantAccountJoin.account_id == str(account_id),
            )
        )
        assert join is not None
        old_join_id = join.id
        session.delete(join)
    return str(account_id), old_join_id


def _history(chain, account_id, workspace_id):
    with Session(chain.local.engine) as session:
        return session.scalar(
            sa.select(History).where(
                History.account_id == str(account_id), History.workspace_id == str(workspace_id)
            )
        )


def _revise_mapping(chain, workspace_id):
    """Add an explicit admin mapping through a correctly digested active revision."""
    local = chain.local
    ref = local.roles.effective_roles[0]
    configuration = local.config.model_copy(
        update={
            "workspace_mappings": (
                WorkspaceRoleMapping(workspace_id=UUID(int=workspace_id), admin=ref),
                *local.config.workspace_mappings,
            )
        }
    )
    session = local.session
    c = local.env[1]
    with session.begin():
        base = dict(
            session.execute(sa.select(*Revision.__table__.columns).where(Revision.id == str(c.revision_id)))
            .one()
            ._mapping
        )
        revision_id = str(uuid4())
        data = json.loads(configuration.canonical_json())
        repository = chain.configuration_service._repository(session)
        plaintext_secret = repository.crypto.decrypt(
            base["encrypted_secret"],
            context=repository._secret_context(base["namespace_id"], base["id"]),
        )
        base["encrypted_secret"] = repository.crypto.encrypt(
            plaintext_secret,
            context=repository._secret_context(base["namespace_id"], revision_id),
        )
        session.execute(
            sa.insert(Revision).values(
                **dict(
                    base,
                    id=revision_id,
                    revision_number=base["revision_number"] + 1,
                    mappings_json=json.dumps(data["workspace_mappings"]),
                )
            )
        )
        revision = session.get(Revision, revision_id)
        digest = repository._validation_digest(configuration, revision)
        session.execute(
            sa.update(Revision)
            .where(Revision.id == revision_id)
            .values(config_digest=digest)
        )
        session.execute(sa.update(Integration).values(active_revision_id=revision_id))
    local.env = (
        session,
        replace(c, revision_id=UUID(revision_id), active_revision_id=UUID(revision_id), config_digest=digest),
        *local.env[2:],
    )
    local.config = configuration
    with Session(local.engine) as verify_session:
        loaded_revision = verify_session.get(Revision, revision_id)
        loaded_config = chain.configuration_service._repository(verify_session)._configuration(loaded_revision)
        expected_fields = configuration.model_dump()
        loaded_fields = loaded_config.model_dump()
        assert [key for key in expected_fields if expected_fields[key] != loaded_fields[key]] == []
    old_operation = chain.operation
    chain.operation = GatewayOperation(
        config=configuration,
        client_secret=old_operation.client_secret,
        registered_redirect_uri=old_operation.registered_redirect_uri,
        deadline=old_operation.deadline,
    )
    chain.consumed = replace(
        chain.consumed,
        context=replace(chain.consumed.context, revision_id=UUID(revision_id)),
    )
    prior_guard = chain.consume_args["guard"]
    chain.consume_args["guard"] = lambda: replace(prior_guard(), revision_id=UUID(revision_id))


def test_explicit_mapping_regrants_removed_member_once_with_same_history_and_new_join(finalized_chain):
    f = finalized_chain
    first = f.chain.invoke(ip_address="192.0.2.7")
    account_id = str(first.persistence.account_id)
    workspace_id = str(UUID(int=200))
    with Session(f.chain.local.engine) as session:
        old = session.scalar(
            sa.select(History).where(History.account_id == account_id, History.workspace_id == workspace_id)
        )
        old_history_id, old_join_id = old.id, old.join_id
        baseline = old.baseline_json
        account_before = session.get(Account, account_id)
        account_values = (account_before.email, account_before.status, account_before.initialized_at)
        quota_before = session.scalar(sa.select(Quota).where(Quota.account_id == account_id))
        quota_values = (quota_before.id, quota_before.total_quota, quota_before.used_quota)
        identity_before = session.scalar(sa.select(Identity).where(Identity.account_id == account_id))
        identity_values = (identity_before.id, identity_before.namespace_id, identity_before.subject)

    removed_account, removed_join_id = _remove_locally(f.chain, workspace_id)
    assert removed_account == account_id and removed_join_id == old_join_id
    removed = _history(f.chain, account_id, workspace_id)
    assert removed.id == old_history_id and removed.join_id == old_join_id
    assert removed.ownership is Ownership.LOCAL_OVERRIDE and removed.tombstone

    outcome = new_authorization(f.chain)
    assert outcome.tokens
    with Session(f.chain.local.engine) as session:
        restored = session.scalar(
            sa.select(History).where(History.account_id == account_id, History.workspace_id == workspace_id)
        )
        join = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == account_id, TenantAccountJoin.tenant_id == workspace_id
            )
        )
        assert restored.id == old_history_id and restored.join_id == join.id
        assert restored.ownership is Ownership.MANAGED and not restored.tombstone
        assert join.id != old_join_id and join.role is Role.ADMIN
        assert session.get(TenantAccountJoin, old_join_id) is None
        assert restored.baseline_json == baseline
        account_after = session.get(Account, account_id)
        assert (account_after.email, account_after.status, account_after.initialized_at) == account_values
        quota_after = session.scalar(sa.select(Quota).where(Quota.account_id == account_id))
        assert (quota_after.id, quota_after.total_quota, quota_after.used_quota) == quota_values
        identity_after = session.scalar(sa.select(Identity).where(Identity.account_id == account_id))
        assert (identity_after.id, identity_after.namespace_id, identity_after.subject) == identity_values
        assert session.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "local_member_regrant")) == 1

    new_join_id = join.id
    repeated = new_authorization(f.chain)
    assert repeated.tokens
    with Session(f.chain.local.engine) as session:
        current = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == account_id, TenantAccountJoin.tenant_id == workspace_id
            )
        )
        assert current.id == new_join_id and current.role is Role.ADMIN
        assert session.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "local_member_regrant")) == 1


def test_explicit_admin_mapping_restores_default_workspace(finalized_chain):
    f = finalized_chain
    _revise_mapping(f.chain, 100)
    # The helper takes a correlation ID from a prior prepared login. Supply a
    # fixture-only seed for this first signed authorization, then keep the real
    # prepared login it appends.
    f.chain.prepared.append(SimpleNamespace(account_id=UUID(int=701)))
    mapped = new_authorization(f.chain)
    f.chain.prepared.pop(0)
    assert mapped.tokens
    account_id = str(mapped.persistence.account_id)
    with Session(f.chain.local.engine) as session:
        original = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == account_id,
                TenantAccountJoin.tenant_id == str(UUID(int=100)),
            )
        )
        assert original.role is Role.ADMIN
    removed_account, old_join_id = _remove_locally(f.chain, str(UUID(int=100)))
    assert removed_account == account_id
    empty_baseline = role_baseline_json(
        MembershipObservation(UUID(int=100), UUID(account_id), None, None, MembershipBackend.LOCAL)
    )
    with Session(f.chain.local.engine) as session, session.begin():
        session.execute(
            sa.update(History)
            .where(History.account_id == account_id, History.workspace_id == str(UUID(int=100)))
            .values(baseline_json=empty_baseline)
        )
    outcome = new_authorization(f.chain)
    assert outcome.tokens
    with Session(f.chain.local.engine) as session:
        current = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == account_id,
                TenantAccountJoin.tenant_id == str(UUID(int=100)),
            )
        )
        row = session.scalar(
            sa.select(History).where(
                History.account_id == account_id, History.workspace_id == str(UUID(int=100))
            )
        )
        assert current.role is Role.ADMIN and current.id != old_join_id
        assert row.ownership is Ownership.MANAGED and not row.tombstone
        assert row.baseline_json == empty_baseline
    following = new_authorization(f.chain)
    assert following.tokens
    with Session(f.chain.local.engine) as session:
        again = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == account_id,
                TenantAccountJoin.tenant_id == str(UUID(int=100)),
            )
        )
        assert again.id == current.id and again.role is Role.ADMIN


@pytest.mark.parametrize("state", ["fallback_only", "released_tombstone", "managed_tombstone", "local_role_override"])
def test_non_regrantable_local_states_keep_their_local_disposition(finalized_chain, state):
    f = finalized_chain
    f.chain.invoke(ip_address="192.0.2.7")
    account_id = str(f.chain.prepared[0].account_id)
    workspace_id = str(UUID(int=200 if state != "fallback_only" else 100))
    old_join_id = None
    if state in ("fallback_only", "released_tombstone"):
        account_id, old_join_id = _remove_locally(f.chain, workspace_id)
    elif state == "managed_tombstone":
        with Session(f.chain.local.engine) as session, session.begin():
            row = session.scalar(sa.select(History).where(History.workspace_id == workspace_id))
            session.execute(sa.update(History).where(History.id == row.id).values(tombstone=True))
    else:
        with Session(f.chain.local.engine) as session, session.begin():
            row = session.scalar(sa.select(History).where(History.workspace_id == workspace_id))
            mark_local_manual_member_mutation(
                session,
                workspace_id=workspace_id,
                account_ids=(account_id,),
                kind=ManualMutationKind.ROLE_CHANGE,
            )
            session.execute(
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.id == row.join_id)
                .values(role=Role.EDITOR)
            )
    if state == "released_tombstone":
        with Session(f.chain.local.engine) as session, session.begin():
            session.execute(
                sa.update(History)
                .where(History.account_id == account_id, History.workspace_id == workspace_id)
                .values(ownership=Ownership.RELEASED)
            )

    before_token_writes = len(f.calls)
    if state in ("fallback_only", "released_tombstone"):
        with pytest.raises(CasdoorLoginScopeConflict):
            new_authorization(f.chain)
        assert len(f.calls) == before_token_writes
    else:
        outcome = new_authorization(f.chain)
        assert outcome.tokens

    with Session(f.chain.local.engine) as session:
        row = session.scalar(
            sa.select(History).where(History.account_id == account_id, History.workspace_id == workspace_id)
        )
        join = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == account_id, TenantAccountJoin.tenant_id == workspace_id
            )
        )
        if state == "local_role_override":
            assert row.ownership is Ownership.LOCAL_OVERRIDE and join.role is Role.EDITOR
        elif state in ("fallback_only", "released_tombstone"):
            assert row is not None
            assert join is None
            assert row.join_id == old_join_id
        else:
            assert row.ownership is Ownership.MANAGED and row.tombstone
            assert join is not None and join.id == row.join_id


def test_required_intent_still_blocks_fresh_authorization(finalized_chain):
    f = finalized_chain
    first = f.chain.invoke(ip_address="192.0.2.7")
    workspace_id = str(UUID(int=200))
    row = _history(f.chain, str(first.persistence.account_id), workspace_id)
    with Session(f.chain.local.engine) as session, session.begin():
        session.add(
            Intent(
                namespace_id=row.namespace_id,
                identity_id=row.identity_id,
                account_id=row.account_id,
                workspace_id=workspace_id,
                membership_id=row.id,
                revision_id=row.revision_id,
                generation=row.desired_generation,
                ownership_epoch=row.ownership_epoch,
                fence_epoch=0,
                kind="resource_grant",
                scope_digest="a" * 64,
                idempotency_key=uuid4().hex * 2,
                desired_json="{}",
                operation_state="pending",
                termination_state=Termination.CONFIRMED,
            )
        )
    with pytest.raises(ValueError):
        new_authorization(f.chain)
