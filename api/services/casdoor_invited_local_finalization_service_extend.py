"""Private invited LOCAL membership-phase SQL producer; no admission or session.

The verified-role owner supplies current roles and already acquired full leases.
This service never fetches roles, expands leases, completes an intent, or retries
an unknown commit. Caller remains responsible for lease cleanup and independent
future F2 recovery after uncertain commit acknowledgement.
"""

from core.casdoor.leases import CasdoorLeases
from core.casdoor.role_graph import EffectiveRoleSnapshot
from repositories.casdoor_invitation_operation_repository_extend import (
    InvitationOperationAttempt,
)
from repositories.casdoor_invited_local_finalization_repository_extend import (
    CasdoorInvitedLocalFinalizationRepository,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from sqlalchemy.orm import Session


class CasdoorInvitedLocalFinalizationService:
    def __init__(self, *, session_factory, configuration_factory):
        self._session_factory = session_factory
        self._configuration_factory = configuration_factory

    def finalize_invited_local_memberships(self, attempt, *, roles, leases, deadline):
        """One clean root, one producer, one commit; exception yields no result."""
        if (
            type(attempt) is not InvitationOperationAttempt
            or type(roles) is not EffectiveRoleSnapshot
            or type(leases) is not CasdoorLeases
        ):
            raise CasdoorLoginScopeConflict()
        owner = None
        try:
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
                    owner = CasdoorInvitedLocalFinalizationRepository(session, self._configuration_factory)
                    receipt = owner.produce(attempt, roles=roles, leases=leases, deadline=deadline)
                    # produce's last deadline follows its full SQL reread. No I/O
                    # or callback belongs between that barrier and this commit.
                    session.commit()
                except BaseException:
                    # Rollback never makes an attempted commit safe to retry.
                    session.rollback()
                    raise
                return receipt
        finally:
            if owner is not None:
                owner.revoke()
