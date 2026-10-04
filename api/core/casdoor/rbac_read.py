"""Pure raw RBAC candidates; no authenticated/fresh/visible read is issued here.

The only selected contract is an offline strict subset of current enterprise
DTO/fixtures. It does not enable any deployment. Top-level workspace scope is
absent in the actual endpoints: the UUID argument only binds a candidate. I13-B2
must prove release, principal, full visibility, HTTP scope and freshness before
issuing a trusted handoff. These public dataclasses are never that proof.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from models.account import TenantAccountRole

from core.casdoor.configuration import TargetRole
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.ownership import MAX_MEMBER_ROLES, MAX_PERMISSION_KEYS, MemberRole, canonical_roles

MAX_MEMBER_BYTES = 256 * 1024
MAX_PAGE_BYTES = 256 * 1024
MAX_CATALOG_BYTES = 512 * 1024
MAX_CATALOG_PAGES = 32
MAX_PER_PAGE = 128
MAX_METADATA_BYTES = 2048


class RawRbacContract(StrEnum):
    """Candidate fixture shape only, not server release/authorization evidence."""

    OFFLINE_FIXTURE_V1 = "offline_fixture_v1"


class RbacCandidateError(ValueError):
    """Fixed safe pending code, without response text or raw identifiers."""

    code = CasdoorErrorCode.AUTHORIZATION_PENDING

    def __init__(self) -> None:
        super().__init__(self.code.value)


@dataclass(frozen=True)
class MemberRolesCandidate:
    contract: RawRbacContract
    workspace_id: UUID
    account_id: UUID
    roles: tuple[MemberRole, ...]


@dataclass(frozen=True)
class RoleCatalogCandidate:
    contract: RawRbacContract
    workspace_id: UUID
    roles: tuple[MemberRole, ...]
    page_count: int
    total_count: int


@dataclass(frozen=True)
class DesiredBuiltinCandidate:
    """Exact singleton desired set; not a trusted mapping BuiltinResolution."""

    contract: RawRbacContract
    workspace_id: UUID
    target_role: TargetRole
    roles: tuple[MemberRole, ...]
    join_role: TenantAccountRole = TenantAccountRole.NORMAL


def _require_contract(contract: RawRbacContract | None, workspace_id: UUID) -> None:
    if contract is not RawRbacContract.OFFLINE_FIXTURE_V1 or not isinstance(workspace_id, UUID):
        raise RbacCandidateError()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RbacCandidateError()
        result[key] = value
    return result


def _reject_number(value: str) -> object:
    # No supported field is a float. This also rejects NaN/Infinity/1e999.
    raise RbacCandidateError()


def _check_bytes(raw: bytes, limit: int) -> None:
    if type(raw) is not bytes or not raw or len(raw) > limit:
        raise RbacCandidateError()


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_number,
            parse_float=_reject_number,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise RbacCandidateError() from None
    if type(value) is not dict:
        raise RbacCandidateError()
    return value


def _fields(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise RbacCandidateError()
    return value


def _metadata(value: object, *, empty: bool) -> None:
    try:
        valid = (
            type(value) is str
            and (empty or bool(value))
            and len(value.encode("utf-8")) <= MAX_METADATA_BYTES
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise RbacCandidateError()


def _roles(value: object, workspace_id: UUID, *, limit: int) -> tuple[MemberRole, ...]:
    if type(value) is not list or len(value) > limit:
        raise RbacCandidateError()
    roles = []
    permissions = 0
    for item in value:
        role = _fields(
            item,
            {"id", "tenant_id", "type", "name", "category", "role_tag", "is_builtin", "permission_keys"},
            {"description"},
        )
        if role["tenant_id"] != str(workspace_id) or role["type"] != "workspace":
            raise RbacCandidateError()
        _metadata(role["name"], empty=False)
        if "description" in role:
            _metadata(role["description"], empty=True)
        keys = role["permission_keys"]
        if type(keys) is not list or len(keys) > MAX_PERMISSION_KEYS:
            raise RbacCandidateError()
        permissions += len(keys)
        if permissions > MAX_PERMISSION_KEYS:
            raise RbacCandidateError()
        roles.append(MemberRole(role["id"], role["is_builtin"], role["category"], role["role_tag"], tuple(keys)))
    return _canonical(tuple(roles))


def _canonical(roles: tuple[MemberRole, ...]) -> tuple[MemberRole, ...]:
    try:
        return canonical_roles(roles)
    except ValueError:
        raise RbacCandidateError() from None


def decode_member_candidate(
    raw: bytes,
    *,
    workspace_id: UUID,
    account_id: UUID,
    contract: RawRbacContract | None = None,
) -> MemberRolesCandidate:
    """Explicit [] is a fixture full-set candidate, never visibility/freshness."""
    _require_contract(contract, workspace_id)
    if not isinstance(account_id, UUID):
        raise RbacCandidateError()
    _check_bytes(raw, MAX_MEMBER_BYTES)
    envelope = _fields(_decode(raw), {"account_id", "roles"})
    if envelope["account_id"] != str(account_id):
        raise RbacCandidateError()
    roles = _roles(envelope["roles"], workspace_id, limit=MAX_MEMBER_ROLES)
    return MemberRolesCandidate(RawRbacContract.OFFLINE_FIXTURE_V1, workspace_id, account_id, roles)


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RbacCandidateError()
    return value


def decode_catalog_candidate(
    pages: tuple[bytes, ...], *, workspace_id: UUID, contract: RawRbacContract | None = None
) -> RoleCatalogCandidate:
    """Assemble exactly all ordered pages; no first-page/partial fallback.

    Bound every body and aggregate before parsing even the first JSON body.
    Count/contiguity proves only this fixture envelope's internal consistency.
    It cannot prove a real server returned every role the principal must see.
    """
    _require_contract(contract, workspace_id)
    if type(pages) is not tuple or not 1 <= len(pages) <= MAX_CATALOG_PAGES:
        raise RbacCandidateError()
    for raw in pages:
        _check_bytes(raw, MAX_PAGE_BYTES)
    if sum(map(len, pages)) > MAX_CATALOG_BYTES:
        raise RbacCandidateError()
    expected = None
    roles: tuple[MemberRole, ...] = ()
    for index, raw in enumerate(pages, 1):
        envelope = _fields(_decode(raw), {"data", "pagination"})
        pagination = _fields(envelope["pagination"], {"total_count", "per_page", "current_page", "total_pages"})
        total = _integer(pagination["total_count"], minimum=0, maximum=MAX_MEMBER_ROLES)
        per_page = _integer(pagination["per_page"], minimum=1, maximum=MAX_PER_PAGE)
        current = _integer(pagination["current_page"], minimum=1, maximum=MAX_CATALOG_PAGES)
        count = _integer(pagination["total_pages"], minimum=1, maximum=MAX_CATALOG_PAGES)
        signature = (total, per_page, count)
        if (
            current != index
            or count != len(pages)
            or count != max(1, (total + per_page - 1) // per_page)
            or (expected is not None and signature != expected)
        ):
            raise RbacCandidateError()
        expected = signature
        page_roles = _roles(envelope["data"], workspace_id, limit=per_page)
        if len(page_roles) != min(per_page, max(0, total - (index - 1) * per_page)):
            raise RbacCandidateError()
        # Reuse the single authorization metadata/budget owner across pages,
        # including cross-page duplicates and aggregate permission/scalar bytes.
        roles = _canonical(roles + page_roles)
    if expected is None or len(roles) != expected[0]:
        raise RbacCandidateError()
    return RoleCatalogCandidate(RawRbacContract.OFFLINE_FIXTURE_V1, workspace_id, roles, len(pages), expected[0])


def desired_builtin_candidate(catalog: RoleCatalogCandidate, target_role: TargetRole) -> DesiredBuiltinCandidate:
    """Unique exact admin/editor/normal only; absent/ambiguous stays pending."""
    if not isinstance(catalog, RoleCatalogCandidate) or type(target_role) is not str:
        raise RbacCandidateError()
    _require_contract(catalog.contract, catalog.workspace_id)
    if target_role not in ("admin", "editor", "normal"):
        raise RbacCandidateError()
    roles = _canonical(catalog.roles)
    if (
        type(catalog.total_count) is not int
        or catalog.total_count != len(roles)
        or type(catalog.page_count) is not int
        or not 1 <= catalog.page_count <= MAX_CATALOG_PAGES
    ):
        raise RbacCandidateError()
    matches = tuple(
        role
        for role in roles
        if role.is_builtin and role.category == "global_system_default" and role.role_tag == target_role
    )
    if len(matches) != 1 or len(matches[0].role_id.encode("utf-8")) > 255:
        raise RbacCandidateError()
    return DesiredBuiltinCandidate(catalog.contract, catalog.workspace_id, target_role, matches)
