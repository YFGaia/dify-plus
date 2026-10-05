"""Observe a current conservative role fence without recovery or send authority.

The service owns a fresh explicit root and its rollback/close. Policy remains
with the accepted intent/attempt owners; this reader never reserves or arms.
"""

from dataclasses import dataclass
from uuid import UUID

from core.casdoor.mapping import DesiredWorkspaceTarget
from models.casdoor_extend import CasdoorOperationState, CasdoorTerminationState
from sqlalchemy.orm import Session, SessionTransactionOrigin

from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_role_attempt_repository_extend import (
    CasdoorRoleAttemptRepository,
)
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict


@dataclass(frozen=True, repr=False)
class RoleAttemptFenceReadObservation:
    """Current trusted SQL projection only, never proof of send or termination."""

    intent_id: UUID
    reservation_id: UUID
    membership_id: UUID
    join_id: UUID
    desired_payload_digest: str
    scope_digest: str


class CasdoorRoleAttemptFenceReaderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._attempt = CasdoorRoleAttemptRepository(session)

    def observe(
        self,
        version: GenerationPlanVersion,
        target: DesiredWorkspaceTarget,
        *,
        intent_id: UUID,
        reservation_id: UUID,
    ) -> RoleAttemptFenceReadObservation:
        """Read twice in a fresh root; any owner, scope or complete row drift refuses.

        A reserved/absent row remains pending. Nothing here establishes whether
        an independently executed request escaped or which transaction committed.
        """
        if not isinstance(self._session, Session):
            raise CasdoorRoleIntentConflict()
        root = self._session.get_transaction()
        connections = getattr(root, "_connections", None)
        if (
            root is None
            or root.origin is not SessionTransactionOrigin.BEGIN
            or type(connections) is not dict
            or connections
        ):
            raise CasdoorRoleIntentConflict()
        self._attempt._guard._require_clean_root()
        self._attempt._guard._validate(version, target)
        if type(intent_id) is not UUID or type(reservation_id) is not UUID:
            raise CasdoorRoleIntentConflict()
        with self._session.no_autoflush:
            before = self._attempt._read_attempt_scope(version, target, intent_id)
            scope, digest, values, join, history, row = before
            armed = dict(
                values,
                attempt_count=1,
                attempt_id=str(reservation_id),
                operation_state=CasdoorOperationState.IN_FLIGHT,
                termination_state=CasdoorTerminationState.UNCONFIRMED,
            )
            if {name: row[name] for name in values} != armed:
                raise CasdoorRoleIntentConflict()
            after = self._attempt._read_attempt_scope(version, target, intent_id)
            self._attempt._guard._require_clean_root()
            if after != before:
                raise CasdoorRoleIntentConflict()
            return RoleAttemptFenceReadObservation(
                intent_id, reservation_id, UUID(history.id), UUID(join.id), scope["desired_payload_digest"], digest
            )
