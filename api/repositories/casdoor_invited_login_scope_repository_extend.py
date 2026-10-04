"""Same-root completed invitation observation and candidate LOCAL lease scope.

The attempt and returned facts authenticate nothing. No token/receipt store,
writer, intent exemption, lease acquisition or session issuance lives here.
P3L owns its precise existing NOWAIT chain; expanded discovery adds no locks
and makes no global-snapshot or missing-row serialization claim.
"""

from collections.abc import Callable
from dataclasses import dataclass

from models.account import AccountStatus, TenantStatus
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransactionOrigin

from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationRepository,
)
from repositories.casdoor_invitation_finalization_repository_extend import (
    CasdoorInvitationFinalizationRepository,
    InvitationFinalizationFacts,
)
from repositories.casdoor_invitation_operation_repository_extend import (
    _SNAPSHOT_FIELDS,
    InvitationOperationAttempt,
    _attempt,
    _strict_json,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
    LoginScope,
)
from repositories.invitation_authority_repository_extend import _payload


@dataclass(frozen=True, slots=True, repr=False)
class CompletedInvitationLoginScope:
    """Current read observation only; no admission or future-writer capability."""

    attempt: InvitationOperationAttempt
    completion: InvitationFinalizationFacts
    scope: LoginScope


class CasdoorInvitedLoginScopeRepository:
    def __init__(self, session: Session, configuration_factory: Callable[[Session], CasdoorConfigurationRepository]):
        self.session = session
        self.configuration_factory = configuration_factory

    def discover_completed_invitation(self, attempt: InvitationOperationAttempt) -> CompletedInvitationLoginScope:
        """Require real same-session completion, then preserve every scalar barrier."""
        projection = CasdoorLoginScopeRepository(self.session, self.configuration_factory)
        projection._clean()
        transaction = self.session.get_transaction()
        if (
            transaction is None
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or type(attempt) is not InvitationOperationAttempt
        ):
            raise CasdoorLoginScopeConflict()
        try:
            with self.session.no_autoflush:
                completion = CasdoorInvitationFinalizationRepository(self.session).inspect(attempt)
                if type(completion) is not InvitationFinalizationFacts or completion.completed is not True:
                    raise CasdoorLoginScopeConflict()
                scope = projection._project_scope(
                    attempt.context,
                    attempt.key,
                    attempt.account_id,
                    creation_email=None,
                    extra_workspace_ids=(attempt.workspace_id,),
                )
                self._binding(attempt, completion, scope)
                return CompletedInvitationLoginScope(attempt, completion, scope)
        except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            raise CasdoorLoginScopeConflict() from None

    @staticmethod
    def _binding(attempt: InvitationOperationAttempt, completion: InvitationFinalizationFacts, scope: LoginScope):
        """Bind projected current scalars to the exact P3L-owned immutable facts."""
        data = _strict_json(completion.snapshot.desired_json, _SNAPSHOT_FIELDS)
        issuance = _payload(completion.issuance.payload_json)
        context = attempt.context
        expected = {
            "integration_id": str(context.integration_id),
            "namespace_id": str(context.namespace_id),
            "revision_id": str(context.revision_id),
            "config_digest": context.config_digest,
            "subject_digest": attempt.key.subject_digest,
            "account_id": str(attempt.account_id),
            "workspace_id": str(attempt.workspace_id),
            "issuance_id": str(attempt.issuance_id),
            "generation": attempt.expected_generation,
            "fence_epoch": context.fence_epoch,
            "operation_id": str(completion.snapshot.operation_id),
            "payload_digest": completion.issuance.payload_digest,
        }
        if any(data[key] != value for key, value in expected.items()):
            raise CasdoorLoginScopeConflict()
        identities = [row for row in scope.identities if row.id == data["identity_id"]]
        joins = [row for row in scope.joins if row.id == completion.join_id]
        intents = [row for row in scope.intents if row.id == data["operation_id"]]
        if (
            scope.account is None
            or (scope.account.id, scope.account.email) != (str(attempt.account_id), issuance["email"])
            or len(identities) != 1
            or tuple(identities[0])[1:]
            != (
                str(context.namespace_id),
                str(attempt.account_id),
                attempt.key.issuer,
                attempt.key.organization,
                attempt.key.subject,
                attempt.key.subject_digest,
                attempt.expected_generation,
            )
            or len(joins) != 1
            or (joins[0].account_id, joins[0].tenant_id, joins[0].role)
            != (
                str(attempt.account_id),
                str(attempt.workspace_id),
                issuance["role"] if completion.membership_created else completion.issuance.observed_join_role,
            )
            or len(intents) != 1
            or tuple(intents[0])[1:]
            != (
                str(context.namespace_id),
                data["identity_id"],
                str(attempt.account_id),
                str(attempt.workspace_id),
                None,
                str(context.revision_id),
                "invitation_finalize",
                attempt.expected_generation,
                0,
                context.fence_epoch,
                "applied",
                "confirmed",
            )
            or str(attempt.workspace_id) not in {row.id for row in scope.workspaces}
        ):
            raise CasdoorLoginScopeConflict()

    def prelock_and_recheck_completed_invitation(
        self, attempt: InvitationOperationAttempt
    ) -> CompletedInvitationLoginScope:
        """First SQL owner in a fresh root; observe, serialize, prelock and recheck.

        Serialization is limited to same-integration Casdoor UoWs following the
        integration-first contract. This is never an admission/writer capability
        or a promise about non-cooperating owners, gaps or global snapshots.
        """
        projection = CasdoorLoginScopeRepository(self.session, self.configuration_factory)
        projection._clean()
        transaction = self.session.get_transaction()
        # api/uv.lock pins SQLAlchemy 2.0.49. Any enlisted connection means this
        # root already executed SQL (possibly P3M-A/P3L locks); never extend it.
        connections = getattr(transaction, "_connections", None)
        if (
            transaction is None
            or not transaction.is_active
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or type(connections) is not dict
            or connections
        ):
            raise CasdoorLoginScopeConflict()
        try:
            _attempt(attempt)
            with self.session.no_autoflush:
                candidate = self._invited_candidate(projection, attempt)
                projection._lock_invited_integration(attempt.context)
                if self._invited_candidate(projection, attempt) != candidate:
                    raise CasdoorLoginScopeConflict()
                projection._lock_invited_candidate_parents(attempt.context, candidate)
                completion = CasdoorInvitationFinalizationRepository(self.session).inspect(attempt)
                if type(completion) is not InvitationFinalizationFacts or completion.completed is not True:
                    raise CasdoorLoginScopeConflict()
                # Every parent lock is already held. A new parent is observed
                # without locking it and makes this complete comparison fail.
                current = self._invited_candidate(projection, attempt)
                if current != candidate:
                    raise CasdoorLoginScopeConflict()
                self._binding(attempt, completion, current)
                return CompletedInvitationLoginScope(attempt, completion, current)
        except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            raise CasdoorLoginScopeConflict() from None

    @staticmethod
    def _invited_candidate(projection: CasdoorLoginScopeRepository, attempt: InvitationOperationAttempt) -> LoginScope:
        """Full unlocked scope with existing authority parents present before P3L."""
        scope = projection._project_scope(
            attempt.context,
            attempt.key,
            attempt.account_id,
            creation_email=None,
            extra_workspace_ids=(attempt.workspace_id,),
        )
        own = [row for row in scope.identities if row.namespace_id == str(attempt.context.namespace_id)]
        invited = [row for row in scope.workspaces if row.id == str(attempt.workspace_id)]
        if (
            scope.account is None
            or scope.account.id != str(attempt.account_id)
            or scope.account.status not in (AccountStatus.ACTIVE, AccountStatus.PENDING, AccountStatus.UNINITIALIZED)
            or len(own) != 1
            or tuple(own[0])[1:]
            != (
                str(attempt.key.namespace_id),
                str(attempt.account_id),
                attempt.key.issuer,
                attempt.key.organization,
                attempt.key.subject,
                attempt.key.subject_digest,
                attempt.expected_generation,
            )
            or len(invited) != 1
            or invited[0].status != TenantStatus.NORMAL
        ):
            raise CasdoorLoginScopeConflict()
        return scope
