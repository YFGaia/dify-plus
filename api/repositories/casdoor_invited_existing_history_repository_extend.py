"""Bounded factual prior LOCAL history checks; never an invitation capability.

Finalized exact-identity history is reconciled before its original owner inspects
the current signed plan. Controlled absence is factual state, not permission to
regrant. Pending, unknown and cross-namespace history remains closed here.
"""

from datetime import datetime, timedelta
from uuid import UUID

from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    OwnershipDecision,
    parse_local_withdrawal_json,
    parse_role_baseline_json,
    role_baseline_json,
    roles_fingerprint,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
)
from models.casdoor_extend import CasdoorManagedMembershipExtend as History

from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_login_scope_repository_extend import (
    MAX_SCOPE_ROWS,
    CasdoorLoginScopeConflict,
)
from repositories.casdoor_membership_repository_extend import (
    CasdoorMembershipRepository,
)


def prior_history_rows(session, scope, *, namespace_id, account_id, identity_id, generation, plan=None):
    """Reconcile complete scalar associations with bounded full rows and owners."""
    from repositories.casdoor_invited_write_receipt_repository_extend import (
        CasdoorInvitedWriteReceiptRepository,
    )

    reader = CasdoorInvitedWriteReceiptRepository(session, None)
    ids = tuple(row.id for row in scope.histories)
    rows = reader._bounded_rows(History, History.id.in_(ids), cap=MAX_SCOPE_ROWS)
    projected = {row.id: row for row in scope.histories}
    if len(projected) != len(ids) or set(projected) != {row.id for row in rows}:
        raise CasdoorLoginScopeConflict()
    joins = {row.id: row for row in scope.joins}
    targets = {str(target.workspace_id): target for target in plan.targets} if plan is not None else None
    namespace = next(item for item in scope.namespaces if item.id == str(namespace_id))
    for row in rows:
        join = joins.get(row.join_id)
        if (
            any(getattr(row, field) != value for field, value in projected[row.id]._mapping.items())
            or (row.namespace_id, row.account_id, row.identity_id)
            != (str(namespace_id), str(account_id), str(identity_id))
            or row.finalization is not CasdoorFinalizationState.FINALIZED
            or row.tombstone is not False
            or type(row.desired_generation) is not int
            or not 0 <= row.desired_generation <= generation
            or (row.desired_generation == 0 and row.source is not CasdoorMembershipSource.ADOPT)
            or (join is not None and (join.account_id, join.tenant_id) != (str(account_id), row.workspace_id))
        ):
            raise CasdoorLoginScopeConflict()
        for raw in (row.baseline_json, row.last_applied_roles_json):
            if parse_role_baseline_json(raw).backend is not MembershipBackend.LOCAL:
                raise CasdoorLoginScopeConflict()
        if join is None:
            marker = parse_local_withdrawal_json(row.desired_roles_json)
            observation = MembershipObservation(UUID(row.workspace_id), account_id, None, None, MembershipBackend.LOCAL)
            if (
                row.ownership is not CasdoorMembershipOwnership.MANAGED
                or marker["removed_join_id"] != row.join_id
                or marker["withdrawal_epoch"] != row.ownership_epoch
                or marker["withdrawal_generation"] != row.desired_generation
                or marker["fence_epoch"] != namespace.fence_epoch
                or row.last_applied_roles_json != role_baseline_json(observation)
                or row.last_applied_fingerprint != roles_fingerprint(observation)
            ):
                raise CasdoorLoginScopeConflict()
            # Include the removed ID outside the current account's projection:
            # an unrelated live Join with that ID invalidates this absence.
            CasdoorMembershipRepository(session)._require_removed_id_absent(row)
        if plan is not None:
            target = targets.get(row.workspace_id)
            owner = CasdoorMembershipRepository(session)
            view = owner.inspect(
                GenerationPlanVersion(plan, namespace.fence_epoch, generation),
                UUID(row.workspace_id),
                backend=MembershipBackend.LOCAL,
            )
            if view.decision not in (
                OwnershipDecision.MANAGED_CURRENT,
                OwnershipDecision.PRESERVE_OVERRIDE,
                OwnershipDecision.OWNER_PROTECTED,
                OwnershipDecision.CONTROLLED_WITHDRAWN,
            ):
                raise CasdoorLoginScopeConflict()
            if target is None and row.workspace_id == str(scope.configuration.default_workspace_id):
                raise CasdoorLoginScopeConflict()
    return rows


def invited_withdrawal_workspaces(session, scope, plan, generation):
    """Derive the real original owner's controlled workspace set before B3."""
    from repositories.casdoor_login_scope_repository_extend import (
        CasdoorLoginScopeRepository,
    )

    if generation == 0:
        # Read-only initial ADOPT inspection is supported by its original owner;
        # original withdrawal preparation still requires a prior positive write.
        if any(row.workspace_id not in {str(t.workspace_id) for t in plan.targets} for row in scope.histories):
            raise CasdoorLoginScopeConflict()
        return ()
    withdrawals, _rows = CasdoorLoginScopeRepository(session, None)._classify_local_changes(scope, plan)
    return withdrawals


def verify_prior_write_rows(before, current, plan, result):
    """Exact full old row comparison around original LOCAL write effects.

    Inputs come only from the live root guard's captured rows and its actual B3
    return. This factual helper neither issues a guard nor appends a receipt.
    The original scalar scope verifier separately inspects current ownership.
    """
    from core.casdoor.local_roles import LocalRoleOutcome
    from models.account import TenantAccountRole

    from repositories.casdoor_membership_repository_extend import (
        _LocalHistoryState,
        _LocalMembershipEffect,
    )

    def require(value):
        if not value:
            raise CasdoorLoginScopeConflict()

    old_joins = {row.id: row for row in before["joins"]}
    new_joins = {row.id: row for row in current["joins"]}
    old_histories = {row.id: row for row in before["histories"]}
    new_histories = {row.id: row for row in current["histories"]}
    require(set(old_histories) <= set(new_histories))
    captured = tuple(_LocalHistoryState(*row) for row in before["histories"])
    effects = {}
    for effect in result._controlled_effects:
        require(type(effect) is _LocalMembershipEffect and effect.before.id not in effects)
        prior, actual = old_histories.get(effect.before.id), new_histories.get(effect.before.id)
        require(prior is not None and actual is not None)
        require(_LocalHistoryState(*prior) == effect.before and _LocalHistoryState(*actual) == effect.after)
        effects[effect.before.id] = effect
    changed_joins, added_joins, changed_histories, added_histories = set(), set(), set(), set()
    for target, summary in zip(plan.targets, result.workspaces, strict=True):
        require(target.workspace_id == summary.workspace_id)
        require(summary.intent_barrier.value == "clear")
        join = new_joins.get(str(summary.join_id))
        require(
            join is not None
            and join.account_id == str(plan.context.account_id)
            and join.tenant_id == str(target.workspace_id)
        )
        require(type(join.role) is TenantAccountRole and join.role is summary.current_role)
        if summary.membership_created:
            require(summary.ownership_decision is OwnershipDecision.NEW_JOIN_REQUIRED)
            require(not summary.membership_regranted and join.id not in old_joins)
            added_joins.add(join.id)
            added_histories.add(str(summary.membership_id))
        elif summary.membership_regranted:
            require(summary.ownership_decision is OwnershipDecision.CONTROLLED_WITHDRAWN)
            effect = effects.get(str(summary.membership_id))
            require(effect is not None and effect.after.join_id == join.id and join.id not in old_joins)
            added_joins.add(join.id)
            changed_histories.add(effect.before.id)
        elif summary.ownership_decision is OwnershipDecision.MANAGED_CURRENT:
            prior = old_histories.get(str(summary.membership_id))
            require(prior is not None and prior.join_id == join.id and join.id in old_joins)
            actual = new_histories[prior.id]
            require(summary.outcome in (LocalRoleOutcome.APPLIED, LocalRoleOutcome.NOOP))
            if summary.role_changed:
                require(summary.outcome is LocalRoleOutcome.APPLIED and summary.metadata_changed is True)
            require(summary.finalization is CasdoorFinalizationState.FINALIZED)
            immutable = {
                "desired_generation",
                "revision_id",
                "last_applied_roles_json",
                "last_applied_fingerprint",
                "desired_roles_json",
                "updated_at",
            }
            require(
                all(getattr(actual, field) == item for field, item in prior._mapping.items() if field not in immutable)
            )
            observation = MembershipObservation(
                target.workspace_id, plan.context.account_id, UUID(join.id), join.role, MembershipBackend.LOCAL
            )
            require(join.role.value == target.target_role)
            require(actual.desired_generation == result.generation and actual.revision_id == str(result.revision_id))
            require(actual.last_applied_roles_json == role_baseline_json(observation))
            require(actual.last_applied_fingerprint == roles_fingerprint(observation))
            require(
                actual.desired_roles_json
                == CasdoorMembershipRepository._local_desired(join.role, result.fence_epoch, target.reason)
            )
            require(summary.role_changed == (old_joins[join.id].role is not join.role))
            require(
                type(actual.updated_at) is datetime
                and type(prior.updated_at) is datetime
                and actual.updated_at.utcoffset() in (None, timedelta(0))
                and prior.updated_at.utcoffset() in (None, timedelta(0))
                and actual.updated_at >= prior.updated_at
            )
            if summary.role_changed:
                changed_joins.add(join.id)
            changed_histories.add(prior.id)
        else:
            require(summary.outcome is LocalRoleOutcome.PRESERVED)
            require(
                summary.ownership_decision
                in (
                    OwnershipDecision.PRESERVE_UNMANAGED,
                    OwnershipDecision.PRESERVE_OVERRIDE,
                    OwnershipDecision.OWNER_PROTECTED,
                )
            )
            require(not summary.role_changed and not summary.metadata_changed)
    removed_joins = {str(item.removed_join_id) for item in result.withdrawals}
    changed_histories.update(str(item.membership_id) for item in result.withdrawals)
    require(set(old_joins) - set(new_joins) == removed_joins)
    require(set(new_joins) - set(old_joins) == added_joins)
    require(set(new_histories) - set(old_histories) == added_histories)
    for join_id, prior in old_joins.items():
        if join_id in removed_joins:
            continue
        actual = new_joins[join_id]
        mutable = {"role", "updated_at"} if join_id in changed_joins else set()
        require(all(getattr(actual, field) == item for field, item in prior._mapping.items() if field not in mutable))
        if join_id in changed_joins:
            require(
                type(actual.updated_at) is datetime
                and type(prior.updated_at) is datetime
                and actual.updated_at.utcoffset() in (None, timedelta(0))
                and prior.updated_at.utcoffset() in (None, timedelta(0))
                and actual.updated_at >= prior.updated_at
            )
    for history_id, prior in old_histories.items():
        if history_id not in changed_histories:
            require(new_histories[history_id] == prior)
    return captured
