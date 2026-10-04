"""Bounded local role policy; internal plans are not permission proofs.

I18 reconstructs the immutable configuration/identity plan after fresh role reads
and complete leases. This owner cannot attest that sequence or authorize payloads.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorFinalizationState

from core.casdoor.configuration import RoleRef
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, validate_mapping_context

LOCAL_ROLES = (TenantAccountRole.ADMIN, TenantAccountRole.EDITOR, TenantAccountRole.NORMAL)


class LocalRoleOutcome(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"
    PRESERVED = "preserved"
    PENDING = "pending"


@dataclass(frozen=True, repr=False)
class LocalRoleApplyReceipt:
    """Local mutation only, never committed access or resource finalization.

    Existing local role-change owner has no postcommit cache/event obligation.
    The caller still owes I14-I18 invite/resource and ordinary-session barriers.
    """

    outcome: LocalRoleOutcome
    applied: bool
    namespace_id: UUID
    identity_id: UUID
    account_id: UUID
    workspace_id: UUID
    revision_id: UUID
    generation: int
    membership_id: UUID | None
    join_id: UUID | None
    ownership_epoch: int | None
    finalization: CasdoorFinalizationState | None
    prior_role: TenantAccountRole | None
    current_role: TenantAccountRole | None
    role_changed: bool
    metadata_changed: bool


def resolve_local_target(plan: DesiredWorkspacePlan, target: DesiredWorkspaceTarget) -> TenantAccountRole:
    """Resolve established server builtin enum; never trust arbitrary ID/tag."""
    validate_mapping_context(plan.context)
    if (
        not isinstance(plan, DesiredWorkspacePlan)
        or not isinstance(plan.targets, tuple)
        or not 1 <= len(plan.targets) <= 100
        or not isinstance(target, DesiredWorkspaceTarget)
        or target not in plan.targets
    ):
        raise ValueError("authorization_pending")
    workspaces = set()
    for item in plan.targets:
        if (
            not isinstance(item, DesiredWorkspaceTarget)
            or not isinstance(item.workspace_id, UUID)
            or item.workspace_id in workspaces
            or type(item.target_role) is not str
            or item.target_role not in LOCAL_ROLES
            or type(item.builtin_id) is not str
            or item.builtin_id != TenantAccountRole(item.target_role).value
            or item.reason not in (CasdoorDecisionReason.ROLE_MAPPING, CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK)
            or not isinstance(item.reason, CasdoorDecisionReason)
            or not isinstance(item.matched_role_refs, tuple)
            or len(item.matched_role_refs) > 3
            or any(
                not isinstance(ref, RoleRef)
                or ref.organization != plan.context.organization
                or not ref.name
                or len(ref.name.encode("utf-8")) > 255
                for ref in item.matched_role_refs
            )
            or len(set(item.matched_role_refs)) != len(item.matched_role_refs)
            or (
                item.reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
                and (item.target_role != "normal" or item.matched_role_refs)
            )
        ):
            raise ValueError("authorization_pending")
        workspaces.add(item.workspace_id)
    return TenantAccountRole(target.target_role)
