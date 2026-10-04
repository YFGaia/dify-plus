"""Caller-owned Casdoor audit write; no commit, rollback, external I/O or locks.

Caller already holds all parent/scope locks for this batch. Session.flush() flushes
the entire Session, so reject unrelated pending state before issuing any SQL.
Append after business changes were explicitly flushed in an active caller
transaction. A failed flush leaves recovery/rollback exclusively to the caller.
"""

import json
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.invited_write_receipt import MAX_RECEIPT_BYTES, RECEIPT_KIND, InvitedLocalWriteReceipt
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


class CasdoorAuditRepository:
    def __init__(self, session: Session):
        self._session = session

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
