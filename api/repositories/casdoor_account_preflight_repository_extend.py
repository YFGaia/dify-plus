"""Bounded read-only observations, never claims, leases or admission authority.

The future caller owns signed claims, source/invite eligibility, complete sorted
leases, online projections and shared preparation before its write UoW. These
observations authorize no mutation/session and do not establish global email
uniqueness. Integration/namespace locks precede account locks and final identity
reads. A missing account row cannot itself be locked.
"""

import re
from dataclasses import dataclass, fields
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.admission import AccountObservation, AdmissionContext, ExactBindingObservation
from core.casdoor.errors import CasdoorErrorCode
from libs.helper import email as validate_email
from models.account import Account, AccountStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
)
from services.account_email import normalize_email
from sqlalchemy.orm import Session

from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey

MAX_COLLISION_ACCOUNTS = 2048


class AccountPreflightConflict(ValueError):
    """Stable non-enumerating denial; no provider/account inputs in errors."""

    code = CasdoorErrorCode.IDENTITY_CONFLICT

    def __init__(self) -> None:
        super().__init__(self.code.value)


class AccountCollisionUnknown(AccountPreflightConflict):
    """Bounded scan was incomplete; never convert this failure to no collision."""

    code = CasdoorErrorCode.AUTHORIZATION_PENDING


class CollisionKnowledge(StrEnum):
    UNCHECKED = "unchecked"
    COMPLETE = "complete"


@dataclass(frozen=True, repr=False)
class AccountPreflightSnapshot:
    context: AdmissionContext
    exact_binding: ExactBindingObservation | None
    account: AccountObservation | None
    candidate_account_id: UUID | None
    candidate_absent: bool
    collision_knowledge: CollisionKnowledge
    collision_account_ids: tuple[UUID, ...]


def _text(value: object, limit: int) -> bool:
    try:
        return (
            type(value) is str
            and bool(value.strip())
            and len(value.encode("utf-8")) <= limit
            and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        )
    except UnicodeError:
        return False


def _complete(value: object, expected: type) -> bool:
    return type(value) is expected and all(hasattr(value, field.name) for field in fields(expected))


def _persisted_uuid(value: object) -> UUID:
    """Reject malformed/noncanonical stored IDs without exposing stored values."""
    if type(value) is not str or len(value) != 36:
        raise AccountPreflightConflict()
    try:
        parsed = UUID(value)
    except ValueError:
        raise AccountPreflightConflict() from None
    if str(parsed) != value:
        raise AccountPreflightConflict()
    return parsed


def _validate(context, key, candidate, collision_email) -> None:
    if (
        not _complete(context, AdmissionContext)
        or not _complete(key, VerifiedIdentityKey)
        or not all(
            type(value) is UUID
            for value in (
                context.integration_id,
                context.revision_id,
                context.active_revision_id,
                context.namespace_id,
                key.namespace_id,
            )
        )
        or (candidate is not None and type(candidate) is not UUID)
        or context.revision_id != context.active_revision_id
        or context.namespace_lifecycle is not CasdoorNamespaceLifecycle.ACTIVE
        or type(context.fence_epoch) is not int
        or not 0 <= context.fence_epoch <= 2**63 - 1
        or type(context.config_digest) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", context.config_digest)
        or not all(
            _text(value, limit)
            for value, limit in (
                (context.issuer, 2048),
                (context.organization, 255),
                (context.application, 255),
                (context.client_id, 255),
                (context.subject, 255),
                (key.issuer, 2048),
                (key.organization, 255),
                (key.subject, 255),
            )
        )
        or (key.namespace_id, key.issuer, key.organization, key.subject)
        != (context.namespace_id, context.issuer, context.organization, context.subject)
        or (collision_email is not None and not _text(collision_email, 254))
    ):
        raise AccountPreflightConflict()
    if collision_email is not None:
        try:
            validate_email(collision_email)
        except ValueError:
            raise AccountPreflightConflict() from None


class CasdoorAccountPreflightRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def reconstruct(
        self,
        context: AdmissionContext,
        key: VerifiedIdentityKey,
        *,
        candidate_account_id: UUID | None = None,
        collision_email: str | None = None,
    ) -> AccountPreflightSnapshot:
        """Reload current owners without flushing or owning transaction lifetime.

        Candidate UUID is only a selector, never source/invite/creation proof.
        Omitted email (and the exact-bound fast path) leaves collisions UNCHECKED.
        Discovery is unlocked and used only for ordering; final binding must match.
        """
        return self._reconstruct(
            context, key, candidate_account_id=candidate_account_id, collision_email=collision_email
        )

    def _observe_postwrite_new_collisions(
        self,
        context: AdmissionContext,
        key: VerifiedIdentityKey,
        *,
        own_new_account_id: UUID,
        collision_email: str,
    ) -> AccountPreflightSnapshot:
        """Observe after caller flush; the selector does not prove NEW creation.

        The caller must separately possess same-attempt creation provenance and
        retain current complete leases and commit guards. Equivalent old bound
        state can pass these consistency checks; no capability is issued here.
        """
        if type(own_new_account_id) is not UUID or not _text(collision_email, 254):
            raise AccountPreflightConflict()
        return self._reconstruct(
            context,
            key,
            candidate_account_id=own_new_account_id,
            collision_email=collision_email,
            postwrite_account_id=own_new_account_id,
        )

    def _reconstruct(
        self, context, key, *, candidate_account_id, collision_email, postwrite_account_id=None
    ) -> AccountPreflightSnapshot:
        session = self._session
        if (
            not isinstance(session, Session)
            or not session.is_active
            or session.get_transaction() is None
            or not session.get_transaction().is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise AccountPreflightConflict()
        _validate(context, key, candidate_account_id, collision_email)
        with session.no_autoflush:
            self._check_chain(context)
            discovered = self._subject(key, lock=False)
            selected = candidate_account_id
            if discovered is not None:
                self._check_exact(discovered, key)
                bound_id = _persisted_uuid(discovered.account_id)
                if selected is not None and selected != bound_id:
                    raise AccountPreflightConflict()
                selected = bound_id
            # One selected account in this slice. Explicit sorting preserves the
            # ordering contract if the selector set is expanded in a later packet.
            accounts = {}
            for account_id in sorted(() if selected is None else (selected,), key=str):
                accounts[account_id] = session.execute(
                    sa.select(
                        Account.id,
                        sa.cast(Account.status, sa.String).label("status"),
                        Account.initialized_at,
                        Account.email,
                        Account.normalized_email,
                    )
                    .where(Account.id == str(account_id))
                    .with_for_update()
                ).one_or_none()
            final = self._subject(key, lock=True)
            if final != discovered:
                raise AccountPreflightConflict()
            reverse = None
            if selected is not None:
                reverse = session.execute(
                    self._identity_select()
                    .where(
                        CasdoorIdentityExtend.namespace_id == str(key.namespace_id),
                        CasdoorIdentityExtend.account_id == str(selected),
                    )
                    .limit(2)
                    .with_for_update()
                ).all()
                if len(reverse) > 1 or (final is not None and reverse != [final]):
                    raise AccountPreflightConflict()
                if reverse:
                    # Any other subject already attached to the candidate denies.
                    self._check_exact(reverse[0], key)
                    if final is None:
                        raise AccountPreflightConflict()
            row = accounts.get(selected)
            if final is not None and row is None:
                raise AccountPreflightConflict()
            account = None
            if row is not None:
                try:
                    status = AccountStatus(row.status)
                except ValueError:
                    raise AccountPreflightConflict() from None
                if status not in (AccountStatus.ACTIVE, AccountStatus.PENDING, AccountStatus.UNINITIALIZED):
                    raise AccountPreflightConflict()
                account = AccountObservation(
                    _persisted_uuid(row.id),
                    status,
                    row.initialized_at,
                    row.email,
                    reverse[0].subject if reverse else None,
                )
            binding = (
                None
                if final is None
                else ExactBindingObservation(
                    key.namespace_id, key.issuer, key.organization, key.subject, _persisted_uuid(final.account_id)
                )
            )
            knowledge = CollisionKnowledge.UNCHECKED
            collisions = ()
            if postwrite_account_id is not None:
                if (
                    binding is None
                    or binding.account_id != postwrite_account_id
                    or account is None
                    or account.account_id != postwrite_account_id
                    or account.status is not AccountStatus.ACTIVE
                    or account.initialized_at is None
                    or account.email != collision_email
                ):
                    raise AccountPreflightConflict()
                try:
                    target = normalize_email(collision_email)
                    if row.normalized_email != target or normalize_email(row.email) != target:
                        raise AccountPreflightConflict()
                except (AttributeError, TypeError, ValueError, UnicodeError):
                    raise AccountPreflightConflict() from None
                collisions = self._collisions(collision_email, exclude_account_id=postwrite_account_id)
                knowledge = CollisionKnowledge.COMPLETE
            elif final is None and collision_email is not None:
                collisions = self._collisions(collision_email)
                knowledge = CollisionKnowledge.COMPLETE
            return AccountPreflightSnapshot(
                context,
                binding,
                account,
                candidate_account_id,
                candidate_account_id is not None and row is None,
                knowledge,
                collisions,
            )

    @staticmethod
    def _identity_select():
        return sa.select(
            CasdoorIdentityExtend.id,
            CasdoorIdentityExtend.namespace_id,
            CasdoorIdentityExtend.account_id,
            CasdoorIdentityExtend.issuer,
            CasdoorIdentityExtend.organization,
            CasdoorIdentityExtend.subject,
            CasdoorIdentityExtend.subject_digest,
        )

    def _subject(self, key, *, lock):
        statement = (
            self._identity_select()
            .where(
                CasdoorIdentityExtend.namespace_id == str(key.namespace_id),
                CasdoorIdentityExtend.subject_digest == key.subject_digest,
            )
            .limit(2)
        )
        rows = self._session.execute(statement.with_for_update() if lock else statement).all()
        if len(rows) > 1:
            raise AccountPreflightConflict()
        return rows[0] if rows else None

    @staticmethod
    def _check_exact(row, key):
        if (row.namespace_id, row.issuer, row.organization, row.subject, row.subject_digest) != (
            str(key.namespace_id),
            key.issuer,
            key.organization,
            key.subject,
            key.subject_digest,
        ):
            raise AccountPreflightConflict()

    def _collisions(self, email, *, exclude_account_id=None):
        statement = sa.select(Account.id, Account.email, Account.normalized_email)
        # Exclude only the freshly validated selected row before the sentinel
        # limit, preserving the prewrite budget of 2048 other accounts.
        if exclude_account_id is not None:
            statement = statement.where(Account.id != str(exclude_account_id))
        rows = self._session.execute(statement.order_by(Account.id).limit(MAX_COLLISION_ACCOUNTS + 1)).all()
        if len(rows) > MAX_COLLISION_ACCOUNTS:
            raise AccountCollisionUnknown()
        try:
            target = normalize_email(email)
            return tuple(
                _persisted_uuid(row.id)
                for row in rows
                if normalize_email(row.email) == target or row.normalized_email == target
            )
        except (AttributeError, TypeError, ValueError, UnicodeError):
            if exclude_account_id is None:
                raise
            raise AccountPreflightConflict() from None

    def _check_chain(self, context):
        integration = self._session.execute(
            sa.select(CasdoorIntegrationExtend.enabled, CasdoorIntegrationExtend.active_revision_id)
            .where(CasdoorIntegrationExtend.id == str(context.integration_id), CasdoorIntegrationExtend.slot == 1)
            .with_for_update()
        ).one_or_none()
        if (
            integration is None
            or integration.enabled is not True
            or integration.active_revision_id != str(context.revision_id)
        ):
            raise AccountPreflightConflict()
        namespace = self._session.execute(
            sa.select(
                CasdoorNamespaceExtend.integration_id,
                CasdoorNamespaceExtend.expected_issuer,
                CasdoorNamespaceExtend.organization,
                CasdoorNamespaceExtend.application,
                CasdoorNamespaceExtend.client_id,
                CasdoorNamespaceExtend.lifecycle,
                CasdoorNamespaceExtend.fence_epoch,
            )
            .where(CasdoorNamespaceExtend.id == str(context.namespace_id))
            .with_for_update()
        ).one_or_none()
        chain = (
            str(context.integration_id),
            context.issuer,
            context.organization,
            context.application,
            context.client_id,
        )
        if namespace is None or tuple(namespace) != (*chain, CasdoorNamespaceLifecycle.ACTIVE, context.fence_epoch):
            raise AccountPreflightConflict()
        revision = self._session.execute(
            sa.select(
                CasdoorConfigRevisionExtend.integration_id,
                CasdoorConfigRevisionExtend.expected_issuer,
                CasdoorConfigRevisionExtend.organization,
                CasdoorConfigRevisionExtend.application,
                CasdoorConfigRevisionExtend.client_id,
                CasdoorConfigRevisionExtend.namespace_id,
                CasdoorConfigRevisionExtend.config_digest,
            ).where(CasdoorConfigRevisionExtend.id == str(context.revision_id))
        ).one_or_none()
        if revision is None or tuple(revision) != (*chain, str(context.namespace_id), context.config_digest):
            raise AccountPreflightConflict()
