"""Private invited LOGIN account composition, never invitation finalization.

The trusted caller owns signed claims, same-attempt token preparation, complete
sorted leases, configuration/fence/deadline barriers and whole-root rollback.
Invitation withdrawal/expiry, membership, token consumption and sessions remain
I19 responsibilities. Public projections and this wrapper cannot authenticate a
provider or a token. Only the original shared persistence owner checks issuance.
"""

import math
import time
from dataclasses import dataclass, field, fields
from uuid import UUID
from weakref import WeakSet

import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.casdoor.admission import (
    AccountObservation,
    AdmissionAction,
    AdmissionContext,
    AdmissionPlan,
    ExactBindingObservation,
    InvitationObservation,
    SharedOwnerRequirement,
)
from core.casdoor.auth_transactions import AuthMode
from models.account import Account, AccountStatus, Tenant, TenantStatus
from repositories.casdoor_account_preflight_repository_extend import (
    AccountPreflightConflict,
    AccountPreflightSnapshot,
    CasdoorAccountPreflightRepository,
)
from repositories.casdoor_identity_repository_extend import CasdoorIdentityRepository, VerifiedIdentityKey
from services.account_activation_service import AccountActivationService, _PreparedInvitedInitialization
from services.entities.account_activation_entities import AccountInvitation, InvitedAccountObservation


@dataclass(frozen=True, slots=True, repr=False)
class InvitedLoginAccountPersistence:
    """Uncommitted local IDs and requested account outcome only."""

    account_id: UUID
    identity_id: UUID
    workspace_id: UUID
    account_outcome: AdmissionAction


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class _PreparedInvitedLoginAccount:
    plan: AdmissionPlan
    preflight: AccountPreflightSnapshot
    invitation: InvitationObservation
    key: VerifiedIdentityKey
    deadline: float
    _shared: _PreparedInvitedInitialization
    _owner: object
    _consumed: bool = field(default=False, init=False)


def _complete(value, expected):
    return type(value) is expected and all(hasattr(value, item.name) for item in fields(expected))


def _deadline(value):
    try:
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value - time.monotonic() <= 45:
            raise AccountPreflightConflict()
    except OverflowError:
        raise AccountPreflightConflict() from None


def _consistent(plan, snapshot, invitation, shared):
    # Shape and equality are consistency checks, deliberately not shared issuance.
    if (
        not _complete(plan, AdmissionPlan)
        or not _complete(plan.context, AdmissionContext)
        or not _complete(snapshot, AccountPreflightSnapshot)
        or not _complete(invitation, InvitationObservation)
        or not _complete(shared, _PreparedInvitedInitialization)
        or not _complete(shared.observation, InvitedAccountObservation)
        or not _complete(shared.observation.invitation, AccountInvitation)
        or not _complete(snapshot.account, AccountObservation)
        or plan.context != snapshot.context
        or plan.mode is not AuthMode.LOGIN
        or type(plan.action) is not AdmissionAction
        or plan.creation_email is not None
    ):
        raise AccountPreflightConflict()
    account, binding, actual = snapshot.account, snapshot.exact_binding, shared.observation.invitation
    if (
        any(
            type(value) is not UUID
            for value in (plan.account_id, invitation.account_id, invitation.workspace_id, invitation.namespace_id)
        )
        or (invitation.namespace_id, invitation.subject) != (plan.context.namespace_id, plan.context.subject)
        or (account.account_id, invitation.account_id, snapshot.candidate_account_id)
        != (plan.account_id, plan.account_id, plan.account_id)
        or snapshot.candidate_absent is not False
        or (shared.account_id, actual.account_id, actual.workspace_id)
        != (str(plan.account_id), str(plan.account_id), str(invitation.workspace_id))
        or (account.email, shared.account_email, actual.account_email) != (invitation.email,) * 3
        or type(account.status) is not AccountStatus
        or account.status not in (AccountStatus.ACTIVE, AccountStatus.PENDING, AccountStatus.UNINITIALIZED)
        or (shared.account_status, actual.account_status) != (account.status,) * 2
        or (shared.initialized_at, shared.observation.initialized_at) != (account.initialized_at,) * 2
        or plan.setup != shared.setup
    ):
        raise AccountPreflightConflict()
    if binding is None:
        if account.namespace_subject is not None:
            raise AccountPreflightConflict()
    elif (
        not _complete(binding, ExactBindingObservation)
        or (binding.namespace_id, binding.issuer, binding.organization, binding.subject, binding.account_id)
        != (
            plan.context.namespace_id,
            plan.context.issuer,
            plan.context.organization,
            plan.context.subject,
            plan.account_id,
        )
        or account.namespace_subject != plan.context.subject
    ):
        raise AccountPreflightConflict()
    owners = (SharedOwnerRequirement.INVITATION_ACTIVATION_PREPARE,)
    if account.initialized_at is None:
        action = AdmissionAction.INITIALIZE_BOUND if binding else AdmissionAction.INITIALIZE_INVITED
        owners += (SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,)
        if shared.setup is None:
            raise AccountPreflightConflict()
    elif account.status is not AccountStatus.ACTIVE:
        action = AdmissionAction.ACTIVATE_BOUND if binding else AdmissionAction.ACTIVATE_INVITED
        owners += (SharedOwnerRequirement.ACCOUNT_STATUS_ACTIVATION_PERSIST,)
    else:
        action = AdmissionAction.USE_BOUND if binding else AdmissionAction.USE_INVITED
    if (
        (account.initialized_at is not None and plan.setup is not None)
        or plan.action is not action
        or plan.required_shared_owners != owners
    ):
        raise AccountPreflightConflict()


class CasdoorInvitedLoginAccountService:
    def __init__(self, *, activation: AccountActivationService):
        self._activation = activation
        self._issued: WeakSet[_PreparedInvitedLoginAccount] = WeakSet()

    def _prepare_invited_login(self, *, plan, preflight, invitation, shared, deadline, source=None):
        """Wrap ACTUAL same-attempt shared preparation obtained outside writes.

        The admission caller must already apply the original verified-email
        policy to signed provider claims. InvitationObservation cannot do this.
        No second policy/freeze call or shared-registry predicate is introduced.
        """
        _deadline(deadline)
        _consistent(plan, preflight, invitation, shared)
        if source is not None or shared._owner is not self._activation or shared._consumed:
            raise AccountPreflightConflict()
        context = plan.context
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        prepared = _PreparedInvitedLoginAccount(plan, preflight, invitation, key, deadline, shared, self)
        self._issued.add(prepared)
        return prepared

    def persist_invited_login_account(
        self, prepared, *, session: Session, context: AdmissionContext, key: VerifiedIdentityKey
    ):
        """Consume wrapper, reconstruct current owners, assign account, bind last.

        All failures require whole caller rollback and a fresh full preparation.
        This method neither commits nor rolls back. Outer sorted scope must
        include the exact invitation workspace before entering this method.
        """
        if (
            type(prepared) is not _PreparedInvitedLoginAccount
            or prepared not in self._issued
            or prepared._owner is not self
            or prepared._consumed
        ):
            raise AccountPreflightConflict()
        self._issued.discard(prepared)
        object.__setattr__(prepared, "_consumed", True)
        if (
            not isinstance(session, Session)
            or not session.is_active
            or session.get_transaction() is None
            or not session.get_transaction().is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
            or not _complete(context, AdmissionContext)
            or context != prepared.plan.context
            or not _complete(key, VerifiedIdentityKey)
            or key != prepared.key
        ):
            raise AccountPreflightConflict()
        _deadline(prepared.deadline)
        current = CasdoorAccountPreflightRepository(session).reconstruct(
            context, key, candidate_account_id=prepared.invitation.account_id
        )
        _consistent(prepared.plan, current, prepared.invitation, prepared._shared)
        if current.account != prepared.preflight.account or current.exact_binding != prepared.preflight.exact_binding:
            raise AccountPreflightConflict()
        with session.no_autoflush:
            workspace = session.scalar(
                sa.select(Tenant.id)
                .where(Tenant.id == str(prepared.invitation.workspace_id), Tenant.status == TenantStatus.NORMAL)
                .with_for_update()
            )
            account = session.scalar(
                sa.select(Account)
                .where(Account.id == str(prepared.invitation.account_id))
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        if (
            workspace is None
            or account is None
            or (account.id, account.email, account.status, account.initialized_at)
            != (
                str(current.account.account_id),
                current.account.email,
                current.account.status,
                current.account.initialized_at,
            )
        ):
            raise AccountPreflightConflict()
        _deadline(prepared.deadline)
        # Original issued-object registry is the ONLY shared authenticity owner.
        # It executes before account assignment and before identity's flush/savepoint.
        self._activation.persist_invited_initialization(prepared._shared, account=account, session=session)
        identity = CasdoorIdentityRepository(session).bind(
            key, account_id=prepared.invitation.account_id, expected_fence_epoch=context.fence_epoch
        )
        _deadline(prepared.deadline)
        return InvitedLoginAccountPersistence(
            prepared.invitation.account_id, identity.identity_id, prepared.invitation.workspace_id, prepared.plan.action
        )
