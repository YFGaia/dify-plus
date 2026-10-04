"""Caller-owned RBAC-off role apply foundation for an existing managed join.

prepare -> apply -> caller commit, under the caller's complete leases and fresh
trusted I18 plan. Rejections after any mutation must escape and roll back the
WHOLE UoW. No member creation/removal, hidden commit, I/O, intent termination,
sharedwriter hooks or finalization. Locks protect participating writers only.
"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from configs import dify_config
from core.casdoor.local_roles import LOCAL_ROLES, LocalRoleApplyReceipt, LocalRoleOutcome, resolve_local_target
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingError, validate_mapping_context
from core.casdoor.ownership import (
    MAX_SNAPSHOT_BYTES,
    ManagedMembershipSnapshot,
    MembershipBackend,
    MembershipObservation,
    OwnershipDecision,
    decide_ownership,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorMembershipOwnership,
    CasdoorNamespaceLifecycle,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from sqlalchemy.orm import Session, SessionTransaction

from repositories.casdoor_generation_repository_extend import MAX_GENERATION, GenerationPlanVersion
from repositories.casdoor_required_intent_repository_extend import CasdoorRequiredIntentRepository
from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository

MAX_NAMESPACES = 2000


class CasdoorLocalRoleConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("authorization_pending")


@dataclass(frozen=True, repr=False)
class LocalRolePreparation:
    """Issuer registry identity plus exact root UoW; fields are not authority."""

    version: GenerationPlanVersion
    target: DesiredWorkspaceTarget
    transaction: SessionTransaction
    _state: tuple


class CasdoorLocalRoleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._preparations: dict[int, LocalRolePreparation] = {}
        self._invitation_guards: dict[int, object] = {}

    def validate_parent_scope(self, version: GenerationPlanVersion, target: DesiredWorkspaceTarget) -> None:
        """Read parent/default consistency without current-history preparation.

        The caller already holds all namespace/default/target parent locks before
        earlier account writes. This observation cannot prove those prelocks,
        leases, admission or finalization. Rejection requires whole UoW rollback.
        """
        self._require_clean_root()
        self._validate(version, target)
        with self._session.no_autoflush:
            parents = self._guard_owner(version, target)
            default_workspace_id = parents[2][-1]
            status = self._session.scalar(
                sa.select(Tenant.status).where(Tenant.id == default_workspace_id).with_for_update()
            )
            if status is not TenantStatus.NORMAL:
                raise CasdoorLocalRoleConflict()

    def prepare(
        self, version: GenerationPlanVersion, target: DesiredWorkspaceTarget, *, invitation_guard: object | None = None
    ) -> LocalRolePreparation:
        self._require_clean_root()
        self._validate(version, target)
        with self._session.no_autoflush:
            state = (
                self._read_locked(version, target)
                if invitation_guard is None
                else self._read_locked(version, target, invitation_guard=invitation_guard)
            )
        transaction = self._session.get_transaction()
        assert transaction is not None
        token = LocalRolePreparation(version, target, transaction, state)
        self._preparations[id(token)] = token
        if invitation_guard is not None:
            self._invitation_guards[id(token)] = invitation_guard
        return token

    def apply(self, token: LocalRolePreparation) -> LocalRoleApplyReceipt:
        """Recheck locks and CAS join + metadata; never catch and commit failure."""
        self._require_clean_root()
        if not isinstance(token, LocalRolePreparation) or self._preparations.pop(id(token), None) is not token:
            raise CasdoorLocalRoleConflict()
        invitation_guard = self._invitation_guards.pop(id(token), None)
        if self._session.get_transaction() is not token.transaction:
            raise CasdoorLocalRoleConflict()
        role = self._validate(token.version, token.target)
        with self._session.no_autoflush:
            state = (
                self._read_locked(token.version, token.target)
                if invitation_guard is None
                else self._read_locked(token.version, token.target, invitation_guard=invitation_guard)
            )
            if state != token._state:
                raise CasdoorLocalRoleConflict()
            _, join, history, intents = state
            observation = MembershipObservation(
                token.target.workspace_id,
                token.version.plan.context.account_id,
                UUID(join.id) if join else None,
                join.role if join else None,
                MembershipBackend.LOCAL,
            )
            managed = self._snapshot(history) if history else None
            decision = decide_ownership(observation, managed)
            if intents or decision in (
                OwnershipDecision.AUTHORIZATION_PENDING,
                OwnershipDecision.MARK_OVERRIDE_REQUIRED,
                OwnershipDecision.REQUEST_OWNER_FENCE,
            ):
                return self._receipt(token, join, history, LocalRoleOutcome.PENDING)
            if decision is not OwnershipDecision.MANAGED_CURRENT or join.role not in LOCAL_ROLES:
                return self._receipt(token, join, history, LocalRoleOutcome.PRESERVED)
            assert history is not None and join is not None
            desired = json.dumps(
                {
                    "schema_version": 1,
                    "backend": "local",
                    "target_role": role.value,
                    "builtin_id": role.value,
                    "role_ids": [role.value],
                    "reason": token.target.reason.value,
                    "fence_epoch": token.version.fence_epoch,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            c = token.version.plan.context
            changed = join.role is not role
            if changed:
                result = self._session.execute(
                    sa.update(TenantAccountJoin)
                    .where(
                        TenantAccountJoin.id == join.id,
                        TenantAccountJoin.account_id == str(c.account_id),
                        TenantAccountJoin.tenant_id == str(token.target.workspace_id),
                        TenantAccountJoin.role == join.role,
                    )
                    .values(role=role)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise CasdoorLocalRoleConflict()
            actual = self._session.execute(
                sa.select(TenantAccountJoin.id, TenantAccountJoin.role)
                .where(
                    TenantAccountJoin.id == join.id,
                    TenantAccountJoin.account_id == str(c.account_id),
                    TenantAccountJoin.tenant_id == str(token.target.workspace_id),
                )
                .with_for_update()
            ).one_or_none()
            if actual is None or actual.role is not role:
                raise CasdoorLocalRoleConflict()
            after = MembershipObservation(
                observation.workspace_id, observation.account_id, UUID(actual.id), actual.role, MembershipBackend.LOCAL
            )
            values = dict(
                last_applied_roles_json=role_baseline_json(after),
                last_applied_fingerprint=roles_fingerprint(after),
                desired_roles_json=desired,
                revision_id=str(c.revision_id),
                desired_generation=token.version.generation,
            )
            metadata_changed = any(getattr(history, key) != value for key, value in values.items())
            if metadata_changed:
                result = self._session.execute(
                    sa.update(History)
                    .where(
                        History.id == history.id,
                        History.namespace_id == str(c.namespace_id),
                        History.identity_id == str(c.identity_id),
                        History.account_id == str(c.account_id),
                        History.workspace_id == str(token.target.workspace_id),
                        History.join_id == join.id,
                        History.ownership == CasdoorMembershipOwnership.MANAGED,
                        History.ownership_epoch == history.ownership_epoch,
                        History.tombstone.is_(False),
                        History.revision_id == history.revision_id,
                        History.desired_generation == history.desired_generation,
                        History.last_applied_fingerprint == history.last_applied_fingerprint,
                        History.last_applied_roles_json == history.last_applied_roles_json,
                        History.desired_roles_json == history.desired_roles_json,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise CasdoorLocalRoleConflict()
            if changed:
                # Keep Join/History before lifecycle; caller rollback owns all writes.
                InvitationAuthorityRepository().record_local_role_change(
                    self._session, account_id=str(c.account_id), workspace_id=str(token.target.workspace_id)
                )
            return self._receipt(
                token,
                join,
                history,
                LocalRoleOutcome.APPLIED if changed or metadata_changed else LocalRoleOutcome.NOOP,
                applied=True,
                role=role,
                changed=changed,
                metadata_changed=metadata_changed,
            )

    def _require_clean_root(self) -> None:
        transaction = self._session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or not self._session.is_active
            or self._session.get_nested_transaction() is not None
        ):
            raise RuntimeError("Casdoor local role apply requires a clean active caller-owned root transaction")
        if self._session.new or self._session.dirty or self._session.deleted or dify_config.RBAC_ENABLED:
            raise CasdoorLocalRoleConflict()

    @staticmethod
    def _validate(version, target) -> TenantAccountRole:
        try:
            if not isinstance(version, GenerationPlanVersion):
                raise ValueError()
            return resolve_local_target(version.plan, target)
        except (ValueError, AttributeError, TypeError, UnicodeError):
            raise CasdoorLocalRoleConflict() from None

    @staticmethod
    def _epoch(value) -> bool:
        return type(value) is int and 0 <= value <= MAX_GENERATION

    def _guard_owner(self, version: GenerationPlanVersion, target: DesiredWorkspaceTarget) -> tuple:
        if (
            not isinstance(version, GenerationPlanVersion)
            or not isinstance(version.plan, DesiredWorkspacePlan)
            or type(version.generation) is not int
            or not 1 <= version.generation <= MAX_GENERATION
            or type(version.fence_epoch) is not int
            or not 0 <= version.fence_epoch <= MAX_GENERATION
        ):
            raise CasdoorLocalRoleConflict()
        try:
            validate_mapping_context(version.plan.context)
        except (MappingError, AttributeError):
            raise CasdoorLocalRoleConflict() from None
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
            raise CasdoorLocalRoleConflict()
        namespaces = tuple(
            self._session.execute(
                sa.select(
                    Namespace.id,
                    Namespace.integration_id,
                    Namespace.expected_issuer,
                    Namespace.organization,
                    Namespace.application,
                    Namespace.client_id,
                    Namespace.lifecycle,
                    Namespace.fence_epoch,
                )
                .order_by(Namespace.id)
                .limit(MAX_NAMESPACES + 1)
                .with_for_update()
            )
        )
        if len(namespaces) > MAX_NAMESPACES or any(
            row.integration_id != str(c.integration_id) or not self._epoch(row.fence_epoch) for row in namespaces
        ):
            raise CasdoorLocalRoleConflict()
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
            raise CasdoorLocalRoleConflict()
        revision = self._session.execute(
            sa.select(
                CasdoorConfigRevisionExtend.integration_id,
                CasdoorConfigRevisionExtend.namespace_id,
                CasdoorConfigRevisionExtend.expected_issuer,
                CasdoorConfigRevisionExtend.organization,
                CasdoorConfigRevisionExtend.application,
                CasdoorConfigRevisionExtend.client_id,
                CasdoorConfigRevisionExtend.config_digest,
                CasdoorConfigRevisionExtend.default_workspace_id,
            ).where(CasdoorConfigRevisionExtend.id == str(c.revision_id))
        ).one_or_none()
        if revision is None or tuple(revision)[:-1] != (
            str(c.integration_id),
            str(c.namespace_id),
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            c.config_digest,
        ):
            raise CasdoorLocalRoleConflict()
        if not self._uuid(revision.default_workspace_id) or (
            target.reason.value == "default_normal_fallback"
            and revision.default_workspace_id != str(target.workspace_id)
        ):
            raise CasdoorLocalRoleConflict()
        account = self._session.execute(
            sa.select(Account.id, Account.status).where(Account.id == str(c.account_id)).with_for_update()
        ).one_or_none()
        if account is None or account.status in (AccountStatus.BANNED, AccountStatus.CLOSED):
            raise CasdoorLocalRoleConflict()
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
            raise CasdoorLocalRoleConflict()

        return (tuple(integration), namespaces, tuple(revision), tuple(account), tuple(identity))

    def _byte_length(self, column):
        if self._session.get_bind().dialect.name == "sqlite":
            return sa.func.length(sa.cast(column, sa.LargeBinary))
        return sa.func.octet_length(column)

    def _read_locked(self, version, target, *, invitation_guard=None) -> tuple:
        parents = self._guard_owner(version, target)
        c = version.plan.context
        workspace = self._session.execute(
            sa.select(Tenant.status).where(Tenant.id == str(target.workspace_id)).with_for_update()
        ).one_or_none()
        if workspace is None or workspace.status is not TenantStatus.NORMAL:
            raise CasdoorLocalRoleConflict()
        join = self._session.execute(
            sa.select(TenantAccountJoin.id, TenantAccountJoin.role)
            .where(
                TenantAccountJoin.tenant_id == str(target.workspace_id),
                TenantAccountJoin.account_id == str(c.account_id),
            )
            .with_for_update()
        ).one_or_none()
        if join is not None and (not isinstance(join.role, TenantAccountRole) or not self._uuid(join.id)):
            raise CasdoorLocalRoleConflict()
        # Detect a second history without loading any TEXT or adopting old history.
        refs = tuple(
            self._session.execute(
                sa.select(History.id, History.namespace_id, History.identity_id)
                .where(History.account_id == str(c.account_id), History.workspace_id == str(target.workspace_id))
                .order_by(History.namespace_id, History.id)
                .limit(2)
                .with_for_update()
            )
        )
        if len(refs) > 1 or any(not all(self._uuid(v) for v in row) for row in refs):
            raise CasdoorLocalRoleConflict()
        if refs and (refs[0].namespace_id, refs[0].identity_id) != (str(c.namespace_id), str(c.identity_id)):
            raise CasdoorLocalRoleConflict()
        history = None
        if refs:
            text_fields = (History.last_applied_roles_json, History.desired_roles_json, History.baseline_json)
            lengths = self._session.execute(
                sa.select(*(self._byte_length(col) for col in text_fields)).where(History.id == refs[0].id)
            ).one()
            if any(type(length) is not int or not 0 <= length <= MAX_SNAPSHOT_BYTES for length in lengths):
                raise CasdoorLocalRoleConflict()
            history = self._session.execute(
                sa.select(*History.__table__.columns).where(History.id == refs[0].id).with_for_update()
            ).one()
            if (
                not self._epoch(history.ownership_epoch)
                or not self._epoch(history.desired_generation)
                or history.desired_generation > version.generation
                or not self._uuid(history.revision_id)
                or (history.join_id is not None and not self._uuid(history.join_id))
            ):
                raise CasdoorLocalRoleConflict()
            revision = self._session.execute(
                sa.select(
                    CasdoorConfigRevisionExtend.integration_id,
                    CasdoorConfigRevisionExtend.namespace_id,
                    CasdoorConfigRevisionExtend.expected_issuer,
                    CasdoorConfigRevisionExtend.organization,
                    CasdoorConfigRevisionExtend.application,
                    CasdoorConfigRevisionExtend.client_id,
                ).where(CasdoorConfigRevisionExtend.id == history.revision_id)
            ).one_or_none()
            if revision is None or tuple(revision) != (
                str(c.integration_id),
                str(c.namespace_id),
                c.issuer,
                c.organization,
                c.application,
                c.client_id,
            ):
                raise CasdoorLocalRoleConflict()
        owner = CasdoorRequiredIntentRepository(self._session)
        intents = (
            owner.read_locked(c.account_id, target.workspace_id)
            if invitation_guard is None
            else owner.read_locked(c.account_id, target.workspace_id, invitation_guard=invitation_guard)
        )
        return (parents + (tuple(workspace),), join, history, intents)

    @staticmethod
    def _uuid(value) -> bool:
        try:
            return isinstance(value, str) and str(UUID(value)) == value
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _snapshot(row) -> ManagedMembershipSnapshot:
        return ManagedMembershipSnapshot(
            UUID(row.id),
            UUID(row.namespace_id),
            UUID(row.identity_id),
            UUID(row.account_id),
            UUID(row.workspace_id),
            UUID(row.join_id) if row.join_id else None,
            row.ownership,
            row.ownership_epoch,
            row.source,
            row.desired_generation,
            UUID(row.revision_id),
            row.last_applied_roles_json,
            row.last_applied_fingerprint,
            row.desired_roles_json,
            row.baseline_json,
            row.tombstone,
        )

    @staticmethod
    def _receipt(token, join, history, outcome, *, applied=False, role=None, changed=False, metadata_changed=False):
        c = token.version.plan.context
        return LocalRoleApplyReceipt(
            outcome,
            applied,
            c.namespace_id,
            c.identity_id,
            c.account_id,
            token.target.workspace_id,
            c.revision_id,
            token.version.generation,
            UUID(history.id) if history else None,
            UUID(join.id) if join else None,
            history.ownership_epoch if history else None,
            history.finalization if history else None,
            join.role if join else None,
            role if applied else (join.role if join else None),
            changed,
            metadata_changed,
        )
