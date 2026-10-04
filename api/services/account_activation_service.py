"""Application service for checking and accepting account invitations."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID
from weakref import WeakSet

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from models.account import Account

from services.entities.account_activation_entities import (
    AccountInvitation,
    AccountSetup,
    ActivationCheckData,
    ActivationCheckResult,
    ActivationCommand,
    ActivationPersistenceResult,
    InvitationConsumptionReadback,
    InvitationConsumptionRecovery,
    InvitationConsumptionRecoveryStore,
    InvitationConsumptionResult,
    InvitationLookup,
    InvitationObservationResult,
    InvitationRecoveryBinding,
    InvitationToken,
    InvitedAccountObservation,
    VersionedInvitationObservation,
    VersionedInvitationTokenStore,
)

_DEFAULT_ROLE = "normal"
_NON_OWNER_ROLES = frozenset({"admin", "editor", "normal", "dataset_operator"})
_PENDING_ACCOUNT_STATUS = "pending"


class InvitationTokenStore(Protocol):
    def find(self, invitation: InvitationLookup) -> InvitationToken | None: ...

    def revoke(self, invitation: InvitationLookup) -> None: ...


class AccountActivationRepository(Protocol):
    def resolve(self, invitation: InvitationToken) -> AccountInvitation | None: ...

    def observe_invited_account(self, invitation: AccountInvitation) -> InvitedAccountObservation | None: ...

    def activate(
        self,
        invitation: AccountInvitation,
        *,
        role: str,
        setup: AccountSetup | None,
    ) -> ActivationPersistenceResult | None: ...

    def persist_activation(
        self,
        invitation: AccountInvitation,
        *,
        role: str,
        setup: AccountSetup | None,
        session: "Session",
    ) -> ActivationPersistenceResult | None: ...


class WorkspaceInvitePolicy(Protocol):
    def ensure_allowed(self, workspace_id: str) -> None: ...


class AccountActivationEligibility(Protocol):
    def get_freeze_type(self, email: str) -> str | None: ...


class WorkspaceMembershipCache(Protocol):
    def invalidate(self, workspace_id: str) -> None: ...


class WorkspaceMemberAccessSync(Protocol):
    def sync(self, workspace_id: str, account_id: str) -> None: ...


class InvalidInvitationError(Exception):
    """The invitation is invalid, stale, or missing required activation data."""


class InvitationAccountMismatchError(Exception):
    """An authenticated account attempted to consume another account's invitation."""


class FrozenAccountError(Exception):
    """The invited account is temporarily ineligible for activation."""


class EmailDomainSuspendedError(Exception):
    """The invited account uses a suspended email domain."""


class InvalidAccountInitializationError(ValueError):
    """The exact account preparation or caller-owned persistence state is invalid."""


@dataclass(frozen=True, slots=True)
class _PreparedAccountActivation:
    """One-attempt internal preparation, never a serialized authorization proof."""

    invitation: AccountInvitation
    role: str
    setup: AccountSetup | None
    _owner: object = field(repr=False, compare=False)
    _consumed: bool = field(default=False, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class _PreparedBoundInitialization:
    """Transient eligibility observation, never binding or authentication proof."""

    account_id: str
    account_email: str
    account_status: str
    initialized_at: datetime | None
    setup: AccountSetup | None
    _owner: object = field(repr=False)
    _consumed: bool = field(default=False, init=False, repr=False)


_issued_bound_initializations: WeakSet[_PreparedBoundInitialization] = WeakSet()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class _PreparedInvitedInitialization:
    """One-use account observation; never provider admission or token authority."""

    observation: InvitedAccountObservation
    account_id: str
    account_email: str
    account_status: str
    initialized_at: datetime | None
    setup: AccountSetup | None
    _owner: object = field(repr=False)
    _consumed: bool = field(default=False, init=False, repr=False)


_issued_invited_initializations: WeakSet[_PreparedInvitedInitialization] = WeakSet()


class AccountActivationService:
    def __init__(
        self,
        *,
        tokens: InvitationTokenStore,
        accounts: AccountActivationRepository,
        workspace_policy: WorkspaceInvitePolicy,
        eligibility: AccountActivationEligibility,
        membership_cache: WorkspaceMembershipCache,
        member_access_sync: WorkspaceMemberAccessSync,
    ) -> None:
        self._tokens = tokens
        self._accounts = accounts
        self._workspace_policy = workspace_policy
        self._eligibility = eligibility
        self._membership_cache = membership_cache
        self._member_access_sync = member_access_sync

    def observe_versioned_invitation(self, token: str) -> InvitationObservationResult:
        """Observe Redis issuance facts without account or business side effects."""
        if not isinstance(self._tokens, VersionedInvitationTokenStore):
            return InvitationObservationResult("unavailable")
        return self._tokens.observe_versioned_invitation(token)

    def consume_versioned_invitation(
        self, observation: VersionedInvitationObservation, *, operation_id: str, receipt_ttl_seconds: int
    ) -> InvitationConsumptionResult:
        """Delegate exact Redis consumption; this does not finalize SQL or login."""
        if not isinstance(self._tokens, VersionedInvitationTokenStore):
            return InvitationConsumptionResult("unavailable")
        return self._tokens.consume_versioned_invitation(
            observation, operation_id=operation_id, receipt_ttl_seconds=receipt_ttl_seconds
        )

    def read_invitation_consumption(
        self, observation: VersionedInvitationObservation, *, operation_id: str
    ) -> InvitationConsumptionReadback:
        """Read back this operation only, within the issuing store/process."""
        if not isinstance(self._tokens, VersionedInvitationTokenStore):
            return InvitationConsumptionReadback("unavailable")
        return self._tokens.read_invitation_consumption(observation, operation_id=operation_id)

    def recover_invitation_consumption(
        self,
        recovery: InvitationConsumptionRecovery,
        *,
        token: str,
        trusted_attempt: InvitationRecoveryBinding,
    ) -> InvitationConsumptionReadback:
        """Read only after the caller matched verified subject and durable scope.

        This compares trusted candidate IDs, not provider claims. Confirmation
        grants no admission, finalization or session and does not consume again.
        """
        if (
            type(recovery) is not InvitationConsumptionRecovery
            or type(trusted_attempt) is not InvitationRecoveryBinding
            or any(
                getattr(recovery, name) != getattr(trusted_attempt, name)
                for name in ("namespace_id", "identity_id", "account_id", "workspace_id")
            )
            or not isinstance(self._tokens, InvitationConsumptionRecoveryStore)
        ):
            return InvitationConsumptionReadback("unavailable")
        return self._tokens.recover_invitation_consumption(recovery, token=token, trusted_attempt=trusted_attempt)

    def check(self, invitation: InvitationLookup) -> ActivationCheckResult:
        resolved = self._resolve(invitation)
        if resolved is None:
            return ActivationCheckResult(is_valid=False)

        self._workspace_policy.ensure_allowed(resolved.workspace_id)
        return ActivationCheckResult(
            is_valid=True,
            data=ActivationCheckData(
                workspace_name=resolved.workspace_name,
                workspace_id=resolved.workspace_id,
                email=resolved.account_email,
                account_status=resolved.account_status,
                requires_setup=self._requires_setup(resolved),
            ),
        )

    def activate(self, command: ActivationCommand, *, authenticated_account_id: str | None) -> None:
        prepared = self.prepare_activation(command, authenticated_account_id=authenticated_account_id)
        invitation = prepared.invitation

        normalized_email = command.invitation.email.lower() if command.invitation.email else None
        self._tokens.revoke(
            InvitationLookup(
                workspace_id=command.invitation.workspace_id,
                email=normalized_email,
                token=command.invitation.token,
            )
        )
        result = self._accounts.activate(invitation, role=prepared.role, setup=prepared.setup)
        if result is None:
            raise InvalidInvitationError
        if result.membership_created:
            self._membership_cache.invalidate(invitation.workspace_id)
        self._member_access_sync.sync(invitation.workspace_id, invitation.account_id)

    def prepare_activation(
        self, command: ActivationCommand, *, authenticated_account_id: str | None
    ) -> _PreparedAccountActivation:
        """Reuse invitation resolution and eligibility without consuming the token.

        This is the legacy activation policy, including its separate check()
        workspace-policy owner. Provider callers must first establish trusted
        identity, invitation/source authorization, local status, workspace policy,
        and active configuration/lease fences. Commands and matching emails alone
        do not prove that authorization. Keep the result in the same bounded
        attempt; reads and the freeze owner run before the caller's write transaction.
        """
        invitation = self._resolve(command.invitation)
        if invitation is None:
            raise InvalidInvitationError

        if authenticated_account_id is not None and authenticated_account_id != invitation.account_id:
            raise InvitationAccountMismatchError

        self._ensure_account_eligible(invitation.account_email)

        setup = self._resolve_setup(invitation, command)
        raw_role = invitation.role
        role = raw_role if raw_role is not None and raw_role in _NON_OWNER_ROLES else _DEFAULT_ROLE

        return _PreparedAccountActivation(invitation=invitation, role=role, setup=setup, _owner=self)

    def prepare_bound_initialization(
        self,
        *,
        account_id: str,
        account_email: str,
        account_status: str,
        initialized_at: datetime | None,
        setup: AccountSetup | None,
    ) -> _PreparedBoundInitialization:
        """Check existing-account eligibility before the caller's write UoW.

        The caller must establish actual exact identity/invitation authorization,
        current configuration, verified claims, source and lease fences. These
        observations and the returned receipt do not establish any of them.
        Existing accounts consume no new seat. Already initialized pending or
        uninitialized accounts activate without overwriting their setup fields.
        """
        self._validate_initialization(
            account_id=account_id,
            account_email=account_email,
            account_status=account_status,
            initialized_at=initialized_at,
            setup=setup,
            allow_initialized_active=False,
        )

        self._ensure_account_eligible(account_email)
        prepared = _PreparedBoundInitialization(account_id, account_email, account_status, initialized_at, setup, self)
        _issued_bound_initializations.add(prepared)
        return prepared

    @staticmethod
    def _validate_initialization(
        *,
        account_id: str,
        account_email: str,
        account_status: str,
        initialized_at: datetime | None,
        setup: AccountSetup | None,
        allow_initialized_active: bool,
    ) -> None:
        """Shared original initialization validation; bound callers remain strict."""
        from constants.languages import supported_language
        from libs.helper import email as validate_email
        from libs.helper import timezone as validate_timezone

        def bounded_text(value: object) -> bool:
            try:
                return (
                    type(value) is str
                    and bool(value.strip())
                    and len(value.encode("utf-8")) <= 255
                    and not any(ord(char) < 32 or ord(char) == 127 for char in value)
                )
            except UnicodeError:
                return False

        try:
            if (
                type(account_id) is not str
                or str(UUID(account_id)) != account_id
                or not bounded_text(account_email)
                or account_status not in {"pending", "uninitialized", "active"}
                or not isinstance(account_status, str)
                or (
                    initialized_at is not None
                    and (type(initialized_at) is not datetime or initialized_at.tzinfo is not None)
                )
            ):
                raise ValueError
            validate_email(account_email)
            if initialized_at is None:
                if type(setup) is not AccountSetup or not all(
                    bounded_text(getattr(setup, attr, None)) for attr in ("name", "interface_language", "timezone")
                ):
                    raise ValueError
                supported_language(setup.interface_language)
                validate_timezone(setup.timezone)
            elif setup is not None or (account_status == "active" and not allow_initialized_active):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise InvalidAccountInitializationError from None

    def persist_bound_initialization(
        self, prepared: _PreparedBoundInitialization, *, account: "Account", session: "Session"
    ) -> None:
        """Apply one preparation to a fresh, locked ORM account in a clean root UoW.

        Caller owns database freshness/row locking, binding/configuration/lease
        rechecks, commit and rollback. This helper issues no SQL, implicit flush,
        token consumption, role/member/quota change or session/finalization proof.
        Every persistence attempt consumes the receipt, including a rejected one.
        """
        if (
            type(prepared) is not _PreparedBoundInitialization
            or prepared not in _issued_bound_initializations
            or prepared._owner is not self
            or prepared._consumed
        ):
            raise InvalidAccountInitializationError
        _issued_bound_initializations.discard(prepared)
        object.__setattr__(prepared, "_consumed", True)
        self._persist_initialization(prepared, account=account, session=session, allow_initialized_active=False)

    @staticmethod
    def _persist_initialization(
        prepared: _PreparedBoundInitialization | _PreparedInvitedInitialization,
        *,
        account: "Account",
        session: "Session",
        allow_initialized_active: bool,
    ) -> None:
        """Assignment-only original owner after exact clean-root reconstruction."""
        from sqlalchemy import inspect
        from sqlalchemy.orm import Session

        from models.account import Account
        from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository

        if (
            not isinstance(session, Session)
            or not session.is_active
            or session.get_transaction() is None
            or not session.get_transaction().is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
            or type(account) is not Account
        ):
            raise InvalidAccountInitializationError
        state = inspect(account)
        observed = {"id", "email", "status", "initialized_at"}
        setup_fields = {"name", "interface_language", "timezone", "interface_theme"}
        if (
            not state.persistent
            or state.session is not session
            or (observed | setup_fields) & (state.unloaded | state.expired_attributes)
            or account.id != prepared.account_id
            or account.email != prepared.account_email
            or account.status != prepared.account_status
            or account.initialized_at != prepared.initialized_at
            or account.status not in {"pending", "uninitialized", "active"}
        ):
            raise InvalidAccountInitializationError
        if type(prepared) is _PreparedInvitedInitialization and any(
            getattr(account, name) != getattr(prepared.observation, name) for name in setup_fields
        ):
            raise InvalidAccountInitializationError
        if prepared.setup is None:
            if account.initialized_at is None or (account.status == "active" and not allow_initialized_active):
                raise InvalidAccountInitializationError
            if account.status != "active":
                SQLAlchemyAccountActivationRepository.persist_account_active_status(account)
        else:
            if account.initialized_at is not None:
                raise InvalidAccountInitializationError
            SQLAlchemyAccountActivationRepository.persist_account_setup(account, prepared.setup)

    def _ensure_account_eligible(self, email: str) -> None:
        """Keep the original billing freeze classification in one owner."""
        freeze_type = self._eligibility.get_freeze_type(email)
        if freeze_type == "email_domain_suspended":
            raise EmailDomainSuspendedError
        if freeze_type:
            raise FrozenAccountError

    def prepare_invited_initialization(
        self,
        invitation: InvitationLookup,
        *,
        setup: AccountSetup | None,
        authenticated_account_id: str | None,
    ) -> _PreparedInvitedInitialization:
        """Resolve a real invitation and prepare only its exact account fields.

        All reads close before workspace policy and the one original freeze
        check. The caller owns authenticated provider admission, fresh locking,
        token/workspace/configuration/lease rechecks and the final barrier. This
        receipt neither consumes an invitation nor authorizes session issuance.
        The token-length limit does not bound the store's decoded JSON payload.
        """
        try:
            if type(invitation) is not InvitationLookup or (
                type(invitation.token) is not str or not 0 < len(invitation.token) <= 512
            ):
                raise ValueError
            invitation.token.encode("utf-8")
            if invitation.workspace_id is not None:
                self._validate_invitation_id(invitation.workspace_id)
            if invitation.email is not None:
                self._validate_invitation_email(invitation.email)
            if authenticated_account_id is not None:
                self._validate_invitation_id(authenticated_account_id)
            resolved = self._resolve_invitation(invitation, validate_token=True)
            if resolved is None:
                raise ValueError
            observation = self._accounts.observe_invited_account(resolved)
            if observation is None:
                raise ValueError
            self._validate_initialization(
                account_id=resolved.account_id,
                account_email=resolved.account_email,
                account_status=resolved.account_status,
                initialized_at=observation.initialized_at,
                setup=setup,
                allow_initialized_active=True,
            )
        except (TypeError, ValueError, AttributeError):
            raise InvalidInvitationError from None

        if authenticated_account_id is not None and authenticated_account_id != resolved.account_id:
            raise InvitationAccountMismatchError
        self._workspace_policy.ensure_allowed(resolved.workspace_id)
        self._ensure_account_eligible(resolved.account_email)
        prepared = _PreparedInvitedInitialization(
            observation,
            resolved.account_id,
            resolved.account_email,
            resolved.account_status,
            observation.initialized_at,
            setup,
            self,
        )
        _issued_invited_initializations.add(prepared)
        return prepared

    def persist_invited_initialization(
        self, prepared: _PreparedInvitedInitialization, *, account: "Account", session: "Session"
    ) -> None:
        """Consume a same-owner receipt and assign only in a clean caller root.

        Caller must supply a fresh locked exact Account and roll back the whole
        root on any later failure. No SQL, token/member/current/quota mutation,
        flush, commit, rollback, cache, task or external I/O occurs here.
        Initialized ACTIVE is an assignment-free no-op after the same checks.
        """
        if (
            type(prepared) is not _PreparedInvitedInitialization
            or prepared not in _issued_invited_initializations
            or prepared._owner is not self
            or prepared._consumed
        ):
            raise InvalidAccountInitializationError
        _issued_invited_initializations.discard(prepared)
        object.__setattr__(prepared, "_consumed", True)
        self._persist_initialization(prepared, account=account, session=session, allow_initialized_active=True)

    @staticmethod
    def _validate_invitation_id(value: str) -> None:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError

    @staticmethod
    def _validate_invitation_email(value: str) -> None:
        from libs.helper import email as validate_email

        if (
            type(value) is not str
            or not value.strip()
            or len(value.encode("utf-8")) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError
        validate_email(value)

    def _find_invitation_token(self, invitation: InvitationLookup, *, validate_token: bool) -> InvitationToken | None:
        token = self._tokens.find(invitation)
        if validate_token and token is not None:
            if type(token) is not InvitationToken:
                raise ValueError
            self._validate_invitation_id(token.account_id)
            self._validate_invitation_id(token.workspace_id)
            self._validate_invitation_email(token.email)
            if (invitation.workspace_id is not None and token.workspace_id != invitation.workspace_id) or (
                invitation.email is not None and token.email != invitation.email
            ):
                return None
        return token

    def persist_activation(
        self, prepared: _PreparedAccountActivation, *, session: "Session"
    ) -> ActivationPersistenceResult:
        """Apply one prepared invitation inside the caller-owned transaction.

        No token consumption, commit/rollback, cache or access-sync effects occur.
        A failed attempt requires fresh preparation even after caller rollback.
        Necessary invitation/permission finalization remains the durable owner’s
        responsibility; the persistence result is not a session-issuance barrier.
        """
        if not isinstance(prepared, _PreparedAccountActivation) or prepared._owner is not self or prepared._consumed:
            raise InvalidInvitationError
        object.__setattr__(prepared, "_consumed", True)
        result = self._accounts.persist_activation(
            prepared.invitation, role=prepared.role, setup=prepared.setup, session=session
        )
        if result is None:
            raise InvalidInvitationError
        return result

    def _resolve(self, invitation: InvitationLookup) -> AccountInvitation | None:
        return self._resolve_invitation(invitation)

    def _resolve_invitation(
        self, invitation: InvitationLookup, *, validate_token: bool = False
    ) -> AccountInvitation | None:
        token = self._find_invitation_token(invitation, validate_token=validate_token)
        resolved = self._accounts.resolve(token) if token is not None else None
        if resolved is not None:
            return resolved

        if invitation.email is None or invitation.email == invitation.email.lower():
            return None

        token = self._find_invitation_token(
            InvitationLookup(
                workspace_id=invitation.workspace_id,
                email=invitation.email.lower(),
                token=invitation.token,
            ),
            validate_token=validate_token,
        )
        if token is None:
            return None
        return self._accounts.resolve(token)

    @staticmethod
    def _requires_setup(invitation: AccountInvitation) -> bool:
        if invitation.requires_setup is not None:
            return invitation.requires_setup
        return invitation.account_status == _PENDING_ACCOUNT_STATUS

    @classmethod
    def _resolve_setup(cls, invitation: AccountInvitation, command: ActivationCommand) -> AccountSetup | None:
        if not cls._requires_setup(invitation):
            return None
        if not command.name or not command.interface_language or not command.timezone:
            raise InvalidInvitationError
        return AccountSetup(
            name=command.name,
            interface_language=command.interface_language,
            timezone=command.timezone,
        )
