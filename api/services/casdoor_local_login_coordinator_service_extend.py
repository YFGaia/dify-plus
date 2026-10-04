"""Private ordinary LOCAL login choreography; deliberately not production wired.

Only original consumed/exchanged inputs enter this bounded composition. Synthetic
contracts can exercise the private path, never activate the production factory.
A committed local membership result is not final permission or session authority.
"""

import time
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from configs import dify_config
from core.casdoor.admission import AdmissionContext, decide_admission
from core.casdoor.auth_transactions import AuthMode, ConsumedAuthTransaction
from core.casdoor.claims import ClaimsValidator, NativeTokenContract, VerifiedOnlineUser
from core.casdoor.crypto import CertificateTrustStore, TrustedCertificate
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import (
    CasdoorDirectoryGateway,
    DirectoryCredentialStrategy,
    GatewayOperation,
    RawTokens,
)
from core.casdoor.leases import CasdoorLeases, RedisLeaseClient
from core.casdoor.role_graph import DirectorySnapshotContract, OnlineRoleSnapshotLoader, _contract
from models.casdoor_extend import CasdoorNamespaceExtend, CasdoorNamespaceLifecycle
from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
from sqlalchemy.orm import Session

from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from services.casdoor_local_login_finalization_service_extend import CasdoorLocalLoginFinalizationService
from services.casdoor_local_login_service_extend import CasdoorLocalLoginService
from services.casdoor_local_membership_service_extend import LocalMembershipPersistence
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from services.entities.account_login_entities import AuthTokenPair


@dataclass(frozen=True, repr=False)
class _LocalLoginOutcome:
    """Separate commit/issuance/cleanup facts; tokens require confirmed cleanup."""

    persistence: LocalMembershipPersistence
    cleanup_released: bool
    local_outcome: str = "committed"
    tokens: AuthTokenPair | None = None
    finalization_outcome: str = "not_started"
    token_outcome: str = "not_started"


class _CoordinationConflict(ValueError):
    code = CasdoorErrorCode.AUTHORIZATION_PENDING

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("casdoor_coordination_" + reason)


class CasdoorLocalLoginCoordinatorService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        configuration_service: CasdoorConfigurationService,
        account_owner: CasdoorLoginAccountService,
        redis_client: RedisLeaseClient,
        finalization_service: CasdoorLocalLoginFinalizationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._configuration_service = configuration_service
        self._account_owner = account_owner
        self._redis_client = redis_client
        if finalization_service is not None and type(finalization_service) is not CasdoorLocalLoginFinalizationService:
            raise _CoordinationConflict("invalid_finalizer")
        self._finalization = finalization_service
        self._local = CasdoorLocalLoginService(
            session_factory=session_factory, configuration_factory=configuration_service._repository
        )

    @classmethod
    def for_production(
        cls,
        *,
        session_factory: Callable[[], Session],
        configuration_service: CasdoorConfigurationService,
        account_owner: CasdoorLoginAccountService,
        redis_client: RedisLeaseClient,
    ) -> "CasdoorLocalLoginCoordinatorService":
        """No actual G0 proof producer exists; deny before constructing anything."""
        raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "deployment_proof_missing")

    def _with_request_redis(self, redis_client) -> "CasdoorLocalLoginCoordinatorService":
        """Retain the admitted owners; bind only the private request Redis."""
        coordinator = copy(self)
        coordinator._redis_client = redis_client
        return coordinator

    def _configuration(self, consumed, operation, subject):
        operation._check()
        if dify_config.RBAC_ENABLED is not False or self._configuration_service._rbac_enabled is not False:
            raise _CoordinationConflict("local_mode_required")
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            integration = owner._integration()
            c = consumed.context
            if (
                integration is None
                or integration.enabled is not True
                or integration.active_revision_id != str(c.revision_id)
            ):
                raise _CoordinationConflict("configuration_changed")
            revision = owner._revision(integration.id, str(c.revision_id))
            namespace = session.get(CasdoorNamespaceExtend, str(c.namespace_id))
            if (
                revision.namespace_id != str(c.namespace_id)
                or namespace is None
                or namespace.lifecycle is not CasdoorNamespaceLifecycle.ACTIVE
            ):
                raise _CoordinationConflict("configuration_changed")
            config = owner._configuration(revision)
            if config != operation.config or c.registered_redirect_uri != operation.registered_redirect_uri:
                raise _CoordinationConflict("configuration_changed")
            secret = owner.crypto.decrypt(
                revision.encrypted_secret, context=owner._secret_context(revision.namespace_id, revision.id)
            )
            if secret != operation.client_secret:
                raise _CoordinationConflict("configuration_changed")
            del secret
            context = AdmissionContext(
                UUID(integration.id),
                UUID(revision.id),
                UUID(integration.active_revision_id),
                UUID(namespace.id),
                namespace.lifecycle,
                namespace.fence_epoch,
                revision.config_digest,
                config.expected_issuer,
                config.organization,
                config.application,
                config.client_id,
                subject,
            )
        operation._check()
        return config, context

    def _admission(self, context, consumed, bundle, online, profile):
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        with self._session_factory() as session, session.begin():
            preflight = CasdoorAccountPreflightRepository(session).reconstruct(
                context, key, collision_email=profile.email
            )
            plan = decide_admission(
                context=context,
                mode=AuthMode.LOGIN,
                tokens=bundle,
                online=online,
                profile=profile,
                account=preflight.account,
                binding=preflight.exact_binding,
                email_collision_account_ids=preflight.collision_account_ids,
                request_language=consumed.context.locale,
                request_timezone=consumed.context.timezone,
            )
        return preflight, plan

    def _discover(self, prepared):
        with self._session_factory() as session, session.begin():
            return CasdoorLoginScopeRepository(session, self._configuration_service._repository).discover(prepared)

    @staticmethod
    def _cleanup(leases, deadline):
        # The original owner compare-deletes only its own tokens, including SET
        # timeouts. False is not release; one retry uses the unchanged budget.
        for attempt in range(2):
            try:
                if leases.release() is True:
                    return True
            except Exception:
                pass
            if attempt or time.monotonic() >= deadline:
                break
        return False

    def _coordinate_local_login(
        self,
        *,
        consumed: ConsumedAuthTransaction,
        raw_tokens: RawTokens,
        operation: GatewayOperation,
        native_contract: NativeTokenContract,
        directory_contract: DirectorySnapshotContract,
        credential_strategy: DirectoryCredentialStrategy,
        correlation_id: UUID,
        ip_address: str | None = None,
    ) -> _LocalLoginOutcome:
        """At most two full passes; never retry once actual C1 has been entered.

        Exceptions retain their primary owner type/reason and receive only two
        scalar facts: local_outcome and cleanup_released. Cleanup cancellation
        preserves an existing primary failure. Without a primary it propagates,
        including after known commit, and never becomes a successful return.
        In particular a commit/ACK exception cannot establish rollback. No input
        is a substitute for the original same-callback consume/exchange owners.
        """
        if (
            type(consumed) is not ConsumedAuthTransaction
            or type(raw_tokens) is not RawTokens
            or type(operation) is not GatewayOperation
            or type(correlation_id) is not UUID
            or consumed.context.mode is not AuthMode.LOGIN
            or any(
                v is not None
                for v in (
                    consumed.context.invite,
                    consumed.context.source,
                    consumed.context.identity_id,
                    consumed.context.action,
                )
            )
            or not operation._exchange_started
        ):
            raise _CoordinationConflict("invalid_attempt")
        retained = None
        original_context = None
        for pass_number in range(2):
            leases = None
            started = False
            result = None
            tokens = None
            finalization_outcome, token_outcome = "not_started", "not_started"
            cleanup = True
            primary = None
            try:
                config, _ = self._configuration(consumed, operation, "")
                validator = ClaimsValidator(
                    trust_store=CertificateTrustStore(
                        [TrustedCertificate(**pin.model_dump()) for pin in config.certificates]
                    ),
                    expected_issuer=config.expected_issuer,
                    organization=config.organization,
                    application=config.application,
                    client_id=config.client_id,
                )
                bundle = validator.verify_token_bundle(
                    raw_tokens,
                    expected_nonce=consumed.nonce,
                    auth_started_at=consumed.auth_started_at,
                    contract=native_contract,
                    now=datetime.now(UTC),
                )
                _, context = self._configuration(consumed, operation, bundle.identity.subject)
                if original_context is not None and context != original_context:
                    raise _CoordinationConflict("configuration_changed")
                original_context = context
                profile = validator.verify_userinfo(
                    operation.userinfo(raw_tokens.payload["access_token"]), identity=bundle.identity
                )
                contract = _contract(directory_contract, config.organization)
                directory = CasdoorDirectoryGateway(
                    operation, verified_subject=bundle.identity.subject, credential_strategy=credential_strategy
                )
                if (
                    not contract.deployment_proof.matches(config)
                    or directory.deployment_proof != contract.deployment_proof
                    or native_contract.release_fingerprint != contract.deployment_proof.release_fingerprint
                ):
                    raise _CoordinationConflict("directory_contract_binding")
                online = validator.verify_online_user(directory.get_verified_user(), identity=bundle.identity)
                operation._check()
                preflight, plan = self._admission(context, consumed, bundle, online, profile)
                operation._check()
                prepared = None
                if retained is not None:
                    old_profile, old_preflight, old_plan, old_scope, candidate = retained
                    if (profile, preflight, plan) == (old_profile, old_preflight, old_plan):
                        current_scope = self._discover(candidate)
                        if current_scope.account == old_scope.account:
                            prepared = candidate
                if prepared is None:
                    prepared = self._account_owner._prepare_login(
                        plan=plan, preflight=preflight, deadline=operation.deadline
                    )
                operation._check()
                discovered = self._discover(prepared)
                operation._check()
                retained = (profile, preflight, plan, discovered, prepared)
                leases = CasdoorLeases(self._redis_client, discovered.lease_scope, deadline=operation.deadline)
                leases.acquire()
                leases.ensure_owned()
                fresh_scope = self._discover(prepared)
                leases.ensure_owned()
                if fresh_scope == discovered:
                    fresh_profile = validator.verify_userinfo(
                        operation.userinfo(raw_tokens.payload["access_token"]), identity=bundle.identity
                    )
                    leases.ensure_owned()
                    roles = OnlineRoleSnapshotLoader(
                        operation,
                        identity=bundle.identity,
                        claims_validator=validator,
                        leases=leases,
                        credential_strategy=credential_strategy,
                        contract=directory_contract,
                    ).load()
                    # This projection is local to the actual successful loader
                    # stack; it is never accepted as caller authentication input.
                    fresh_online = VerifiedOnlineUser(roles.subject, roles.user_ref)
                    leases.ensure_owned()
                    _, fresh_context = self._configuration(consumed, operation, bundle.identity.subject)
                    if fresh_context != context:
                        raise _CoordinationConflict("configuration_changed")
                    fresh_preflight, fresh_plan = self._admission(
                        fresh_context, consumed, bundle, fresh_online, fresh_profile
                    )
                    final_scope = self._discover(prepared)
                    operation._check()
                    leases.ensure_owned()
                    if (fresh_profile, fresh_preflight, fresh_plan, final_scope) == (
                        profile,
                        preflight,
                        plan,
                        fresh_scope,
                    ) and fresh_online == online:
                        started = True
                        result = self._local._persist_local_login(
                            account_owner=self._account_owner,
                            prepared=prepared,
                            discovered=final_scope,
                            roles=roles,
                            leases=leases,
                            profile=fresh_profile,
                            auth_started_at=consumed.auth_started_at,
                            correlation_id=correlation_id,
                            now=datetime.now(UTC),
                        )
                        if self._finalization is not None:
                            tokens = self._finalization._finalize_local_login(
                                prepared=prepared,
                                persisted=result,
                                roles=roles,
                                leases=leases,
                                configuration=config,
                                ip_address=ip_address,
                            )
                            finalization_outcome, token_outcome = "committed", "issued"
            except BaseException as error:
                primary = error
                finalization_outcome = getattr(error, "finalization_outcome", finalization_outcome)
                token_outcome = getattr(error, "token_outcome", token_outcome)
                raise
            finally:
                local_outcome = "committed" if result is not None else "unknown" if started else "not_started"
                try:
                    if leases is not None:
                        cleanup = self._cleanup(leases, operation.deadline)
                except BaseException as cleanup_error:
                    cleanup = False
                    # Cancellation is not a cleanup success or a reason to retry.
                    # Preserve any already propagating failure, including its
                    # exact identity; otherwise propagate the cancellation with
                    # the actual commit phase, even after a successful C1 return.
                    if primary is None:
                        cleanup_error.local_outcome = local_outcome
                        cleanup_error.cleanup_released = False
                        cleanup_error.finalization_outcome = finalization_outcome
                        cleanup_error.token_outcome = token_outcome
                        raise
                finally:
                    if primary is not None:
                        primary.local_outcome = local_outcome
                        primary.cleanup_released = cleanup
                        primary.finalization_outcome = finalization_outcome
                        primary.token_outcome = token_outcome
            if result is not None:
                return _LocalLoginOutcome(
                    result,
                    cleanup,
                    tokens=tokens if cleanup else None,
                    finalization_outcome=finalization_outcome,
                    token_outcome=token_outcome,
                )
            if not cleanup or pass_number == 1:
                error = _CoordinationConflict("cleanup_pending" if not cleanup else "drift_limit")
                error.local_outcome = "not_started"
                error.cleanup_released = cleanup
                raise error
        raise AssertionError("unreachable")
