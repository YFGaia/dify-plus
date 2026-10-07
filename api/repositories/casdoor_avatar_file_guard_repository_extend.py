"""Caller-owned per-file SQL coordination, without tenant or physical-I/O authority.

Acquire this parent before Integration/Account locks. Core statements deliberately
avoid ORM pending state. SQLite must acquire its writer lock before the fresh read;
FOR UPDATE alone is not a SQLite exclusion mechanism. Every failure leaves rollback
to the caller. No savepoint, commit, expired-parent deletion or cleanup issuer exists.
"""

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransactionOrigin

from models.casdoor_avatar_file_guard_extend import CasdoorAvatarFileGuardExtend

_MAX_VERSION = 9007199254740991
_STAGES = frozenset({"unbound", "reserved", "cleanup_pending", "cleanup_complete"})
_TABLE = CasdoorAvatarFileGuardExtend.__table__
_FAILED_ROOT = "_casdoor_avatar_file_guard_failed_root"


class CasdoorAvatarFileGuardConflict(ValueError):
    """The caller must roll back; no incomplete guard grants write admission."""


@dataclass(frozen=True)
class AvatarFileGuardSnapshot:
    file_id: UUID
    version: int
    stage: str
    intent_id: UUID | None
    attempt_id: UUID | None


def _insert_parent(dialect: str, file_id: UUID):
    values = dict(file_id=str(file_id), version=1, stage="unbound", intent_id=None, attempt_id=None)
    if dialect == "postgresql":
        return postgresql.insert(_TABLE).values(**values).on_conflict_do_nothing(index_elements=[_TABLE.c.file_id])
    if dialect == "sqlite":
        return sqlite.insert(_TABLE).values(**values).on_conflict_do_nothing(index_elements=[_TABLE.c.file_id])
    if dialect == "mysql":
        return mysql.insert(_TABLE).values(**values).on_duplicate_key_update(file_id=_TABLE.c.file_id)
    raise CasdoorAvatarFileGuardConflict()


class CasdoorAvatarFileGuardRepository:
    def _cleanup_cas(self, snapshot: AvatarFileGuardSnapshot, *, complete=False) -> AvatarFileGuardSnapshot:
        """Existing exact same-root binding; stage is admission state, not I/O authority."""
        transaction = self._root()
        prior, target = ("cleanup_pending", "cleanup_complete") if complete else ("reserved", "cleanup_pending")
        if (
            type(complete) is not bool
            or type(snapshot) is not AvatarFileGuardSnapshot
            or self._held.get(snapshot.file_id) is not snapshot
            or snapshot.stage != prior
            or snapshot.version >= _MAX_VERSION
            or snapshot.intent_id is None
            or snapshot.attempt_id is None
        ):
            raise CasdoorAvatarFileGuardConflict()
        try:
            changed = self._session.execute(
                sa.update(_TABLE)
                .where(
                    _TABLE.c.file_id == str(snapshot.file_id),
                    _TABLE.c.version == snapshot.version,
                    _TABLE.c.stage == prior,
                    _TABLE.c.intent_id == str(snapshot.intent_id),
                    _TABLE.c.attempt_id == str(snapshot.attempt_id),
                )
                .values(version=snapshot.version + 1, stage=target)
            ).rowcount
            fresh = self._read(snapshot.file_id)
            if changed != 1 or fresh != AvatarFileGuardSnapshot(
                snapshot.file_id, snapshot.version + 1, target, snapshot.intent_id, snapshot.attempt_id
            ):
                raise CasdoorAvatarFileGuardConflict()
            self._held[snapshot.file_id] = fresh
            return fresh
        except (SQLAlchemyError, CasdoorAvatarFileGuardConflict):
            self._session.info[_FAILED_ROOT] = transaction
            self._held.clear()
            raise CasdoorAvatarFileGuardConflict() from None

    def __init__(self, session: Session):
        self._session = session
        self._transaction = None
        self._held: dict[UUID, AvatarFileGuardSnapshot] = {}

    def _root(self):
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
            or session.get_bind().dialect.name not in ("postgresql", "mysql", "sqlite")
            or session.info.get(_FAILED_ROOT) is transaction
        ):
            raise CasdoorAvatarFileGuardConflict()
        if self._transaction is not transaction:
            self._held.clear()
            self._transaction = transaction
        session.info.pop(_FAILED_ROOT, None)
        return transaction

    def _read(self, file_id: UUID) -> AvatarFileGuardSnapshot:
        row = (
            self._session.execute(sa.select(*_TABLE.columns).where(_TABLE.c.file_id == str(file_id)).with_for_update())
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise CasdoorAvatarFileGuardConflict()
        try:
            parsed_id = UUID(row["file_id"])
            intent_id = UUID(row["intent_id"]) if row["intent_id"] is not None else None
            attempt_id = UUID(row["attempt_id"]) if row["attempt_id"] is not None else None
        except (ValueError, TypeError, AttributeError):
            raise CasdoorAvatarFileGuardConflict() from None
        if (
            parsed_id != file_id
            or str(parsed_id) != row["file_id"]
            or type(row["version"]) is not int
            or not 1 <= row["version"] <= _MAX_VERSION
            or row["stage"] not in _STAGES
            or (row["stage"] == "unbound") != (intent_id is None and attempt_id is None)
            or (row["stage"] != "unbound" and (intent_id is None or attempt_id is None))
            or (intent_id is not None and str(intent_id) != row["intent_id"])
            or (attempt_id is not None and str(attempt_id) != row["attempt_id"])
        ):
            raise CasdoorAvatarFileGuardConflict()
        return AvatarFileGuardSnapshot(parsed_id, row["version"], row["stage"], intent_id, attempt_id)

    def _lock(self, file_id: UUID, *, create: bool) -> AvatarFileGuardSnapshot:
        transaction = self._root()
        if type(file_id) is not UUID:
            raise CasdoorAvatarFileGuardConflict()
        try:
            with self._session.no_autoflush:
                if create:
                    self._session.execute(_insert_parent(self._session.get_bind().dialect.name, file_id))
                if self._session.get_bind().dialect.name == "sqlite":
                    # Even an existing parent needs a write before its fresh snapshot.
                    changed = self._session.execute(
                        sa.update(_TABLE).where(_TABLE.c.file_id == str(file_id)).values(version=_TABLE.c.version)
                    ).rowcount
                    if changed != 1:
                        raise CasdoorAvatarFileGuardConflict()
                snapshot = self._read(file_id)
                self._held[file_id] = snapshot
                return snapshot
        except (SQLAlchemyError, CasdoorAvatarFileGuardConflict):
            self._session.info[_FAILED_ROOT] = transaction
            self._held.clear()
            raise CasdoorAvatarFileGuardConflict() from None

    def lock_or_create(self, file_id: UUID) -> AvatarFileGuardSnapshot:
        """Permanent common parent, including the initial Integration-absence case."""
        return self._lock(file_id, create=True)

    def lock_existing(self, file_id: UUID) -> AvatarFileGuardSnapshot:
        """Existing parent only; absence never reconstructs a reserved file's proof."""
        return self._lock(file_id, create=False)

    def bind_reservation(
        self, snapshot: AvatarFileGuardSnapshot, *, intent_id: UUID, attempt_id: UUID
    ) -> AvatarFileGuardSnapshot:
        """Same-root scalar CAS; the original caller still owes all reservation/audit checks."""
        transaction = self._root()
        if (
            type(snapshot) is not AvatarFileGuardSnapshot
            or type(snapshot.file_id) is not UUID
            or self._held.get(snapshot.file_id) is not snapshot
            or type(intent_id) is not UUID
            or type(attempt_id) is not UUID
            or snapshot.stage != "unbound"
            or snapshot.version >= _MAX_VERSION
        ):
            raise CasdoorAvatarFileGuardConflict()
        try:
            with self._session.no_autoflush:
                result = self._session.execute(
                    sa.update(_TABLE)
                    .where(
                        _TABLE.c.file_id == str(snapshot.file_id),
                        _TABLE.c.version == snapshot.version,
                        _TABLE.c.stage == "unbound",
                        _TABLE.c.intent_id.is_(None),
                        _TABLE.c.attempt_id.is_(None),
                    )
                    .values(
                        version=snapshot.version + 1,
                        stage="reserved",
                        intent_id=str(intent_id),
                        attempt_id=str(attempt_id),
                    )
                )
                if result.rowcount != 1:
                    raise CasdoorAvatarFileGuardConflict()
                fresh = self._read(snapshot.file_id)
                if fresh != AvatarFileGuardSnapshot(
                    snapshot.file_id, snapshot.version + 1, "reserved", intent_id, attempt_id
                ):
                    raise CasdoorAvatarFileGuardConflict()
                self._held[snapshot.file_id] = fresh
                return fresh
        except (SQLAlchemyError, CasdoorAvatarFileGuardConflict):
            self._session.info[_FAILED_ROOT] = transaction
            self._held.clear()
            raise CasdoorAvatarFileGuardConflict() from None
