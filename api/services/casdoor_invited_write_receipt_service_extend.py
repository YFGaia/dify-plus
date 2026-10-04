"""Fresh SQL-only receipt observation; no login/session or downstream authority.

The returned observation is stale after this read root closes. A future owner
must establish its own current scope, roles, leases and resource barriers.
"""

from sqlalchemy.orm import Session

from repositories.casdoor_invitation_operation_repository_extend import InvitationOperationAttempt
from repositories.casdoor_invited_write_receipt_repository_extend import CasdoorInvitedWriteReceiptRepository
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict


class CasdoorInvitedWriteReceiptService:
    def __init__(self, *, session_factory, configuration_factory):
        self._session_factory = session_factory
        self._configuration_factory = configuration_factory

    def observe_invited_write_receipt(self, attempt):
        """Observe in an independent root, then roll it back without committing."""
        if type(attempt) is not InvitationOperationAttempt:
            raise CasdoorLoginScopeConflict()
        with self._session_factory() as session:
            if (
                not isinstance(session, Session)
                or session.in_transaction()
                or not session.is_active
                or session.new
                or session.dirty
                or session.deleted
            ):
                raise CasdoorLoginScopeConflict()
            session.begin()
            try:
                observation = CasdoorInvitedWriteReceiptRepository(session, self._configuration_factory).observe(
                    attempt
                )
            finally:
                # No writes belong here, and no commit acknowledgement is needed.
                # A rollback/close failure must still prevent returning a value.
                session.rollback()
        return observation
