"""Bounded, tracked SQL cleanup diagnostics; never storage or worker authority.

The authenticated self reader owns every SELECT and its final recheck. These facts
retain the original producer's closed completion evidence, without probing current
physical storage, issuing a grant, taking a lock, or reconstructing a live permit.
"""

from uuid import UUID

import sqlalchemy as sa
from models.casdoor_avatar_file_guard_extend import CasdoorAvatarFileGuardExtend as Guard
from models.casdoor_extend import CasdoorAuditExtend as Audit

from repositories.casdoor_audit_repository_extend import (
    _AVATAR_PRE_STORAGE_SUMMARY_BYTES,
    _AVATAR_RESERVATION_FENCE_BYTES,
    _AVATAR_RETRY_SUMMARY_BYTES,
    _avatar_cleanup_summary,
    _reservation_fence_summary,
)
from repositories.casdoor_avatar_repository_extend import (
    _cleanup_afterimage,
    _cleanup_beforeimage,
    _reservation_row_hash,
    _worker_state,
    _worker_time,
)


def _audit(reader, action, correlation, maximum):
    """Globally detect duplicates before bounded full-row hydration and recheck."""
    predicate = (Audit.action == action, Audit.correlation_id == correlation)
    headers = []
    columns = []
    for column in Audit.__table__.columns:
        if isinstance(column.type, (sa.String, sa.Text)) or column.name == "id" or column.name.endswith("_id"):
            cap = maximum if column.name == "summary_json" else 64
            bound = reader._length(column).between(0, cap)
            headers.append(sa.cast(sa.or_(column.is_(None), bound) if column.nullable else bound, sa.Boolean))
            columns.append(reader.bounded(column, cap))
        else:
            columns.append(column)
    sizes = reader.read(sa.select(*headers).where(*predicate).limit(2))
    if len(sizes) != 1 or not all(value is True for value in sizes[0].values()):
        raise ValueError("invalid cleanup observation audit")
    rows = reader.read(sa.select(*columns).where(*predicate).limit(2))
    if len(rows) != 1:
        raise ValueError("missing cleanup observation audit")
    row = dict(rows[0])
    if type(row["id"]) is not str or str(UUID(row["id"])) != row["id"]:
        raise ValueError("invalid cleanup observation audit id")
    _worker_time(row["created_at"])
    return row


def _bound_audit(audit, refs, result):
    if (
        any(audit[key] != refs[key] for key in ("namespace_id", "revision_id", "identity_id", "account_id"))
        or audit["actor_account_id"] is not None
        or audit["result_code"] != result
    ):
        raise ValueError("foreign cleanup observation audit")


def completed_cleanup(reader, row, data, revision, *, current):
    """Prove exact complete terminal, file guard, original fence and count lineage."""
    if type(current) is not bool or row["account_id"] != reader.account_id or data["cleanup_state"] != "complete":
        raise ValueError("cleanup completion is unavailable")
    last = data["reservations"][-1]
    file_id = last["file_id"]
    audit = _audit(reader, "avatar_cleanup", file_id, 8192)
    summary = _avatar_cleanup_summary(audit["summary_json"])
    before = _cleanup_beforeimage(row, data, audit)
    before_data = _worker_state(before)
    if (
        before["operation_state"] != "in_flight"
        or before["termination_state"] != "unconfirmed"
        or summary["count"] != row["attempt_count"]
        or summary["proof_ref"] != row["proof_ref"]
    ):
        raise ValueError("invalid cleanup beforeimage")
    refs = {key: row[key] for key in ("namespace_id", "revision_id", "identity_id", "account_id")}
    refs.update(
        intent_id=row["id"],
        attempt_id=row["attempt_id"],
        file_id=file_id,
        tenant_id=revision["default_workspace_id"],
        claim_correlation_id=data["correlation_id"],
    )
    if summary["references"] != refs:
        raise ValueError("invalid cleanup references")
    _bound_audit(audit, refs, "pending")
    fence_audit = _audit(reader, "avatar_reservation_fence", file_id, _AVATAR_RESERVATION_FENCE_BYTES)
    fence = _reservation_fence_summary(fence_audit["summary_json"])
    _bound_audit(fence_audit, refs, "reserved")
    if (
        fence["references"] != refs
        or fence["count"] != row["attempt_count"]
        or fence["generation"] != row["generation"]
        or fence["fence_epoch"] != row["fence_epoch"]
        or fence["guard_version"] != summary["guard_version"]
        or fence["claim_intent_sha256"] != _reservation_row_hash(before)
        or fence["reservation_sha256"] != _reservation_row_hash(before_data["reservations"])
        or summary["fence_sha256"] != _reservation_row_hash(fence_audit)
    ):
        raise ValueError("invalid cleanup reservation fence")
    for item in fence["lineage"]:
        action = item["action"]
        retry = action in ("avatar_retry", "avatar_retry_claim")
        original = _audit(
            reader,
            action,
            row["id"] if retry else data["correlation_id"],
            _AVATAR_RETRY_SUMMARY_BYTES if retry else _AVATAR_PRE_STORAGE_SUMMARY_BYTES,
        )
        if original["id"] != item["id"] or _reservation_row_hash(original) != item["sha256"]:
            raise ValueError("invalid original cleanup lineage")
    if row["attempt_count"] == 2:
        # Today's active scope is needed for a current record. An immutable
        # historical diagnostic must survive the original disable's FENCING
        # transition; its exact original reservation/retry proof still applies.
        if current:
            reader._avatar_retry_scope(before)
        reader._avatar_retry_lineage(before, before_data)
    pending = _cleanup_afterimage(before, before_data, row["proof_ref"], audit["created_at"], complete=False)
    if summary["terminal_sha256"] != _reservation_row_hash(pending):
        raise ValueError("invalid cleanup pending terminal")
    completion = _audit(reader, "avatar_cleanup_complete", file_id, 8192)
    final = _avatar_cleanup_summary(completion["summary_json"], complete=True)
    _bound_audit(completion, refs, "complete")
    expected = _cleanup_afterimage(
        pending, _worker_state(pending), row["proof_ref"], completion["created_at"], complete=True
    )
    if (
        expected != row
        or final["references"] != refs
        or final["proof_ref"] != row["proof_ref"]
        or final["cleanup_sha256"] != _reservation_row_hash(audit)
        or final["terminal_sha256"] != _reservation_row_hash(row)
    ):
        raise ValueError("invalid cleanup completion")
    guards = reader.read(
        sa.select(
            *[
                reader.bounded(column, 64)
                if isinstance(column.type, (sa.String, sa.Text)) or column.name.endswith("_id")
                else column
                for column in Guard.__table__.columns
            ]
        )
        .where(Guard.file_id == file_id)
        .limit(2)
    )
    if (
        len(guards) != 1
        or guards[0]["file_id"] != file_id
        or guards[0]["intent_id"] != row["id"]
        or guards[0]["attempt_id"] != row["attempt_id"]
        or guards[0]["stage"] != "cleanup_complete"
        or type(guards[0]["version"]) is not int
        or guards[0]["version"] != summary["guard_version"] + 2
        or guards[0]["version"] > 9007199254740991
    ):
        raise ValueError("invalid cleanup complete guard")
    return True
