"""Bounded required-intent observation under caller-owned root and parent locks.

The caller owns admission, configuration checks, complete leases and ordered
parent/scope locks. This read neither establishes authority nor proves global
quiescence, finalization or session eligibility. All states and generations block.
"""

from uuid import UUID

import sqlalchemy as sa
from models.casdoor_extend import CasdoorIntentKind
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from sqlalchemy.orm import Session


class CasdoorRequiredIntentConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("authorization_pending")


class CasdoorRequiredIntentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def read_locked(self, account_id: UUID, workspace_id: UUID, *, invitation_guard: object | None = None) -> tuple:
        """Return zero or one scalar ID rows, preserving LOCAL preparation state.

        Exact UUID inputs are internal scope data, never an authorization receipt.
        History association covers every namespace, without materializing history.
        """
        if not isinstance(self._session, Session):
            raise CasdoorRequiredIntentConflict()
        transaction = self._session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or not self._session.is_active
            or self._session.get_nested_transaction() is not None
            or self._session.new
            or self._session.dirty
            or self._session.deleted
            or type(account_id) is not UUID
            or type(workspace_id) is not UUID
        ):
            raise CasdoorRequiredIntentConflict()
        history_ids = sa.select(History.id).where(
            History.account_id == str(account_id), History.workspace_id == str(workspace_id)
        )
        excluded = None
        if invitation_guard is not None:
            from repositories.casdoor_invited_login_guard_repository_extend import _invitation_exclusion

            excluded = _invitation_exclusion(invitation_guard, self._session, account_id, workspace_id)
        with self._session.no_autoflush:
            statement = sa.select(Intent.id).where(
                Intent.kind != CasdoorIntentKind.PROFILE_AVATAR,
                sa.or_(
                    sa.and_(
                        Intent.account_id == str(account_id),
                        sa.or_(Intent.workspace_id == str(workspace_id), Intent.workspace_id.is_(None)),
                    ),
                    Intent.membership_id.in_(history_ids),
                ),
            )
            if excluded is not None:
                statement = statement.where(Intent.id != excluded)
            return tuple(
                self._session.execute(
                    statement.order_by(Intent.namespace_id, Intent.scope_digest, Intent.id).limit(1).with_for_update()
                )
            )
