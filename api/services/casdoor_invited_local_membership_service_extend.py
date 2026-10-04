"""Private invited LOCAL persistence leaf; no admission, session or account setup.

An authenticated upstream owner must already obtain current roles and acquire
the complete canonical lease set. This service only owns one fresh SQL root and
one real B3 attempt. Unknown commit never becomes success or an automatic retry.
"""

from sqlalchemy.orm import Session

from repositories.casdoor_invited_login_guard_repository_extend import CasdoorInvitedLoginGuardRepository
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict


class CasdoorInvitedLocalMembershipService:
    def __init__(self, *, session_factory, configuration_factory):
        self._session_factory = session_factory
        self._configuration_factory = configuration_factory

    def persist_invited_local_memberships(self, attempt, *, roles, leases, deadline):
        """Return committed scalar persistence only; never caller authentication.

        No caller-supplied scope, completion, desired plan or B3 result is accepted.
        Caller owns lease cleanup, including SQL/commit uncertainty.
        """
        guard = None
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
                with session.begin():
                    owner = CasdoorInvitedLoginGuardRepository(session, self._configuration_factory)
                    guard = owner.prelock(attempt, roles=roles, leases=leases, deadline=deadline)
                    owner.persist_once(guard)
                    result = owner.verify_after(guard)
                    owner.append_receipt(guard)
                return result
        finally:
            if owner is not None:
                owner.revoke(guard)
