"""Pure OFFLINE_FIXTURE_V1 resource-toggle candidates, never remote authority.

Inventory scalars establish candidate consistency only. B2B must reconstruct the
exact subset from a real typed local inventory and verify its query provenance.
This module neither imports that repository nor proves DB/tenant/principal scope.

Synthetic grammar: {data: [rows]}; rows require resource_type, resource_id and an
actual boolean automatic_include_workspace_members. Optional account_ids may be
missing, null or a unique canonical UUID list. Optional rbac_whitelist_scope OR
scope may be missing, null or a bounded string (including empty); both aliases
are rejected. These names come from ResourceWhitelistConfigItem, but no live
backend schema is established. Metadata is validated then discarded, never ACL
baseline evidence. No DTO defaults, completeness, freshness or absence issuance.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from services.enterprise.rbac_service import RBACResourceType

MAX_INVENTORY = 4096
MAX_BATCH = 500
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_DEPTH = 8
MAX_STRING_BYTES = 2048
MAX_ROW_ACCOUNTS = 128
MAX_ACCOUNTS = 4096
_KIND_ORDER = (RBACResourceType.APP, RBACResourceType.DATASET, RBACResourceType.AGENT)
type ResourcePair = tuple[RBACResourceType, UUID]


class ResourceConfigContract(StrEnum):
    OFFLINE_FIXTURE_V1 = "offline_fixture_v1"


class ResourceConfigObservation(StrEnum):
    RETURNED_TRUE = "returned_true"
    RETURNED_FALSE = "returned_false"
    OMITTED_UNKNOWN = "omitted_unknown"


class ResourceConfigCandidateError(ValueError):
    def __init__(self) -> None:
        super().__init__("resource_config_candidate_invalid")


def _validate_pairs(values: tuple[ResourcePair, ...], limit: int) -> None:
    if type(values) is not tuple or len(values) > limit:
        raise ResourceConfigCandidateError()
    seen = set()
    for pair in values:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not RBACResourceType
            or pair[0] not in _KIND_ORDER
            or type(pair[1]) is not UUID
            or pair in seen
        ):
            raise ResourceConfigCandidateError()
        seen.add(pair)


def _body(pairs: tuple[ResourcePair, ...]) -> bytes:
    body = json.dumps(
        {"resources": [{"resource_type": kind.value, "resource_id": str(key)} for kind, key in pairs]},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ResourceConfigCandidateError()
    return body


@dataclass(frozen=True)
class ResourceConfigRequestCandidate:
    """Public synthetic context/request identity; not a trusted runtime receipt."""

    contract: ResourceConfigContract
    workspace_id: UUID
    context_id: UUID
    pairs: tuple[ResourcePair, ...]

    def __post_init__(self) -> None:
        if (
            self.contract is not ResourceConfigContract.OFFLINE_FIXTURE_V1
            or type(self.workspace_id) is not UUID
            or type(self.context_id) is not UUID
        ):
            raise ResourceConfigCandidateError()
        _validate_pairs(self.pairs, MAX_BATCH)
        if self.pairs != tuple(sorted(self.pairs, key=lambda pair: (_KIND_ORDER.index(pair[0]), pair[1].int))):
            raise ResourceConfigCandidateError()
        _body(self.pairs)

    @property
    def body(self) -> bytes:
        return _body(self.pairs)


@dataclass(frozen=True)
class ResourceConfigReadCandidate:
    request: ResourceConfigRequestCandidate
    observations: tuple[tuple[ResourcePair, ResourceConfigObservation], ...]
    validated_account_count: int = 0


def build_resource_config_request(
    *,
    contract: ResourceConfigContract,
    workspace_id: UUID,
    context_id: UUID,
    inventory: tuple[ResourcePair, ...],
    selected: tuple[ResourcePair, ...],
) -> ResourceConfigRequestCandidate:
    """Check scalar consistency, NOT inventory type/status/query provenance.

    Selection is emitted in original APP/DATASET/AGENT, ascending UUID order.
    Empty selection yields an empty request candidate, with no remote knowledge.
    The future reader must skip HTTP for it and independently check real inventory.
    """
    _validate_pairs(inventory, MAX_INVENTORY)
    _validate_pairs(selected, MAX_BATCH)
    if inventory != tuple(sorted(inventory, key=lambda pair: (_KIND_ORDER.index(pair[0]), pair[1].int))):
        raise ResourceConfigCandidateError()
    selected_set = set(selected)
    if not selected_set <= set(inventory):
        raise ResourceConfigCandidateError()
    return ResourceConfigRequestCandidate(
        contract, workspace_id, context_id, tuple(pair for pair in inventory if pair in selected_set)
    )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResourceConfigCandidateError()
        result[key] = value
    return result


def _no_number(value: str) -> object:
    raise ResourceConfigCandidateError()


def _preflight(raw: bytes) -> str:
    # Body budget precedes even UTF-8 decode; depth precedes recursive JSON decode.
    if type(raw) is not bytes or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ResourceConfigCandidateError()
    text = raw.decode("utf-8", errors="strict")
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ResourceConfigCandidateError()
        elif char in "]}":
            depth -= 1
    return text


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ResourceConfigCandidateError()
    parsed = UUID(value)
    if str(parsed) != value:
        raise ResourceConfigCandidateError()
    return parsed


def _metadata(row: dict) -> int:
    if "scope" in row and "rbac_whitelist_scope" in row:
        raise ResourceConfigCandidateError()
    scope = row.get("scope", row.get("rbac_whitelist_scope"))
    if scope is not None and (
        type(scope) is not str
        or len(scope.encode("utf-8")) > MAX_STRING_BYTES
        or any(ord(char) < 32 or ord(char) == 127 for char in scope)
    ):
        raise ResourceConfigCandidateError()
    accounts = row.get("account_ids")
    if accounts is None:
        return 0
    if type(accounts) is not list or len(accounts) > MAX_ROW_ACCOUNTS:
        raise ResourceConfigCandidateError()
    parsed = [_uuid(value) for value in accounts]
    if len(set(parsed)) != len(parsed):
        raise ResourceConfigCandidateError()
    return len(parsed)


def parse_resource_config(raw: bytes, *, request: ResourceConfigRequestCandidate) -> ResourceConfigReadCandidate:
    """All-or-nothing raw parse; omission remains unknown, including data=[]."""
    try:
        if type(request) is not ResourceConfigRequestCandidate:
            raise ResourceConfigCandidateError()
        request.__post_init__()
        value = json.loads(
            _preflight(raw),
            object_pairs_hook=_object,
            parse_int=_no_number,
            parse_float=_no_number,
            parse_constant=_no_number,
        )
        if type(value) is not dict or set(value) != {"data"} or type(value["data"]) is not list:
            raise ResourceConfigCandidateError()
        if len(value["data"]) > MAX_BATCH:
            raise ResourceConfigCandidateError()
        requested = set(request.pairs)
        returned = {}
        accounts = 0
        required = {"resource_type", "resource_id", "automatic_include_workspace_members"}
        optional = {"account_ids", "scope", "rbac_whitelist_scope"}
        for row in value["data"]:
            if type(row) is not dict or not required <= row.keys() or row.keys() - required - optional:
                raise ResourceConfigCandidateError()
            pair = (RBACResourceType(row["resource_type"]), _uuid(row["resource_id"]))
            toggle = row["automatic_include_workspace_members"]
            if pair not in requested or pair in returned or type(toggle) is not bool:
                raise ResourceConfigCandidateError()
            accounts += _metadata(row)
            if accounts > MAX_ACCOUNTS:
                raise ResourceConfigCandidateError()
            returned[pair] = (
                ResourceConfigObservation.RETURNED_TRUE if toggle else ResourceConfigObservation.RETURNED_FALSE
            )
        return ResourceConfigReadCandidate(
            request,
            tuple((pair, returned.get(pair, ResourceConfigObservation.OMITTED_UNKNOWN)) for pair in request.pairs),
            accounts,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, AttributeError):
        raise ResourceConfigCandidateError() from None
