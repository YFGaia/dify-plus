"""Persist a previously saved remote desired plan in a clean caller root UoW.

This is staging only: historical baselines and local generation tokens never
prove current remote authorization, visibility, freshness or termination. No
dispatch API exists here. I13/I14-B/I18 must supply those barriers separately.
Only I11's already persisted desired ID schema is supported; full desired role
metadata and advancing managed desired plans belong to their future owner.
"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import sqlalchemy as sa
from configs import dify_config
from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingError, validate_mapping_context
from core.casdoor.ownership import (
    MAX_SNAPSHOT_BYTES,
    ExternalMemberRolesProjection,
    ManagedMembershipSnapshot,
    MembershipBackend,
    MembershipObservation,
    OwnershipDecision,
    RolesKnowledge,
    decide_ownership,
    has_remote_owner,
    parse_role_baseline_json,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from pydantic import ValidationError
from sqlalchemy.orm import Session

from repositories.casdoor_generation_repository_extend import MAX_GENERATION, GenerationPlanVersion

MAX_NAMESPACES = 2000
MAX_MAPPING_BYTES = 512 * 1024


class CasdoorRoleIntentConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("authorization_pending")


@dataclass(frozen=True, repr=False)
class RoleIntentPersistenceReceipt:
    """Uncommitted local persistence, never dispatch eligibility or access."""

    intent_id: UUID
    membership_id: UUID
    join_id: UUID
    desired_payload_digest: str
    scope_digest: str
    idempotency_key: str
    created: bool


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise CasdoorRoleIntentConflict()
        result[key] = value
    return result


def _invalid_number(_value: str) -> None:
    raise CasdoorRoleIntentConflict()


def _decode(value: str) -> object:
    try:
        return json.loads(value, object_pairs_hook=_pairs, parse_float=_invalid_number, parse_constant=_invalid_number)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise CasdoorRoleIntentConflict() from None


class CasdoorRoleIntentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, version: GenerationPlanVersion, target: DesiredWorkspaceTarget) -> RoleIntentPersistenceReceipt:
        """Stage only saved desired IDs; every failure must escape the whole UoW.

        Sorted leases and trusted plan reconstruction are caller obligations.
        Even a pristine pending intent is not authorized for remote dispatch.
        Related history/intents from any namespace/generation block new staging;
        this owner never retires old pending, unknown or in-flight side effects.
        """
        self._require_clean_root()
        self._validate(version, target)
        with self._session.no_autoflush:
            self._guard_owner(version, target)
            self._saved_mapping(version, target)
            join, history = self._scope(version, target)
            c = version.plan.context
            scope, scope_digest, key, values = self._role_intent_scope(version, target, join, history)
            refs = tuple(
                self._session.execute(
                    sa.select(Intent.id, Intent.idempotency_key)
                    .where(
                        sa.or_(
                            Intent.idempotency_key == key,
                            sa.and_(
                                Intent.kind != CasdoorIntentKind.PROFILE_AVATAR,
                                sa.or_(
                                    sa.and_(
                                        Intent.account_id == str(c.account_id),
                                        sa.or_(
                                            Intent.workspace_id == str(target.workspace_id),
                                            Intent.workspace_id.is_(None),
                                        ),
                                    ),
                                    Intent.membership_id == history.id,
                                ),
                            ),
                        )
                    )
                    .order_by(Intent.namespace_id, Intent.scope_digest, Intent.id)
                    .limit(2)
                    .with_for_update()
                )
            )
            created = not refs
            if refs:
                if len(refs) != 1 or refs[0].idempotency_key != key or not self._uuid(refs[0].id):
                    raise CasdoorRoleIntentConflict()
                self._bounded_text(Intent, refs[0].id, (Intent.desired_json,), MAX_SNAPSHOT_BYTES)
                stored = self._session.execute(
                    sa.select(*(getattr(Intent, name) for name in values))
                    .where(Intent.id == refs[0].id)
                    .with_for_update()
                ).one()
                if dict(stored._mapping) != values:
                    raise CasdoorRoleIntentConflict()
                intent_id = refs[0].id
            else:
                intent_id = str(uuid4())
                self._session.execute(sa.insert(Intent).values(id=intent_id, **values))
            return RoleIntentPersistenceReceipt(
                UUID(intent_id),
                UUID(history.id),
                UUID(join.id),
                scope["desired_payload_digest"],
                scope_digest,
                key,
                created,
            )

    def _role_intent_scope(self, version, target, join, history) -> tuple:
        """Single staging/reservation schema and pristine-field guard owner."""
        c = version.plan.context
        desired = self._saved_desired(version, target, history)
        self._historical_baseline(target, version, join, history)
        scope = {
            "schema_version": 2,
            "integration_id": str(c.integration_id),
            "config_digest": c.config_digest,
            "revision_id": str(c.revision_id),
            "namespace_id": str(c.namespace_id),
            "identity_id": str(c.identity_id),
            "account_id": str(c.account_id),
            "workspace_id": str(target.workspace_id),
            "join_id": join.id,
            "membership_id": history.id,
            "generation": version.generation,
            "ownership_epoch": history.ownership_epoch,
            "fence_epoch": version.fence_epoch,
            "kind": CasdoorIntentKind.ROLE_REPLACE.value,
            "desired": desired,
            "desired_payload_digest": _digest(history.desired_roles_json),
            "historical_baseline_fingerprint": history.last_applied_fingerprint,
            "initial_baseline_schema_version": 1,
            "initial_baseline_digest": self._initial_baseline(history),
        }
        payload = _json(scope)
        if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise CasdoorRoleIntentConflict()
        scope_digest = _digest(payload)
        key = _digest("casdoor-role-intent-v2:" + payload)
        values = {
            name: scope[name]
            for name in (
                "namespace_id",
                "identity_id",
                "account_id",
                "workspace_id",
                "membership_id",
                "revision_id",
                "generation",
                "ownership_epoch",
                "fence_epoch",
            )
        }
        values.update(
            kind=CasdoorIntentKind.ROLE_REPLACE,
            resource_type=None,
            resource_id=None,
            scope_digest=scope_digest,
            idempotency_key=key,
            desired_json=payload,
            operation_state=CasdoorOperationState.PENDING,
            termination_state=CasdoorTerminationState.NOT_STARTED,
            attempt_count=0,
            **dict.fromkeys(
                (
                    "attempt_id",
                    "lease_owner",
                    "lease_expires_at",
                    "sent_at",
                    "acknowledged_at",
                    "readback_at",
                    "terminated_at",
                    "termination_proof_kind",
                    "proof_ref",
                    "retry_at",
                    "error_code",
                )
            ),
        )
        return scope, scope_digest, key, values

    @staticmethod
    def _initial_baseline(history) -> str:
        try:
            baseline = parse_role_baseline_json(history.baseline_json)
            if (
                baseline.backend is not MembershipBackend.REMOTE
                or baseline.join_role is TenantAccountRole.OWNER
                or has_remote_owner(baseline.roles)
            ):
                raise CasdoorRoleIntentConflict()
            return _digest(history.baseline_json)
        except (ValueError, TypeError):
            raise CasdoorRoleIntentConflict() from None

    def _require_clean_root(self) -> None:
        transaction = self._session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or not self._session.is_active
            or self._session.get_nested_transaction() is not None
        ):
            raise RuntimeError("Casdoor role intent requires a clean active caller-owned root transaction")
        if self._session.new or self._session.dirty or self._session.deleted or dify_config.RBAC_ENABLED is not True:
            raise CasdoorRoleIntentConflict()

    @staticmethod
    def _validate(version, target) -> None:
        try:
            if (
                not isinstance(version, GenerationPlanVersion)
                or not isinstance(version.plan, DesiredWorkspacePlan)
                or type(version.generation) is not int
                or not 1 <= version.generation <= MAX_GENERATION
                or type(version.fence_epoch) is not int
                or not 0 <= version.fence_epoch <= MAX_GENERATION
                or not isinstance(version.plan.targets, tuple)
                or not 1 <= len(version.plan.targets) <= 100
                or not isinstance(target, DesiredWorkspaceTarget)
                or target not in version.plan.targets
            ):
                raise CasdoorRoleIntentConflict()
            validate_mapping_context(version.plan.context)
            workspaces = set()
            for item in version.plan.targets:
                if (
                    not isinstance(item, DesiredWorkspaceTarget)
                    or not isinstance(item.workspace_id, UUID)
                    or item.workspace_id in workspaces
                    or type(item.target_role) is not str
                    or item.target_role not in ("admin", "editor", "normal")
                    or type(item.builtin_id) is not str
                    or not item.builtin_id.strip()
                    or len(item.builtin_id.encode("utf-8")) > 255
                    or any(ord(char) < 32 or ord(char) == 127 for char in item.builtin_id)
                    or not isinstance(item.reason, CasdoorDecisionReason)
                    or item.reason
                    not in (CasdoorDecisionReason.ROLE_MAPPING, CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK)
                    or not isinstance(item.matched_role_refs, tuple)
                    or len(item.matched_role_refs) > 3
                    or any(
                        not isinstance(ref, RoleRef) or ref.organization != version.plan.context.organization
                        for ref in item.matched_role_refs
                    )
                    or len(set(item.matched_role_refs)) != len(item.matched_role_refs)
                    or (
                        item.reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
                        and (item.target_role != "normal" or item.matched_role_refs)
                    )
                ):
                    raise CasdoorRoleIntentConflict()
                workspaces.add(item.workspace_id)
        except (ValueError, AttributeError, TypeError, UnicodeError):
            raise CasdoorRoleIntentConflict() from None

    def _bounded_text(self, model, row_id, columns, limit) -> None:
        lengths = self._session.execute(
            sa.select(
                *(
                    sa.func.length(sa.cast(col, sa.LargeBinary))
                    if self._session.get_bind().dialect.name == "sqlite"
                    else sa.func.octet_length(col)
                    for col in columns
                )
            ).where(model.id == row_id)
        ).one()
        if any(type(length) is not int or not 0 <= length <= limit for length in lengths):
            raise CasdoorRoleIntentConflict()

    def _saved_mapping(self, version, target) -> None:
        revision_id = str(version.plan.context.revision_id)
        self._bounded_text(
            CasdoorConfigRevisionExtend, revision_id, (CasdoorConfigRevisionExtend.mappings_json,), MAX_MAPPING_BYTES
        )
        self._bounded_text(
            CasdoorConfigRevisionExtend, revision_id, (CasdoorConfigRevisionExtend.policy_json,), MAX_SNAPSHOT_BYTES
        )
        row = self._session.execute(
            sa.select(
                CasdoorConfigRevisionExtend.default_workspace_id,
                CasdoorConfigRevisionExtend.mappings_json,
                CasdoorConfigRevisionExtend.policy_json,
            ).where(CasdoorConfigRevisionExtend.id == revision_id)
        ).one()
        raw = _decode(row.mappings_json)
        if type(raw) is not list or len(raw) > 100:
            raise CasdoorRoleIntentConflict()
        try:
            mappings = tuple(WorkspaceRoleMapping.model_validate(item) for item in raw)
        except (ValidationError, ValueError, TypeError):
            raise CasdoorRoleIntentConflict() from None
        if len({item.workspace_id for item in mappings}) != len(mappings) or any(
            ref.organization != version.plan.context.organization
            for item in mappings
            for ref in (item.admin, item.editor, item.normal)
            if ref is not None
        ):
            raise CasdoorRoleIntentConflict()
        policy = _decode(row.policy_json)
        if (
            type(policy) is not dict
            or type(policy.get("schema_version")) is not int
            or policy["schema_version"] != 1
            or policy.get("default_normal_fallback") is not True
        ):
            raise CasdoorRoleIntentConflict()
        mapping = next((item for item in mappings if item.workspace_id == target.workspace_id), None)
        if target.reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK:
            if row.default_workspace_id != str(target.workspace_id):
                raise CasdoorRoleIntentConflict()
        elif (
            mapping is None
            or not target.matched_role_refs
            or any(ref not in (mapping.admin, mapping.editor, mapping.normal) for ref in target.matched_role_refs)
            or getattr(mapping, target.target_role) not in target.matched_role_refs
            or next(
                role for role in ("admin", "editor", "normal") if getattr(mapping, role) in target.matched_role_refs
            )
            != target.target_role
        ):
            raise CasdoorRoleIntentConflict()

    def _scope(self, version, target) -> tuple:
        c = version.plan.context
        workspace = self._session.execute(
            sa.select(Tenant.status).where(Tenant.id == str(target.workspace_id)).with_for_update()
        ).one_or_none()
        if workspace is None or workspace.status is not TenantStatus.NORMAL:
            raise CasdoorRoleIntentConflict()
        join = self._session.execute(
            sa.select(TenantAccountJoin.id, TenantAccountJoin.role)
            .where(
                TenantAccountJoin.tenant_id == str(target.workspace_id),
                TenantAccountJoin.account_id == str(c.account_id),
            )
            .with_for_update()
        ).one_or_none()
        if join is None or not self._uuid(join.id) or join.role is not TenantAccountRole.NORMAL:
            raise CasdoorRoleIntentConflict()
        refs = tuple(
            self._session.execute(
                sa.select(History.id, History.namespace_id, History.identity_id)
                .where(History.account_id == str(c.account_id), History.workspace_id == str(target.workspace_id))
                .order_by(History.namespace_id, History.id)
                .limit(2)
                .with_for_update()
            )
        )
        if (
            len(refs) != 1
            or any(not all(self._uuid(value) for value in row) for row in refs)
            or (refs[0].namespace_id, refs[0].identity_id) != (str(c.namespace_id), str(c.identity_id))
        ):
            raise CasdoorRoleIntentConflict()
        self._bounded_text(
            History,
            refs[0].id,
            (History.last_applied_roles_json, History.desired_roles_json, History.baseline_json),
            MAX_SNAPSHOT_BYTES,
        )
        history = self._session.execute(
            sa.select(*History.__table__.columns).where(History.id == refs[0].id).with_for_update()
        ).one()
        if (
            history.join_id != join.id
            or history.ownership is not CasdoorMembershipOwnership.MANAGED
            or history.tombstone is not False
            or not self._epoch(history.ownership_epoch)
            or history.desired_generation != version.generation
            or not self._epoch(history.desired_generation)
            or history.revision_id != str(c.revision_id)
            or history.finalization is not CasdoorFinalizationState.PENDING
        ):
            raise CasdoorRoleIntentConflict()
        return join, history

    @staticmethod
    def _saved_desired(version, target, history) -> dict:
        expected = {
            "schema_version": 1,
            "backend": "remote",
            "target_role": target.target_role,
            "builtin_id": target.builtin_id,
            "role_ids": [target.builtin_id],
            "fence_epoch": version.fence_epoch,
        }
        saved = _decode(history.desired_roles_json)
        if (
            saved != expected
            or type(saved.get("schema_version")) is not int
            or type(saved.get("fence_epoch")) is not int
        ):
            raise CasdoorRoleIntentConflict()
        if _json(expected) != history.desired_roles_json:
            raise CasdoorRoleIntentConflict()
        return expected

    @staticmethod
    def _historical_baseline(target, version, join, history) -> None:
        """Validate only persisted historical metadata, never current remote state."""
        try:
            baseline = parse_role_baseline_json(history.last_applied_roles_json)
            if baseline.backend is not MembershipBackend.REMOTE or baseline.join_role is not join.role:
                raise CasdoorRoleIntentConflict()
            roles = baseline.roles
            observation = MembershipObservation(
                target.workspace_id,
                version.plan.context.account_id,
                UUID(join.id),
                join.role,
                MembershipBackend.REMOTE,
                ExternalMemberRolesProjection(
                    target.workspace_id,
                    version.plan.context.account_id,
                    RolesKnowledge.COMPLETE,
                    roles,
                ),
            )
            snapshot = ManagedMembershipSnapshot(
                UUID(history.id),
                UUID(history.namespace_id),
                UUID(history.identity_id),
                UUID(history.account_id),
                UUID(history.workspace_id),
                UUID(history.join_id),
                history.ownership,
                history.ownership_epoch,
                history.source,
                history.desired_generation,
                UUID(history.revision_id),
                history.last_applied_roles_json,
                history.last_applied_fingerprint,
                history.desired_roles_json,
                history.baseline_json,
                history.tombstone,
            )
            if (
                role_baseline_json(observation) != history.last_applied_roles_json
                or roles_fingerprint(observation) != history.last_applied_fingerprint
                or decide_ownership(observation, snapshot) is not OwnershipDecision.MANAGED_CURRENT
            ):
                raise CasdoorRoleIntentConflict()
        except (ValueError, TypeError, AttributeError):
            raise CasdoorRoleIntentConflict() from None

    @staticmethod
    def _epoch(value) -> bool:
        return type(value) is int and 0 <= value <= MAX_GENERATION

    @staticmethod
    def _uuid(value) -> bool:
        try:
            return isinstance(value, str) and str(UUID(value)) == value
        except (ValueError, TypeError):
            return False

    def _guard_owner(self, version: GenerationPlanVersion, target: DesiredWorkspaceTarget) -> tuple:
        if (
            not isinstance(version, GenerationPlanVersion)
            or not isinstance(version.plan, DesiredWorkspacePlan)
            or type(version.generation) is not int
            or not 1 <= version.generation <= MAX_GENERATION
            or type(version.fence_epoch) is not int
            or not 0 <= version.fence_epoch <= MAX_GENERATION
        ):
            raise CasdoorRoleIntentConflict()
        try:
            validate_mapping_context(version.plan.context)
        except (MappingError, AttributeError):
            raise CasdoorRoleIntentConflict() from None
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
            raise CasdoorRoleIntentConflict()
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
            raise CasdoorRoleIntentConflict()
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
            raise CasdoorRoleIntentConflict()
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
            raise CasdoorRoleIntentConflict()
        if not self._uuid(revision.default_workspace_id) or (
            target.reason.value == "default_normal_fallback"
            and revision.default_workspace_id != str(target.workspace_id)
        ):
            raise CasdoorRoleIntentConflict()
        account = self._session.execute(
            sa.select(Account.id, Account.status).where(Account.id == str(c.account_id)).with_for_update()
        ).one_or_none()
        if account is None or account.status in (AccountStatus.BANNED, AccountStatus.CLOSED):
            raise CasdoorRoleIntentConflict()
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
            raise CasdoorRoleIntentConflict()

        return (tuple(integration), namespaces, tuple(revision), tuple(account), tuple(identity))
