"""Independent SQL recovery observation after F1 acknowledgement uncertainty.

No admission/session, retry, repair or new durable receipt is produced. The
root must roll back and the Session close before a current observation escapes.
"""

from repositories.casdoor_invited_finalization_receipt_repository_extend import (
    CasdoorInvitedFinalizationReceiptRepository,
    _inputs,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


class CasdoorInvitedFinalizationReceiptService:
    def __init__(self, *, session_factory, configuration_factory):
        self._session_factory = session_factory
        self._configuration_factory = configuration_factory

    def observe_invited_finalization(self, attempt, *, roles):
        """Observe once in a clean new explicit root; any failure remains pending."""
        try:
            _inputs(attempt, roles)
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
                    observation = CasdoorInvitedFinalizationReceiptRepository(
                        session, self._configuration_factory
                    ).observe(attempt, roles=roles)
                finally:
                    session.rollback()
            return observation
        except (
            SQLAlchemyError,
            ValueError,
            RuntimeError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
            StopIteration,
        ):
            raise CasdoorLoginScopeConflict() from None
