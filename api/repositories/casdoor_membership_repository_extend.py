"""Bounded managed-membership foundation with caller-owned transactions.

Join effects remain with the original member owner. This repository owns the
finite controlled LOCAL history CAS, with no commit/rollback, I/O or saga.
I18 must acquire sorted leases, load fresh trusted projections before the
DB transaction, check lease ownership after I/O/pre-commit, and use the existing
member helper only after prepare_new has acquired parent locks. Legacy writers
do not all share those locks: registration additionally requires the actual
helper's new-member receipt. Python handoffs are not a global concurrency proof.
"""

import hashlib
import json
from collections import namedtuple
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa
from configs import dify_config
from core.casdoor.errors import CasdoorDecisionReason, CasdoorErrorCode
from core.casdoor.local_roles import LOCAL_ROLES, resolve_local_target
from core.casdoor.mapping import (
    DesiredWorkspacePlan,
    DesiredWorkspaceTarget,
    MappingError,
    validate_mapping_context,
)
from core.casdoor.ownership import (
    MAX_SNAPSHOT_BYTES,
    UNKNOWN_MEMBER_ROLES,
    ExternalMemberRolesProjection,
    ManagedMembershipSnapshot,
    MembershipBackend,
    MembershipObservation,
    OwnershipDecision,
    decide_ownership,
    parse_local_withdrawal_json,
    parse_role_baseline_json,
    role_baseline_json,
    roles_fingerprint,
)
from enums import DeploymentEdition
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
    TenantStatus,
)
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorManagedMembershipExtend,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
)
from sqlalchemy.orm import Session, SessionTransaction

from repositories.casdoor_generation_repository_extend import (
    MAX_GENERATION,
    GenerationPlanVersion,
)
from repositories.casdoor_required_intent_repository_extend import (
    CasdoorRequiredIntentRepository,
)

MAX_NAMESPACES = 2000
# Immutable copy of the original full row; ORM refreshes must not mutate a
# previously issued one-use preparation's CAS state.
_LocalHistoryState = namedtuple("_LocalHistoryState", CasdoorManagedMembershipExtend.__table__.columns.keys())

if TYPE_CHECKING:
    from services.account_service import _PersistedTenantMember


class CasdoorMembershipConflict(ValueError):
    code = CasdoorErrorCode.CONFIG_CONFLICT

    def __init__(self) -> None:
        super().__init__(self.code.value)


@dataclass(frozen=True, repr=False)
class MembershipInspection:
    observation: MembershipObservation
    managed: ManagedMembershipSnapshot | None
    decision: OwnershipDecision
    invited_by: UUID | None = None


@dataclass(frozen=True, repr=False)
class NewMembershipPreparation:
    """Repository-issued one-use absence handoff, bound to the exact DB UoW.

    The fields and Python type are not authentication. The issuing repository
    verifies object identity in its private preparation registry on consumption.
    """

    version: GenerationPlanVersion
    target: DesiredWorkspaceTarget
    backend: MembershipBackend
    remote: ExternalMemberRolesProjection
    transaction: SessionTransaction


@dataclass(frozen=True, repr=False)
class _LocalMembershipEffect:
    """Original CAS expectations for later exact comparison, never authority.

    Private full rows stay within the trusted local stack. The after row is
    derived from the old state plus this CAS's SET, not a post-audit observation.
    """

    before: tuple
    after: tuple
    managed: ManagedMembershipSnapshot


@dataclass(frozen=True, repr=False)
class _LocalMembershipPreparation:
    """One-use actual-state handoff for only withdrawal or regrant in this root."""

    version: GenerationPlanVersion
    workspace_id: UUID
    target: DesiredWorkspaceTarget | None
    transaction: SessionTransaction
    state: tuple
    withdrawal: bool


class CasdoorMembershipRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._preparations: dict[int, NewMembershipPreparation] = {}
        self._local_preparations: dict[int, _LocalMembershipPreparation] = {}
        self._ordinary_guards: dict[int, object] = {}

    def inspect(
        self,
        version: GenerationPlanVersion,
        workspace_id: UUID,
        *,
        backend: MembershipBackend,
        remote: ExternalMemberRolesProjection = UNKNOWN_MEMBER_ROLES,
    ) -> MembershipInspection:
        """Re-read fresh columns under full parent/scope locks without flushing."""
        self._require_clean_transaction()
        if not isinstance(workspace_id, UUID) or not isinstance(backend, MembershipBackend):
            raise CasdoorMembershipConflict()
        with self._session.no_autoflush:
            namespaces = self._guard_owner(version, _initial_read=True)
            view = self._inspect_scope(version, workspace_id, backend, remote, namespaces)
            if version.generation == 0 and (
                backend is not MembershipBackend.LOCAL
                or view.managed is None
                or view.managed.source is not CasdoorMembershipSource.ADOPT
                or view.managed.desired_generation != 0
            ):
                raise CasdoorMembershipConflict()
            return view

    def prepare_new(
        self,
        version: GenerationPlanVersion,
        target: DesiredWorkspaceTarget,
        *,
        backend: MembershipBackend,
        remote: ExternalMemberRolesProjection = UNKNOWN_MEMBER_ROLES,
    ) -> NewMembershipPreparation:
        """Lock and prove absence before the caller invokes the existing helper.

        I18 owns the complete lease/fresh-read flow. No pending join is flushed
        here, and historical released/override/tombstone records block creation.
        No remote grants are implicitly adopted even when their role matches.
        """
        if (
            not isinstance(version, GenerationPlanVersion)
            or not isinstance(version.plan, DesiredWorkspacePlan)
            or not isinstance(version.plan.targets, tuple)
            or not isinstance(target, DesiredWorkspaceTarget)
            or target not in version.plan.targets
            or target.target_role not in ("admin", "editor", "normal")
            or not isinstance(target.reason, CasdoorDecisionReason)
            or target.reason not in (CasdoorDecisionReason.ROLE_MAPPING, CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK)
            or not isinstance(target.builtin_id, str)
            or not target.builtin_id
            or len(target.builtin_id) > 2048
            or (target.reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK and target.target_role != "normal")
        ):
            raise CasdoorMembershipConflict()
        try:
            if len(target.builtin_id.encode("utf-8")) > 2048:
                raise CasdoorMembershipConflict()
        except UnicodeError:
            raise CasdoorMembershipConflict() from None
        view = self.inspect(version, target.workspace_id, backend=backend, remote=remote)
        if view.decision is not OwnershipDecision.NEW_JOIN_REQUIRED:
            raise CasdoorMembershipConflict()
        transaction = self._session.get_transaction()
        assert transaction is not None
        token = NewMembershipPreparation(version, target, backend, remote, transaction)
        self._preparations[id(token)] = token
        return token

    def register_new(
        self, token: NewMembershipPreparation, receipt: "_PersistedTenantMember"
    ) -> ManagedMembershipSnapshot:
        """Register only an actual same-UoW helper-created and flushed join.

        A receipt alone, naked join ID, caller boolean or equal role cannot adopt
        an existing join. Consumption is one-use even after a failed attempt.
        No join fields are written. Finalization remains pending for I13/I16.
        Every failure must escape the caller UoW and roll it back: the shared
        helper can update an existing join before returning a False receipt.
        Catching this rejection and committing that helper write is unsafe.
        """
        from services.account_service import _PersistedTenantMember

        self._require_clean_transaction()
        if not isinstance(token, NewMembershipPreparation) or self._preparations.pop(id(token), None) is not token:
            raise CasdoorMembershipConflict()
        if (
            self._session.get_transaction() is not token.transaction
            or not isinstance(receipt, _PersistedTenantMember)
            or receipt.membership_created is not True
            or not isinstance(receipt.join, TenantAccountJoin)
            or sa.inspect(receipt.join).session is not self._session
            or not sa.inspect(receipt.join).persistent
        ):
            raise CasdoorMembershipConflict()
        view = self.inspect(token.version, token.target.workspace_id, backend=token.backend, remote=token.remote)
        context = token.version.plan.context
        if (
            view.managed is not None
            or view.decision is not OwnershipDecision.PRESERVE_UNMANAGED
            or view.observation.join_id != self._uuid(receipt.join.id)
            or receipt.join.account_id != str(context.account_id)
            or receipt.join.tenant_id != str(token.target.workspace_id)
            or receipt.join.invited_by is not None
            or view.invited_by is not None
            or view.observation.join_role
            != (TenantAccountRole.NORMAL if token.backend is MembershipBackend.REMOTE else token.target.target_role)
        ):
            raise CasdoorMembershipConflict()
        # Recheck every historical namespace, not just today's scope key.
        with self._session.no_autoflush:
            if (
                self._session.scalar(
                    sa.select(CasdoorManagedMembershipExtend.id)
                    .where(*self._history_scope(context.account_id, token.target.workspace_id))
                    .limit(1)
                    .with_for_update()
                )
                is not None
            ):
                raise CasdoorMembershipConflict()
        baseline = role_baseline_json(view.observation)
        row = CasdoorManagedMembershipExtend(
            namespace_id=str(context.namespace_id),
            identity_id=str(context.identity_id),
            account_id=str(context.account_id),
            workspace_id=str(token.target.workspace_id),
            join_id=str(view.observation.join_id),
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=0,
            source=(
                CasdoorMembershipSource.FALLBACK
                if token.target.reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
                else CasdoorMembershipSource.MAPPING
            ),
            desired_generation=token.version.generation,
            revision_id=str(context.revision_id),
            last_applied_roles_json=baseline,
            last_applied_fingerprint=roles_fingerprint(view.observation),
            desired_roles_json=json.dumps(
                {
                    "schema_version": 1,
                    "backend": token.backend.value,
                    "target_role": token.target.target_role,
                    "builtin_id": token.target.builtin_id,
                    "role_ids": [token.target.builtin_id],
                    "fence_epoch": token.version.fence_epoch,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            baseline_json=baseline,
            finalization=CasdoorFinalizationState.PENDING,
            tombstone=False,
        )
        self._session.add(row)
        self._session.flush()
        return self._snapshot(row)

    def _read_local_state(
        self, version, workspace_id, *, _ordinary_guard=None, _mapped_regrant_target=None, _present_target=None
    ):
        """Full bounded row plus exact current parents, all history and intent guards."""
        self._require_clean_transaction()
        if _mapped_regrant_target is not None:
            self._validate_mapped_regrant_target(version, _mapped_regrant_target)
            if _mapped_regrant_target.workspace_id != workspace_id:
                raise CasdoorMembershipConflict()
        if _present_target is not None:
            resolve_local_target(version.plan, _present_target)
            if _present_target.workspace_id != workspace_id or _mapped_regrant_target is not None:
                raise CasdoorMembershipConflict()
        if dify_config.RBAC_ENABLED is not False or dify_config.DEPLOYMENT_EDITION == DeploymentEdition.ENTERPRISE:
            raise CasdoorMembershipConflict()
        namespaces = self._guard_owner(version)
        c = version.plan.context
        account = self._session.execute(
            sa.select(Account.status, Account.initialized_at).where(Account.id == str(c.account_id)).with_for_update()
        ).one()
        default_id = self._session.scalar(
            sa.select(CasdoorConfigRevisionExtend.default_workspace_id).where(
                CasdoorConfigRevisionExtend.id == str(c.revision_id)
            )
        )
        self._uuid(default_id)
        if account.status is not AccountStatus.ACTIVE or account.initialized_at is None:
            raise CasdoorMembershipConflict()
        if (
            self._session.scalar(sa.select(Tenant.status).where(Tenant.id == str(workspace_id)).with_for_update())
            is not TenantStatus.NORMAL
        ):
            raise CasdoorMembershipConflict()
        refs = self._current_local_refs(c.account_id, workspace_id, c.namespace_id, c.identity_id)
        self._validate_refs(refs, namespaces)
        if len(refs) != 1 or (refs[0].namespace_id, refs[0].identity_id) != (str(c.namespace_id), str(c.identity_id)):
            raise CasdoorMembershipConflict()
        row = self._current_row(refs[0])
        mapped_regrant = (
            _mapped_regrant_target is not None
            and row.ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
            and row.tombstone is True
        )
        # Only an explicit mapping can restore a removed override in the default
        # workspace. Controlled withdrawal/regrant keeps its original protection.
        if default_id == str(workspace_id) and not mapped_regrant and _present_target is None:
            raise CasdoorMembershipConflict()
        if mapped_regrant and self._history_refs(c.account_id, workspace_id) != refs:
            raise CasdoorMembershipConflict()
        revision = self._session.execute(
            sa.select(
                CasdoorConfigRevisionExtend.integration_id,
                CasdoorConfigRevisionExtend.namespace_id,
                CasdoorConfigRevisionExtend.expected_issuer,
                CasdoorConfigRevisionExtend.organization,
                CasdoorConfigRevisionExtend.application,
                CasdoorConfigRevisionExtend.client_id,
            ).where(CasdoorConfigRevisionExtend.id == row.revision_id)
        ).one_or_none()
        if (
            revision is None
            or tuple(revision)
            != (str(c.integration_id), str(c.namespace_id), c.issuer, c.organization, c.application, c.client_id)
            or (not mapped_regrant and row.ownership is not CasdoorMembershipOwnership.MANAGED)
            or (not mapped_regrant and row.tombstone is not False)
            or row.source
            not in (CasdoorMembershipSource.MAPPING, CasdoorMembershipSource.FALLBACK, CasdoorMembershipSource.ADOPT)
            or row.finalization not in (CasdoorFinalizationState.PENDING, CasdoorFinalizationState.FINALIZED)
            or type(row.ownership_epoch) is not int
            or not 0 <= row.ownership_epoch <= MAX_GENERATION
            or type(row.desired_generation) is not int
            or not 1 <= row.desired_generation <= version.generation
            or row.join_id is None
        ):
            raise CasdoorMembershipConflict()
        self._uuid(row.join_id)
        initial = parse_role_baseline_json(row.baseline_json)
        if initial.backend is not MembershipBackend.LOCAL or initial.join_role not in (None, *LOCAL_ROLES):
            raise CasdoorMembershipConflict()
        if mapped_regrant:
            applied = parse_role_baseline_json(row.last_applied_roles_json)
            observation = MembershipObservation(
                workspace_id, c.account_id, UUID(row.join_id), applied.join_role, MembershipBackend.LOCAL
            )
            if (
                applied.backend is not MembershipBackend.LOCAL
                or applied.join_role not in LOCAL_ROLES
                or row.last_applied_fingerprint != roles_fingerprint(observation)
            ):
                raise CasdoorMembershipConflict()
        joins = tuple(
            self._session.execute(
                sa.select(
                    TenantAccountJoin.id,
                    TenantAccountJoin.role,
                    TenantAccountJoin.invited_by,
                )
                .where(TenantAccountJoin.tenant_id == str(workspace_id), TenantAccountJoin.account_id == str(c.account_id))
                .order_by(TenantAccountJoin.id)
                .limit(2)
                .with_for_update()
            )
        )
        if len(joins) > 1:
            raise CasdoorMembershipConflict()
        join = joins[0] if joins else None
        if join:
            self._uuid(join.id)
        if _present_target is not None:
            role = resolve_local_target(version.plan, _present_target)
            observation = MembershipObservation(
                workspace_id,
                c.account_id,
                UUID(join.id) if join else None,
                join.role if join else None,
                MembershipBackend.LOCAL,
            )
            if (
                join is None
                or join.id != row.join_id
                or join.role is not role
                or row.desired_generation != version.generation
                or row.revision_id != str(c.revision_id)
                or row.last_applied_roles_json != role_baseline_json(observation)
                or row.last_applied_fingerprint != roles_fingerprint(observation)
            ):
                raise CasdoorMembershipConflict()
        if CasdoorRequiredIntentRepository(self._session).read_locked(
            c.account_id, workspace_id, _ordinary_guard=_ordinary_guard
        ):
            raise CasdoorMembershipConflict()
        return row, join, default_id

    def read_present_local_state(self, version, target, *, _ordinary_guard=None):
        """Validate an exact managed target, including an existing default join.

        This never accepts absence or ownership transfer. Withdrawal and regrant
        preparation continue to use their separate default-workspace guards.
        """
        return self._read_local_state(
            version, target.workspace_id, _ordinary_guard=_ordinary_guard, _present_target=target
        )

    def _require_removed_id_absent(self, row):
        if (
            self._session.scalar(
                sa.select(TenantAccountJoin.id).where(TenantAccountJoin.id == row.join_id).with_for_update()
            )
            is not None
        ):
            raise CasdoorMembershipConflict()

    def _validate_local_absence(self, version, row, join):
        marker = parse_local_withdrawal_json(row.desired_roles_json)
        observation = MembershipObservation(
            UUID(row.workspace_id), UUID(row.account_id), None, None, MembershipBackend.LOCAL
        )
        if (
            join is not None
            or marker["removed_join_id"] != row.join_id
            or marker["withdrawal_epoch"] != row.ownership_epoch
            or marker["withdrawal_generation"] != row.desired_generation
            or marker["fence_epoch"] != version.fence_epoch
            or row.last_applied_roles_json != role_baseline_json(observation)
            or row.last_applied_fingerprint != roles_fingerprint(observation)
        ):
            raise CasdoorMembershipConflict()
        self._require_removed_id_absent(row)

    @staticmethod
    def _validate_mapped_regrant_target(version, target):
        resolve_local_target(version.plan, target)
        if target.reason is not CasdoorDecisionReason.ROLE_MAPPING or not target.matched_role_refs:
            raise CasdoorMembershipConflict()

    def _validate_mapped_regrant_absence(self, version, target, row, join):
        """Removed current-owner override only; a retained manual join never qualifies."""
        self._validate_mapped_regrant_target(version, target)
        if (
            row.ownership is not CasdoorMembershipOwnership.LOCAL_OVERRIDE
            or row.tombstone is not True
            or join is not None
        ):
            raise CasdoorMembershipConflict()
        self._require_removed_id_absent(row)

    @staticmethod
    def _local_desired(role, fence, reason=None):
        value = dict(
            schema_version=1,
            backend="local",
            target_role=role.value,
            builtin_id=role.value,
            role_ids=[role.value],
            fence_epoch=fence,
        )
        if reason is not None:
            value["reason"] = reason.value
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def prepare_local_withdrawal(self, version: GenerationPlanVersion, workspace_id: UUID, *, _ordinary_guard=None):
        """Capture present actual LOCAL state before the original removal effect."""
        if not isinstance(workspace_id, UUID):
            raise CasdoorMembershipConflict()
        row, join, default_id = self._read_local_state(version, workspace_id, _ordinary_guard=_ordinary_guard)
        if join is None or join.role not in LOCAL_ROLES or join.id != row.join_id:
            raise CasdoorMembershipConflict()
        observation = MembershipObservation(
            workspace_id, version.plan.context.account_id, UUID(join.id), join.role, MembershipBackend.LOCAL
        )
        desired = {
            self._local_desired(join.role, version.fence_epoch, reason)
            for reason in (None, CasdoorDecisionReason.ROLE_MAPPING, CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK)
            if reason is not CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK or join.role is TenantAccountRole.NORMAL
        }
        if (
            row.last_applied_roles_json != role_baseline_json(observation)
            or row.last_applied_fingerprint != roles_fingerprint(observation)
            or row.desired_roles_json not in desired
            or row.ownership_epoch == MAX_GENERATION
        ):
            raise CasdoorMembershipConflict()
        return self._prepare_local(
            version, workspace_id, None, (row, default_id), True, _ordinary_guard=_ordinary_guard
        )

    def prepare_local_regrant(
        self, version: GenerationPlanVersion, target: DesiredWorkspaceTarget, *, _ordinary_guard=None
    ):
        resolve_local_target(version.plan, target)
        mapped_target = (
            target if target.reason is CasdoorDecisionReason.ROLE_MAPPING and target.matched_role_refs else None
        )
        row, join, default_id = self._read_local_state(
            version, target.workspace_id, _ordinary_guard=_ordinary_guard, _mapped_regrant_target=mapped_target
        )
        if row.ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE:
            self._validate_mapped_regrant_absence(version, target, row, join)
        else:
            self._validate_local_absence(version, row, join)
        if row.ownership_epoch == MAX_GENERATION:
            raise CasdoorMembershipConflict()
        return self._prepare_local(
            version, target.workspace_id, target, (row, default_id), False, _ordinary_guard=_ordinary_guard
        )

    def _prepare_local(self, version, workspace_id, target, state, withdrawal, *, _ordinary_guard=None):
        transaction = self._session.get_transaction()
        assert transaction is not None
        token = _LocalMembershipPreparation(version, workspace_id, target, transaction, state, withdrawal)
        self._local_preparations[id(token)] = token
        if _ordinary_guard is not None:
            self._ordinary_guards[id(token)] = _ordinary_guard
        return token

    def _consume_local(self, token, *, withdrawal):
        self._require_clean_transaction()
        if (
            not isinstance(token, _LocalMembershipPreparation)
            or self._local_preparations.pop(id(token), None) is not token
            or token.withdrawal is not withdrawal
            or self._session.get_transaction() is not token.transaction
        ):
            raise CasdoorMembershipConflict()
        ordinary_guard = self._ordinary_guards.pop(id(token), None)
        mapped_target = (
            token.target
            if not withdrawal and token.state[0].ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
            else None
        )
        row, join, default_id = self._read_local_state(
            token.version, token.workspace_id, _ordinary_guard=ordinary_guard, _mapped_regrant_target=mapped_target
        )
        if (row, default_id) != token.state:
            raise CasdoorMembershipConflict()
        self._require_removed_id_absent(row)
        return row, join

    def _cas_local(self, row, **values):
        history = CasdoorManagedMembershipExtend
        result = self._session.execute(
            sa.update(history)
            .where(
                *(
                    getattr(history, key).is_(None) if value is None else getattr(history, key) == value
                    for key, value in row._asdict().items()
                )
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CasdoorMembershipConflict()
        self._session.flush()
        refs = self._current_local_refs(
            UUID(row.account_id), UUID(row.workspace_id), UUID(row.namespace_id), UUID(row.identity_id)
        )
        if len(refs) != 1 or refs[0].id != row.id:
            raise CasdoorMembershipConflict()
        after = self._current_row(refs[0])
        # updated_at is maintained by the original model; every other field must
        # match this exact CAS, including immutable source and initial baseline.
        if any(
            getattr(after, key) != values.get(key, value) for key, value in row._asdict().items() if key != "updated_at"
        ):
            raise CasdoorMembershipConflict()
        expected = row._replace(**values, updated_at=after.updated_at)
        return _LocalMembershipEffect(row, expected, self._snapshot(expected))

    def register_local_withdrawal(self, token):
        """Consume after actual flushed removal, then CAS the original full history."""
        row, join = self._consume_local(token, withdrawal=True)
        if join is not None:
            raise CasdoorMembershipConflict()
        observation = MembershipObservation(
            token.workspace_id, token.version.plan.context.account_id, None, None, MembershipBackend.LOCAL
        )
        marker = json.dumps(
            dict(
                schema_version=2,
                backend="local",
                operation="controlled_withdrawal",
                removed_join_id=row.join_id,
                withdrawal_generation=token.version.generation,
                withdrawal_epoch=row.ownership_epoch + 1,
                fence_epoch=token.version.fence_epoch,
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return self._cas_local(
            row,
            ownership_epoch=row.ownership_epoch + 1,
            revision_id=str(token.version.plan.context.revision_id),
            desired_generation=token.version.generation,
            last_applied_roles_json=role_baseline_json(observation),
            last_applied_fingerprint=roles_fingerprint(observation),
            desired_roles_json=marker,
            finalization=CasdoorFinalizationState.PENDING,
        )

    def register_local_regrant(self, token, receipt: "_PersistedTenantMember"):
        """Consume the original helper's actual same-Session created receipt."""
        from services.account_service import _PersistedTenantMember

        row, join = self._consume_local(token, withdrawal=False)
        target = token.target
        assert target is not None
        role = resolve_local_target(token.version.plan, target)
        if (
            not isinstance(receipt, _PersistedTenantMember)
            or receipt.membership_created is not True
            or not isinstance(receipt.join, TenantAccountJoin)
            or sa.inspect(receipt.join).session is not self._session
            or not sa.inspect(receipt.join).persistent
            or join is None
            or join.id == row.join_id
            or join.id != receipt.join.id
            or join.role is not role
            or join.invited_by is not None
            or receipt.join.invited_by is not None
            or receipt.join.account_id != row.account_id
            or receipt.join.tenant_id != row.workspace_id
            or receipt.join.role is not role
        ):
            raise CasdoorMembershipConflict()
        observation = MembershipObservation(
            target.workspace_id, token.version.plan.context.account_id, UUID(join.id), role, MembershipBackend.LOCAL
        )
        return self._cas_local(
            row,
            join_id=join.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            tombstone=False,
            ownership_epoch=row.ownership_epoch + 1,
            revision_id=str(token.version.plan.context.revision_id),
            desired_generation=token.version.generation,
            last_applied_roles_json=role_baseline_json(observation),
            last_applied_fingerprint=roles_fingerprint(observation),
            desired_roles_json=self._local_desired(role, token.version.fence_epoch, target.reason),
            finalization=CasdoorFinalizationState.PENDING,
        )

    def _require_clean_transaction(self) -> None:
        if (
            not self._session.in_transaction()
            or not self._session.is_active
            or self._session.get_nested_transaction() is not None
        ):
            raise RuntimeError("Casdoor membership repository requires an active caller-owned root transaction")
        if self._session.new or self._session.dirty or self._session.deleted:
            raise CasdoorMembershipConflict()

    def _guard_owner(self, version: GenerationPlanVersion, *, _initial_read=False) -> frozenset[str]:
        if (
            type(_initial_read) is not bool
            or not isinstance(version, GenerationPlanVersion)
            or not isinstance(version.plan, DesiredWorkspacePlan)
            or type(version.generation) is not int
            or not (0 if _initial_read else 1) <= version.generation <= MAX_GENERATION
            or type(version.fence_epoch) is not int
            or not 0 <= version.fence_epoch <= MAX_GENERATION
        ):
            raise CasdoorMembershipConflict()
        try:
            validate_mapping_context(version.plan.context)
        except (MappingError, AttributeError):
            raise CasdoorMembershipConflict() from None
        c = version.plan.context
        integration = self._session.execute(
            sa.select(CasdoorIntegrationExtend.enabled, CasdoorIntegrationExtend.active_revision_id)
            .where(CasdoorIntegrationExtend.id == str(c.integration_id), CasdoorIntegrationExtend.slot == 1)
            .with_for_update()
        ).one_or_none()
        if (
            integration is None
            or integration.enabled is not True
            or integration.active_revision_id != str(c.revision_id)
        ):
            raise CasdoorMembershipConflict()
        namespaces = tuple(
            self._session.execute(
                sa.select(
                    CasdoorNamespaceExtend.id,
                    CasdoorNamespaceExtend.integration_id,
                    CasdoorNamespaceExtend.expected_issuer,
                    CasdoorNamespaceExtend.organization,
                    CasdoorNamespaceExtend.application,
                    CasdoorNamespaceExtend.client_id,
                    CasdoorNamespaceExtend.lifecycle,
                    CasdoorNamespaceExtend.fence_epoch,
                )
                .where(CasdoorNamespaceExtend.integration_id == str(c.integration_id))
                .order_by(CasdoorNamespaceExtend.id)
                .limit(MAX_NAMESPACES + 1)
                .with_for_update()
            )
        )
        if len(namespaces) > MAX_NAMESPACES:
            raise CasdoorMembershipConflict()
        for row in namespaces:
            self._uuid(row.id)
            if type(row.fence_epoch) is not int or not 0 <= row.fence_epoch <= MAX_GENERATION:
                raise CasdoorMembershipConflict()
        namespace = next((row for row in namespaces if row.id == str(c.namespace_id)), None)
        if namespace is None or tuple(namespace)[1:] != (
            str(c.integration_id),
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            CasdoorNamespaceLifecycle.ACTIVE,
            version.fence_epoch,
        ):
            raise CasdoorMembershipConflict()
        revision = self._session.execute(
            sa.select(
                CasdoorConfigRevisionExtend.integration_id,
                CasdoorConfigRevisionExtend.namespace_id,
                CasdoorConfigRevisionExtend.expected_issuer,
                CasdoorConfigRevisionExtend.organization,
                CasdoorConfigRevisionExtend.application,
                CasdoorConfigRevisionExtend.client_id,
                CasdoorConfigRevisionExtend.config_digest,
            ).where(CasdoorConfigRevisionExtend.id == str(c.revision_id))
        ).one_or_none()
        if revision is None or tuple(revision) != (
            str(c.integration_id),
            str(c.namespace_id),
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            c.config_digest,
        ):
            raise CasdoorMembershipConflict()
        account = self._session.execute(
            sa.select(Account.id, Account.status).where(Account.id == str(c.account_id)).with_for_update()
        ).one_or_none()
        if account is None or account.status in (AccountStatus.BANNED, AccountStatus.CLOSED):
            raise CasdoorMembershipConflict()
        digest = hashlib.sha256(c.subject.encode("utf-8")).hexdigest()
        identity = self._session.execute(
            sa.select(
                CasdoorIdentityExtend.namespace_id,
                CasdoorIdentityExtend.account_id,
                CasdoorIdentityExtend.issuer,
                CasdoorIdentityExtend.organization,
                CasdoorIdentityExtend.subject,
                CasdoorIdentityExtend.subject_digest,
                CasdoorIdentityExtend.sync_generation,
            )
            .where(CasdoorIdentityExtend.id == str(c.identity_id))
            .with_for_update()
        ).one_or_none()
        if identity is None or tuple(identity) != (
            str(c.namespace_id),
            str(c.account_id),
            c.issuer,
            c.organization,
            c.subject,
            digest,
            version.generation,
        ):
            raise CasdoorMembershipConflict()

        return frozenset(row.id for row in namespaces)

    @staticmethod
    def _uuid(raw: str) -> UUID:
        """Persisted associations must already be canonical, never normalized."""
        try:
            value = UUID(raw)
        except (ValueError, TypeError, AttributeError):
            raise CasdoorMembershipConflict() from None
        if str(value) != raw:
            raise CasdoorMembershipConflict()
        return value

    @staticmethod
    def _history_scope(account_id: UUID, workspace_id: UUID):
        return (
            CasdoorManagedMembershipExtend.account_id == str(account_id),
            CasdoorManagedMembershipExtend.workspace_id == str(workspace_id),
        )

    def _history_refs(self, account_id: UUID, workspace_id: UUID, namespace_id: UUID | None = None):
        """Two scalar rows distinguish zero, exactly one and at least two.

        This is not a truncated snapshot: exact current discovery is separate,
        and unrelated history TEXT is never selected.
        """
        history = CasdoorManagedMembershipExtend
        statement = sa.select(
            history.id,
            history.namespace_id,
            history.identity_id,
            history.account_id,
            history.workspace_id,
            history.join_id,
            history.revision_id,
        ).where(*self._history_scope(account_id, workspace_id))
        if namespace_id is not None:
            statement = statement.where(history.namespace_id == str(namespace_id))
        return tuple(
            self._session.execute(statement.order_by(history.namespace_id, history.id).limit(2).with_for_update())
        )

    def _validate_refs(self, refs, namespaces: frozenset[str]) -> None:
        for row in refs:
            for field in row._mapping:
                value = getattr(row, field)
                if field != "join_id" or value is not None:
                    self._uuid(value)
            if row.namespace_id not in namespaces:
                raise CasdoorMembershipConflict()

    def _byte_length(self, column):
        if self._session.get_bind().dialect.name == "sqlite":
            return sa.func.length(sa.cast(column, sa.LargeBinary))
        return sa.func.octet_length(column)

    def _current_snapshot(self, ref) -> ManagedMembershipSnapshot:
        return self._snapshot(self._current_row(ref))

    def _current_row(self, ref):
        history = CasdoorManagedMembershipExtend
        association = tuple(
            getattr(history, field).is_(None) if value is None else getattr(history, field) == value
            for field, value in ref._mapping.items()
        )
        lengths = tuple(
            self._byte_length(column)
            for column in (
                history.last_applied_roles_json,
                history.desired_roles_json,
                history.baseline_json,
            )
        )
        bounded = tuple(length.between(0, MAX_SNAPSHOT_BYTES) for length in lengths)
        # The guards are repeated on materialization so drift between queries
        # cannot fetch oversized TEXT. Parent locks cover participating writers.
        size = self._session.execute(sa.select(*lengths).where(*association).with_for_update()).one_or_none()
        if size is None or any(type(value) is not int or not 0 <= value <= MAX_SNAPSHOT_BYTES for value in size):
            raise CasdoorMembershipConflict()
        row = self._session.scalar(
            sa.select(history)
            .where(*association, *bounded)
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or any(getattr(row, field) != value for field, value in ref._mapping.items()):
            raise CasdoorMembershipConflict()
        return _LocalHistoryState(*(getattr(row, field) for field in _LocalHistoryState._fields))

    def _inspect_scope(
        self,
        version: GenerationPlanVersion,
        workspace_id: UUID,
        backend: MembershipBackend,
        remote: ExternalMemberRolesProjection,
        namespaces: frozenset[str],
    ) -> MembershipInspection:
        c = version.plan.context
        workspace = self._session.execute(
            sa.select(Tenant.status).where(Tenant.id == str(workspace_id)).with_for_update()
        ).one_or_none()
        if workspace is None or workspace.status is not TenantStatus.NORMAL:
            raise CasdoorMembershipConflict()
        join = self._session.execute(
            sa.select(TenantAccountJoin.id, TenantAccountJoin.role, TenantAccountJoin.invited_by)
            .where(TenantAccountJoin.tenant_id == str(workspace_id), TenantAccountJoin.account_id == str(c.account_id))
            .with_for_update()
        ).one_or_none()
        if join is not None and not isinstance(join.role, TenantAccountRole):
            raise CasdoorMembershipConflict()
        observation = MembershipObservation(
            workspace_id,
            c.account_id,
            self._uuid(join.id) if join is not None else None,
            join.role if join is not None else None,
            backend,
            remote,
        )
        invited_by = self._uuid(join.invited_by) if join is not None and join.invited_by is not None else None
        history = self._history_refs(c.account_id, workspace_id)
        self._validate_refs(history, namespaces)
        scoped = self._history_refs(c.account_id, workspace_id, c.namespace_id) if history else ()
        self._validate_refs(scoped, namespaces)
        if len(scoped) > 1:
            raise CasdoorMembershipConflict()
        managed = self._current_snapshot(scoped[0]) if scoped else None
        if managed is not None and managed.identity_id != c.identity_id:
            raise CasdoorMembershipConflict()
        if managed is not None:
            revision_owner = self._session.execute(
                sa.select(CasdoorConfigRevisionExtend.integration_id, CasdoorConfigRevisionExtend.namespace_id).where(
                    CasdoorConfigRevisionExtend.id == str(managed.revision_id)
                )
            ).one_or_none()
            if (
                revision_owner is None
                or tuple(revision_owner) != (str(c.integration_id), str(c.namespace_id))
                or type(managed.ownership_epoch) is not int
                or not 0 <= managed.ownership_epoch <= MAX_GENERATION
                or type(managed.desired_generation) is not int
                or not 0 <= managed.desired_generation <= version.generation
            ):
                raise CasdoorMembershipConflict()
        if backend is MembershipBackend.LOCAL and any(r.namespace_id != str(c.namespace_id) for r in history):
            selected = self._current_local_refs(c.account_id, workspace_id, c.namespace_id, c.identity_id)
            if scoped != selected:
                raise CasdoorMembershipConflict()
            if selected:
                history = selected
        decision = decide_ownership(observation, managed)
        if history and managed is None and decision is OwnershipDecision.NEW_JOIN_REQUIRED:
            decision = OwnershipDecision.PRESERVE_UNMANAGED
        if managed is not None and len(history) != 1 and decision is OwnershipDecision.MANAGED_CURRENT:
            decision = OwnershipDecision.AUTHORIZATION_PENDING
        if (
            backend is MembershipBackend.LOCAL
            and decision is OwnershipDecision.MARK_OVERRIDE_REQUIRED
            and observation.join_id is None
            and managed is not None
        ):
            try:
                row, actual_join, _default = self._read_local_state(version, workspace_id)
                self._validate_local_absence(version, row, actual_join)
            except (ValueError, TypeError, AttributeError):
                pass  # Keep the original missing/drift decision; never heal malformed history.
            else:
                decision = OwnershipDecision.CONTROLLED_WITHDRAWN
        if (
            backend is MembershipBackend.LOCAL
            and decision is OwnershipDecision.PRESERVE_OVERRIDE
            and observation.join_id is None
            and managed is not None
            and managed.ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
            and managed.tombstone is True
        ):
            target = next((item for item in version.plan.targets if item.workspace_id == workspace_id), None)
            if target is not None:
                try:
                    row, actual_join, _default = self._read_local_state(
                        version, workspace_id, _mapped_regrant_target=target
                    )
                    self._validate_mapped_regrant_absence(version, target, row, actual_join)
                    if self._snapshot(row) != managed or row.ownership_epoch == MAX_GENERATION:
                        raise CasdoorMembershipConflict()
                except (ValueError, TypeError, AttributeError):
                    pass  # Preserve overrides unless the complete current mapping/absence proof succeeds.
                else:
                    decision = OwnershipDecision.MAPPED_REGRANT_REQUIRED
        return MembershipInspection(observation, managed, decision, invited_by)

    @staticmethod
    def _snapshot(row: CasdoorManagedMembershipExtend) -> ManagedMembershipSnapshot:
        return ManagedMembershipSnapshot(
            CasdoorMembershipRepository._uuid(row.id),
            CasdoorMembershipRepository._uuid(row.namespace_id),
            CasdoorMembershipRepository._uuid(row.identity_id),
            CasdoorMembershipRepository._uuid(row.account_id),
            CasdoorMembershipRepository._uuid(row.workspace_id),
            CasdoorMembershipRepository._uuid(row.join_id) if row.join_id is not None else None,
            row.ownership,
            row.ownership_epoch,
            row.source,
            row.desired_generation,
            CasdoorMembershipRepository._uuid(row.revision_id),
            row.last_applied_roles_json,
            row.last_applied_fingerprint,
            row.desired_roles_json,
            row.baseline_json,
            row.tombstone,
        )

    def _current_local_refs(self, account_id, workspace_id, namespace_id, identity_id):
        """Exact current owner after a full immutable archived SQL proof."""
        refs = self._history_refs(account_id, workspace_id)
        if any(r.namespace_id != str(namespace_id) for r in refs):
            from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

            owner = CasdoorLocalLifecycleRepository(self._session)
            archive = owner._archived_release_facts(
                account_id=account_id, namespace_id=namespace_id, identity_id=identity_id
            )
            if not archive[0]:
                raise CasdoorMembershipConflict()
            scoped = self._history_refs(account_id, workspace_id, namespace_id)
            if scoped:
                membership_id, _facts = owner._adopted_current_refs(
                    account_id=account_id, namespace_id=namespace_id, identity_id=identity_id, workspace_id=workspace_id
                )
                if len(scoped) != 1 or scoped[0].id != membership_id:
                    raise CasdoorMembershipConflict()
            return scoped
        return refs
