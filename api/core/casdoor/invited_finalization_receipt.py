"""Closed invited LOCAL finalization facts; parsing confers no authority.

Both hashes bind the operation UUID and a fixed versioned domain. Full history
rows are hashed, never stored in the receipt. These projections are shared with
a future independent F2 reader; neither a hash nor this DTO authorizes retry.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from core.casdoor.invited_write_receipt import REFERENCE_FIELDS
from core.casdoor.ownership import parse_role_baseline_json

MAX_RECEIPT_BYTES = 32 * 1024
MAX_ROWS = 100
MAX_TEXT_BYTES = 16384
RECEIPT_KIND = "invited_local_membership_finalization"
MODE = "community_rbac_off_local"
PLAN_DOMAIN = "dify-plus:casdoor:invited-local-finalization-plan:v1"
ROWS_DOMAIN = "dify-plus:casdoor:invited-local-finalization-rows:v1"
FULL_HISTORY_FIELDS = (
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
    "last_applied_roles_json",
    "last_applied_fingerprint",
    "desired_roles_json",
    "baseline_json",
    "finalization",
    "tombstone",
    "created_at",
    "updated_at",
)
TOP_FIELDS = (
    "schema_version",
    "receipt_kind",
    "mode",
    "references",
    "generation",
    "fence_epoch",
    "write_receipt_sha256",
    "before_postwrite_sha256",
    "plan_sha256",
    "finalized_ids",
    "finalized_rows_sha256",
    "postwrite_sha256",
)
PLAN_FIELDS = ("workspace_id", "target_role", "builtin_id", "reason")
REASONS = ("role_mapping", "default_normal_fallback")


def _require(condition):
    if not condition:
        raise ValueError("invited_finalization_receipt_invalid")


def _closed(value, fields):
    _require(type(value) is dict and set(value) == set(fields))


def _uuid(value):
    _require(type(value) is str and len(value) == 36)
    _require(str(UUID(value)) == value)


def _counter(value):
    _require(type(value) is int and 0 <= value <= 2**63 - 1)


def _digest(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _invalid(_value):
    raise ValueError("invited_finalization_receipt_invalid")


def _json(raw, limit):
    _require(type(raw) is str and len(raw.encode("utf-8")) <= limit)
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid)
    _require(canonical(value) == raw)
    return value


def _validate(value):
    _closed(value, TOP_FIELDS)
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    _require(value["receipt_kind"] == RECEIPT_KIND and value["mode"] == MODE)
    _closed(value["references"], REFERENCE_FIELDS)
    for item in value["references"].values():
        _uuid(item)
    for name in ("generation", "fence_epoch"):
        _counter(value[name])
    _require(value["generation"] >= 1)
    for name in (
        "write_receipt_sha256",
        "before_postwrite_sha256",
        "plan_sha256",
        "finalized_rows_sha256",
        "postwrite_sha256",
    ):
        _digest(value[name])
    ids = value["finalized_ids"]
    _require(type(ids) is list and len(ids) <= MAX_ROWS)
    for item in ids:
        _uuid(item)
    _require(ids == sorted(set(ids)))


@dataclass(frozen=True, slots=True, repr=False)
class InvitedLocalFinalizationReceipt:
    """Immutable canonical bytes only; no admission, session or write capability."""

    canonical_json: str

    def __post_init__(self):
        self.values()

    @classmethod
    def from_values(cls, value):
        _validate(value)
        return cls(canonical(value))

    def values(self):
        try:
            value = _json(self.canonical_json, MAX_RECEIPT_BYTES)
            _validate(value)
            return value
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError("invited_finalization_receipt_invalid") from None


def mapping_plan_projection(targets):
    """Closed ordered LOCAL target projection; effective roles/claims excluded."""
    _require(type(targets) is tuple and 1 <= len(targets) <= MAX_ROWS)
    for row in targets:
        _closed(row, PLAN_FIELDS)
        _uuid(row["workspace_id"])
        _require(type(row["target_role"]) is str and row["target_role"] in ("admin", "editor", "normal"))
        _require(type(row["builtin_id"]) is str and row["builtin_id"] == row["target_role"])
        _require(type(row["reason"]) is str and row["reason"] in REASONS)
    ids = [row["workspace_id"] for row in targets]
    _require(ids == sorted(set(ids)))
    return [dict(row) for row in targets]


def _time(value):
    _require(type(value) is datetime and value.tzinfo is None)
    return value.isoformat(timespec="microseconds") + "Z"


def finalized_rows_projection(rows):
    """Every current history column, exact canonical role text, UTC microseconds.

    Input rows contain primitive enum values and naive UTC datetimes, matching
    the model. A schema change fails closed until this explicit version changes.
    """
    _require(type(rows) is tuple and len(rows) <= MAX_ROWS)
    result = []
    for row in rows:
        _closed(row, FULL_HISTORY_FIELDS)
        for field in ("id", "namespace_id", "identity_id", "account_id", "workspace_id", "join_id", "revision_id"):
            _uuid(row[field])
        for field in ("ownership_epoch", "desired_generation"):
            _counter(row[field])
        _require(row["ownership"] == "managed" and row["finalization"] == "finalized")
        _require(row["source"] in ("mapping", "fallback") and row["tombstone"] is False)
        _digest(row["last_applied_fingerprint"])
        for field in ("baseline_json", "last_applied_roles_json"):
            _json(row[field], MAX_TEXT_BYTES)
            parse_role_baseline_json(row[field])
        desired = _json(row["desired_roles_json"], MAX_TEXT_BYTES)
        _require(type(desired) is dict)
        if desired.get("schema_version") == 2:
            from core.casdoor.ownership import (
                MembershipBackend,
                MembershipObservation,
                parse_local_withdrawal_json,
                role_baseline_json,
                roles_fingerprint,
            )

            marker = parse_local_withdrawal_json(row["desired_roles_json"])
            _require(marker["removed_join_id"] == row["join_id"])
            _require(marker["withdrawal_epoch"] == row["ownership_epoch"])
            _require(marker["withdrawal_generation"] == row["desired_generation"])
            observation = MembershipObservation(
                UUID(row["workspace_id"]), UUID(row["account_id"]), None, None, MembershipBackend.LOCAL
            )
            _require(row["last_applied_roles_json"] == role_baseline_json(observation))
            _require(row["last_applied_fingerprint"] == roles_fingerprint(observation))
        else:
            _closed(
                desired, ("schema_version", "backend", "target_role", "builtin_id", "role_ids", "reason", "fence_epoch")
            )
            _require(type(desired["schema_version"]) is int and desired["schema_version"] == 1)
            _require(desired["backend"] == "local" and desired["target_role"] in ("admin", "editor", "normal"))
            _require(
                desired["builtin_id"] == desired["target_role"] and desired["role_ids"] == [desired["target_role"]]
            )
            _require(desired["reason"] in REASONS)
            _counter(desired["fence_epoch"])
        item = dict(row, created_at=_time(row["created_at"]), updated_at=_time(row["updated_at"]))
        _require(row["created_at"] <= row["updated_at"])
        result.append(item)
    _require(len({row["id"] for row in result}) == len(result))
    return sorted(result, key=lambda row: row["id"])


def _hash(domain, operation_id, key, projection):
    _uuid(operation_id)
    envelope = {"domain": domain, "operation_id": operation_id, key: projection}
    return hashlib.sha256(canonical(envelope).encode("utf-8")).hexdigest()


def mapping_plan_sha256(operation_id, targets):
    return _hash(PLAN_DOMAIN, operation_id, "targets", mapping_plan_projection(targets))


def finalized_rows_sha256(operation_id, rows):
    return _hash(ROWS_DOMAIN, operation_id, "rows", finalized_rows_projection(rows))
