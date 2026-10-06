"""Complete online directory graph validation; no authorization writes or generation.

The server selects the supported flat directory schema. Each use validates the
actual organization/user/role responses and their completeness before matching
roles. The caller acquires its DB-reconstructed namespace/subject lease before
loading, keeps network I/O outside write transactions, and assigns generation
later (I11).
"""

import re
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from core.casdoor.claims import ClaimsError, ClaimsValidator, StructuredUserRef, VerifiedIDToken, VerifiedOnlineUser
from core.casdoor.configuration import RoleRef
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import (
    CasdoorDirectoryGateway,
    DirectoryCredentialStrategy,
    DirectoryDeploymentProof,
    GatewayError,
    GatewayOperation,
)

MAX_NODES = 2000
MAX_EDGES = 20000
MAX_DEPTH = 32
MAX_RELATIONS = 2000
MAX_REF_BYTES = 511
_VISIBLE_FIELDS = frozenset({"owner", "id", "name", "isforbidden", "isdeleted", "groups", "roles"})


class RoleSnapshotError(ValueError):
    """Stable, redacted rejection. Unknown input never creates a fallback."""

    code = CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    retry_allowed = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"casdoor_role_snapshot_{reason}")


def _fail(reason: str) -> Any:
    raise RoleSnapshotError(reason) from None


class DirectorySnapshotSchema(StrEnum):
    FLAT_DIRECTORY_V1 = "flat_directory_v1"


@dataclass(frozen=True, repr=False)
class DirectorySnapshotContract:
    """Server-selected response schema, bound to the configured organization.

    The normal diagnostic/login path uses the built-in flat Casdoor profile and
    leaves review evidence fields empty. A separately reviewed profile can still
    be supplied by legacy optional flows; neither shape bypasses parsing of the
    actual directory response or the role authorization checks below.
    """

    deployment_proof: DirectoryDeploymentProof | None = None
    schema_proof_fingerprint: str | None = None
    visibility_proof_fingerprint: str | None = None
    schema: DirectorySnapshotSchema = DirectorySnapshotSchema.FLAT_DIRECTORY_V1
    organization: str | None = None

    def __repr__(self) -> str:
        return "DirectorySnapshotContract(<redacted>)"


@dataclass(frozen=True, repr=False)
class EffectiveRoleSnapshot:
    """Minimal immutable internal input for I11; no raw relations or credentials."""

    subject: str
    user_ref: StructuredUserRef
    effective_roles: tuple[RoleRef, ...]

    def __repr__(self) -> str:
        return "EffectiveRoleSnapshot(<redacted>)"


def _text(value: object, limit: int = 255) -> bool:
    try:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value.encode("utf-8")) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except UnicodeError:
        return False


def _contract(contract: DirectorySnapshotContract | None, organization: str) -> DirectorySnapshotContract:
    if not isinstance(contract, DirectorySnapshotContract) or contract.schema is not DirectorySnapshotSchema.FLAT_DIRECTORY_V1:
        _fail("contract_unknown")
    if contract.deployment_proof is None:
        if (
            contract.organization != organization
            or contract.schema_proof_fingerprint is not None
            or contract.visibility_proof_fingerprint is not None
        ):
            _fail("contract_unknown")
    elif (
        not isinstance(contract.deployment_proof, DirectoryDeploymentProof)
        or contract.deployment_proof.organization != organization
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (
                contract.deployment_proof.release_fingerprint,
                contract.deployment_proof.creator_proof_fingerprint,
                contract.deployment_proof.credential_proof_fingerprint,
                contract.schema_proof_fingerprint,
                contract.visibility_proof_fingerprint,
            )
        )
    ):
        _fail("contract_unknown")
    return contract


def _visibility(raw: object, organization: str) -> None:
    if not isinstance(raw, dict) or raw.get("owner") != "admin" or raw.get("name") != organization:
        _fail("organization_schema")
    if "accountItems" not in raw:
        _fail("visibility_unknown")
    items = raw["accountItems"]
    if items is None:
        return  # Only reached after the trusted fixed-release contract check.
    if not isinstance(items, list) or len(items) > MAX_RELATIONS:
        _fail("visibility_schema")
    for item in items:
        if (
            not isinstance(item, dict)
            or not _text(item.get("name"))
            or not item["name"].isascii()
            or type(item.get("visible")) is not bool
            or not _text(item.get("viewRule"))
        ):
            _fail("visibility_schema")
        field = item["name"].lower().replace(" ", "")
        if field in _VISIBLE_FIELDS and item["viewRule"] not in ("Public", "Self", "Admin"):
            _fail("visibility_hidden")


def _relations(raw: dict[str, Any], key: str, organization: str) -> frozenset[str]:
    if key not in raw:
        _fail("relations_missing")
    values = raw[key]
    if values is None:
        return frozenset()  # Field present and trusted release proves nil is empty.
    if not isinstance(values, list) or len(values) > MAX_RELATIONS:
        _fail("relations_schema")
    prefix = organization + "/"
    for value in values:
        if not _text(value, MAX_REF_BYTES) or not value.startswith(prefix):
            _fail("relation_boundary")
        if not _text(value[len(prefix) :]):
            _fail("relation_boundary")
    if len(set(values)) != len(values):
        _fail("relations_duplicate")
    return frozenset(values)


def compute_effective_roles(
    *,
    organization: str,
    verified_subject: str,
    online_user: VerifiedOnlineUser,
    raw_user: dict[str, Any],
    raw_roles: list[dict[str, Any]],
    raw_organization: dict[str, Any],
    contract: DirectorySnapshotContract | None = None,
) -> EffectiveRoleSnapshot:
    """Pure bounded validation and enabled-parent closure of complete raw input.

    Raw inputs come from the controlled gateway (which owns strict JSON and byte
    bounds). No SDK User/Role instance, pagination fragment, alias, native roles
    or JWT roles is accepted. The entire graph is validated before seed matching.
    I06 supplies online_user; this owner checks its raw snapshot consistency and
    parses groups. Only Role.roles references have a known full catalog for
    dangling checks. Other users/groups receive format+org checks, not a claim
    that unread directory entities exist. Releases requiring a group catalog or
    group enabled-state lookup cannot use this three-read contract.
    Disabled nodes stop grants; structural cycles/dangling refs still reject the
    complete snapshot. Depth counts enabled edges with seed depth zero and is
    checked on every enabled component, including unrelated/disconnected roles.
    """
    if not _text(organization) or not _text(verified_subject):
        _fail("identity_schema")
    _contract(contract, organization)
    _visibility(raw_organization, organization)
    if (
        not isinstance(online_user, VerifiedOnlineUser)
        or online_user.subject != verified_subject
        or not isinstance(online_user.user_ref, StructuredUserRef)
        or online_user.user_ref.owner != organization
        or not _text(online_user.user_ref.name)
        or not isinstance(raw_user, dict)
        or raw_user.get("owner") != online_user.user_ref.owner
        or raw_user.get("id") != online_user.subject
        or raw_user.get("name") != online_user.user_ref.name
        or raw_user.get("isForbidden") is not False
        or raw_user.get("isDeleted") is not False
    ):
        _fail("user_schema")
    groups = _relations(raw_user, "groups", organization)
    user_ref = online_user.user_ref
    full_user_ref = user_ref.owner + "/" + user_ref.name
    if not isinstance(raw_roles, list) or len(raw_roles) > MAX_NODES:
        _fail("nodes_limit")
    # Store only the graph-required projection. Full user/org objects are never
    # copied, logged, cached or returned, including password/LDAP/native flags.
    enabled: dict[str, bool] = {}
    children: dict[str, frozenset[str]] = {}
    seeds: set[str] = set()
    edge_count = 0
    for role in raw_roles:
        if (
            not isinstance(role, dict)
            or role.get("owner") != organization
            or not _text(role.get("name"))
            or type(role.get("isEnabled")) is not bool
        ):
            _fail("role_schema")
        ref = organization + "/" + role["name"]
        if ref in enabled:
            _fail("role_duplicate")
        users = _relations(role, "users", organization)
        role_groups = _relations(role, "groups", organization)
        child_refs = _relations(role, "roles", organization)
        edge_count += len(child_refs)
        if edge_count > MAX_EDGES:
            _fail("edges_limit")
        enabled[ref] = role["isEnabled"]
        children[ref] = child_refs
        if enabled[ref] and (full_user_ref in users or groups & role_groups):
            seeds.add(ref)
    parents: dict[str, set[str]] = {ref: set() for ref in enabled}
    for parent, child_refs in children.items():
        for child in child_refs:
            if child not in enabled:
                _fail("role_dangling")
            parents[child].add(parent)
    # Kahn traversal avoids unbounded Python recursion, detects cycles throughout
    # the raw graph, and computes longest enabled path (not shortest BFS depth).
    outstanding = {ref: len(values) for ref, values in children.items()}
    pending = deque(ref for ref, count in outstanding.items() if count == 0)
    depth = dict.fromkeys(enabled, 0)
    effective = set(seeds)
    visited = 0
    while pending:
        child = pending.popleft()
        visited += 1
        for parent in parents[child]:
            if enabled[child] and enabled[parent]:
                depth[parent] = max(depth[parent], depth[child] + 1)
                if depth[parent] > MAX_DEPTH:
                    _fail("depth_limit")
                if child in effective:
                    effective.add(parent)
            outstanding[parent] -= 1
            if outstanding[parent] == 0:
                pending.append(parent)
    if visited != len(enabled):
        _fail("role_cycle")
    prefix_length = len(organization) + 1
    return EffectiveRoleSnapshot(
        verified_subject,
        user_ref,
        tuple(RoleRef(organization=organization, name=ref[prefix_length:]) for ref in sorted(effective)),
    )


class OwnedSnapshotLease(Protocol):
    """Already-acquired namespace+subject lease; implemented by CasdoorLeases."""

    def ensure_owned(self, *, renew: bool = False) -> None: ...


class OnlineRoleSnapshotLoader:
    """Fresh three-read coordinator sharing the existing callback operation.

    It owns no Session/Redis/generation, does not acquire or release the caller's
    lease, and never caches raw objects or results. Any failure halts the shared
    gateway operation. The caller must use the same operation/lease deadline and
    acquire its complete scope before calling load outside write transactions.
    """

    def __init__(
        self,
        operation: GatewayOperation,
        *,
        identity: VerifiedIDToken,
        claims_validator: ClaimsValidator,
        leases: OwnedSnapshotLease,
        credential_strategy: DirectoryCredentialStrategy | None = None,
        contract: DirectorySnapshotContract | None = None,
    ) -> None:
        self._operation = operation
        self._identity = identity
        self._claims_validator = claims_validator
        self._leases = leases
        self._contract = contract
        self._directory = CasdoorDirectoryGateway(
            operation,
            verified_subject=identity.subject if isinstance(identity, VerifiedIDToken) else "",
            credential_strategy=credential_strategy,
        )

    def _guard(self) -> None:
        op = self._operation
        try:
            op._check()
        except GatewayError as error:
            op.fail(
                "snapshot_operation_stopped",
                CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN,
                termination_confirmed=error.termination_confirmed,
            )
        try:
            self._leases.ensure_owned()
        except Exception:
            op.fail("snapshot_ownership_unknown", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        if time.monotonic() >= op.deadline:
            op.fail("snapshot_deadline", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)

    def load(self) -> EffectiveRoleSnapshot:
        op = self._operation
        self._guard()
        try:
            config = op.config
            identity = self._identity
            if (
                not isinstance(identity, VerifiedIDToken)
                or not isinstance(self._claims_validator, ClaimsValidator)
                or identity.issuer != config.expected_issuer
                or identity.client_id != config.client_id
                or not _text(identity.subject)
            ):
                _fail("identity_schema")
            contract = _contract(self._contract, config.organization)
            if contract.deployment_proof is None:
                # The ordinary Community adapter is selected by server code and
                # carries no external reviewer manifest. It remains bound to the
                # configured organization and must pass every live graph check.
                if self._directory.deployment_proof is not None:
                    _fail("contract_binding")
            elif (
                not contract.deployment_proof.matches(config)
                or self._directory.deployment_proof != contract.deployment_proof
            ):
                _fail("contract_binding")
            # Visibility is read first so hidden/unknown policy stops before any
            # role/user fetch. Each read receives the SAME operation deadline.
            self._guard()
            raw_organization = self._directory.get_organization_visibility()
            self._guard()
            _visibility(raw_organization, config.organization)
            self._guard()
            raw_user = self._directory.get_verified_user()
            self._guard()
            online_user = self._claims_validator.verify_online_user(raw_user, identity=identity)
            _relations(raw_user, "groups", config.organization)
            self._guard()
            raw_roles = self._directory.get_complete_roles()
            self._guard()
            snapshot = compute_effective_roles(
                organization=config.organization,
                verified_subject=identity.subject,
                online_user=online_user,
                raw_user=raw_user,
                raw_roles=raw_roles,
                raw_organization=raw_organization,
                contract=contract,
            )
            self._guard()
            return snapshot
        except (RoleSnapshotError, ClaimsError) as error:
            op.fail(error.reason, error.code)
        except GatewayError as error:
            # GatewayOperation's shared deadline latch also serves exchange and
            # UserInfo, so its generic late-result check may use provider code.
            # All directory unknowns must retain the graph's stable public code.
            if error.code not in (CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN, CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED):
                op.fail(
                    error.reason,
                    CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN,
                    termination_confirmed=error.termination_confirmed,
                )
            raise
