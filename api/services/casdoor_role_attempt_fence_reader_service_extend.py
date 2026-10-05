"""Fresh SQL observation after an uncertain fence commit; no remote authority."""

from uuid import UUID

from core.casdoor.mapping import DesiredWorkspaceTarget
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_role_attempt_fence_reader_repository_extend import (
    CasdoorRoleAttemptFenceReaderRepository,
    RoleAttemptFenceReadObservation,
)
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session


class CasdoorRoleAttemptFenceReaderService:
    def __init__(self, *, session_factory):
        self._session_factory = session_factory

    def observe_dispatch_fence(
        self,
        version: GenerationPlanVersion,
        target: DesiredWorkspaceTarget,
        *,
        intent_id: UUID,
        reservation_id: UUID,
    ) -> RoleAttemptFenceReadObservation:
        """Return scalars only after successful rollback and close of our new root.

        The factory is trusted to create an independent actual Session. All
        ordinary lifecycle and read failures map to authorization_pending.
        """
        session = None
        try:
            if type(intent_id) is not UUID or type(reservation_id) is not UUID:
                raise CasdoorRoleIntentConflict()
            session = self._session_factory()
            try:
                if (
                    not isinstance(session, Session)
                    or not session.is_active
                    or session.in_transaction()
                    or session.new
                    or session.dirty
                    or session.deleted
                ):
                    raise CasdoorRoleIntentConflict()
                bind = session.get_bind()
                if isinstance(bind, Connection) and (bind.in_transaction() or bind.closed):
                    raise CasdoorRoleIntentConflict()
                session.begin()
                observation = CasdoorRoleAttemptFenceReaderRepository(session).observe(
                    version, target, intent_id=intent_id, reservation_id=reservation_id
                )
            finally:
                try:
                    if isinstance(session, Session):
                        session.rollback()
                finally:
                    if session is not None:
                        session.close()
            return observation
        except Exception:
            raise CasdoorRoleIntentConflict() from None
