"""Pure membership ownership policy, never a permission or termination proof.

Remote projections are trusted internal handoffs from a future bounded I13
reader, not controller flags. This foundation supplies no reader. A complete
readback does not prove queued/in-flight writes terminated. Owner decisions
request fencing/reconciliation; I16/I21 must finish it before ownership moves.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorMembershipOwnership, CasdoorMembershipSource

MAX_MEMBER_ROLES = 2048
MAX_PERMISSION_KEYS = 8192
MAX_SNAPSHOT_BYTES = 65535
# Reserve 4 KiB for schema/backend/join metadata and JSON framing. Budget each
# bounded scalar before allocating the complete canonical snapshot.
MAX_ROLE_SET_BYTES = 60 * 1024


class MembershipBackend(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class RolesKnowledge(StrEnum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"


@dataclass(frozen=True)
class MemberRole:
    """Exact authorization-relevant RBACRole fields, including custom roles."""

    role_id: str
    is_builtin: bool
    category: str
    role_tag: str
    permission_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExternalMemberRolesProjection:
    """Full exact member scope. Types alone do not attest freshness/provenance.

    I13 must validate actual raw presence/model_fields_set for roles and nested
    owner metadata and release-specific completeness. Existing RBAC DTO defaults
    (roles=[], is_builtin=False, empty category/tag) cannot establish completeness.
    Any permission keys supplied here are fingerprinted; this foundation does
    not monitor external mutations of role definitions or prove termination.
    """

    workspace_id: UUID | None = None
    account_id: UUID | None = None
    knowledge: RolesKnowledge = RolesKnowledge.UNKNOWN
    roles: tuple[MemberRole, ...] = ()


UNKNOWN_MEMBER_ROLES = ExternalMemberRolesProjection()


@dataclass(frozen=True)
class MembershipObservation:
    workspace_id: UUID
    account_id: UUID
    join_id: UUID | None
    join_role: TenantAccountRole | None
    backend: MembershipBackend
    remote: ExternalMemberRolesProjection = UNKNOWN_MEMBER_ROLES


@dataclass(frozen=True, repr=False)
class ManagedMembershipSnapshot:
    membership_id: UUID
    namespace_id: UUID
    identity_id: UUID
    account_id: UUID
    workspace_id: UUID
    join_id: UUID | None
    ownership: CasdoorMembershipOwnership
    ownership_epoch: int
    source: CasdoorMembershipSource
    desired_generation: int
    revision_id: UUID
    last_applied_roles_json: str
    last_applied_fingerprint: str | None
    desired_roles_json: str
    baseline_json: str
    tombstone: bool


class OwnershipDecision(StrEnum):
    NEW_JOIN_REQUIRED = "new_join_required"
    PRESERVE_UNMANAGED = "preserve_unmanaged"
    PRESERVE_OVERRIDE = "preserve_override"
    OWNER_PROTECTED = "owner_protected"
    REQUEST_OWNER_FENCE = "request_owner_fence"
    AUTHORIZATION_PENDING = "authorization_pending"
    MANAGED_CURRENT = "managed_current"
    MARK_OVERRIDE_REQUIRED = "mark_override_required"
    CONTROLLED_WITHDRAWN = "controlled_withdrawn"
    MAPPED_REGRANT_REQUIRED = "mapped_regrant_required"


def _text(value: object, *, empty: bool = False) -> bool:
    try:
        return (
            isinstance(value, str)
            and (empty or bool(value))
            and len(value) <= 2048
            and len(value.encode("utf-8")) <= 2048
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except UnicodeError:
        return False


def canonical_roles(roles: tuple[MemberRole, ...]) -> tuple[MemberRole, ...]:
    """Reject partial/duplicate entries rather than silently collapsing a set."""
    if not isinstance(roles, tuple) or len(roles) > MAX_MEMBER_ROLES:
        raise ValueError("authorization_pending")
    identifiers: set[str] = set()
    permission_count = 0
    byte_budget = 2
    for role in roles:
        if (
            not isinstance(role, MemberRole)
            or not _text(role.role_id)
            or type(role.is_builtin) is not bool
            or not _text(role.category, empty=True)
            or not _text(role.role_tag, empty=True)
            or not isinstance(role.permission_keys, tuple)
            or len(role.permission_keys) > MAX_PERMISSION_KEYS
            or any(not _text(key) for key in role.permission_keys)
            or len(set(role.permission_keys)) != len(role.permission_keys)
            or role.role_id in identifiers
        ):
            raise ValueError("authorization_pending")
        permission_count += len(role.permission_keys)
        if permission_count > MAX_PERMISSION_KEYS:
            raise ValueError("authorization_pending")
        # Conservative framing/field-name bound plus exact escaped scalar bytes.
        # Each scalar is independently bounded before json.dumps; never create
        # the potentially large Cartesian role/permission snapshot to measure it.
        byte_budget += 160
        for value in (role.role_id, role.category, role.role_tag, *role.permission_keys):
            byte_budget += len(json.dumps(value, ensure_ascii=True).encode("utf-8")) + 2
            if byte_budget > MAX_ROLE_SET_BYTES:
                raise ValueError("authorization_pending")
        identifiers.add(role.role_id)
    return tuple(sorted(roles, key=lambda role: role.role_id))


def complete_remote_roles(observation: MembershipObservation) -> tuple[MemberRole, ...]:
    remote = observation.remote
    if (
        not isinstance(remote, ExternalMemberRolesProjection)
        or remote.knowledge is not RolesKnowledge.COMPLETE
        or remote.workspace_id != observation.workspace_id
        or remote.account_id != observation.account_id
    ):
        raise ValueError("authorization_pending")
    return canonical_roles(remote.roles)


def has_remote_owner(roles: tuple[MemberRole, ...]) -> bool:
    return any(
        role.is_builtin and role.category == "global_system_default" and role.role_tag == "owner" for role in roles
    )


@dataclass(frozen=True)
class ParsedRoleBaseline:
    """Validated historical bytes only; no scope, provenance or runtime authority."""

    backend: MembershipBackend
    join_role: TenantAccountRole | None
    roles: tuple[MemberRole, ...]


def parse_role_baseline_json(value: str) -> ParsedRoleBaseline:
    """Strict schema-1 canonical snapshot, bounded before JSON materialization."""

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("authorization_pending")
            result[key] = item
        return result

    def invalid_number(_value):
        raise ValueError("authorization_pending")

    try:
        if not isinstance(value, str) or len(value) > MAX_SNAPSHOT_BYTES:
            raise ValueError("authorization_pending")
        if len(value.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("authorization_pending")
        data = json.loads(value, object_pairs_hook=pairs, parse_float=invalid_number, parse_constant=invalid_number)
        if (
            type(data) is not dict
            or set(data) != {"schema_version", "backend", "join_role", "roles"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or type(data["roles"]) is not list
            or len(data["roles"]) > MAX_MEMBER_ROLES
        ):
            raise ValueError("authorization_pending")
        backend = MembershipBackend(data["backend"])
        join_role = None if data["join_role"] is None else TenantAccountRole(data["join_role"])
        roles = []
        for item in data["roles"]:
            if (
                type(item) is not dict
                or set(item) != {"role_id", "is_builtin", "category", "role_tag", "permission_keys"}
                or type(item["permission_keys"]) is not list
            ):
                raise ValueError("authorization_pending")
            roles.append(
                MemberRole(
                    item["role_id"],
                    item["is_builtin"],
                    item["category"],
                    item["role_tag"],
                    tuple(item["permission_keys"]),
                )
            )
        canonical = canonical_roles(tuple(roles))
        if any(
            (role.is_builtin != (role.category == "global_system_default"))
            or (role.role_tag == "owner" and not role.is_builtin)
            or (role.is_builtin and role.role_tag not in ("owner", "admin", "editor", "normal", "dataset_operator"))
            for role in canonical
        ):
            raise ValueError("authorization_pending")
        if backend is MembershipBackend.LOCAL and canonical:
            raise ValueError("authorization_pending")
        if _serialize_role_baseline(backend, join_role, canonical) != value:
            raise ValueError("authorization_pending")
        return ParsedRoleBaseline(backend, join_role, canonical)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("authorization_pending") from None


def parse_local_withdrawal_json(value: str) -> dict:
    """Strict historical schema-2 bytes only; the repository owns all authority."""

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("authorization_pending")
            result[key] = item
        return result

    def invalid_number(_value):
        raise ValueError("authorization_pending")

    try:
        if not isinstance(value, str) or len(value) > MAX_SNAPSHOT_BYTES:
            raise ValueError()
        if len(value.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError()
        data = json.loads(value, object_pairs_hook=pairs, parse_float=invalid_number, parse_constant=invalid_number)
        if (
            type(data) is not dict
            or set(data)
            != {
                "schema_version",
                "backend",
                "operation",
                "removed_join_id",
                "withdrawal_generation",
                "withdrawal_epoch",
                "fence_epoch",
            }
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 2
            or data["backend"] != "local"
            or data["operation"] != "controlled_withdrawal"
            or type(data["removed_join_id"]) is not str
            or str(UUID(data["removed_join_id"])) != data["removed_join_id"]
            or any(
                type(data[key]) is not int or not minimum <= data[key] <= 2**63 - 1
                for key, minimum in (("withdrawal_generation", 1), ("withdrawal_epoch", 1), ("fence_epoch", 0))
            )
            or json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) != value
        ):
            raise ValueError()
        return data
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise ValueError("authorization_pending") from None


def role_baseline_json(observation: MembershipObservation) -> str:
    """Canonical complete set plus local join role; never a desired-readback claim."""
    if not isinstance(observation.backend, MembershipBackend):
        raise ValueError("authorization_pending")
    if observation.join_role is not None and not isinstance(observation.join_role, TenantAccountRole):
        raise ValueError("authorization_pending")
    roles = complete_remote_roles(observation) if observation.backend is MembershipBackend.REMOTE else ()
    return _serialize_role_baseline(observation.backend, observation.join_role, roles)


def _serialize_role_baseline(backend, join_role, roles) -> str:
    value = {
        "schema_version": 1,
        "backend": backend.value,
        "join_role": join_role.value if join_role is not None else None,
        "roles": [
            {
                "role_id": role.role_id,
                "is_builtin": role.is_builtin,
                "category": role.category,
                "role_tag": role.role_tag,
                "permission_keys": sorted(role.permission_keys),
            }
            for role in roles
        ],
    }
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(serialized.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("authorization_pending")
    return serialized


def roles_fingerprint(observation: MembershipObservation) -> str:
    return hashlib.sha256(role_baseline_json(observation).encode("utf-8")).hexdigest()


def decide_ownership(
    observation: MembershipObservation, managed: ManagedMembershipSnapshot | None
) -> OwnershipDecision:
    """Safe read decision only: no adopt/release, role update or override write."""
    if (
        not isinstance(observation, MembershipObservation)
        or not isinstance(observation.workspace_id, UUID)
        or not isinstance(observation.account_id, UUID)
        or (observation.join_id is not None and not isinstance(observation.join_id, UUID))
        or (observation.join_id is None) != (observation.join_role is None)
        or (observation.join_role is not None and not isinstance(observation.join_role, TenantAccountRole))
        or (
            managed is not None
            and (
                not isinstance(managed, ManagedMembershipSnapshot)
                or not isinstance(managed.ownership, CasdoorMembershipOwnership)
                or managed.account_id != observation.account_id
                or managed.workspace_id != observation.workspace_id
            )
        )
    ):
        return OwnershipDecision.AUTHORIZATION_PENDING
    active = managed is not None and managed.ownership is CasdoorMembershipOwnership.MANAGED
    if observation.join_role is TenantAccountRole.OWNER:
        return OwnershipDecision.REQUEST_OWNER_FENCE if active else OwnershipDecision.OWNER_PROTECTED
    if observation.backend is MembershipBackend.REMOTE:
        try:
            remote_roles = complete_remote_roles(observation)
        except ValueError:
            return OwnershipDecision.AUTHORIZATION_PENDING
        if has_remote_owner(remote_roles):
            return OwnershipDecision.REQUEST_OWNER_FENCE if active else OwnershipDecision.OWNER_PROTECTED
    elif observation.backend is not MembershipBackend.LOCAL:
        return OwnershipDecision.AUTHORIZATION_PENDING
    if managed is not None:
        if managed.tombstone or managed.ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE:
            return OwnershipDecision.PRESERVE_OVERRIDE
        if managed.ownership is CasdoorMembershipOwnership.RELEASED:
            return OwnershipDecision.PRESERVE_UNMANAGED
        try:
            baseline = role_baseline_json(observation)
            fingerprint = roles_fingerprint(observation)
        except ValueError:
            return OwnershipDecision.AUTHORIZATION_PENDING
        if (
            observation.join_id is None
            or observation.join_id != managed.join_id
            or fingerprint != managed.last_applied_fingerprint
            or baseline != managed.last_applied_roles_json
        ):
            return OwnershipDecision.MARK_OVERRIDE_REQUIRED
        return OwnershipDecision.MANAGED_CURRENT
    if observation.join_id is not None:
        return OwnershipDecision.PRESERVE_UNMANAGED
    # A pre-existing remote grant has a different owner even without a DB join.
    if observation.backend is MembershipBackend.REMOTE and complete_remote_roles(observation):
        return OwnershipDecision.PRESERVE_UNMANAGED
    return OwnershipDecision.NEW_JOIN_REQUIRED
