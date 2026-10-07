"""Ordinary LOCAL login choreography bound to an active configuration revision.

Only original consumed/exchanged inputs enter this bounded composition. The
production factory reconstructs the active database owner chain; token claims,
online account state and the complete role graph are checked on every callback.
A committed local membership result is not final permission or session authority.
"""

import time
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from configs import dify_config
from core.casdoor.admission import AdmissionContext, decide_admission, local_account_email
from core.casdoor.auth_transactions import AuthMode, ConsumedAuthTransaction
from core.casdoor.claims import NativeTokenContract, NativeTokenSchema, VerifiedOnlineUser
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.deployment_evidence import AcceptedDeploymentPolicy
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import (
    CasdoorBasicDirectoryCredentialStrategy,
    CasdoorDirectoryGateway,
    DirectoryCredentialStrategy,
    GatewayOperation,
    RawTokens,
)
from core.casdoor.leases import CasdoorLeases, RedisLeaseClient
from core.casdoor.role_graph import DirectorySnapshotContract, OnlineRoleSnapshotLoader, _contract
from enums import DeploymentEdition
from models.casdoor_extend import CasdoorNamespaceExtend, CasdoorNamespaceLifecycle
from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
from sqlalchemy.orm import Session

from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from services.casdoor_deployment_policy_service_extend import CasdoorDeploymentPolicyService
from services.casdoor_local_login_finalization_service_extend import CasdoorLocalLoginFinalizationService
from services.casdoor_local_login_service_extend import CasdoorLocalLoginService
from services.casdoor_local_membership_service_extend import LocalMembershipPersistence
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from services.casdoor_rp_logout_service_extend import RPLogoutTokenSnapshot
from services.casdoor_session_service_extend import SessionProvenanceSeed
from services.casdoor_signing_validator_service_extend import create_claims_validator
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
    provenance: SessionProvenanceSeed | None = None


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
        self._deployment_policy_service: CasdoorDeploymentPolicyService | None = None
        self._production_policy: AcceptedDeploymentPolicy | None = None
        self._production_configuration: CasdoorConfiguration | None = None
        self._production_binding: tuple[str, str, str] | None = None
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
        deployment_policy_service: CasdoorDeploymentPolicyService | None = None,
    ) -> "CasdoorLocalLoginCoordinatorService":
        """Reconstruct the active database owner chain before Redis/provider work.

        External reviewer files are not a login prerequisite. The active
        namespace/revision/digest remains fenced throughout the callback.
        """
        if (
            dify_config.RBAC_ENABLED is not False
            or configuration_service._rbac_enabled is not False
            or dify_config.DEPLOYMENT_EDITION != DeploymentEdition.COMMUNITY
        ):
            raise _CoordinationConflict("local_mode_required")
        with session_factory() as session, session.begin():
            owner = configuration_service._repository(session)
            integration = owner._integration()
            if integration is None or integration.enabled is not True or not integration.active_revision_id:
                raise CasdoorConfigurationError(CasdoorErrorCode.NOT_CONFIGURED, "inactive")
            revision = owner._revision(integration.id, integration.active_revision_id)
            namespace = session.get(CasdoorNamespaceExtend, revision.namespace_id)
            if namespace is None or namespace.lifecycle is not CasdoorNamespaceLifecycle.ACTIVE:
                raise _CoordinationConflict("configuration_changed")
            configuration = owner._configuration(revision)
            binding = (namespace.id, revision.id, revision.config_digest)
        coordinator = cls(
            session_factory=session_factory,
            configuration_service=configuration_service,
            account_owner=account_owner,
            redis_client=redis_client,
        )
        coordinator._deployment_policy_service = deployment_policy_service
        coordinator._production_configuration = configuration
        coordinator._production_binding = binding
        return coordinator

    def _require_production_policy(self) -> AcceptedDeploymentPolicy | None:
        """Compatibility guard that rechecks the active database revision."""
        if self._production_binding is None:
            return None
        if (
            dify_config.RBAC_ENABLED is not False
            or self._configuration_service._rbac_enabled is not False
            or dify_config.DEPLOYMENT_EDITION != DeploymentEdition.COMMUNITY
        ):
            raise _CoordinationConflict("local_mode_required")
        expected_namespace, expected_revision, expected_digest = self._production_binding
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            integration = owner._integration()
            if (
                integration is None
                or integration.enabled is not True
                or integration.active_revision_id != expected_revision
            ):
                raise _CoordinationConflict("configuration_changed")
            revision = owner._revision(integration.id, expected_revision)
            namespace = session.get(CasdoorNamespaceExtend, revision.namespace_id)
            if (
                namespace is None
                or namespace.lifecycle is not CasdoorNamespaceLifecycle.ACTIVE
                or namespace.id != expected_namespace
                or revision.config_digest != expected_digest
                or owner._configuration(revision) != self._production_configuration
            ):
                raise _CoordinationConflict("configuration_changed")
        return None

    def _with_request_redis(self, redis_client) -> "CasdoorLocalLoginCoordinatorService":
        """Bind request Redis and rebuild the production issuer on that client."""
        coordinator = copy(self)
        coordinator._redis_client = redis_client
        if self._production_binding is not None:
            coordinator._require_production_policy()
            coordinator._finalization = CasdoorLocalLoginFinalizationService(
                session_factory=self._session_factory,
                configuration_factory=self._configuration_service._repository,
                session_gateway=RedisAccountSessionGateway(redis=redis_client),
                production_guard=coordinator._require_production_policy,
            )
        return coordinator

    def _configuration(self, consumed, operation, subject):
        operation._check()
        self._require_production_policy()
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
            if self._production_binding is not None and (
                self._production_binding != (namespace.id, revision.id, revision.config_digest)
                or config != self._production_configuration
            ):
                raise _CoordinationConflict("configuration_changed")
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
        self._require_production_policy()
        return config, context

    def _admission(self, context, consumed, bundle, online, profile):
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        # Existing exact bindings retain their snapshot-only email policy. NEW
        # admission validates a present email; only absence needs a local value
        # before the preflight can perform its complete collision scan.
        collision_email = profile.email if profile.email is not None else local_account_email(context, online, profile)
        with self._session_factory() as session, session.begin():
            preflight = CasdoorAccountPreflightRepository(session).reconstruct(
                context, key, collision_email=collision_email
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
        self._require_production_policy()
        from core.casdoor.admission import AdmissionAction
        from models.casdoor_extend import CasdoorIntentKind
        from repositories.casdoor_terminal_local_invitation_repository_extend import (
            _begin_ordinary_terminal_attempt,
            _end_ordinary_terminal_attempt,
        )

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
            ordinary_attempt = None
            try:
                config, _ = self._configuration(consumed, operation, "")
                if self._production_binding is not None:
                    if (
                        native_contract
                        != NativeTokenContract(
                            NativeTokenSchema.FLAT_USER_V1,
                            config.expected_issuer,
                            config.organization,
                            config.application,
                            config.client_id,
                        )
                        or directory_contract != DirectorySnapshotContract(organization=config.organization)
                        or credential_strategy != CasdoorBasicDirectoryCredentialStrategy(config.client_id)
                    ):
                        raise _CoordinationConflict("directory_contract_binding")
                validator = create_claims_validator(
                    operation,
                    namespace_id=consumed.context.namespace_id,
                    revision_id=consumed.context.revision_id,
                    diagnostic=False,
                    redis_client=self._redis_client,
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
                if self._production_binding is not None:
                    if (
                        contract != DirectorySnapshotContract(organization=config.organization)
                        or directory.deployment_proof is not None
                    ):
                        raise _CoordinationConflict("directory_contract_binding")
                elif (
                    contract.deployment_proof is None
                    or not contract.deployment_proof.matches(config)
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
                        if (
                            prepared.plan.action is AdmissionAction.USE_BOUND
                            and len(final_scope.intents) == 1
                            and final_scope.intents[0].kind is CasdoorIntentKind.INVITATION_FINALIZE
                        ):
                            ordinary_attempt = _begin_ordinary_terminal_attempt(
                                prepared=prepared, roles=roles, leases=leases, configuration=config
                            )
                        ordinary_kwargs = (
                            {"_ordinary_attempt": ordinary_attempt} if ordinary_attempt is not None else {}
                        )
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
                            **ordinary_kwargs,
                        )
                        if self._finalization is not None:
                            tokens = self._finalization._finalize_local_login(
                                prepared=prepared,
                                persisted=result,
                                roles=roles,
                                leases=leases,
                                configuration=config,
                                ip_address=ip_address,
                                **ordinary_kwargs,
                            )
                            finalization_outcome, token_outcome = "committed", "issued"
            except BaseException as error:
                primary = error
                finalization_outcome = getattr(error, "finalization_outcome", finalization_outcome)
                token_outcome = getattr(error, "token_outcome", token_outcome)
                raise
            finally:
                _end_ordinary_terminal_attempt(ordinary_attempt)
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
                    provenance=SessionProvenanceSeed(
                        result.account_id,
                        consumed.context.namespace_id,
                        consumed.context.revision_id,
                        float(bundle.identity.expires_at),
                        self._optional_rp_token(config, consumed, bundle, raw_tokens),
                    )
                    if cleanup and type(tokens) is AuthTokenPair
                    else None,
                )
            if not cleanup or pass_number == 1:
                error = _CoordinationConflict("cleanup_pending" if not cleanup else "drift_limit")
                error.local_outcome = "not_started"
                error.cleanup_released = cleanup
                raise error
        raise AssertionError("unreachable")

    def _optional_rp_token(self, configuration, consumed, bundle, raw_tokens):
        """Transient exact native slot after complete claims/online/local chain.

        Durable retention is still denied unless the post-cleanup source owner
        finds the current reviewed profile AND its real protocol observation.
        Optional snapshot failure cannot cancel an already issued normal pair.
        """
        try:
            policy = self._production_policy
            if (
                type(policy) is not AcceptedDeploymentPolicy
                or policy.rp_logout is None
                or not configuration.rp_logout
                or policy.binding.configuration_digest != configuration.config_digest()
                or (policy.binding.namespace_id, policy.binding.revision_id)
                != (str(consumed.context.namespace_id), str(consumed.context.revision_id))
            ):
                return None
            return RPLogoutTokenSnapshot(
                policy.binding,
                policy.proof_fingerprint,
                raw_tokens.payload["id_token"],
                float(bundle.identity.expires_at),
            )
        except Exception:
            return None
