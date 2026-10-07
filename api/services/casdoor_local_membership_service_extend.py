"""LOCAL, uncommitted membership composition after flushed LOGIN identity binding.

The trusted caller must already hold complete leases and integration -> ALL
namespace -> account/identity/workspace parents before earlier account writes.
Later repository locks cannot repair that ordering. This helper neither attests
those preconditions nor authenticates a public plan. I18-C owns fresh admission,
trusted plan reconstruction and final configuration/graph/fence/deadline
checks; I19 owns finalization and session decisions. Every exception requires
whole caller rollback, including earlier account/quota/identity writes.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from configs import dify_config
from core.casdoor.local_roles import LocalRoleOutcome, resolve_local_target
from core.casdoor.mapping import DesiredWorkspacePlan
from core.casdoor.ownership import MembershipBackend, OwnershipDecision
from enums import DeploymentEdition
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
    TenantStatus,
)
from models.casdoor_extend import CasdoorConfigRevisionExtend, CasdoorFinalizationState
from models.dataset import Dataset
from models.model import App
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_generation_repository_extend import (
    CasdoorGenerationRepository,
)
from repositories.casdoor_local_role_repository_extend import CasdoorLocalRoleRepository
from repositories.casdoor_membership_repository_extend import (
    CasdoorMembershipRepository,
    _LocalMembershipEffect,
)
from repositories.casdoor_required_intent_repository_extend import (
    CasdoorRequiredIntentRepository,
)
from sqlalchemy.orm import Session, SessionTransaction

from services.account_service import TenantService


class CasdoorLocalMembershipConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("authorization_pending")


class RequiredIntentBarrier(StrEnum):
    CLEAR = "clear"
    PENDING = "pending"


@dataclass(frozen=True, slots=True, repr=False)
class LocalWorkspacePersistence:
    """Observation plus local effect, never a repository or authorization receipt.

    join_id/current_role distinguish preserved presence from historical absence.
    A clear intent observation is not a global finalization or access proof.
    finalization is reported only when an actual LOCAL receipt provides it.
    """

    workspace_id: UUID
    ownership_decision: OwnershipDecision
    intent_barrier: RequiredIntentBarrier
    outcome: LocalRoleOutcome
    join_id: UUID | None
    membership_id: UUID | None
    current_role: TenantAccountRole | None
    membership_created: bool = False
    role_changed: bool = False
    metadata_changed: bool = False
    finalization: CasdoorFinalizationState | None = None
    membership_regranted: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class LocalMembershipWithdrawal:
    """Actual removal scalars only; no authorization or termination proof."""

    workspace_id: UUID
    membership_id: UUID
    removed_join_id: UUID
    prior_role: TenantAccountRole
    epoch_before: int
    epoch_after: int
    finalization: CasdoorFinalizationState


@dataclass(frozen=True, slots=True, repr=False)
class LocalMembershipPersistence:
    """Sorted scalar LOCAL uncommitted results, with no session conclusion."""

    account_id: UUID
    identity_id: UUID
    namespace_id: UUID
    revision_id: UUID
    fence_epoch: int
    generation: int
    workspaces: tuple[LocalWorkspacePersistence, ...]
    withdrawals: tuple[LocalMembershipWithdrawal, ...] = ()
    # Internal exact CAS deltas for C1/L3 final comparison, never serialized as
    # public authorization/finalization results or reconstructed after auditing.
    _controlled_effects: tuple[_LocalMembershipEffect, ...] = field(default=(), repr=False)
    _archived_facts: tuple[str, ...] = field(default=(), repr=False)


class CasdoorLocalMembershipService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _root(self, expected: SessionTransaction | None = None) -> SessionTransaction:
        if not isinstance(self._session, Session) or dify_config.RBAC_ENABLED is not False:
            raise CasdoorLocalMembershipConflict()
        transaction = self._session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or not self._session.is_active
            or self._session.in_nested_transaction()
            or self._session.new
            or self._session.dirty
            or self._session.deleted
            or (expected is not None and transaction is not expected)
        ):
            raise CasdoorLocalMembershipConflict()
        return transaction

    def persist_local_memberships(
        self,
        plan: DesiredWorkspacePlan,
        *,
        expected_fence_epoch: int,
        expected_generation: int,
        withdrawal_workspace_ids: tuple[UUID, ...] = (),
        correlation_id: UUID | None = None,
        invitation_guard: object | None = None,
        _ordinary_guard=None,
    ) -> LocalMembershipPersistence:
        """Reuse real owners in one root; no local transaction or recovery loop.

        Full plan shape is checked before generation DML. Preserved histories do
        not enter LOCAL prepare, whose current-only history contract is narrower.
        Required intents across all histories/states/generations block mutation.
        """
        root = self._root()
        if invitation_guard is not None and _ordinary_guard is not None:
            raise CasdoorLocalMembershipConflict()
        if _ordinary_guard is not None:
            from repositories.casdoor_terminal_local_invitation_repository_extend import _ordinary_terminal_last

            _ordinary_terminal_last(_ordinary_guard)
        try:
            if not isinstance(plan, DesiredWorkspacePlan) or not plan.targets:
                raise ValueError()
            # The core owner validates the entire bounded plan on each call.
            resolve_local_target(plan, plan.targets[0])
            targets = tuple(sorted(plan.targets, key=lambda target: str(target.workspace_id)))
            roles = tuple(resolve_local_target(plan, target) for target in targets)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise CasdoorLocalMembershipConflict() from None
        context = plan.context
        if (
            type(withdrawal_workspace_ids) is not tuple
            or len(withdrawal_workspace_ids) > 100
            or any(type(value) is not UUID for value in withdrawal_workspace_ids)
        ):
            raise CasdoorLocalMembershipConflict()
        if invitation_guard is not None:
            from repositories.casdoor_invited_login_guard_repository_extend import _begin_invited_b3

            _begin_invited_b3(
                invitation_guard,
                self._session,
                plan,
                expected_fence_epoch,
                expected_generation,
                withdrawal_workspace_ids,
            )
        withdrawal_ids = frozenset(withdrawal_workspace_ids)
        target_by_id = {target.workspace_id: (target, role) for target, role in zip(targets, roles, strict=True)}
        if withdrawal_ids & target_by_id.keys():
            raise CasdoorLocalMembershipConflict()
        if withdrawal_ids:
            if dify_config.DEPLOYMENT_EDITION == DeploymentEdition.ENTERPRISE:
                raise CasdoorLocalMembershipConflict()
            default_id = self._session.scalar(
                sa.select(CasdoorConfigRevisionExtend.default_workspace_id).where(
                    CasdoorConfigRevisionExtend.id == str(context.revision_id)
                )
            )
            if default_id is None or default_id in {str(value) for value in withdrawal_ids}:
                raise CasdoorLocalMembershipConflict()
        with self._session.no_autoflush:
            account_state = self._session.execute(
                sa.select(Account.status, Account.initialized_at)
                .where(Account.id == str(context.account_id))
                .with_for_update()
            ).one_or_none()
        if (
            account_state is None
            or account_state.status is not AccountStatus.ACTIVE
            or account_state.initialized_at is None
        ):
            raise CasdoorLocalMembershipConflict()
        self._root(root)
        version = CasdoorGenerationRepository(self._session).allocate(
            plan, expected_fence_epoch=expected_fence_epoch, expected_generation=expected_generation
        )
        members = CasdoorMembershipRepository(self._session)
        local_roles = CasdoorLocalRoleRepository(self._session)
        intents = CasdoorRequiredIntentRepository(self._session)
        summaries = []
        withdrawals = []
        effects = []
        for workspace_id in sorted(set(target_by_id) | withdrawal_ids, key=str):
            if workspace_id in withdrawal_ids:
                self._root(root)
                view = members.inspect(version, workspace_id, backend=MembershipBackend.LOCAL)
                if intents.read_locked(context.account_id, workspace_id, _ordinary_guard=_ordinary_guard):
                    raise CasdoorLocalMembershipConflict()
                if view.decision is OwnershipDecision.CONTROLLED_WITHDRAWN:
                    continue
                if view.decision is not OwnershipDecision.MANAGED_CURRENT:
                    raise CasdoorLocalMembershipConflict()
                self._correlation(correlation_id)
                prepared = members.prepare_local_withdrawal(version, workspace_id, _ordinary_guard=_ordinary_guard)
                self._root(root)
                self._remove_local_member(
                    workspace_id, context.account_id, view.observation.join_id, view.observation.join_role, root
                )
                effect = members.register_local_withdrawal(prepared)
                effects.append(effect)
                after = effect.managed
                self._root(root)
                self._audit_local(version, workspace_id, view.managed, after, correlation_id, withdrawal=True)
                self._root(root)
                withdrawals.append(
                    LocalMembershipWithdrawal(
                        workspace_id,
                        after.membership_id,
                        view.observation.join_id,
                        view.observation.join_role,
                        view.managed.ownership_epoch,
                        after.ownership_epoch,
                        CasdoorFinalizationState.PENDING,
                    )
                )
                continue
            target, role = target_by_id[workspace_id]
            target_guard = None
            if invitation_guard is not None:
                from repositories.casdoor_invited_login_guard_repository_extend import _invited_target_guard

                target_guard = _invited_target_guard(invitation_guard, self._session, target.workspace_id)
            self._root(root)
            local_roles.validate_parent_scope(version, target)
            self._root(root)
            view = members.inspect(version, target.workspace_id, backend=MembershipBackend.LOCAL)
            self._root(root)
            barrier = (
                RequiredIntentBarrier.PENDING
                if (
                    intents.read_locked(context.account_id, target.workspace_id, _ordinary_guard=_ordinary_guard)
                    if target_guard is None
                    else intents.read_locked(context.account_id, target.workspace_id, invitation_guard=target_guard)
                )
                else RequiredIntentBarrier.CLEAR
            )
            self._root(root)
            decision = view.decision
            created = False
            regranted = False
            if barrier is RequiredIntentBarrier.CLEAR and decision in (
                OwnershipDecision.NEW_JOIN_REQUIRED,
                OwnershipDecision.MANAGED_CURRENT,
                OwnershipDecision.CONTROLLED_WITHDRAWN,
            ):
                if decision in (OwnershipDecision.NEW_JOIN_REQUIRED, OwnershipDecision.CONTROLLED_WITHDRAWN):
                    regranted = decision is OwnershipDecision.CONTROLLED_WITHDRAWN
                    if regranted:
                        self._correlation(correlation_id)
                        token = members.prepare_local_regrant(version, target, _ordinary_guard=_ordinary_guard)
                    else:
                        token = members.prepare_new(version, target, backend=MembershipBackend.LOCAL)
                    self._root(root)
                    with self._session.no_autoflush:
                        account = self._session.scalar(
                            sa.select(Account)
                            .where(Account.id == str(context.account_id))
                            .execution_options(populate_existing=True)
                            .with_for_update()
                        )
                        tenant = self._session.scalar(
                            sa.select(Tenant)
                            .where(Tenant.id == str(target.workspace_id))
                            .execution_options(populate_existing=True)
                            .with_for_update()
                        )
                    if (
                        account is None
                        or account.status is not AccountStatus.ACTIVE
                        or account.initialized_at is None
                        or tenant is None
                        or tenant.status is not TenantStatus.NORMAL
                    ):
                        raise CasdoorLocalMembershipConflict()
                    self._root(root)
                    original_receipt = TenantService.persist_tenant_member(tenant, account, self._session, role.value)
                    self._root(root)
                    if regranted:
                        effect = members.register_local_regrant(token, original_receipt)
                        effects.append(effect)
                        after = effect.managed
                        self._audit_local(version, workspace_id, view.managed, after, correlation_id, withdrawal=False)
                    else:
                        members.register_new(token, original_receipt)
                    self._root(root)
                    created = not regranted
                prepared = (
                    local_roles.prepare(version, target, _ordinary_guard=_ordinary_guard)
                    if target_guard is None
                    else local_roles.prepare(version, target, invitation_guard=target_guard)
                )
                self._root(root)
                receipt = local_roles.apply(prepared)
                self._root(root)
                if (created or regranted) and receipt.outcome not in (LocalRoleOutcome.APPLIED, LocalRoleOutcome.NOOP):
                    # A later barrier cannot leave our new join partially applied.
                    raise CasdoorLocalMembershipConflict()
                if regranted and (
                    receipt.outcome is not LocalRoleOutcome.NOOP
                    or receipt.metadata_changed
                    or receipt.role_changed
                    or receipt.ownership_epoch != after.ownership_epoch
                ):
                    raise CasdoorLocalMembershipConflict()
                if receipt.outcome is LocalRoleOutcome.PENDING:
                    barrier = (
                        RequiredIntentBarrier.PENDING
                        if (
                            intents.read_locked(
                                context.account_id, target.workspace_id, _ordinary_guard=_ordinary_guard
                            )
                            if target_guard is None
                            else intents.read_locked(
                                context.account_id, target.workspace_id, invitation_guard=target_guard
                            )
                        )
                        else RequiredIntentBarrier.CLEAR
                    )
                    self._root(root)
                summaries.append(
                    LocalWorkspacePersistence(
                        target.workspace_id,
                        decision,
                        barrier,
                        receipt.outcome,
                        receipt.join_id,
                        receipt.membership_id,
                        receipt.current_role,
                        created,
                        receipt.role_changed,
                        receipt.metadata_changed,
                        receipt.finalization,
                        regranted,
                    )
                )
            else:
                pending = barrier is RequiredIntentBarrier.PENDING or decision in (
                    OwnershipDecision.AUTHORIZATION_PENDING,
                    OwnershipDecision.MARK_OVERRIDE_REQUIRED,
                    OwnershipDecision.REQUEST_OWNER_FENCE,
                )
                summaries.append(
                    LocalWorkspacePersistence(
                        target.workspace_id,
                        decision,
                        barrier,
                        LocalRoleOutcome.PENDING if pending else LocalRoleOutcome.PRESERVED,
                        view.observation.join_id,
                        view.managed.membership_id if view.managed else None,
                        view.observation.join_role,
                    )
                )
        self._root(root)
        if _ordinary_guard is not None:
            _ordinary_terminal_last(_ordinary_guard)
        return LocalMembershipPersistence(
            context.account_id,
            context.identity_id,
            context.namespace_id,
            context.revision_id,
            version.fence_epoch,
            version.generation,
            tuple(summaries),
            tuple(withdrawals),
            tuple(effects),
        )

    @staticmethod
    def _correlation(value):
        if type(value) is not UUID:
            raise CasdoorLocalMembershipConflict()

    def _audit_local(self, version, workspace_id, before, after, correlation_id, *, withdrawal):
        c = version.plan.context
        CasdoorAuditRepository(self._session).append_local_membership(
            withdrawal=withdrawal,
            namespace_id=c.namespace_id,
            revision_id=c.revision_id,
            identity_id=c.identity_id,
            account_id=c.account_id,
            workspace_id=workspace_id,
            membership_id=after.membership_id,
            old_join_id=before.join_id,
            new_join_id=None if withdrawal else after.join_id,
            epoch_before=before.ownership_epoch,
            epoch_after=after.ownership_epoch,
            generation=version.generation,
            fence_epoch=version.fence_epoch,
            correlation_id=correlation_id,
        )

    def _remove_local_member(self, workspace_id, account_id, join_id, prior_role, root):
        """Resolve actual recipient, use L1 scalar effect, then check residuals."""
        with self._session.no_autoflush:
            tenant = self._session.scalar(
                sa.select(Tenant)
                .where(Tenant.id == str(workspace_id))
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            join = self._session.scalar(
                sa.select(TenantAccountJoin)
                .where(
                    TenantAccountJoin.id == str(join_id),
                    TenantAccountJoin.tenant_id == str(workspace_id),
                    TenantAccountJoin.account_id == str(account_id),
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            owners = tuple(
                self._session.execute(
                    sa.select(TenantAccountJoin.id, TenantAccountJoin.account_id)
                    .where(
                        TenantAccountJoin.tenant_id == str(workspace_id),
                        TenantAccountJoin.role == TenantAccountRole.OWNER,
                    )
                    .order_by(TenantAccountJoin.id)
                    .limit(2)
                    .with_for_update()
                )
            )
        if (
            tenant is None
            or tenant.status is not TenantStatus.NORMAL
            or join is None
            or join.role is not prior_role
            or join.role not in (TenantAccountRole.ADMIN, TenantAccountRole.EDITOR, TenantAccountRole.NORMAL)
            or len(owners) != 1
            or owners[0].account_id == str(account_id)
        ):
            raise CasdoorLocalMembershipConflict()
        for value in owners[0]:
            if str(UUID(value)) != value:
                raise CasdoorLocalMembershipConflict()
        self._root(root)
        TenantService._persist_member_removal_effect(
            tenant, str(account_id), join, owners[0].account_id, session=self._session
        )
        self._session.flush()
        self._root(root)
        for model in (App, Dataset):
            if (
                self._session.scalar(
                    sa.select(model.id)
                    .where(model.tenant_id == str(workspace_id), model.maintainer == str(account_id))
                    .limit(1)
                )
                is not None
            ):
                raise CasdoorLocalMembershipConflict()
