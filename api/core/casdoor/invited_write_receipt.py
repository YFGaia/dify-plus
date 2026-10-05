"""Closed, privacy-safe invited LOCAL write facts; never an access capability.

Parsing proves only shape and canonical encoding. The producing SQL owner must
separately prove the actual P3L/B3 facts; the audit writer requires its live guard.
The projection and digest are pure so a future reader can reuse their exact bytes.
"""

import hashlib
import json
import operator
import re
from dataclasses import dataclass
from uuid import UUID

from core.casdoor.ownership import OwnershipDecision
from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorFinalizationState, CasdoorMembershipOwnership, CasdoorMembershipSource

MAX_RECEIPT_BYTES = 32 * 1024
MAX_COUNTER = 2**63 - 1
MAX_PROJECTION_ROWS = 2048
RECEIPT_KIND = "invited_local_membership_write"
POSTWRITE_DOMAIN = "dify-plus:casdoor:invited-local-membership-postwrite:v1"
REFERENCE_FIELDS = (
    "operation_id",
    "issuance_id",
    "integration_id",
    "namespace_id",
    "revision_id",
    "identity_id",
    "account_id",
    "workspace_id",
    "invitation_join_id",
)
RESULT_FIELDS = (
    "workspace_id",
    "ownership_decision",
    "intent_barrier",
    "outcome",
    "join_id",
    "membership_id",
    "current_role",
    "membership_created",
    "role_changed",
    "metadata_changed",
    "membership_regranted",
)
IDENTITY_FIELDS = ("id", "namespace_id", "account_id", "sync_generation")
JOIN_FIELDS = ("id", "account_id", "tenant_id", "role")
MEMBERSHIP_FIELDS = (
    "id",
    "namespace_id",
    "identity_id",
    "account_id",
    "workspace_id",
    "join_id",
    "ownership",
    "ownership_epoch",
    "source",
    "desired_generation",
    "revision_id",
    "finalization",
    "tombstone",
)
_TOP_FIELDS = (
    "schema_version",
    "receipt_kind",
    "references",
    "generation_before",
    "generation_after",
    "fence_epoch",
    "scope_digest",
    "completion_proof_ref",
    "payload_digest",
    "results",
    "postwrite_sha256",
)


def _require(condition):
    if not condition:
        raise ValueError("invited_write_receipt_invalid")


def _closed(value, keys):
    _require(type(value) is dict and len(value) == len(keys) and set(value) == set(keys))


def _uuid(value):
    _require(type(value) is str and len(value) == 36)
    try:
        _require(str(UUID(value)) == value)
    except (ValueError, AttributeError):
        raise ValueError("invited_write_receipt_invalid") from None


def _counter(value):
    _require(type(value) is int and 0 <= value <= MAX_COUNTER)


def _digest(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _enum(value, enum):
    _require(type(value) is str and value in tuple(item.value for item in enum))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _proof_join(value):
    _require(type(value) is str and value.isascii() and 0 < len(value) <= 128)
    parts = value.split(":")
    _require(len(parts) == 5 and parts[0] == "v1" and parts[2] in ("0", "1"))
    _uuid(parts[1])
    _require(re.fullmatch(r"[1-9][0-9]{0,15}", parts[3]) is not None and int(parts[3]) <= 2**53 - 1)
    _digest(parts[4])
    return parts[1]


def _validate(value):
    _require(type(value) is dict and type(value.get("schema_version")) is int)
    version = value["schema_version"]
    _require(version in (1, 2))
    _closed(value, _TOP_FIELDS if version == 1 else (*_TOP_FIELDS, "withdrawals"))
    _require(type(value["receipt_kind"]) is str and value["receipt_kind"] == RECEIPT_KIND)
    refs = value["references"]
    _closed(refs, REFERENCE_FIELDS)
    for item in refs.values():
        _uuid(item)
    for name in ("generation_before", "generation_after", "fence_epoch"):
        _counter(value[name])
    _require(value["generation_after"] == value["generation_before"] + 1)
    for name in ("scope_digest", "payload_digest", "postwrite_sha256"):
        _digest(value[name])
    _require(_proof_join(value["completion_proof_ref"]) == refs["invitation_join_id"])
    results = value["results"]
    _require(type(results) is list and 1 <= len(results) <= 100)
    workspaces = []
    for result in results:
        _closed(result, RESULT_FIELDS)
        _uuid(result["workspace_id"])
        _uuid(result["join_id"])
        if result["membership_id"] is not None:
            _uuid(result["membership_id"])
        _enum(result["ownership_decision"], OwnershipDecision)
        _enum(result["current_role"], TenantAccountRole)
        _require(type(result["intent_barrier"]) is str and result["intent_barrier"] == "clear")
        _require(type(result["outcome"]) is str and result["outcome"] in ("applied", "noop", "preserved"))
        for name in ("membership_created", "role_changed", "metadata_changed", "membership_regranted"):
            _require(type(result[name]) is bool)
        if version == 1:
            _require(result["membership_regranted"] is False)
        elif result["membership_regranted"]:
            _require(result["ownership_decision"] == "controlled_withdrawn")
            _require(result["membership_id"] is not None and not result["membership_created"])
            _require(result["outcome"] == "noop" and not result["role_changed"] and not result["metadata_changed"])
        if result["workspace_id"] == refs["workspace_id"]:
            _require(result["join_id"] == refs["invitation_join_id"])
        workspaces.append(result["workspace_id"])
    _require(workspaces == sorted(set(workspaces)))
    if version == 2:
        from core.casdoor.invited_controlled_write_receipt import validate_controlled_withdrawals

        validate_controlled_withdrawals(value)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("invited_write_receipt_invalid")


def _parse(raw):
    _require(type(raw) is str)
    try:
        _require(len(raw.encode("utf-8")) <= MAX_RECEIPT_BYTES)
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant)
        _validate(value)
        _require(_canonical(value) == raw)
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("invited_write_receipt_invalid") from None


@dataclass(frozen=True, slots=True, repr=False)
class InvitedLocalWriteReceipt:
    """Immutable canonical bytes only; constructing/parsing confers no authority."""

    canonical_json: str

    def __post_init__(self):
        _parse(self.canonical_json)

    @classmethod
    def from_values(cls, value):
        _validate(value)
        return cls(_canonical(value))

    def values(self):
        """Return a fresh closed projection, never mutable receipt internals."""
        return _parse(self.canonical_json)


def postwrite_projection(*, identity, joins, memberships):
    """Whitelist, validate and order bounded primitive observations, without PII."""
    _closed(identity, IDENTITY_FIELDS)
    for name in ("id", "namespace_id", "account_id"):
        _uuid(identity[name])
    _counter(identity["sync_generation"])
    _require(type(joins) is tuple and len(joins) <= MAX_PROJECTION_ROWS)
    _require(type(memberships) is tuple and len(memberships) <= MAX_PROJECTION_ROWS)
    for row in joins:
        _closed(row, JOIN_FIELDS)
        for name in ("id", "account_id", "tenant_id"):
            _uuid(row[name])
        _enum(row["role"], TenantAccountRole)
        _require(row["account_id"] == identity["account_id"])
    for row in memberships:
        _closed(row, MEMBERSHIP_FIELDS)
        for name in ("id", "namespace_id", "identity_id", "account_id", "workspace_id", "revision_id"):
            _uuid(row[name])
        if row["join_id"] is not None:
            _uuid(row["join_id"])
        _enum(row["ownership"], CasdoorMembershipOwnership)
        _enum(row["source"], CasdoorMembershipSource)
        _enum(row["finalization"], CasdoorFinalizationState)
        _counter(row["ownership_epoch"])
        _counter(row["desired_generation"])
        _require(type(row["tombstone"]) is bool and row["account_id"] == identity["account_id"])
    _require(len({row["id"] for row in joins}) == len(joins))
    _require(len({row["id"] for row in memberships}) == len(memberships))
    return {
        "identity": dict(identity),
        "joins": [dict(row) for row in sorted(joins, key=operator.itemgetter("tenant_id", "id"))],
        "memberships": [
            dict(row) for row in sorted(memberships, key=operator.itemgetter("namespace_id", "workspace_id", "id"))
        ],
    }


def postwrite_sha256(operation_id, *, identity, joins, memberships):
    _uuid(operation_id)
    envelope = {
        "domain": POSTWRITE_DOMAIN,
        "operation_id": operation_id,
        "postwrite": postwrite_projection(identity=identity, joins=joins, memberships=memberships),
    }
    return hashlib.sha256(_canonical(envelope).encode("utf-8")).hexdigest()
