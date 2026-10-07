"""Caller-owned Casdoor audit write; no commit, rollback, external I/O or locks.

Caller already holds all parent/scope locks for this batch. Session.flush() flushes
the entire Session, so reject unrelated pending state before issuing any SQL.
Append after business changes were explicitly flushed in an active caller
transaction. A failed flush leaves recovery/rollback exclusively to the caller.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.invited_write_receipt import (
    MAX_RECEIPT_BYTES,
    RECEIPT_KIND,
    InvitedLocalWriteReceipt,
)
from core.casdoor.request_safety import ProfileAuditEvent, SafetyEvent
from libs.datetime_utils import naive_utc_now
from models.casdoor_extend import CasdoorAuditExtend
from sqlalchemy.orm import Session, SessionTransactionOrigin


@dataclass(frozen=True, repr=False)
class _AvatarPendingAudit:
    namespace_id: UUID
    revision_id: UUID
    identity_id: UUID
    account_id: UUID
    intent_id: UUID
    correlation_id: UUID
    generation: int
    fence_epoch: int


@dataclass(frozen=True, repr=False)
class _AvatarAttemptAudit:
    namespace_id: UUID
    revision_id: UUID
    identity_id: UUID
    account_id: UUID
    intent_id: UUID
    correlation_id: UUID
    attempt_id: UUID
    file_id: UUID
    generation: int
    fence_epoch: int
    count: int
    action: str
    result: str
    reason: str


@dataclass(frozen=True, repr=False)
class _AvatarAttachmentAudit:
    namespace_id: UUID
    revision_id: UUID
    identity_id: UUID
    account_id: UUID
    intent_id: UUID
    correlation_id: UUID
    attempt_id: UUID
    file_id: UUID
    generation: int
    fence_epoch: int
    count: int


@dataclass(frozen=True, repr=False)
class _AvatarPreStorageAudit:
    namespace_id: UUID
    revision_id: UUID
    identity_id: UUID
    account_id: UUID
    intent_id: UUID
    correlation_id: UUID
    attempt_id: UUID
    file_id: UUID
    generation: int
    fence_epoch: int
    count: int
    reason: str

    proof_ref: UUID
    terminal_intent_sha256: str


_AVATAR_PRE_STORAGE_SUMMARY_BYTES = 2048
_AVATAR_PRE_STORAGE_REFERENCES = (
    "namespace_id",
    "revision_id",
    "identity_id",
    "account_id",
    "intent_id",
    "attempt_id",
    "file_id",
)


def _pre_storage_summary_v2(raw: str) -> dict:
    """Closed canonical persisted evidence only; no v1 upgrade or authority."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("casdoor_audit_invalid")
            result[key] = value
        return result

    if type(raw) is not str or len(raw.encode("utf-8")) > _AVATAR_PRE_STORAGE_SUMMARY_BYTES:
        raise ValueError("casdoor_audit_invalid")
    data = json.loads(raw, object_pairs_hook=unique)
    if type(data) is not dict or set(data) != {
        "count",
        "fence_epoch",
        "generation",
        "proof_ref",
        "reason",
        "references",
        "schema_version",
        "terminal_intent_sha256",
    }:
        raise ValueError("casdoor_audit_invalid")
    refs = data["references"]
    if (
        type(refs) is not dict
        or set(refs) != set(_AVATAR_PRE_STORAGE_REFERENCES)
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 2
        or type(data["count"]) is not int
        or data["count"] != 1
        or any(type(data[key]) is not int or not 0 <= data[key] <= 2**63 - 1 for key in ("generation", "fence_epoch"))
        or type(data["reason"]) is not str
        or data["reason"] not in ("fetch_rejected", "fetch_failed", "image_rejected")
        or type(data["terminal_intent_sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", data["terminal_intent_sha256"]) is None
    ):
        raise ValueError("casdoor_audit_invalid")
    for value in (*refs.values(), data["proof_ref"]):
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("casdoor_audit_invalid")
    if json.dumps(data, sort_keys=True, separators=(",", ":")) != raw:
        raise ValueError("casdoor_audit_invalid")
    return data



_AVATAR_RESERVATION_FENCE_BYTES = 4096
@dataclass(frozen=True, repr=False)
class _AvatarCleanupAudit:
    canonical_json: str
    created_at: datetime
    complete: bool = False


def _avatar_cleanup_summary(raw: str, *, complete=False) -> dict:
    """Closed durable diagnostics/lineage, never a live physical execution permit."""

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("casdoor_audit_invalid")
            value[key] = item
        return value

    if type(raw) is not str or len(raw.encode()) > 8192:
        raise ValueError("casdoor_audit_invalid")
    data = json.loads(raw, object_pairs_hook=unique)
    keys = (
        {"schema_version", "references", "proof_ref", "terminal_sha256", "cleanup_sha256"}
        if complete
        else {
            "schema_version",
            "references",
            "proof_ref",
            "count",
            "guard_version",
            "before_mutable",
            "before_sha256",
            "terminal_sha256",
            "fence_sha256",
            "manifest_sha256",
            "image_sha3_256",
            "image_size",
        }
    )
    refs = {
        "namespace_id",
        "revision_id",
        "identity_id",
        "account_id",
        "intent_id",
        "attempt_id",
        "file_id",
        "tenant_id",
        "claim_correlation_id",
    }
    if (
        type(data) is not dict
        or set(data) != keys
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or type(data["references"]) is not dict
        or set(data["references"]) != refs
    ):
        raise ValueError("casdoor_audit_invalid")
    for value in [*data["references"].values(), data["proof_ref"]]:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("casdoor_audit_invalid")
    for key in keys:
        if key.endswith("sha256") or key == "image_sha3_256":
            if type(data[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", data[key]) is None:
                raise ValueError("casdoor_audit_invalid")
    if not complete:
        if (
            type(data["count"]) is not int
            or data["count"] not in (1, 2)
            or type(data["guard_version"]) is not int
            or not 2 <= data["guard_version"] < 9007199254740991
            or type(data["image_size"]) is not int
            or not 0 < data["image_size"] <= 2097152
            or type(data["before_mutable"]) is not list
            or any(
                type(item) is not list or len(item) != 2 or type(item[1]) is not list for item in data["before_mutable"]
            )
            or [item[0] for item in data["before_mutable"]] != list(_RETRY_MUTABLE_FIELDS)
        ):
            raise ValueError("casdoor_audit_invalid")
    if json.dumps(data, sort_keys=True, separators=(",", ":")) != raw:
        raise ValueError("casdoor_audit_invalid")
    return data


_AVATAR_RESERVATION_FENCE_ACTION = "avatar_reservation_fence"


@dataclass(frozen=True, repr=False)
class _AvatarReservationFenceAudit:
    canonical_json: str


def _reservation_fence_summary(raw: str) -> dict:
    """Closed SQL lineage summary; never storage or cleanup authority."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("casdoor_audit_invalid")
            result[key] = value
        return result

    if type(raw) is not str or len(raw.encode("utf-8")) > _AVATAR_RESERVATION_FENCE_BYTES:
        raise ValueError("casdoor_audit_invalid")
    data = json.loads(raw, object_pairs_hook=unique)
    keys = {
        "schema_version",
        "references",
        "count",
        "generation",
        "fence_epoch",
        "guard_version",
        "reservation_sha256",
        "claim_intent_sha256",
        "lineage",
    }
    refs = {
        "namespace_id",
        "revision_id",
        "identity_id",
        "account_id",
        "intent_id",
        "attempt_id",
        "file_id",
        "tenant_id",
        "claim_correlation_id",
    }
    if type(data) is not dict or set(data) != keys or type(data["references"]) is not dict:
        raise ValueError("casdoor_audit_invalid")
    if set(data["references"]) != refs or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("casdoor_audit_invalid")
    for value in data["references"].values():
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("casdoor_audit_invalid")
    if (
        type(data["count"]) is not int
        or data["count"] not in (1, 2)
        or any(type(data[key]) is not int or not 0 <= data[key] <= 2**63 - 1 for key in ("generation", "fence_epoch"))
        or type(data["guard_version"]) is not int
        or not 2 <= data["guard_version"] <= 9007199254740991
    ):
        raise ValueError("casdoor_audit_invalid")
    for key in ("reservation_sha256", "claim_intent_sha256"):
        if type(data[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", data[key]) is None:
            raise ValueError("casdoor_audit_invalid")
    actions = ["avatar_pending", "avatar_claim"]
    if data["count"] == 2:
        actions += ["avatar_pre_storage", "avatar_retry", "avatar_retry_claim"]
    lineage = data["lineage"]
    if type(lineage) is not list or len(lineage) != len(actions):
        raise ValueError("casdoor_audit_invalid")
    ids = set()
    for item, action in zip(lineage, actions, strict=True):
        if (
            type(item) is not dict
            or set(item) != {"action", "id", "sha256"}
            or item["action"] != action
            or type(item["id"]) is not str
            or str(UUID(item["id"])) != item["id"]
            or item["id"] in ids
            or type(item["sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        ):
            raise ValueError("casdoor_audit_invalid")
        ids.add(item["id"])
    if json.dumps(data, sort_keys=True, separators=(",", ":")) != raw:
        raise ValueError("casdoor_audit_invalid")
    return data


class CasdoorAuditRepository:
    def _read_avatar_cleanup(self, file_id: UUID, *, complete=False) -> dict | None:
        """Bound every scalar before hydration and refuse duplicate terminal receipts."""
        self._invited_receipt_root()
        if type(file_id) is not UUID or type(complete) is not bool:
            raise ValueError("casdoor_audit_invalid")
        table = CasdoorAuditExtend.__table__
        action = "avatar_cleanup_complete" if complete else "avatar_cleanup"
        conditions = (table.c.correlation_id == str(file_id), table.c.action == action)
        bounds = []
        for column in table.columns:
            if isinstance(column.type, (sa.String, sa.Text)) or column.name == "id" or column.name.endswith("_id"):
                text = sa.cast(column, sa.Text)
                length = (
                    sa.func.length(sa.cast(text, sa.LargeBinary))
                    if self._session.get_bind().dialect.name == "sqlite"
                    else sa.func.octet_length(text)
                )
                bounded = length.between(0, 8192 if column.name == "summary_json" else 64)
                bounds.append(sa.or_(column.is_(None), bounded) if column.nullable else bounded)
        headers = self._session.execute(
            sa.select(*(sa.cast(x, sa.Boolean) for x in bounds)).where(*conditions).limit(2)
        ).all()
        if not headers:
            return None
        if len(headers) != 1 or not all(x is True for x in headers[0]):
            raise ValueError("casdoor_audit_invalid")
        rows = self._session.execute(sa.select(*table.columns).where(*conditions, *bounds).limit(2)).mappings().all()
        if len(rows) != 1:
            raise ValueError("casdoor_audit_invalid")
        row = dict(rows[0])
        data = _avatar_cleanup_summary(row["summary_json"], complete=complete)
        refs = data["references"]
        if (
            any(row[name] != refs[name] for name in ("namespace_id", "revision_id", "identity_id", "account_id"))
            or refs["file_id"] != str(file_id)
            or row["actor_account_id"] is not None
            or row["result_code"] != ("complete" if complete else "pending")
            or type(row["id"]) is not str
            or str(UUID(row["id"])) != row["id"]
            or type(row["created_at"]) is not datetime
            or row["created_at"].tzinfo is not None
        ):
            raise ValueError("casdoor_audit_invalid")
        return row

    def _append_avatar_cleanup(self, event: "_AvatarCleanupAudit") -> dict:
        """Original caller holds guard and complete parents, and owes root commit/close."""
        self._invited_receipt_root()
        if type(event) is not _AvatarCleanupAudit or type(event.complete) is not bool:
            raise ValueError("casdoor_audit_invalid")
        data = _avatar_cleanup_summary(event.canonical_json, complete=event.complete)
        refs = data["references"]
        file_id = UUID(refs["file_id"])
        if self._read_avatar_cleanup(file_id, complete=event.complete) is not None:
            raise ValueError("casdoor_audit_invalid")
        values = dict(
            id=str(uuid4()),
            actor_account_id=None,
            action="avatar_cleanup_complete" if event.complete else "avatar_cleanup",
            result_code="complete" if event.complete else "pending",
            correlation_id=str(file_id),
            created_at=event.created_at,
            summary_json=event.canonical_json,
            **{name: refs[name] for name in ("namespace_id", "revision_id", "identity_id", "account_id")},
        )
        self._session.execute(sa.insert(CasdoorAuditExtend.__table__).values(**values))
        fresh = self._read_avatar_cleanup(file_id, complete=event.complete)
        if fresh != values:
            raise ValueError("casdoor_audit_invalid")
        return fresh

    def __init__(self, session: Session):
        self._session = session

    def _read_avatar_reservation_fence(self, file_id: UUID) -> dict | None:
        """Existing indexed correlation first; bound bytes before hydrating, refuse duplicates."""
        self._invited_receipt_root()
        if type(file_id) is not UUID:
            raise ValueError("casdoor_audit_invalid")
        table = CasdoorAuditExtend.__table__
        conditions = (table.c.correlation_id == str(file_id), table.c.action == _AVATAR_RESERVATION_FENCE_ACTION)
        length = (
            sa.func.length(sa.cast(table.c.summary_json, sa.LargeBinary))
            if self._session.get_bind().dialect.name == "sqlite"
            else sa.func.octet_length(table.c.summary_json)
        )
        bounds = []
        for column in table.columns:
            if column.name == "summary_json":
                bounds.append(length.between(0, _AVATAR_RESERVATION_FENCE_BYTES))
            elif isinstance(column.type, (sa.String, sa.Text)) or column.name == "id" or column.name.endswith("_id"):
                # StringUUID is a TypeDecorator, not a String subclass. Cast every
                # UUID header before its byte bound; PG UUID needs text conversion.
                text_column = sa.cast(column, sa.Text)
                size = (
                    sa.func.length(sa.cast(text_column, sa.LargeBinary))
                    if self._session.get_bind().dialect.name == "sqlite"
                    else sa.func.octet_length(text_column)
                )
                bounded = size.between(0, 64)
                bounds.append(sa.or_(column.is_(None), bounded) if column.nullable else bounded)
        headers = self._session.execute(
            sa.select(*(sa.cast(bound, sa.Boolean) for bound in bounds)).where(*conditions).limit(2)
        ).all()
        if not headers:
            return None
        if len(headers) != 1 or not all(value is True for value in headers[0]):
            raise ValueError("casdoor_audit_invalid")
        rows = self._session.execute(sa.select(*table.columns).where(*conditions, *bounds).limit(2)).mappings().all()
        if len(rows) != 1:
            raise ValueError("casdoor_audit_invalid")
        row = dict(rows[0])
        data = _reservation_fence_summary(row["summary_json"])
        refs = data["references"]
        if (
            any(row[key] != refs[key] for key in ("namespace_id", "revision_id", "identity_id", "account_id"))
            or refs["file_id"] != str(file_id)
            or row["actor_account_id"] is not None
            or row["result_code"] != "reserved"
            or type(row["id"]) is not str
            or str(UUID(row["id"])) != row["id"]
            or type(row["created_at"]) is not datetime
            or row["created_at"].tzinfo is not None
        ):
            raise ValueError("casdoor_audit_invalid")
        return row

    def _append_avatar_reservation_fence(self, event: _AvatarReservationFenceAudit) -> dict:
        """Caller holds file guard and original parents; exact unique full-row reread."""
        self._invited_receipt_root()
        if type(event) is not _AvatarReservationFenceAudit:
            raise ValueError("casdoor_audit_invalid")
        data = _reservation_fence_summary(event.canonical_json)
        refs = data["references"]
        file_id = UUID(refs["file_id"])
        if self._read_avatar_reservation_fence(file_id) is not None:
            raise ValueError("casdoor_audit_invalid")
        values = dict(
            id=str(uuid4()),
            namespace_id=refs["namespace_id"],
            revision_id=refs["revision_id"],
            identity_id=refs["identity_id"],
            account_id=refs["account_id"],
            actor_account_id=None,
            action=_AVATAR_RESERVATION_FENCE_ACTION,
            result_code="reserved",
            correlation_id=str(file_id),
            created_at=naive_utc_now(),
            summary_json=event.canonical_json,
        )
        self._session.execute(sa.insert(CasdoorAuditExtend.__table__).values(**values))
        fresh = self._read_avatar_reservation_fence(file_id)
        if fresh != values:
            raise ValueError("casdoor_audit_invalid")
        return fresh

    def _invited_receipt_root(self):
        session = self._session
        root = session.get_transaction() if isinstance(session, Session) else None
        if (
            root is None
            or not root.is_active
            or root.origin is not SessionTransactionOrigin.BEGIN
            or not session.is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise RuntimeError("Casdoor audit requires an explicitly flushed caller root")

    def _append_invited_write_receipt(self, receipt, *, invitation_guard):
        """One actual producer-bound receipt; no parsed DTO may authorize a write."""
        if type(receipt) is not InvitedLocalWriteReceipt:
            raise ValueError("casdoor_audit_invalid")
        data = receipt.values()
        self._invited_receipt_root()
        from repositories.casdoor_invited_login_guard_repository_extend import _authorize_receipt_append

        _authorize_receipt_append(invitation_guard, self._session, receipt)
        refs = data["references"]
        predicate = (
            CasdoorAuditExtend.correlation_id == refs["operation_id"],
            CasdoorAuditExtend.action == RECEIPT_KIND,
        )
        if self._session.scalar(sa.select(CasdoorAuditExtend.id).where(*predicate).limit(1)) is not None:
            raise ValueError("casdoor_audit_invalid")
        expected = {
            "id": str(uuid4()),
            "created_at": naive_utc_now(),
            "namespace_id": refs["namespace_id"],
            "revision_id": refs["revision_id"],
            "identity_id": refs["identity_id"],
            "account_id": refs["account_id"],
            "actor_account_id": None,
            "action": RECEIPT_KIND,
            "result_code": "verified",
            "correlation_id": refs["operation_id"],
            "summary_json": receipt.canonical_json,
        }
        self._session.add(CasdoorAuditExtend(**expected))
        self._session.flush()
        return self._read_invited_write_receipt(receipt, expected)

    def _read_invited_write_receipt(self, receipt, expected):
        """Bound summary bytes before complete exact-row readback; duplicates fail."""
        if type(receipt) is not InvitedLocalWriteReceipt:
            raise ValueError("casdoor_audit_invalid")
        data = receipt.values()
        self._invited_receipt_root()
        table = CasdoorAuditExtend.__table__
        if type(expected) is not dict or set(expected) != set(table.columns.keys()):
            raise ValueError("casdoor_audit_invalid")
        conditions = (
            CasdoorAuditExtend.correlation_id == data["references"]["operation_id"],
            CasdoorAuditExtend.action == RECEIPT_KIND,
        )
        length = (
            sa.func.length(sa.cast(CasdoorAuditExtend.summary_json, sa.LargeBinary))
            if self._session.get_bind().dialect.name == "sqlite"
            else sa.func.octet_length(CasdoorAuditExtend.summary_json)
        )
        headers = tuple(
            self._session.execute(
                sa.select(CasdoorAuditExtend.id, length).where(*conditions).order_by(CasdoorAuditExtend.id).limit(2)
            )
        )
        if len(headers) != 1 or type(headers[0][1]) is not int or not 0 <= headers[0][1] <= MAX_RECEIPT_BYTES:
            raise ValueError("casdoor_audit_invalid")
        rows = tuple(
            self._session.execute(
                sa.select(*table.columns)
                .where(*conditions, length <= MAX_RECEIPT_BYTES)
                .order_by(CasdoorAuditExtend.id)
                .limit(2)
            )
        )
        if len(rows) != 1 or rows[0].id != headers[0][0] or dict(rows[0]._mapping) != expected:
            raise ValueError("casdoor_audit_invalid")
        if InvitedLocalWriteReceipt(rows[0].summary_json) != receipt:
            raise ValueError("casdoor_audit_invalid")
        return rows[0]

    def _append_avatar(self, event: _AvatarPendingAudit) -> CasdoorAuditExtend:
        """Closed pending-only avatar audit, after business flush in the caller root."""
        if (
            type(event) is not _AvatarPendingAudit
            or any(
                type(getattr(event, key)) is not UUID
                for key in ("namespace_id", "revision_id", "identity_id", "account_id", "intent_id", "correlation_id")
            )
            or any(
                type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (event.generation, event.fence_epoch)
            )
        ):
            raise ValueError("casdoor_audit_invalid")
        session = self._session
        if (
            not session.is_active
            or not session.in_transaction()
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise RuntimeError("Casdoor audit requires an explicitly flushed caller root")
        refs = {
            key: str(getattr(event, key))
            for key in ("namespace_id", "revision_id", "identity_id", "account_id", "intent_id")
        }
        row = CasdoorAuditExtend(
            namespace_id=refs["namespace_id"],
            revision_id=refs["revision_id"],
            identity_id=refs["identity_id"],
            account_id=refs["account_id"],
            action="avatar_pending",
            result_code="pending",
            correlation_id=str(event.correlation_id),
            summary_json=json.dumps(
                dict(
                    schema_version=1,
                    references=refs,
                    count=1,
                    generation=event.generation,
                    fence_epoch=event.fence_epoch,
                    reason="authenticated_candidate",
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        session.add(row)
        session.flush()
        return row

    def append(self, event: SafetyEvent) -> CasdoorAuditExtend:
        if type(event) is not SafetyEvent:
            raise ValueError("casdoor_audit_invalid")
        event.values()  # Validate the entire whitelist before transaction/SQL work.
        session = self._session
        if not session.is_active:
            raise RuntimeError("Casdoor audit requires an active Session")
        if not session.in_transaction() or session.in_nested_transaction():
            raise RuntimeError("Casdoor audit requires a caller-owned root transaction")
        if session.new or session.dirty or session.deleted:
            raise RuntimeError("Casdoor audit requires an explicitly flushed Session")
        references = event.references.values()
        row = CasdoorAuditExtend(
            namespace_id=references.get("namespace_id"),
            revision_id=references.get("revision_id"),
            identity_id=references.get("identity_id"),
            account_id=references.get("account_id"),
            actor_account_id=references.get("actor_account_id"),
            action=event.action.value,
            result_code=event.result_code.value,
            correlation_id=str(event.correlation_id),
            summary_json=json.dumps(
                {"schema_version": 1, "counts": event.summary.values(), "references": references},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        session.add(row)
        session.flush()
        return row

    def append_profile(self, event: ProfileAuditEvent) -> CasdoorAuditExtend:
        if type(event) is not ProfileAuditEvent:
            raise ValueError("casdoor_audit_invalid")
        event.values()  # Validate the entire whitelist before transaction/SQL work.
        session = self._session
        if not session.is_active:
            raise RuntimeError("Casdoor audit requires an active Session")
        if not session.in_transaction() or session.in_nested_transaction():
            raise RuntimeError("Casdoor audit requires a caller-owned root transaction")
        if session.new or session.dirty or session.deleted:
            raise RuntimeError("Casdoor audit requires an explicitly flushed Session")
        references = event.references.values()
        row = CasdoorAuditExtend(
            namespace_id=references.get("namespace_id"),
            revision_id=references.get("revision_id"),
            identity_id=references.get("identity_id"),
            account_id=references.get("account_id"),
            actor_account_id=references.get("actor_account_id"),
            action=event.action.value,
            result_code=event.result_code.value,
            correlation_id=str(event.correlation_id),
            summary_json=json.dumps(
                {
                    "schema_version": 1,
                    "profile": event.summary.values(),
                    "references": references,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        session.add(row)
        session.flush()
        return row

    def append_local_membership(
        self,
        *,
        withdrawal: bool,
        namespace_id: UUID,
        revision_id: UUID,
        identity_id: UUID,
        account_id: UUID,
        workspace_id: UUID,
        membership_id: UUID,
        old_join_id: UUID,
        new_join_id: UUID | None,
        epoch_before: int,
        epoch_after: int,
        generation: int,
        fence_epoch: int,
        correlation_id: UUID,
    ) -> CasdoorAuditExtend:
        """Fixed local operation, after business flush in the same caller root."""
        refs = dict(
            namespace_id=namespace_id,
            revision_id=revision_id,
            identity_id=identity_id,
            account_id=account_id,
            workspace_id=workspace_id,
            membership_id=membership_id,
            old_join_id=old_join_id,
        )
        if (
            type(withdrawal) is not bool
            or any(type(value) is not UUID for value in refs.values())
            or type(correlation_id) is not UUID
            or (withdrawal and new_join_id is not None)
            or (not withdrawal and (type(new_join_id) is not UUID or new_join_id == old_join_id))
            or any(
                type(value) is not int or not low <= value <= 2**63 - 1
                for value, low in ((epoch_before, 0), (epoch_after, 1), (generation, 1), (fence_epoch, 0))
            )
            or epoch_after != epoch_before + 1
        ):
            raise ValueError("casdoor_audit_invalid")
        session = self._session
        if (
            not session.is_active
            or not session.in_transaction()
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise RuntimeError("Casdoor audit requires an explicitly flushed caller root")
        references = {key: str(value) for key, value in refs.items()}
        references["new_join_id"] = str(new_join_id) if new_join_id else None
        row = CasdoorAuditExtend(
            namespace_id=str(namespace_id),
            revision_id=str(revision_id),
            identity_id=str(identity_id),
            account_id=str(account_id),
            actor_account_id=None,
            action="local_member_withdraw" if withdrawal else "local_member_regrant",
            result_code="ok",
            correlation_id=str(correlation_id),
            summary_json=json.dumps(
                dict(
                    schema_version=1,
                    references=references,
                    count=1,
                    epoch_before=epoch_before,
                    epoch_after=epoch_after,
                    generation=generation,
                    fence_epoch=fence_epoch,
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        session.add(row)
        session.flush()
        return row

    def _append_avatar_attempt(self, event: _AvatarAttemptAudit) -> CasdoorAuditExtend:
        """Closed reservation/unknown audit only; no termination or retry receipt."""
        refs = ("namespace_id", "revision_id", "identity_id", "account_id", "intent_id", "attempt_id", "file_id")
        reasons = {
            "fetch_rejected",
            "fetch_failed",
            "fetch_cancelled",
            "fetch_unknown",
            "image_rejected",
            "storage_unknown",
            "attachment_lost",
            "lease_expired",
            "commit_unknown",
        }
        if (
            type(event) is not _AvatarAttemptAudit
            or any(type(getattr(event, key)) is not UUID for key in (*refs, "correlation_id"))
            or any(
                type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (event.generation, event.fence_epoch)
            )
            or any(type(value) is not str for value in (event.action, event.result, event.reason))
            or type(event.count) is not int
            or not 1 <= event.count <= 3
            or not (
                (
                    event.action == "avatar_claim"
                    and event.result == "reserved"
                    and event.reason == "claimed"
                    and event.count == 1
                )
                or (event.action == "avatar_finish" and event.result == "unknown" and event.reason in reasons)
            )
        ):
            raise ValueError("casdoor_audit_invalid")
        session = self._session
        transaction = session.get_transaction() if isinstance(session, Session) else None
        if (
            transaction is None
            or not transaction.is_active
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or not session.is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise RuntimeError("Casdoor audit requires an explicitly flushed caller root")
        references = {key: str(getattr(event, key)) for key in refs}
        values = dict(
            namespace_id=references["namespace_id"],
            revision_id=references["revision_id"],
            identity_id=references["identity_id"],
            account_id=references["account_id"],
            actor_account_id=None,
            action=event.action,
            result_code=event.result,
            correlation_id=str(event.correlation_id),
            summary_json=json.dumps(
                dict(
                    schema_version=1,
                    references=references,
                    count=event.count,
                    generation=event.generation,
                    fence_epoch=event.fence_epoch,
                    reason=event.reason,
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        row = CasdoorAuditExtend(**values)
        session.add(row)
        session.flush()
        actual = session.execute(
            sa.select(*(getattr(CasdoorAuditExtend, key) for key in values)).where(CasdoorAuditExtend.id == row.id)
        ).one_or_none()
        if actual is None or tuple(actual) != tuple(values.values()):
            raise ValueError("casdoor_audit_invalid")
        return row

    def _append_avatar_attachment(self, event: _AvatarAttachmentAudit) -> CasdoorAuditExtend:
        """Closed DB attachment audit; no storage readback or remote receipt."""
        refs = ("namespace_id", "revision_id", "identity_id", "account_id", "intent_id", "attempt_id", "file_id")
        if (
            type(event) is not _AvatarAttachmentAudit
            or any(type(getattr(event, key)) is not UUID for key in (*refs, "correlation_id"))
            or any(
                type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (event.generation, event.fence_epoch)
            )
            or type(event.count) is not int
            or not 1 <= event.count <= 3
        ):
            raise ValueError("casdoor_audit_invalid")
        session = self._session
        transaction = session.get_transaction() if isinstance(session, Session) else None
        if (
            transaction is None
            or not transaction.is_active
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or not session.is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise RuntimeError("Casdoor audit requires an explicitly flushed caller root")
        references = {key: str(getattr(event, key)) for key in refs}
        values = dict(
            namespace_id=references["namespace_id"],
            revision_id=references["revision_id"],
            identity_id=references["identity_id"],
            account_id=references["account_id"],
            actor_account_id=None,
            action="avatar_attach",
            result_code="applied",
            correlation_id=str(event.correlation_id),
            summary_json=json.dumps(
                dict(
                    schema_version=1,
                    references=references,
                    count=event.count,
                    generation=event.generation,
                    fence_epoch=event.fence_epoch,
                    reason="attached",
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        row = CasdoorAuditExtend(**values)
        session.add(row)
        session.flush()
        actual = session.execute(
            sa.select(*(getattr(CasdoorAuditExtend, key) for key in values)).where(
                CasdoorAuditExtend.id == row.id,
                *(getattr(CasdoorAuditExtend, key) == value for key, value in values.items()),
            )
        ).one_or_none()
        if actual is None or tuple(actual) != tuple(values.values()):
            raise ValueError("casdoor_audit_invalid")
        return row

    def _append_avatar_pre_storage(self, event: _AvatarPreStorageAudit) -> CasdoorAuditExtend:
        """Closed v2 durable pre-storage event; no retry or deletion authority."""
        refs = ("namespace_id", "revision_id", "identity_id", "account_id", "intent_id", "attempt_id", "file_id")
        if (
            type(event) is not _AvatarPreStorageAudit
            or any(type(getattr(event, key)) is not UUID for key in (*refs, "correlation_id", "proof_ref"))
            or any(
                type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (event.generation, event.fence_epoch)
            )
            or type(event.count) is not int
            or event.count != 1
            or type(event.reason) is not str
            or event.reason not in ("fetch_rejected", "fetch_failed", "image_rejected")
        ):
            raise ValueError("casdoor_audit_invalid")
        session = self._session
        transaction = session.get_transaction() if isinstance(session, Session) else None
        if (
            transaction is None
            or not transaction.is_active
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or not session.is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise RuntimeError("Casdoor audit requires an explicitly flushed caller root")
        references = {key: str(getattr(event, key)) for key in refs}
        values = dict(
            namespace_id=references["namespace_id"],
            revision_id=references["revision_id"],
            identity_id=references["identity_id"],
            account_id=references["account_id"],
            actor_account_id=None,
            action="avatar_pre_storage",
            result_code="failed",
            correlation_id=str(event.correlation_id),
            summary_json=json.dumps(
                dict(
                    schema_version=2,
                    references=references,
                    count=event.count,
                    generation=event.generation,
                    fence_epoch=event.fence_epoch,
                    reason=event.reason,
                    proof_ref=str(event.proof_ref),
                    terminal_intent_sha256=event.terminal_intent_sha256,
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        _pre_storage_summary_v2(values["summary_json"])
        row = CasdoorAuditExtend(**values)
        session.add(row)
        session.flush()
        actual = session.execute(
            sa.select(*(getattr(CasdoorAuditExtend, key) for key in values)).where(
                CasdoorAuditExtend.id == row.id,
                *(getattr(CasdoorAuditExtend, key) == value for key, value in values.items()),
            )
        ).one_or_none()
        if actual is None or tuple(actual) != tuple(values.values()):
            raise ValueError("casdoor_audit_invalid")
        return row

    def _append_invited_finalization_receipt(self, receipt, *, finalization_owner):
        """One live F producer, after exact business CAS and full-row readback."""
        from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as action
        from core.casdoor.invited_finalization_receipt import InvitedLocalFinalizationReceipt
        from repositories.casdoor_invited_local_finalization_repository_extend import _authorize_finalization_append

        if type(receipt) is not InvitedLocalFinalizationReceipt:
            raise ValueError("casdoor_audit_invalid")
        data = receipt.values()
        self._invited_receipt_root()
        _authorize_finalization_append(finalization_owner, self._session, receipt)
        refs = data["references"]
        if (
            self._session.scalar(
                sa.select(CasdoorAuditExtend.id)
                .where(CasdoorAuditExtend.correlation_id == refs["operation_id"], CasdoorAuditExtend.action == action)
                .limit(1)
            )
            is not None
        ):
            raise ValueError("casdoor_audit_invalid")
        expected = {
            "id": str(uuid4()),
            "created_at": naive_utc_now(),
            "namespace_id": refs["namespace_id"],
            "revision_id": refs["revision_id"],
            "identity_id": refs["identity_id"],
            "account_id": refs["account_id"],
            "actor_account_id": None,
            "action": action,
            "result_code": "verified",
            "correlation_id": refs["operation_id"],
            "summary_json": receipt.canonical_json,
        }
        self._session.add(CasdoorAuditExtend(**expected))
        self._session.flush()
        return self._read_invited_finalization_receipt(receipt, expected)

    def _read_invited_finalization_receipt(self, receipt, expected):
        """Exact complete F audit row with byte header bound and duplicate denial."""
        from core.casdoor.invited_finalization_receipt import MAX_RECEIPT_BYTES as cap
        from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as action
        from core.casdoor.invited_finalization_receipt import InvitedLocalFinalizationReceipt

        if type(receipt) is not InvitedLocalFinalizationReceipt:
            raise ValueError("casdoor_audit_invalid")
        data = receipt.values()
        self._invited_receipt_root()
        table = CasdoorAuditExtend.__table__
        if type(expected) is not dict or set(expected) != set(table.columns.keys()):
            raise ValueError("casdoor_audit_invalid")
        conditions = (
            CasdoorAuditExtend.correlation_id == data["references"]["operation_id"],
            CasdoorAuditExtend.action == action,
        )
        length = (
            sa.func.length(sa.cast(CasdoorAuditExtend.summary_json, sa.LargeBinary))
            if self._session.get_bind().dialect.name == "sqlite"
            else sa.func.octet_length(CasdoorAuditExtend.summary_json)
        )
        headers = tuple(
            self._session.execute(
                sa.select(CasdoorAuditExtend.id, length).where(*conditions).order_by(CasdoorAuditExtend.id).limit(2)
            )
        )
        if len(headers) != 1 or type(headers[0][1]) is not int or not 0 <= headers[0][1] <= cap:
            raise ValueError("casdoor_audit_invalid")
        rows = tuple(
            self._session.execute(
                sa.select(*table.columns).where(*conditions, length <= cap).order_by(CasdoorAuditExtend.id).limit(2)
            )
        )
        if len(rows) != 1 or rows[0].id != headers[0][0] or dict(rows[0]._mapping) != expected:
            raise ValueError("casdoor_audit_invalid")
        if InvitedLocalFinalizationReceipt(rows[0].summary_json) != receipt:
            raise ValueError("casdoor_audit_invalid")
        return rows[0]


    def _append_avatar_retry(self, event: "_AvatarRetryAudit") -> CasdoorAuditExtend:
        """Append exact producer lineage in the caller root; no authority minting."""
        if (
            type(event) is not _AvatarRetryAudit
            or type(event.audit_id) is not UUID
            or type(event.actor_account_id) is not UUID
        ):
            raise ValueError("casdoor_audit_invalid")
        self._invited_receipt_root()
        summary = _retry_summary_v1(
            json.dumps(event.summary, sort_keys=True, separators=(",", ":"))
        )
        refs = summary["references"]
        values = dict(
            id=str(event.audit_id),
            **{
                key: refs[key]
                for key in ("namespace_id", "revision_id", "identity_id", "account_id")
            },
            actor_account_id=str(event.actor_account_id),
            action="avatar_retry",
            result_code="pending",
            correlation_id=refs["intent_id"],
            summary_json=json.dumps(summary, sort_keys=True, separators=(",", ":")),
            created_at=event.created_at,
        )
        row = CasdoorAuditExtend(**values)
        self._session.add(row)
        self._session.flush()
        actual = (
            self._session.execute(
                sa.select(*CasdoorAuditExtend.__table__.columns).where(
                    CasdoorAuditExtend.id == row.id
                )
            )
            .mappings()
            .one()
        )
        if dict(actual) != values:
            raise ValueError("casdoor_audit_invalid")
        return row


    def _append_avatar_retry_claim(
        self,
        event: _AvatarAttemptAudit,
        *,
        retry_audit_id: UUID,
        claim_mutable,
        claim_sha256,
        pending_sha256,
        created_at,
    ):
        """Second reservation only; the original initial-claim contract stays closed."""
        refs = (
            "namespace_id",
            "revision_id",
            "identity_id",
            "account_id",
            "intent_id",
            "attempt_id",
            "file_id",
        )
        if (
            type(event) is not _AvatarAttemptAudit
            or type(retry_audit_id) is not UUID
            or any(
                type(getattr(event, key)) is not UUID for key in (*refs, "correlation_id")
            )
            or any(
                type(value) is not int or not 0 <= value <= 2**63 - 1
                for value in (event.generation, event.fence_epoch)
            )
            or event.count != 2
            or type(event.count) is not int
            or (event.action, event.result, event.reason)
            != ("avatar_claim", "reserved", "claimed")
        ):
            raise ValueError("casdoor_audit_invalid")
        if (
            type(created_at) is not datetime
            or created_at.tzinfo is not None
            or type(claim_mutable) is not list
            or [item[0] for item in claim_mutable] != list(_RETRY_MUTABLE_FIELDS)
            or any(
                type(value) is not str
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
                for value in (claim_sha256, pending_sha256)
            )
        ):
            raise ValueError("casdoor_audit_invalid")
        self._invited_receipt_root()
        references = {key: str(getattr(event, key)) for key in refs}
        summary = dict(
            schema_version=1,
            references=references,
            count=2,
            generation=event.generation,
            fence_epoch=event.fence_epoch,
            reason="claimed",
            retry_audit_id=str(retry_audit_id),
            claim_mutable=claim_mutable,
            claim_sha256=claim_sha256,
            pending_sha256=pending_sha256,
        )
        values = dict(
            namespace_id=references["namespace_id"],
            revision_id=references["revision_id"],
            identity_id=references["identity_id"],
            account_id=references["account_id"],
            actor_account_id=None,
            action="avatar_retry_claim",
            result_code="reserved",
            correlation_id=references["intent_id"],
            created_at=created_at,
            summary_json=json.dumps(summary, sort_keys=True, separators=(",", ":")),
        )
        if len(values["summary_json"].encode()) > _AVATAR_RETRY_SUMMARY_BYTES:
            raise ValueError("casdoor_audit_invalid")
        row = CasdoorAuditExtend(**values)
        self._session.add(row)
        self._session.flush()
        if (
            any(getattr(row, key) != value for key, value in values.items())
            or str(UUID(row.id)) != row.id
        ):
            raise ValueError("casdoor_audit_invalid")
        return row


_AVATAR_RETRY_SUMMARY_BYTES = 8192
_RETRY_MUTABLE_FIELDS = (
    "resource_id", "operation_state", "termination_state", "attempt_id", "attempt_count",
    "lease_owner", "lease_expires_at", "sent_at", "acknowledged_at", "readback_at", "terminated_at",
    "termination_proof_kind", "proof_ref", "retry_at", "error_code", "updated_at",
)


@dataclass(frozen=True, repr=False)
class _AvatarRetryAudit:
    audit_id: UUID
    actor_account_id: UUID
    summary: dict
    created_at: datetime


def _retry_summary_v1(raw: str) -> dict:
    """Bounded closed SQL lineage facts; no permission, URL or ciphertext."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("casdoor_audit_invalid")
            result[key] = value
        return result

    if type(raw) is not str or len(raw.encode()) > _AVATAR_RETRY_SUMMARY_BYTES:
        raise ValueError("casdoor_audit_invalid")
    data = json.loads(raw, object_pairs_hook=pairs)
    keys = {
        "schema_version",
        "references",
        "before",
        "original_audit_id",
        "original_audit_sha256",
        "original_terminal_sha256",
        "original_desired_sha256",
        "immutable_sha256",
        "pending_sha256",
        "retry_at",
    }
    refs = {"namespace_id", "revision_id", "identity_id", "account_id", "intent_id"}
    if (
        type(data) is not dict
        or set(data) != keys
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or type(data["references"]) is not dict
        or set(data["references"]) != refs
    ):
        raise ValueError("casdoor_audit_invalid")
    for value in (*data["references"].values(), data["original_audit_id"]):
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("casdoor_audit_invalid")
    for key in keys - {
        "schema_version",
        "references",
        "before",
        "original_audit_id",
        "retry_at",
    }:
        if type(data[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", data[key]):
            raise ValueError("casdoor_audit_invalid")
    if type(data["before"]) is not list or [item[0] for item in data["before"]] != list(
        _RETRY_MUTABLE_FIELDS
    ):
        raise ValueError("casdoor_audit_invalid")
    for name, tagged in data["before"]:
        if (
            type(tagged) is not list
            or not tagged
            or tagged[0] not in ("null", "str", "int", "datetime")
        ):
            raise ValueError("casdoor_audit_invalid")
        if tagged[0] == "null":
            if len(tagged) != 1:
                raise ValueError("casdoor_audit_invalid")
        elif len(tagged) != 2 or (tagged[0] == "int" and type(tagged[1]) is not int):
            raise ValueError("casdoor_audit_invalid")
        elif tagged[0] != "int" and (type(tagged[1]) is not str or len(tagged[1]) > 64):
            raise ValueError("casdoor_audit_invalid")
    if type(data["retry_at"]) is not str or len(data["retry_at"]) > 32:
        raise ValueError("casdoor_audit_invalid")
    if json.dumps(data, sort_keys=True, separators=(",", ":")) != raw:
        raise ValueError("casdoor_audit_invalid")
    return data
