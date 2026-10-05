"""Caller-owned SQL storage of invitation facts, without invitation authorization.

Every operation uses the supplied Session. The caller owns commit/rollback and
must abort/retry its transaction after a database uniqueness/serialization error.
Existing rows are locked lifecycle-first where the dialect supports row locks.
SQLite tests prove sequential behavior only. No token, Redis key or provider is
accepted here. A receipt's key digest is shape-checked, never recomputed by SQL.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from libs.datetime_utils import naive_utc_now
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend, InvitationAuthorityLifecycleExtend

MAX_LIFECYCLE_EPOCH = 2**53 - 1
_PAYLOAD_FIELDS = {"account_id", "email", "workspace_id", "role", "requires_setup", "invitation_authority"}
_AUTHORITY_FIELDS = {
    "schema_version",
    "issuance_id",
    "lifecycle_id",
    "lifecycle_epoch",
    "token_digest",
    "join_id_at_issue",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "issuance_id",
    "lifecycle_id",
    "lifecycle_epoch",
    "account_id",
    "workspace_id",
    "join_id_at_issue",
    "token_digest",
    "payload_digest",
    "key_digest",
}


class InvitationAuthorityValidationError(ValueError):
    """Malformed facts, with a fixed message that excludes payload PII."""


class InvitationAuthorityConflict(ValueError):
    """Stored lifecycle or receipt no longer agrees with the supplied facts."""


@dataclass(frozen=True)
class InvitationLifecycleRecord:
    lifecycle_id: str
    account_id: str
    workspace_id: str
    epoch: int
    state: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, repr=False)
class InvitationIssuanceRecord:
    issuance_id: str
    payload_json: str
    payload_digest: str
    actor_id: str | None
    state: str
    consumption_receipt_json: str | None
    consumed_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _require(condition: bool) -> None:
    if not condition:
        raise InvitationAuthorityValidationError("invalid_invitation_authority_facts")


def _uuid(value: Any, *, version4: bool = False) -> None:
    _require(type(value) is str)
    valid = False
    try:
        parsed = UUID(value)
        valid = str(parsed) == value and (not version4 or parsed.version == 4)
    except ValueError:
        pass
    _require(valid)


def _digest(value: Any) -> None:
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _epoch(value: Any) -> None:
    _require(type(value) is int and 1 <= value <= MAX_LIFECYCLE_EPOCH)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    _require(False)


def _json_object(raw: str) -> dict[str, Any]:
    _require(type(raw) is str)
    data = None
    try:
        _require(len(raw.encode("utf-8")) <= 8192)
        data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        pass
    _require(type(data) is dict)
    return data


def _payload(raw: str) -> dict[str, Any]:
    data = _json_object(raw)
    _require(set(data) == _PAYLOAD_FIELDS)
    for name in ("account_id", "workspace_id"):
        _uuid(data[name])
    for name in ("email", "role"):
        value = data[name]
        _require(type(value) is str and bool(value.strip()))
        valid = False
        try:
            valid = len(value.encode("utf-8")) <= 255
        except UnicodeError:
            pass
        _require(valid)
    # Reuse P1's email validator while suppressing its PII-bearing error text.
    from libs.helper import email as validate_email

    valid_email = False
    try:
        validate_email(data["email"])
        valid_email = True
    except ValueError:
        pass
    _require(valid_email)
    _require(type(data["requires_setup"]) is bool)
    authority = data["invitation_authority"]
    _require(type(authority) is dict and set(authority) == _AUTHORITY_FIELDS)
    _require(type(authority["schema_version"]) is int and authority["schema_version"] == 1)
    for name in ("issuance_id", "lifecycle_id"):
        _uuid(authority[name], version4=True)
    _epoch(authority["lifecycle_epoch"])
    _digest(authority["token_digest"])
    if authority["join_id_at_issue"] is not None:
        _uuid(authority["join_id_at_issue"])
    return data


def _receipt(raw: str) -> dict[str, Any]:
    data = _json_object(raw)
    _require(set(data) == _RECEIPT_FIELDS)
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    _require(data["status"] == "consumed")
    for name in ("operation_id", "account_id", "workspace_id"):
        _uuid(data[name])
    for name in ("issuance_id", "lifecycle_id"):
        _uuid(data[name], version4=True)
    if data["join_id_at_issue"] is not None:
        _uuid(data["join_id_at_issue"])
    _epoch(data["lifecycle_epoch"])
    for name in ("token_digest", "payload_digest", "key_digest"):
        _digest(data[name])
    _require(json.dumps(data, sort_keys=True, separators=(",", ":")) == raw)
    return data


def _lifecycle_record(row: InvitationAuthorityLifecycleExtend) -> InvitationLifecycleRecord:
    return InvitationLifecycleRecord(
        row.lifecycle_id, row.account_id, row.workspace_id, row.epoch, row.state, row.created_at, row.updated_at
    )


def _issuance_record(row: InvitationAuthorityIssuanceExtend) -> InvitationIssuanceRecord:
    return InvitationIssuanceRecord(
        row.issuance_id,
        row.payload_json,
        row.payload_digest,
        row.actor_id,
        row.state,
        row.consumption_receipt_json,
        row.consumed_at,
        row.created_at,
        row.updated_at,
    )


class InvitationAuthorityRepository:
    """Persist validated facts only; all returned records are immutable snapshots."""

    @staticmethod
    def _lifecycle(session: Session, account_id: str, workspace_id: str) -> InvitationAuthorityLifecycleExtend | None:
        _uuid(account_id)
        _uuid(workspace_id)
        return session.scalar(
            sa.select(InvitationAuthorityLifecycleExtend)
            .where(
                InvitationAuthorityLifecycleExtend.account_id == account_id,
                InvitationAuthorityLifecycleExtend.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _issuance(session: Session, issuance_id: str) -> InvitationAuthorityIssuanceExtend | None:
        _uuid(issuance_id, version4=True)
        return session.scalar(
            sa.select(InvitationAuthorityIssuanceExtend)
            .where(InvitationAuthorityIssuanceExtend.issuance_id == issuance_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def get_lifecycle(
        self, session: Session, *, account_id: str, workspace_id: str
    ) -> InvitationLifecycleRecord | None:
        row = self._lifecycle(session, account_id, workspace_id)
        return _lifecycle_record(row) if row else None

    def set_lifecycle_state(
        self, session: Session, *, account_id: str, workspace_id: str, state: str
    ) -> InvitationLifecycleRecord:
        _require(type(state) is str and state in ("active", "withdrawn"))
        row = self._lifecycle(session, account_id, workspace_id)
        if row is None:
            row = InvitationAuthorityLifecycleExtend(
                lifecycle_id=str(uuid4()), account_id=account_id, workspace_id=workspace_id, epoch=1, state=state
            )
            session.add(row)
        elif row.state != state:
            if row.epoch == MAX_LIFECYCLE_EPOCH:
                raise InvitationAuthorityConflict("invitation_lifecycle_epoch_exhausted")
            row.state = state
            row.epoch += 1
            row.updated_at = naive_utc_now()
        session.flush([row])
        return _lifecycle_record(row)

    def record_membership_creation(
        self, session: Session, *, account_id: str, workspace_id: str
    ) -> InvitationLifecycleRecord:
        """Advance once after an actual new Join flush in the caller's transaction.

        Unlike setting active state, a new membership advances even an already
        active lifecycle. This records a creation event, not admission or receipt
        consumption; the caller must roll back the Join if this write fails.
        """
        row = self._lifecycle(session, account_id, workspace_id)
        if row is None:
            row = InvitationAuthorityLifecycleExtend(
                lifecycle_id=str(uuid4()), account_id=account_id, workspace_id=workspace_id, epoch=1, state="active"
            )
            session.add(row)
        else:
            if row.epoch == MAX_LIFECYCLE_EPOCH:
                raise InvitationAuthorityConflict("invitation_lifecycle_epoch_exhausted")
            row.state = "active"
            row.epoch += 1
            row.updated_at = naive_utc_now()
        session.flush([row])
        return _lifecycle_record(row)

    def record_local_role_change(
        self, session: Session, *, account_id: str, workspace_id: str
    ) -> InvitationLifecycleRecord:
        """Fence prior facts after a real LOCAL Join/History role change.

        The caller must complete its Join CAS/readback and History CAS first,
        and roll back the whole transaction if this write fails. An existing
        withdrawn lifecycle stays withdrawn; changing role is not a regrant.
        """
        return self.record_membership_role_change(session, account_id=account_id, workspace_id=workspace_id)

    def record_membership_role_change(
        self, session: Session, *, account_id: str, workspace_id: str
    ) -> InvitationLifecycleRecord:
        """Fence prior facts after a proven and flushed LOCAL membership role change.

        The caller owns authorization and must roll back the role write if this
        operation fails. Existing withdrawn authority is never reactivated.
        """
        row = self._lifecycle(session, account_id, workspace_id)
        if row is None:
            row = InvitationAuthorityLifecycleExtend(
                lifecycle_id=str(uuid4()), account_id=account_id, workspace_id=workspace_id, epoch=1, state="active"
            )
            session.add(row)
        else:
            if row.epoch == MAX_LIFECYCLE_EPOCH:
                raise InvitationAuthorityConflict("invitation_lifecycle_epoch_exhausted")
            row.epoch += 1
            row.updated_at = naive_utc_now()
        session.flush([row])
        return _lifecycle_record(row)

    def get_issuance(self, session: Session, *, issuance_id: str) -> InvitationIssuanceRecord | None:
        row = self._issuance(session, issuance_id)
        return _issuance_record(row) if row else None

    def get_issuance_by_token_digest(self, session: Session, *, token_digest: str) -> InvitationIssuanceRecord | None:
        """Bounded nonlocking SQL facts; digest possession grants no authority.

        Header reads bound row count and both TEXT byte lengths before fetching
        their contents. Mutation owners retain their lifecycle-first lock order.
        P1 payload hashing uses the original stored bytes, not reserialization.
        """
        _digest(token_digest)
        _require(
            isinstance(session, Session)
            and session.is_active
            and session.in_transaction()
            and not session.in_nested_transaction()
            and not session.new
            and not session.dirty
            and not session.deleted
        )
        model = InvitationAuthorityIssuanceExtend
        texts = (model.payload_json, model.consumption_receipt_json)
        lengths = tuple(
            sa.func.length(sa.cast(column, sa.LargeBinary))
            if session.get_bind().dialect.name == "sqlite"
            else sa.func.octet_length(column)
            for column in texts
        )
        with session.no_autoflush:
            headers = tuple(
                session.execute(
                    sa.select(model.issuance_id, *lengths).where(model.token_digest == token_digest).limit(2)
                )
            )
            _require(len(headers) <= 1)
            if not headers:
                return None
            _require(all(size is None or type(size) is int and 0 <= size <= 8192 for size in tuple(headers[0])[1:]))
            rows = tuple(
                session.execute(
                    sa.select(*model.__table__.columns)
                    .where(
                        model.token_digest == token_digest,
                        model.issuance_id == headers[0].issuance_id,
                        *(
                            sa.or_(column.is_(None), length <= 8192)
                            for column, length in zip(texts, lengths, strict=True)
                        ),
                    )
                    .limit(1)
                )
            )
            _require(len(rows) == 1)
            row = rows[0]
            data = _payload(row.payload_json)
            authority = data["invitation_authority"]
            _digest(row.payload_digest)
            _require(sha256(row.payload_json.encode("utf-8")).hexdigest() == row.payload_digest)
            expected = {
                **{name: data[name] for name in _PAYLOAD_FIELDS - {"invitation_authority"}},
                **{name: authority[name] for name in _AUTHORITY_FIELDS - {"schema_version"}},
            }
            _require(all(getattr(row, name) == value for name, value in expected.items()))
            _require(row.token_digest == token_digest and type(row.requires_setup) is bool)
            if row.actor_id is not None:
                _uuid(row.actor_id)
            _require(
                all(type(value) is datetime and value.tzinfo is None for value in (row.created_at, row.updated_at))
            )
            _require(row.created_at <= row.updated_at)
            if row.state == "issued":
                _require(row.consumption_receipt_json is None and row.consumed_at is None)
            else:
                _require(
                    row.state == "consumed" and type(row.consumed_at) is datetime and row.consumed_at.tzinfo is None
                )
                _require(row.created_at <= row.consumed_at <= row.updated_at)
                receipt = _receipt(row.consumption_receipt_json)
                _require(
                    all(
                        getattr(row, name) == receipt[name]
                        for name in _RECEIPT_FIELDS - {"schema_version", "status", "operation_id", "key_digest"}
                    )
                )
            return _issuance_record(row)

    def record_issuance(
        self, session: Session, *, payload_json: str, actor_id: str | None = None
    ) -> InvitationIssuanceRecord:
        """Insert an immutable exact P1 snapshot; uniqueness failures propagate.

        The exact UTF-8 payload text is retained because P1 hashes original bytes.
        """
        data = _payload(payload_json)
        if actor_id is not None:
            _uuid(actor_id)
        authority = data["invitation_authority"]
        lifecycle = self._lifecycle(session, data["account_id"], data["workspace_id"])
        self._require_active(lifecycle, authority["lifecycle_id"], authority["lifecycle_epoch"])
        row = InvitationAuthorityIssuanceExtend(
            **{name: data[name] for name in _PAYLOAD_FIELDS - {"invitation_authority"}},
            **{name: authority[name] for name in _AUTHORITY_FIELDS - {"schema_version"}},
            payload_json=payload_json,
            payload_digest=sha256(payload_json.encode("utf-8")).hexdigest(),
            actor_id=actor_id,
            state="issued",
        )
        session.add(row)
        session.flush([row])
        return _issuance_record(row)

    @staticmethod
    def _require_active(row: InvitationAuthorityLifecycleExtend | None, lifecycle_id: str, epoch: int) -> None:
        if row is None or row.state != "active" or row.lifecycle_id != lifecycle_id or row.epoch != epoch:
            raise InvitationAuthorityConflict("invitation_lifecycle_conflict")

    def record_consumption(self, session: Session, *, receipt_json: str) -> InvitationIssuanceRecord:
        """Compare and record once, preserving an exact replay without a write.

        Even replay requires the same active lifecycle. This is storage of a
        caller-provided P1 receipt, not proof that Redis consumption occurred.
        """
        receipt = _receipt(receipt_json)
        lifecycle = self._lifecycle(session, receipt["account_id"], receipt["workspace_id"])
        self._require_active(lifecycle, receipt["lifecycle_id"], receipt["lifecycle_epoch"])
        row = self._issuance(session, receipt["issuance_id"])
        if row is None:
            raise InvitationAuthorityConflict("invitation_issuance_missing")
        stored = _payload(row.payload_json)
        stored_authority = stored["invitation_authority"]
        if sha256(row.payload_json.encode("utf-8")).hexdigest() != row.payload_digest:
            raise InvitationAuthorityConflict("invitation_payload_conflict")
        snapshot = {
            **{name: stored[name] for name in _PAYLOAD_FIELDS - {"invitation_authority"}},
            **{name: stored_authority[name] for name in _AUTHORITY_FIELDS - {"schema_version"}},
        }
        if any(getattr(row, name) != value for name, value in snapshot.items()):
            raise InvitationAuthorityConflict("invitation_payload_conflict")
        for name in _RECEIPT_FIELDS - {"schema_version", "status", "operation_id", "key_digest"}:
            if getattr(row, name) != receipt[name]:
                raise InvitationAuthorityConflict("invitation_receipt_conflict")
        if row.state == "consumed":
            if row.consumption_receipt_json != receipt_json:
                raise InvitationAuthorityConflict("invitation_receipt_conflict")
            return _issuance_record(row)
        if row.state != "issued" or row.consumption_receipt_json is not None or row.consumed_at is not None:
            raise InvitationAuthorityConflict("invitation_receipt_conflict")
        row.state = "consumed"
        row.consumption_receipt_json = receipt_json
        row.consumed_at = row.updated_at = naive_utc_now()
        session.flush([row])
        return _issuance_record(row)
