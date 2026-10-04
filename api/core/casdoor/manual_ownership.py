"""Manual mutation observations, never caller authorization or remote proof.

I12-A supports only local, no-intent ownership metadata. I13 must supply a real
remote reader boundary and I16 must supply genuine termination provenance before
either unsupported domain opens. No COMPLETE flag or confirmed state is proof.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorMembershipOwnership

from core.casdoor.ownership import MembershipBackend, MembershipObservation

MAX_MANUAL_SCOPES = 100


class ManualMutationKind(StrEnum):
    ROLE_CHANGE = "role_change"
    MEMBER_REMOVE = "member_remove"
    OWNER_TRANSFER = "owner_transfer"


class ManualOwnershipDecision(StrEnum):
    PRESERVE_UNMANAGED = "preserve_unmanaged"
    ALREADY_LOCAL = "already_local"
    MANAGED_READY = "managed_ready"
    OWNER_FENCE_REQUIRED = "owner_fence_required"
    AUTHORIZATION_PENDING = "authorization_pending"
    TERMINATION_REQUIRED = "termination_required"


@dataclass(frozen=True)
class ManualMemberScope:
    """Server-reconstructed exact scope, not payload authorization."""

    account_id: UUID
    workspace_id: UUID


@dataclass(frozen=True, repr=False)
class ManualHistoryObservation:
    membership_id: UUID
    ownership: CasdoorMembershipOwnership
    ownership_epoch: int
    join_id: UUID | None
    tombstone: bool


@dataclass(frozen=True, repr=False)
class ManualScopeInspection:
    scope: ManualMemberScope
    decision: ManualOwnershipDecision
    histories: tuple[ManualHistoryObservation, ...]


def validate_manual_scopes(scopes: tuple[ManualMemberScope, ...], kind: ManualMutationKind) -> None:
    if (
        not isinstance(scopes, tuple)
        or not 1 <= len(scopes) <= MAX_MANUAL_SCOPES
        or not isinstance(kind, ManualMutationKind)
        or any(
            not isinstance(scope, ManualMemberScope)
            or not isinstance(scope.account_id, UUID)
            or not isinstance(scope.workspace_id, UUID)
            for scope in scopes
        )
        or len(set(scopes)) != len(scopes)
    ):
        raise ValueError("authorization_pending")
    if kind is ManualMutationKind.OWNER_TRANSFER and (
        len(scopes) != 2
        or scopes[0].account_id == scopes[1].account_id
        or scopes[0].workspace_id != scopes[1].workspace_id
    ):
        raise ValueError("authorization_pending")


def decide_manual_ownership(
    observation: MembershipObservation,
    histories: tuple[ManualHistoryObservation, ...],
    *,
    required_intent_ids: tuple[UUID, ...],
    kind: ManualMutationKind = ManualMutationKind.ROLE_CHANGE,
) -> ManualOwnershipDecision:
    """Only repository-validated observations can support a metadata operation.

    Even terminal/confirmed intent records need the future I16 provenance owner.
    Existing local Owner policy belongs to the caller; unmanaged Owner metadata
    is preserved. Remote managed observations fail closed without I13, including
    any fabricated typed COMPLETE projection attached to the observation.
    """
    if (
        not isinstance(observation, MembershipObservation)
        or not isinstance(observation.account_id, UUID)
        or not isinstance(observation.workspace_id, UUID)
        or not isinstance(observation.backend, MembershipBackend)
        or not isinstance(kind, ManualMutationKind)
        or (observation.join_id is not None and not isinstance(observation.join_id, UUID))
        or (observation.join_role is not None and not isinstance(observation.join_role, TenantAccountRole))
        or (observation.join_id is None) != (observation.join_role is None)
        or not isinstance(required_intent_ids, tuple)
        or any(not isinstance(value, UUID) for value in required_intent_ids)
        or not isinstance(histories, tuple)
        or any(
            not isinstance(row, ManualHistoryObservation)
            or not isinstance(row.membership_id, UUID)
            or not isinstance(row.ownership, CasdoorMembershipOwnership)
            or type(row.ownership_epoch) is not int
            or not 0 <= row.ownership_epoch <= 2**63 - 1
            or type(row.tombstone) is not bool
            or (row.join_id is not None and not isinstance(row.join_id, UUID))
            for row in histories
        )
    ):
        return ManualOwnershipDecision.AUTHORIZATION_PENDING
    if required_intent_ids:
        return ManualOwnershipDecision.TERMINATION_REQUIRED
    managed = tuple(row for row in histories if row.ownership is CasdoorMembershipOwnership.MANAGED)
    if not managed:
        return (
            ManualOwnershipDecision.ALREADY_LOCAL
            if any(row.ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE or row.tombstone for row in histories)
            else ManualOwnershipDecision.PRESERVE_UNMANAGED
        )
    if observation.join_role is TenantAccountRole.OWNER or any(
        row.tombstone or row.ownership is not CasdoorMembershipOwnership.MANAGED for row in histories
    ):
        return ManualOwnershipDecision.OWNER_FENCE_REQUIRED
    if observation.backend is MembershipBackend.REMOTE:
        return ManualOwnershipDecision.AUTHORIZATION_PENDING
    if observation.backend is not MembershipBackend.LOCAL:
        return ManualOwnershipDecision.AUTHORIZATION_PENDING
    if observation.join_id is None:
        historical_ids = {row.join_id for row in managed}
        if kind is ManualMutationKind.MEMBER_REMOVE and len(historical_ids) == 1 and None not in historical_ids:
            return ManualOwnershipDecision.MANAGED_READY
        return ManualOwnershipDecision.OWNER_FENCE_REQUIRED
    if any(row.join_id != observation.join_id for row in managed):
        return ManualOwnershipDecision.OWNER_FENCE_REQUIRED
    return ManualOwnershipDecision.MANAGED_READY
