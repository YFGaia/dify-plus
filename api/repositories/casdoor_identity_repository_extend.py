"""Identity persistence within the caller's transaction, without provisioning.

The claims owner constructs VerifiedIdentityKey only after protocol verification;
this repository does not authenticate tokens or authorize link/invitation flows.
The caller supplies an existing, flushed Account and the expected namespace fence
epoch. No email lookup, account creation, activation, quota, session or external
I/O occurs here. The lease/generation owner remains a separate integration layer.

Writes require a caller-owned transaction. Only the attempted identity INSERT is
inside a SAVEPOINT; begin_nested flushes prior caller changes before that boundary,
and failures in those changes propagate. Never commit or roll back the outer UoW.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.errors import CasdoorErrorCode
from models.account import Account
from models.casdoor_extend import CasdoorIdentityExtend, CasdoorNamespaceExtend, CasdoorNamespaceLifecycle
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


class CasdoorIdentityConflict(ValueError):
    """Stable, non-enumerating error; never include identity/account/provider data."""

    code = CasdoorErrorCode.IDENTITY_CONFLICT

    def __init__(self) -> None:
        super().__init__(self.code.value)


@dataclass(frozen=True, repr=False)
class VerifiedIdentityKey:
    """Internal verified-claims handoff, not a token verification implementation."""

    namespace_id: UUID
    issuer: str
    organization: str
    subject: str

    def __post_init__(self) -> None:
        if not isinstance(self.namespace_id, UUID):
            raise CasdoorIdentityConflict()
        for value, limit in ((self.issuer, 2048), (self.organization, 255), (self.subject, 255)):
            if not isinstance(value, str) or not value:
                raise CasdoorIdentityConflict()
            try:
                encoded = value.encode("utf-8")
            except UnicodeError:
                raise CasdoorIdentityConflict() from None
            if len(encoded) > limit:
                raise CasdoorIdentityConflict()

    @property
    def subject_digest(self) -> str:
        return hashlib.sha256(self.subject.encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class IdentitySnapshot:
    identity_id: UUID
    namespace_id: UUID
    account_id: UUID
    issuer: str
    organization: str
    subject: str
    remote_email: str | None
    email_verified: bool | None


_IDENTITY_UNIQUE_NAMES = frozenset({"casdoor_identity_subject_key", "casdoor_identity_account_key"})
_SQLITE_IDENTITY_UNIQUES = frozenset(
    {
        "UNIQUE constraint failed: casdoor_identity_extend.namespace_id, casdoor_identity_extend.subject_digest",
        "UNIQUE constraint failed: casdoor_identity_extend.namespace_id, casdoor_identity_extend.account_id",
    }
)


def _is_identity_unique_violation(error: IntegrityError) -> bool:
    """Recognize only the two identity keys; other integrity failures propagate."""
    original = error.orig
    if isinstance(original, sqlite3.IntegrityError):
        return (
            original.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE and str(original) in _SQLITE_IDENTITY_UNIQUES
        )
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate == "23505":
        return getattr(getattr(original, "diag", None), "constraint_name", None) in _IDENTITY_UNIQUE_NAMES
    # MySQL DBAPI exposes ER_DUP_ENTRY plus a quoted (possibly table-qualified) key.
    arguments = getattr(original, "args", ())
    if len(arguments) >= 2 and arguments[0] == 1062 and isinstance(arguments[1], str):
        key = arguments[1].rsplit(" for key ", 1)
        if len(key) == 2:
            name = key[1].strip("'`\"").rsplit(".", 1)[-1]
            return name in _IDENTITY_UNIQUE_NAMES
    return False


class CasdoorIdentityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, key: VerifiedIdentityKey, *, expected_fence_epoch: int) -> IdentitySnapshot | None:
        """Find only by namespace/digest, then verify exact raw identity fields."""
        self._require_transaction()
        with self._session.no_autoflush:
            self._check_namespace(key, expected_fence_epoch=expected_fence_epoch)
            row = self._subject_row(key)
            if row is None:
                return None
            self._check_exact(row, key)
            return self._snapshot(row)

    def bind(
        self,
        key: VerifiedIdentityKey,
        *,
        account_id: UUID,
        expected_fence_epoch: int,
        remote_email: str | None = None,
        email_verified: bool | None = None,
    ) -> IdentitySnapshot:
        """Insert or return the exact same binding; never replace an association.

        Remote email/verification are an initial minimal snapshot only. An
        idempotent bind leaves existing fields unchanged; a future generation-aware
        profile owner updates snapshots without changing the local email.
        """
        self._require_transaction()
        if not isinstance(account_id, UUID):
            raise CasdoorIdentityConflict()
        if remote_email is not None and (
            not isinstance(remote_email, str) or not remote_email or len(remote_email.encode("utf-8")) > 255
        ):
            raise CasdoorIdentityConflict()
        if email_verified is not None and not isinstance(email_verified, bool):
            raise CasdoorIdentityConflict()
        # SAVEPOINT entry would flush all caller changes anyway. Do so before
        # checking current DB guards, so a caller's pending fence/delete cannot
        # pass a stale precheck. Caller flush failures remain outside recovery.
        self._session.flush()
        with self._session.no_autoflush:
            self._check_namespace(key, expected_fence_epoch=expected_fence_epoch, lock=True)
            # Shared locks serialize against namespace reset and account deletion;
            # they do not replace the future identity/email/member Redis leases.
            if (
                self._session.scalar(
                    sa.select(Account.id).where(Account.id == str(account_id)).with_for_update(read=True)
                )
                is None
            ):
                raise CasdoorIdentityConflict()
            existing = self._existing_binding(key, account_id)
            if existing is not None:
                return self._snapshot(existing)

        candidate = CasdoorIdentityExtend(
            namespace_id=str(key.namespace_id),
            account_id=str(account_id),
            issuer=key.issuer,
            organization=key.organization,
            subject=key.subject,
            remote_email=remote_email,
            email_verified=email_verified,
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        # Entry flushes caller pending changes outside our error recovery block.
        savepoint = self._session.begin_nested()
        try:
            with savepoint:
                self._session.add(candidate)
                self._session.flush()
        except IntegrityError as error:
            if not _is_identity_unique_violation(error):
                raise
            # Locking reads see the current winning row on MySQL REPEATABLE READ.
            # No winner visible is a safe conflict, not permission to retry INSERT.
            with self._session.no_autoflush:
                self._check_namespace(key, expected_fence_epoch=expected_fence_epoch, lock=True)
                existing = self._existing_binding(key, account_id)
                if existing is None:
                    raise CasdoorIdentityConflict() from None
                return self._snapshot(existing)
        return self._snapshot(candidate)

    def _require_transaction(self) -> None:
        if not self._session.in_transaction() or not self._session.is_active:
            raise RuntimeError("Casdoor identity repository requires an active caller-owned transaction")

    def _check_namespace(self, key: VerifiedIdentityKey, *, expected_fence_epoch: int, lock: bool = False) -> None:
        if type(expected_fence_epoch) is not int or expected_fence_epoch < 0:
            raise CasdoorIdentityConflict()
        statement = sa.select(
            CasdoorNamespaceExtend.expected_issuer,
            CasdoorNamespaceExtend.organization,
            CasdoorNamespaceExtend.lifecycle,
            CasdoorNamespaceExtend.fence_epoch,
        ).where(CasdoorNamespaceExtend.id == str(key.namespace_id))
        if lock:
            statement = statement.with_for_update(read=True)
        row = self._session.execute(statement).one_or_none()
        if (
            row is None
            or row.expected_issuer != key.issuer
            or row.organization != key.organization
            or row.lifecycle != CasdoorNamespaceLifecycle.ACTIVE
            or row.fence_epoch != expected_fence_epoch
        ):
            raise CasdoorIdentityConflict()

    def _subject_row(self, key: VerifiedIdentityKey, *, lock: bool = False) -> CasdoorIdentityExtend | None:
        statement = sa.select(CasdoorIdentityExtend).where(
            CasdoorIdentityExtend.namespace_id == str(key.namespace_id),
            CasdoorIdentityExtend.subject_digest == key.subject_digest,
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement.execution_options(populate_existing=True))

    def _existing_binding(self, key: VerifiedIdentityKey, account_id: UUID) -> CasdoorIdentityExtend | None:
        row = self._subject_row(key, lock=True)
        if row is not None:
            self._check_exact(row, key)
            if row.account_id != str(account_id):
                raise CasdoorIdentityConflict()
            return row
        if (
            self._session.scalar(
                sa.select(CasdoorIdentityExtend.id)
                .where(
                    CasdoorIdentityExtend.namespace_id == str(key.namespace_id),
                    CasdoorIdentityExtend.account_id == str(account_id),
                )
                .with_for_update()
            )
            is not None
        ):
            raise CasdoorIdentityConflict()
        return None

    @staticmethod
    def _check_exact(row: CasdoorIdentityExtend, key: VerifiedIdentityKey) -> None:
        if (
            row.namespace_id != str(key.namespace_id)
            or row.subject != key.subject
            or row.subject_digest != key.subject_digest
            or row.issuer != key.issuer
            or row.organization != key.organization
        ):
            raise CasdoorIdentityConflict()

    @staticmethod
    def _snapshot(row: CasdoorIdentityExtend) -> IdentitySnapshot:
        return IdentitySnapshot(
            identity_id=UUID(row.id),
            namespace_id=UUID(row.namespace_id),
            account_id=UUID(row.account_id),
            issuer=row.issuer,
            organization=row.organization,
            subject=row.subject,
            remote_email=row.remote_email,
            email_verified=row.email_verified,
        )
