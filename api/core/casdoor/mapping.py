"""Pure desired-workspace policy, without membership or authorization writes.

Only the I10 internal known-result projection is accepted. The caller constructs
context from the current database revision/identity and availability from fresh
server-side workspace/builtin reads after acquiring the complete sorted leases.
Types and fingerprints are not provenance proofs. Neither this policy nor its
result authenticates payloads, protects Owners or authorizes existing members.
I11-B owns unmanaged/Owner/override decisions; I18 owns lease/snapshot ordering.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from core.casdoor.claims import StructuredUserRef
from core.casdoor.configuration import CasdoorConfiguration, RoleRef, TargetRole, WorkspaceRoleMapping
from core.casdoor.errors import CasdoorDecisionReason, CasdoorErrorCode
from core.casdoor.role_graph import MAX_NODES, EffectiveRoleSnapshot


class MappingError(ValueError):
    def __init__(self, code: CasdoorErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, repr=False)
class MappingIdentityContext:
    """Trusted DB-reconstructed exact owner chain, never a controller payload."""

    integration_id: UUID
    revision_id: UUID
    namespace_id: UUID
    identity_id: UUID
    account_id: UUID
    config_digest: str
    issuer: str
    organization: str
    application: str
    client_id: str
    subject: str


class WorkspaceState(StrEnum):
    NORMAL = "normal"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuiltinResolution:
    """Fresh backend resolution of an exact global_system_default builtin.

    Local RBAC-off owners also supply their established builtin identity. Missing
    or unknown resolution is represented by absence, never a payload boolean.
    I13/I18 must use bounded backend reads or established server-owned proof,
    never HTTP payload booleans. This module implements no HTTP or Owner readback.
    """

    target_role: TargetRole
    builtin_id: str


@dataclass(frozen=True)
class WorkspaceAvailability:
    workspace_id: UUID
    state: WorkspaceState
    builtins: tuple[BuiltinResolution, ...]


@dataclass(frozen=True)
class ServerWorkspaceAvailability:
    """Server-owned projection, reconstructed afresh by the future caller."""

    workspaces: tuple[WorkspaceAvailability, ...]


@dataclass(frozen=True)
class DesiredWorkspaceTarget:
    workspace_id: UUID
    target_role: TargetRole
    builtin_id: str
    reason: CasdoorDecisionReason
    matched_role_refs: tuple[RoleRef, ...]


@dataclass(frozen=True, repr=False)
class DesiredWorkspacePlan:
    context: MappingIdentityContext
    targets: tuple[DesiredWorkspaceTarget, ...]


def _text(value: object, limit: int) -> bool:
    try:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value.encode("utf-8")) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except UnicodeError:
        return False


def validate_mapping_context(context: MappingIdentityContext) -> None:
    """Validate shape only; caller remains responsible for trusted provenance."""
    if (
        not isinstance(context, MappingIdentityContext)
        or not all(
            isinstance(value, UUID)
            for value in (
                context.integration_id,
                context.revision_id,
                context.namespace_id,
                context.identity_id,
                context.account_id,
            )
        )
        or not _text(context.issuer, 2048)
        or not _text(context.organization, 255)
        or not _text(context.application, 255)
        or not _text(context.client_id, 255)
        or not _text(context.subject, 255)
        or not isinstance(context.config_digest, str)
        or len(context.config_digest) != 64
        or any(c not in "0123456789abcdef" for c in context.config_digest)
    ):
        raise MappingError(CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)


def _valid_ref(ref: object, organization: str) -> bool:
    return (
        isinstance(ref, RoleRef)
        and getattr(ref, "organization", None) == organization
        and _text(getattr(ref, "organization", None), 255)
        and _text(getattr(ref, "name", None), 255)
    )


def resolve_workspace_plan(
    *,
    configuration: CasdoorConfiguration,
    snapshot: EffectiveRoleSnapshot,
    context: MappingIdentityContext,
    availability: ServerWorkspaceAvailability,
) -> DesiredWorkspacePlan:
    """Resolve all targets exactly; unknown never enters normal fallback.

    Every configured workspace and builtin target is checked, including default
    normal and currently unmatched mappings. No clipping, split, normalization,
    rename guessing, member mutation or permission grant occurs here.
    """
    unknown = CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    validate_mapping_context(context)
    if (
        not isinstance(configuration, CasdoorConfiguration)
        or not {
            "organization",
            "expected_issuer",
            "application",
            "client_id",
            "default_workspace_id",
            "default_normal_fallback",
            "workspace_mappings",
        }
        <= configuration.__dict__.keys()
        or configuration.organization != context.organization
        or configuration.expected_issuer != context.issuer
        or configuration.application != context.application
        or configuration.client_id != context.client_id
        or configuration.default_normal_fallback is not True
        or not isinstance(configuration.default_workspace_id, UUID)
        or not isinstance(configuration.workspace_mappings, tuple)
        or not isinstance(snapshot, EffectiveRoleSnapshot)
        or snapshot.subject != context.subject
        or not isinstance(snapshot.user_ref, StructuredUserRef)
        or snapshot.user_ref.owner != context.organization
        or not _text(snapshot.user_ref.name, 255)
        or not isinstance(snapshot.effective_roles, tuple)
        or len(snapshot.effective_roles) > MAX_NODES
        or not all(_valid_ref(ref, context.organization) for ref in snapshot.effective_roles)
    ):
        raise MappingError(unknown)
    refs = frozenset(snapshot.effective_roles)
    if len(refs) != len(snapshot.effective_roles):
        raise MappingError(unknown)
    mappings: dict[UUID, WorkspaceRoleMapping] = {}
    required: dict[UUID, set[str]] = {configuration.default_workspace_id: {"normal"}}
    for mapping in configuration.workspace_mappings:
        if not isinstance(mapping, WorkspaceRoleMapping) or not isinstance(
            getattr(mapping, "workspace_id", None), UUID
        ):
            raise MappingError(unknown)
        if mapping.workspace_id in mappings:
            raise MappingError(unknown)
        slots = [ref for ref in (mapping.admin, mapping.editor, mapping.normal) if ref is not None]
        if not all(_valid_ref(ref, context.organization) for ref in slots) or len(set(slots)) != len(slots):
            raise MappingError(unknown)
        mappings[mapping.workspace_id] = mapping
        required.setdefault(mapping.workspace_id, set()).update(
            role for role in ("admin", "editor", "normal") if getattr(mapping, role) is not None
        )
    if len(required) > 100 or len(configuration.workspace_mappings) > 100:
        raise MappingError(unknown)
    if not isinstance(availability, ServerWorkspaceAvailability) or not isinstance(availability.workspaces, tuple):
        raise MappingError(CasdoorErrorCode.WORKSPACE_UNAVAILABLE)
    resolutions: dict[UUID, dict[str, str]] = {}
    for workspace in availability.workspaces:
        if (
            not isinstance(workspace, WorkspaceAvailability)
            or not isinstance(workspace.workspace_id, UUID)
            or workspace.workspace_id in resolutions
            or workspace.workspace_id not in required
            or workspace.state is not WorkspaceState.NORMAL
        ):
            raise MappingError(CasdoorErrorCode.WORKSPACE_UNAVAILABLE)
        if not isinstance(workspace.builtins, tuple):
            raise MappingError(CasdoorErrorCode.AUTHORIZATION_PENDING)
        builtins: dict[str, str] = {}
        for builtin in workspace.builtins:
            if (
                not isinstance(builtin, BuiltinResolution)
                or builtin.target_role not in ("admin", "editor", "normal")
                or builtin.target_role in builtins
                or not _text(builtin.builtin_id, 255)
                or builtin.builtin_id in builtins.values()
            ):
                raise MappingError(CasdoorErrorCode.AUTHORIZATION_PENDING)
            builtins[builtin.target_role] = builtin.builtin_id
        if not required[workspace.workspace_id] <= builtins.keys():
            raise MappingError(CasdoorErrorCode.AUTHORIZATION_PENDING)
        resolutions[workspace.workspace_id] = builtins
    if resolutions.keys() != required.keys():
        raise MappingError(CasdoorErrorCode.WORKSPACE_UNAVAILABLE)
    targets = []
    for workspace_id in sorted(required, key=str):
        mapping = mappings.get(workspace_id)
        matched = tuple(
            (role, ref)
            for role in ("admin", "editor", "normal")
            if mapping is not None and (ref := getattr(mapping, role)) is not None and ref in refs
        )
        if matched:
            target_role = matched[0][0]
            reason = CasdoorDecisionReason.ROLE_MAPPING
        elif workspace_id == configuration.default_workspace_id:
            target_role = "normal"
            reason = CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
        else:
            continue
        targets.append(
            DesiredWorkspaceTarget(
                workspace_id, target_role, resolutions[workspace_id][target_role], reason, tuple(r for _, r in matched)
            )
        )
    return DesiredWorkspacePlan(context, tuple(targets))
