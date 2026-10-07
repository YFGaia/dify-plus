"""Private ordinary-LOGIN account composition in a caller-owned transaction.

Plans and preflight projections are consistency inputs, never authentication.
The trusted caller owns signed/online proof, complete sorted leases and the whole
transaction's rollback on any exception. Returned IDs prove no admission, final
email collision check, fence barrier, membership, permission or session outcome.
"""

import math
import time
from dataclasses import dataclass, field, fields
from typing import Any
from uuid import UUID
from weakref import WeakSet

import sqlalchemy as sa
from constants.languages import supported_language
from core.casdoor.admission import AdmissionAction, AdmissionContext, AdmissionPlan, SharedOwnerRequirement
from core.casdoor.auth_transactions import AuthMode
from libs.helper import email as validate_email
from libs.helper import timezone as validate_timezone
from models.account import Account, AccountStatus
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from repositories.casdoor_account_preflight_repository_extend import (
    AccountPreflightConflict,
    AccountPreflightSnapshot,
    CasdoorAccountPreflightRepository,
    CollisionKnowledge,
)
from repositories.casdoor_identity_repository_extend import CasdoorIdentityRepository, VerifiedIdentityKey
from sqlalchemy.orm import Session

from services.account_activation_service import AccountActivationService
from services.account_service import AccountService
from services.entities.account_activation_entities import AccountSetup


@dataclass(frozen=True, repr=False)
class LoginAccountPersistence:
    """Local uncommitted identifiers, not a final authorization receipt."""

    account_id: UUID
    identity_id: UUID


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class _PreparedLoginAccount:
    plan: AdmissionPlan
    preflight: AccountPreflightSnapshot
    key: VerifiedIdentityKey
    account_id: UUID
    deadline: float
    _shared: Any
    _owner: object
    _consumed: bool = field(default=False, init=False)


def _complete(value: object, expected: type) -> bool:
    return type(value) is expected and all(hasattr(value, field.name) for field in fields(expected))


def _check_deadline(deadline: float) -> None:
    try:
        if (
            type(deadline) not in (float, int)
            or not math.isfinite(deadline)
            or not 0 < deadline - time.monotonic() <= 45
        ):
            raise AccountPreflightConflict()
    except OverflowError:
        raise AccountPreflightConflict() from None


def _check_setup(setup: AccountSetup | None) -> None:
    if not _complete(setup, AccountSetup):
        raise AccountPreflightConflict()
    try:
        if any(
            type(value) is not str
            or not value.strip()
            or len(value.encode("utf-8")) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in (setup.name, setup.interface_language, setup.timezone)
        ):
            raise ValueError
        supported_language(setup.interface_language)
        validate_timezone(setup.timezone)
    except (TypeError, ValueError, UnicodeError):
        raise AccountPreflightConflict() from None


def _check_consistency(plan: AdmissionPlan, snapshot: AccountPreflightSnapshot) -> None:
    if (
        not _complete(plan, AdmissionPlan)
        or not _complete(snapshot, AccountPreflightSnapshot)
        or not _complete(plan.context, AdmissionContext)
        or plan.context != snapshot.context
        or plan.mode is not AuthMode.LOGIN
        or type(plan.action) is not AdmissionAction
    ):
        raise AccountPreflightConflict()
    action = plan.action
    if action is AdmissionAction.CREATE_INITIALIZED:
        if (
            plan.account_id is not None
            or snapshot.account is not None
            or snapshot.exact_binding is not None
            or snapshot.collision_knowledge is not CollisionKnowledge.COMPLETE
            or snapshot.collision_account_ids != ()
            or (snapshot.candidate_account_id is not None and snapshot.candidate_absent is not True)
            or plan.required_shared_owners
            != (SharedOwnerRequirement.ACCOUNT_CREATION_PREPARE, SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST)
        ):
            raise AccountPreflightConflict()
        try:
            if type(plan.creation_email) is not str or len(plan.creation_email) > 254:
                raise ValueError
            validate_email(plan.creation_email)
        except (TypeError, ValueError):
            raise AccountPreflightConflict() from None
        _check_setup(plan.setup)
        return
    if action not in (AdmissionAction.INITIALIZE_BOUND, AdmissionAction.ACTIVATE_BOUND, AdmissionAction.USE_BOUND):
        raise AccountPreflightConflict()
    account, binding = snapshot.account, snapshot.exact_binding
    from core.casdoor.admission import AccountObservation, ExactBindingObservation

    if (
        not _complete(account, AccountObservation)
        or not _complete(binding, ExactBindingObservation)
        or type(plan.account_id) is not UUID
        or plan.creation_email is not None
        or snapshot.candidate_absent is not False
        or snapshot.candidate_account_id not in (None, plan.account_id)
        or (account.account_id, binding.account_id) != (plan.account_id, plan.account_id)
        or account.namespace_subject != plan.context.subject
        or (binding.namespace_id, binding.issuer, binding.organization, binding.subject)
        != (plan.context.namespace_id, plan.context.issuer, plan.context.organization, plan.context.subject)
        or type(account.status) is not AccountStatus
        or account.status not in (AccountStatus.ACTIVE, AccountStatus.PENDING, AccountStatus.UNINITIALIZED)
    ):
        raise AccountPreflightConflict()
    if action is AdmissionAction.INITIALIZE_BOUND:
        if account.initialized_at is not None or plan.required_shared_owners != (
            SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE,
            SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,
        ):
            raise AccountPreflightConflict()
        _check_setup(plan.setup)
    elif (
        account.initialized_at is None
        or plan.setup is not None
        or (
            action is AdmissionAction.USE_BOUND
            and (account.status is not AccountStatus.ACTIVE or plan.required_shared_owners != ())
        )
        or (
            action is AdmissionAction.ACTIVATE_BOUND
            and (
                account.status not in (AccountStatus.PENDING, AccountStatus.UNINITIALIZED)
                or plan.required_shared_owners
                != (
                    SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE,
                    SharedOwnerRequirement.ACCOUNT_STATUS_ACTIVATION_PERSIST,
                )
            )
        )
    ):
        raise AccountPreflightConflict()


class CasdoorLoginAccountService:
    def __init__(self, *, activation: AccountActivationService) -> None:
        self._activation = activation
        self._issued: WeakSet[_PreparedLoginAccount] = WeakSet()

    def _prepare_login(
        self,
        *,
        plan: AdmissionPlan,
        preflight: AccountPreflightSnapshot,
        deadline: float,
        invitation: object = None,
        source: object = None,
    ) -> _PreparedLoginAccount:
        """Trusted same-attempt seam; call shared eligibility outside write UoW.

        The caller has already reconstructed actual owners and verified provider
        claims/policy. The wrapper only prevents accidental reuse/substitution;
        it cannot authenticate those public inputs or defend against Python code.
        NEW's original UUID must be included in the complete outer lease scope.
        """
        if invitation is not None or source is not None:
            raise AccountPreflightConflict()
        _check_deadline(deadline)
        _check_consistency(plan, preflight)
        context = plan.context
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        shared = None
        account_id = plan.account_id
        if plan.action is AdmissionAction.CREATE_INITIALIZED:
            shared = AccountService.prepare_account_creation(
                email=plan.creation_email,
                name=plan.setup.name,
                interface_language=plan.setup.interface_language,
                timezone=plan.setup.timezone,
            )
            account_id = UUID(shared.account_id)
            if preflight.candidate_account_id not in (None, account_id):
                raise AccountPreflightConflict()
        elif plan.action in (AdmissionAction.INITIALIZE_BOUND, AdmissionAction.ACTIVATE_BOUND):
            account = preflight.account
            shared = self._activation.prepare_bound_initialization(
                account_id=str(account.account_id),
                account_email=account.email,
                account_status=account.status,
                initialized_at=account.initialized_at,
                setup=plan.setup,
            )
        _check_deadline(deadline)
        prepared = _PreparedLoginAccount(plan, preflight, key, account_id, deadline, shared, self)
        self._issued.add(prepared)
        return prepared

    def persist_login_account(
        self,
        prepared: _PreparedLoginAccount,
        *,
        session: Session,
        context: AdmissionContext,
        key: VerifiedIdentityKey,
    ) -> LoginAccountPersistence:
        """Apply account owners then identity last; never commit or roll back.

        Any exception requires whole caller rollback and fresh preparation. The
        caller still owns final collision/fence/lease/deadline checks: B1's bound
        fast path after our own identity INSERT is not a final NEW email scan.
        """
        if (
            type(prepared) is not _PreparedLoginAccount
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
        _check_deadline(prepared.deadline)
        plan = prepared.plan
        current = CasdoorAccountPreflightRepository(session).reconstruct(
            context,
            key,
            candidate_account_id=prepared.account_id,
            collision_email=plan.creation_email,
        )
        _check_consistency(plan, current)
        if current.account != prepared.preflight.account or current.exact_binding != prepared.preflight.exact_binding:
            raise AccountPreflightConflict()
        if plan.action is AdmissionAction.CREATE_INITIALIZED:
            if current.candidate_absent is not True:
                raise AccountPreflightConflict()
            original = prepared._shared.account
            if (
                original.id != str(prepared.account_id)
                or original.email != plan.creation_email
                or (original.name, original.interface_language, original.timezone, original.interface_theme)
                != (plan.setup.name, plan.setup.interface_language, plan.setup.timezone, "light")
                or original.password is not None
                or original.password_salt is not None
            ):
                raise AccountPreflightConflict()
            _check_deadline(prepared.deadline)
            account = AccountService.persist_account_creation(prepared._shared, session=session)
            SQLAlchemyAccountActivationRepository.persist_account_setup(account, plan.setup)
        else:
            with session.no_autoflush:
                account = session.scalar(
                    sa.select(Account)
                    .where(Account.id == str(prepared.account_id))
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            observed = current.account
            if account is None or (account.id, account.email, account.status, account.initialized_at) != (
                str(observed.account_id),
                observed.email,
                observed.status,
                observed.initialized_at,
            ):
                raise AccountPreflightConflict()
            _check_deadline(prepared.deadline)
            if plan.action is not AdmissionAction.USE_BOUND:
                self._activation.persist_bound_initialization(prepared._shared, account=account, session=session)
        identity = CasdoorIdentityRepository(session).bind(
            key,
            account_id=prepared.account_id,
            expected_fence_epoch=context.fence_epoch,
        )
        _check_deadline(prepared.deadline)
        return LoginAccountPersistence(prepared.account_id, identity.identity_id)
