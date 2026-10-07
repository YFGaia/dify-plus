"""Private ordinary LOCAL HTTP orchestration; transport mounting belongs elsewhere.

The production coordinator denies missing or revoked deployment authority.
Construction is lazy. The two private production seams are deliberately concrete:
offline tests may subclass them, but no request/configuration proof flag exists.
Each ingress owns one deadline and a fresh active snapshot, not a historical fence
receipt. Only the actual coordinator's normal finalized return can deliver tokens.
"""

import time
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, replace
from functools import partial, wraps
from urllib.parse import urlencode
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from configs import dify_config
from constants.languages import supported_language
from core.casdoor.admission import AdmissionError
from core.casdoor.auth_transactions import (
    COOKIE_PATH,
    AuthMode,
    AuthTransactionError,
    AuthTransactionStore,
    ConsumedAuthTransaction,
    CookieDirective,
    CookiePolicy,
    CurrentAuthContext,
    RestrictedResultBinding,
    RestrictedResultPayload,
    TrustedAuthContext,
    new_browser_scope,
    result_cookie_name,
    transaction_cookie_name,
)
from core.casdoor.claims import ClaimsError, NativeTokenContract, NativeTokenSchema
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.deployment_evidence import (
    AcceptedDeploymentPolicy,
    DeploymentEvidenceError,
)
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import (
    CasdoorBasicDirectoryCredentialStrategy,
    CasdoorTokenGateway,
    DirectoryCredentialStrategy,
    GatewayError,
    GatewayOperation,
)
from core.casdoor.leases import CasdoorLeaseError
from core.casdoor.mapping import MappingError
from core.casdoor.redis_runtime import CasdoorRedisUnavailable
from core.casdoor.request_safety import (
    CasdoorRequestLimiter,
    PublicError,
    RequestAction,
    RequestSafetyError,
    SafetyEvent,
    SafetyFailure,
    SafetyResultCode,
    TrustedRateLimitScope,
    format_public_error,
    record_safety_event,
)
from core.casdoor.role_graph import DirectorySnapshotContract, RoleSnapshotError
from enums import DeploymentEdition
from libs.helper import timezone as valid_timezone
from models.casdoor_extend import CasdoorNamespaceExtend, CasdoorNamespaceLifecycle
from repositories.casdoor_account_preflight_repository_extend import (
    AccountPreflightConflict,
)
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
)
from repositories.casdoor_identity_repository_extend import CasdoorIdentityConflict
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from services.casdoor_deployment_policy_service_extend import (
    CasdoorDeploymentPolicyService,
)
from services.casdoor_invited_local_login_coordinator_service_extend import (
    CasdoorInvitedLocalLoginCoordinatorService,
)
from services.casdoor_local_login_coordinator_service_extend import (
    CasdoorLocalLoginCoordinatorService,
    _CoordinationConflict,
)
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from services.casdoor_session_service_extend import (
    CasdoorSessionService,
    SessionProvenanceSeed,
)
from services.entities.account_login_entities import AuthTokenPair


def _record_event(event: SafetyEvent) -> None:
    """Ordinary observation failures cannot change an authentication outcome."""
    try:
        record_safety_event(event)
    except Exception:
        pass


def _request_runtime(method):
    """Deployment mode first, then private coordinator binding and a close gate.

    SQL/token phase facts survive a failed close. Suppressing delivery does not
    roll back SQL, revoke a token or establish physical 45-second completion.
    """

    @wraps(method)
    def run(self, **kwargs):
        deadline, correlation = time.monotonic() + 45.0, uuid4()
        action = RequestAction.START if method.__name__ == "start" else RequestAction.CALLBACK
        scope = None
        candidate = None
        try:
            # Reject unsupported deployment/role modes before reconstructing
            # the coordinator or opening its database owner transaction.
            self._check(deadline)
            request = copy(self)
            request._request_deadline, request._request_correlation = deadline, correlation
            coordinator = request._coordinator_for_request()
            request._production_policy = coordinator._production_policy
            if self._redis_runtime_factory is None:
                raise CasdoorRedisUnavailable()
            self._check(deadline)
            scope = self._redis_runtime_factory.open(deadline=deadline)
            self._check(deadline)
            request._redis_client = scope.client
            request._bound_coordinator = coordinator._with_request_redis(scope.client)
            request._request_deadline, request._request_correlation = deadline, correlation
            request._success_event = None
            candidate = method(request, **kwargs)
        except Exception as error:
            public = self._public_error(error, correlation, action)
            candidate = self._runtime_failure(method.__name__, kwargs, public)
        finally:
            # Includes cancellation and partially completed business calls.
            # A close failure must never replace an existing primary exception.
            closed = scope is None
            if scope is not None:
                try:
                    closed = scope.finish() is True
                except BaseException:
                    closed = False
        if not closed and getattr(candidate, "error", None) is None:
            public = self._public_error(CasdoorRedisUnavailable(), correlation, action)
            return self._runtime_failure(method.__name__, kwargs, public, candidate)
        if closed and getattr(candidate, "error", None) is None and request._success_event is not None:
            _record_event(request._success_event)
        # The optional provenance scope starts only AFTER mandatory token-runtime
        # cleanup has succeeded. Its own close/CAS/budget failure cannot suppress
        # a genuine normal pair; the close gate above remains mandatory.
        if (
            closed
            and type(candidate) is _CompleteResult
            and candidate.error is None
            and type(candidate.tokens) is AuthTokenPair
            and self._session_service is not None
            and candidate.provenance is not None
        ):
            try:
                cookie = self._session_service.create(
                    seed=candidate.provenance,
                    refresh_token=candidate.tokens.refresh_token,
                    deadline=deadline,
                )
                candidate = replace(candidate, source_cookie=cookie)
            except Exception:
                pass
        return candidate

    return run


@dataclass(frozen=True, repr=False)
class _StartResult:
    redirect: str | None
    cookies: tuple[CookieDirective, ...]
    error: PublicError | None
    status: int
    correlation_id: UUID


@dataclass(frozen=True, repr=False)
class _PhaseFacts:
    local_outcome: str = "not_started"
    finalization_outcome: str = "not_started"
    token_outcome: str = "not_started"
    cleanup_released: bool | None = None


@dataclass(frozen=True, repr=False)
class _CompleteResult(_StartResult):
    tokens: AuthTokenPair | None = None
    phases: _PhaseFacts = _PhaseFacts()
    provenance: SessionProvenanceSeed | None = None
    source_cookie: CookieDirective | None = None


@dataclass(frozen=True, repr=False)
class _CancelledNavigation:
    redirect: str
    cookies: tuple[CookieDirective, ...]
    correlation_id: UUID


@dataclass(frozen=True, repr=False)
class _RestrictedNavigation:
    handoff: str
    redirect: str
    cookies: tuple[CookieDirective, ...]
    correlation_id: UUID
    phases: _PhaseFacts


@dataclass(frozen=True, repr=False)
class _RestrictedResult:
    payload: RestrictedResultPayload | None
    cookies: tuple[CookieDirective, ...]
    error: PublicError | None
    status: int
    correlation_id: UUID


@dataclass(frozen=True, repr=False)
class _ActiveContext:
    integration_id: UUID
    namespace_id: UUID
    revision_id: UUID
    fence_epoch: int
    config_digest: str
    encrypted_secret: str | None
    configuration: CasdoorConfiguration
    policy: CookiePolicy
    web_origin: str

    @property
    def callback(self) -> str:
        return self.policy.backend_origin.rstrip("/") + COOKIE_PATH + "/callback"


class CasdoorLocalHttpService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        configuration_service: CasdoorConfigurationService,
        account_activation: AccountActivationService,
        redis_client,
        settings,
        redis_runtime_factory=None,
        deployment_policy_service: CasdoorDeploymentPolicyService | None = None,
        session_service: CasdoorSessionService | None = None,
    ) -> None:
        # No crypto, factories, SQL, Redis commands or provider work at startup.
        self._session_factory = session_factory
        self._configuration_service = configuration_service
        self._account_activation = account_activation
        self._redis_client = redis_client
        self._settings = settings
        self._redis_runtime_factory = redis_runtime_factory
        self._deployment_policy_service = deployment_policy_service
        self._session_service = session_service
        self._production_policy: AcceptedDeploymentPolicy | None = None

    def _runtime_failure(self, ingress, kwargs, public, candidate=None):
        cookies = ()
        # Reconstruct only the canonical clear for this request. Never forward
        # a new transaction, browser scope, result cookie or token on uncertainty.
        try:
            if ingress != "start":
                reader = ingress == "restricted_result"
                name = result_cookie_name(kwargs["handoff"]) if reader else transaction_cookie_name(kwargs["state"])
                cookies = (
                    CookieDirective(
                        name, "", 0, COOKIE_PATH + ("/result" if reader else "/callback"), self._policy().secure
                    ),
                )
        except Exception:
            pass
        if ingress == "restricted_result":
            return _RestrictedResult(None, cookies, public, public.status, public.correlation_id)
        if ingress == "start":
            return _StartResult(None, (), public, public.status, public.correlation_id)
        phases = getattr(candidate, "phases", _PhaseFacts())
        return _CompleteResult(None, cookies, public, public.status, public.correlation_id, phases=phases)

    def _coordinator_for_request(self) -> CasdoorLocalLoginCoordinatorService:
        self._check(self._request_deadline)
        return CasdoorLocalLoginCoordinatorService.for_production(
            session_factory=self._session_factory,
            configuration_service=self._configuration_service,
            account_owner=CasdoorLoginAccountService(activation=self._account_activation),
            redis_client=self._redis_client,
            deployment_policy_service=self._deployment_policy_service,
        )

    def _directory_inputs(
        self, operation: GatewayOperation, *, reviewed: bool = False
    ) -> tuple[NativeTokenContract, DirectorySnapshotContract, DirectoryCredentialStrategy]:
        snapshot = self._read_active_context(operation.deadline)
        if snapshot.configuration != operation.config or snapshot.callback != operation.registered_redirect_uri:
            raise AuthTransactionError("context_changed")
        if reviewed:
            policy = self._require_policy(snapshot)
            if policy is None:
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "deployment_proof_missing")
            return policy.native_token_contract, policy.directory_snapshot_contract, policy.credential_strategy
        config = snapshot.configuration
        # Server-selected, code-supported Casdoor profile. The callback still
        # validates the real ID/native tokens, UserInfo, online account and full
        # role graph before applying local workspace authorization.
        return (
            NativeTokenContract(
                NativeTokenSchema.FLAT_USER_V1,
                config.expected_issuer,
                config.organization,
                config.application,
                config.client_id,
            ),
            DirectorySnapshotContract(organization=config.organization),
            CasdoorBasicDirectoryCredentialStrategy(config.client_id),
        )

    def _require_policy(self, snapshot: _ActiveContext) -> AcceptedDeploymentPolicy | None:
        if self._deployment_policy_service is None:
            return None
        policy = self._deployment_policy_service.resolve(
            snapshot.configuration, snapshot.namespace_id, snapshot.revision_id, snapshot.config_digest, "off"
        )
        return policy

    def _check(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise RequestSafetyError(SafetyFailure.DEADLINE)
        if (
            self._settings.RBAC_ENABLED is not False
            or dify_config.RBAC_ENABLED is not False
            or self._configuration_service._rbac_enabled is not False
            or dify_config.DEPLOYMENT_EDITION != DeploymentEdition.COMMUNITY
        ):
            raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "local_mode_required")

    def _policy(self) -> CookiePolicy:
        return CookiePolicy(
            self._settings.CONSOLE_API_URL,
            allow_loopback_http=self._settings.DEPLOY_ENV == "DEVELOPMENT",
        )

    def _read_active_context(self, deadline: float) -> _ActiveContext:
        self._check(deadline)
        policy = self._policy()
        # Reuse the origin policy for the trusted web return destination as well.
        web = CookiePolicy(self._settings.CONSOLE_WEB_URL, allow_loopback_http=policy.allow_loopback_http)
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            integration = owner._integration()
            if integration is None or integration.enabled is not True or not integration.active_revision_id:
                raise CasdoorConfigurationError(CasdoorErrorCode.NOT_CONFIGURED, "inactive")
            revision = owner._revision(integration.id, integration.active_revision_id)
            namespace = session.get(CasdoorNamespaceExtend, revision.namespace_id)
            if namespace is None or namespace.lifecycle is not CasdoorNamespaceLifecycle.ACTIVE:
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "namespace_fenced")
            snapshot = _ActiveContext(
                UUID(integration.id),
                UUID(namespace.id),
                UUID(revision.id),
                namespace.fence_epoch,
                revision.config_digest,
                revision.encrypted_secret,
                owner._configuration(revision),
                policy,
                web.backend_origin.rstrip("/"),
            )
        self._check(deadline)
        return snapshot

    def _current_context(self, snapshot: _ActiveContext, deadline: float) -> CurrentAuthContext:
        if self._read_active_context(deadline) != snapshot:
            raise AuthTransactionError("context_changed")
        return CurrentAuthContext(
            snapshot.namespace_id, snapshot.revision_id, AuthMode.LOGIN, snapshot.callback, None, allowed=True
        )

    def _operation(self, snapshot: _ActiveContext, deadline: float) -> GatewayOperation:
        self._current_context(snapshot, deadline)
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            revision = owner._revision(str(snapshot.integration_id), str(snapshot.revision_id))
            if (
                revision.config_digest != snapshot.config_digest
                or revision.encrypted_secret != snapshot.encrypted_secret
            ):
                raise AuthTransactionError("context_changed")
            secret = owner.crypto.decrypt(
                revision.encrypted_secret, context=owner._secret_context(revision.namespace_id, revision.id)
            )
        self._current_context(snapshot, deadline)
        return GatewayOperation(
            config=snapshot.configuration,
            client_secret=secret,
            registered_redirect_uri=snapshot.callback,
            deadline=deadline,
            allow_loopback_http=snapshot.policy.allow_loopback_http,
        )

    def _store(self) -> AuthTransactionStore:
        return AuthTransactionStore(
            self._redis_client,
            CasdoorCrypto(secret_key=self._configuration_service._secret_key, key_version="v1"),
        )

    @staticmethod
    def _navigation(snapshot: _ActiveContext, return_path, locale, timezone, invite_token=None) -> TrustedAuthContext:
        try:
            language = supported_language(locale) if type(locale) is str else None
        except ValueError:
            language = None
        try:
            zone = valid_timezone(timezone) if type(timezone) is str else None
        except ValueError:
            zone = None
        context = TrustedAuthContext(
            snapshot.namespace_id,
            snapshot.revision_id,
            AuthMode.LOGIN,
            snapshot.callback,
            locale=language,
            timezone=zone,
            invite=invite_token,
        )
        try:
            return replace(context, return_path=return_path) if return_path is not None else context
        except AuthTransactionError:
            return context

    @staticmethod
    def _public_error(error: Exception | CasdoorErrorCode, correlation: UUID, action: RequestAction) -> PublicError:
        if isinstance(error, AuthTransactionError):
            error = (
                CasdoorErrorCode.PROVIDER_UNAVAILABLE
                if error.reason in ("storage_uncertain", "guard_unavailable")
                else error.code
            )
        elif isinstance(error, CasdoorLoginScopeConflict):
            error = CasdoorErrorCode.AUTHORIZATION_PENDING
        elif isinstance(
            error,
            CasdoorConfigurationError
            | DeploymentEvidenceError
            | GatewayError
            | ClaimsError
            | _CoordinationConflict
            | CasdoorLeaseError
            | AdmissionError
            | RoleSnapshotError
            | MappingError
            | AccountPreflightConflict
            | CasdoorIdentityConflict,
        ):
            error = error.code
        public = format_public_error(error, correlation_id=correlation)
        _record_event(SafetyEvent(action, public.code, correlation))
        return public

    @staticmethod
    def _phases(outcome) -> _PhaseFacts:
        # Called only on the actual coordinator return/exception stack. These
        # bounded facts explain failure; they can never independently admit login.
        def phase(name, allowed):
            value = getattr(outcome, name, "not_started")
            return value if type(value) is str and value in allowed else "unknown"

        cleanup = getattr(outcome, "cleanup_released", None)
        return _PhaseFacts(
            phase("local_outcome", ("not_started", "committed", "unknown")),
            phase("finalization_outcome", ("not_started", "not_committed", "committed", "unknown")),
            phase("token_outcome", ("not_started", "issued", "unknown")),
            cleanup if type(cleanup) is bool else None,
        )

    @staticmethod
    def _result_binding(snapshot: _ActiveContext) -> RestrictedResultBinding:
        return RestrictedResultBinding(
            snapshot.namespace_id,
            snapshot.revision_id,
            snapshot.fence_epoch,
            snapshot.config_digest,
            snapshot.policy.backend_origin,
            snapshot.web_origin,
        )

    @staticmethod
    def _restricted_code(error: Exception) -> CasdoorErrorCode | None:
        # Called only by the narrow actual C2 exception handler below. Neither
        # public formatting nor phase metadata establishes producer provenance.
        if type(error) is _CoordinationConflict and error.reason in ("cleanup_pending", "drift_limit"):
            return CasdoorErrorCode.AUTHORIZATION_PENDING
        if type(error) is CasdoorLeaseError and error.reason in ("busy", "ownership_lost", "redis_unavailable"):
            return CasdoorErrorCode.AUTHORIZATION_PENDING
        if type(error) is CasdoorLoginScopeConflict:
            return CasdoorErrorCode.AUTHORIZATION_PENDING
        if type(error) is MappingError and error.code in (
            CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN,
            CasdoorErrorCode.WORKSPACE_UNAVAILABLE,
            CasdoorErrorCode.AUTHORIZATION_PENDING,
        ):
            return error.code
        if (
            type(error) in (RoleSnapshotError, GatewayError)
            and error.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
            and error.reason
            in (
                "organization_schema",
                "visibility_unknown",
                "visibility_schema",
                "visibility_hidden",
                "relations_missing",
                "relations_schema",
                "relation_boundary",
                "relations_duplicate",
                "user_schema",
                "roles_schema",
                "role_schema",
                "role_duplicate",
                "role_dangling",
                "role_cycle",
                "nodes_limit",
                "edges_limit",
                "depth_limit",
            )
        ):
            return error.code
        return None

    def _create_restricted_navigation(
        self,
        *,
        snapshot: _ActiveContext,
        guard: Callable[[], CurrentAuthContext],
        browser_scope: str | None,
        consumed: ConsumedAuthTransaction,
        code: CasdoorErrorCode,
        correlation: UUID,
        phases: _PhaseFacts,
    ) -> _RestrictedNavigation:
        # This private helper has only the two same-call C2 producer callsites.
        # The original clear remains the sole outer failure directive, including
        # after a successful SET whose reply or postguard fails.
        guard()
        created = self._store().create_restricted_result(
            browser_scope=browser_scope,
            context_binding=self._result_binding(snapshot),
            payload=RestrictedResultPayload(code, str(correlation)),
            policy=snapshot.policy,
            guard=guard,
        )
        _record_event(SafetyEvent(RequestAction.CALLBACK, code, correlation))
        guard()
        return _RestrictedNavigation(
            created.handoff,
            snapshot.web_origin + "/signin/casdoor-result?handoff=" + created.handoff,
            (consumed.clear_cookie, created.result_cookie, created.scope_cookie),
            correlation,
            phases,
        )

    @_request_runtime
    def restricted_result(
        self,
        *,
        handoff: str,
        browser_scope: str | None,
        result_cookie: str | None,
        server_ip: str,
    ) -> _RestrictedResult:
        """Read one display result without admission, exchange or session work."""
        deadline, correlation = self._request_deadline, self._request_correlation
        cookies: tuple[CookieDirective, ...] = ()
        try:
            name = result_cookie_name(handoff)
            policy = self._policy()
            cookies = (CookieDirective(name, "", 0, COOKIE_PATH + "/result", policy.secure),)
            CasdoorRequestLimiter(self._redis_client).check_and_increment(
                RequestAction.CALLBACK, TrustedRateLimitScope.for_ip(server_ip), deadline=deadline
            )
            snapshot = self._read_active_context(deadline)
            guard = partial(self._current_context, snapshot, deadline)
            consumed = self._store().consume_restricted_result(
                handoff,
                browser_scope=browser_scope,
                result_cookie=result_cookie,
                context_binding=self._result_binding(snapshot),
                policy=snapshot.policy,
                guard=guard,
            )
            result_correlation = UUID(consumed.payload.correlation_id)
            _record_event(SafetyEvent(RequestAction.CALLBACK, consumed.payload.code, result_correlation))
            guard()
            return _RestrictedResult(consumed.payload, (consumed.clear_cookie,), None, 200, result_correlation)
        except Exception as error:
            public = self._public_error(error, correlation, RequestAction.CALLBACK)
            return _RestrictedResult(None, cookies, public, public.status, correlation)

    @_request_runtime
    def start(
        self,
        *,
        browser_scope: str | None,
        init_handle: str | None = None,
        return_path: str | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        invite_token: str | None = None,
        server_ip: str,
    ) -> _StartResult:
        deadline, correlation = self._request_deadline, self._request_correlation
        try:
            CasdoorRequestLimiter(self._redis_client).check_and_increment(
                RequestAction.START,
                TrustedRateLimitScope.for_ip(server_ip),
                deadline=deadline,
            )
            snapshot = self._read_active_context(deadline)
            guard = partial(self._current_context, snapshot, deadline)
            store = self._store()
            if init_handle is not None:
                if browser_scope is None or any(v is not None for v in (return_path, locale, timezone, invite_token)):
                    raise AuthTransactionError()
                context = store.consume_initialization(
                    init_handle,
                    browser_scope=browser_scope,
                    policy=snapshot.policy,
                    guard=guard,
                )
            else:
                context = self._navigation(snapshot, return_path, locale, timezone, invite_token)
                if browser_scope is None:
                    scope = new_browser_scope(snapshot.policy)
                    handle = store.create_initialization(
                        context,
                        browser_scope=scope.value,
                        policy=snapshot.policy,
                        guard=guard,
                    )
                    guard()
                    redirect = (
                        snapshot.policy.backend_origin.rstrip("/")
                        + COOKIE_PATH
                        + "/login?"
                        + urlencode({"init": handle})
                    )
                    return _StartResult(redirect, (scope,), None, 303, correlation)
            created = store.create(
                context,
                browser_scope=browser_scope,
                policy=snapshot.policy,
                guard=guard,
            )
            operation = self._operation(snapshot, deadline)
            guard()
            endpoints = operation.discover()
            guard()
            parameters = created.authorization_parameters | {
                "client_id": snapshot.configuration.client_id,
                "redirect_uri": snapshot.callback,
            }
            redirect = endpoints.authorization_endpoint + "?" + urlencode(parameters)
            self._success_event = SafetyEvent(RequestAction.START, SafetyResultCode.SUCCESS, correlation)
            return _StartResult(redirect, (created.cookie, created.scope_cookie), None, 302, correlation)
        except Exception as error:
            public = self._public_error(error, correlation, RequestAction.START)
            return _StartResult(None, (), public, public.status, correlation)

    @_request_runtime
    def complete(
        self,
        *,
        state: str,
        transaction_cookie: str | None,
        browser_scope: str | None,
        code: str | None = None,
        provider_error: str | None = None,
        server_ip: str,
    ) -> _CompleteResult | _RestrictedNavigation | _CancelledNavigation:
        deadline, correlation = self._request_deadline, self._request_correlation
        cookies: tuple[CookieDirective, ...] = ()
        phases = _PhaseFacts()
        try:
            # Canonical cookie location is safe even if the production gate or
            # consume fails. Presence of this directive is NOT consume evidence.
            name = transaction_cookie_name(state)
            policy = self._policy()
            cookies = (CookieDirective(name, "", 0, COOKIE_PATH + "/callback", policy.secure),)
            coordinator = self._bound_coordinator
            CasdoorRequestLimiter(self._redis_client).check_and_increment(
                RequestAction.CALLBACK,
                TrustedRateLimitScope.for_ip(server_ip),
                deadline=deadline,
            )
            if (code is None) == (provider_error is None):
                raise AuthTransactionError()
            snapshot = self._read_active_context(deadline)
            guard = partial(self._current_context, snapshot, deadline)
            consumed = self._store().consume(
                state,
                transaction_cookie=transaction_cookie,
                browser_scope=browser_scope,
                policy=snapshot.policy,
                guard=guard,
            )
            cookies = (consumed.clear_cookie,)
            guard()
            if consumed.context.mode is not AuthMode.LOGIN or any(
                value is not None
                for value in (
                    consumed.context.source,
                    consumed.context.identity_id,
                    consumed.context.action,
                )
            ):
                raise AuthTransactionError()
            if provider_error == "access_denied":
                _record_event(
                    SafetyEvent(
                        RequestAction.CALLBACK,
                        CasdoorErrorCode.INVALID_TRANSACTION,
                        correlation,
                    )
                )
                guard()
                return _CancelledNavigation(snapshot.web_origin + "/signin", cookies, correlation)
            if provider_error is not None:
                raise AuthTransactionError()
            operation = self._operation(snapshot, deadline)
            guard()
            raw = CasdoorTokenGateway(operation).exchange_code(code, consumed.code_verifier)
            guard()
            native, directory, strategy = self._directory_inputs(
                operation, reviewed=consumed.context.invite is not None
            )
            guard()
            if consumed.context.invite is not None:
                activation = copy(self._account_activation)
                activation._tokens = RedisInvitationTokenStore(redis=self._redis_client)
                coordinator = CasdoorInvitedLocalLoginCoordinatorService(
                    ordinary=coordinator, activation=activation
                )
            try:
                outcome = coordinator._coordinate_local_login(
                    consumed=consumed,
                    raw_tokens=raw,
                    operation=operation,
                    native_contract=native,
                    directory_contract=directory,
                    credential_strategy=strategy,
                    correlation_id=correlation,
                    ip_address=server_ip,
                )
            except Exception as error:
                phases = self._phases(error)
                restricted_code = self._restricted_code(error)
                if restricted_code is None:
                    raise
                return self._create_restricted_navigation(
                    snapshot=snapshot,
                    guard=guard,
                    browser_scope=browser_scope,
                    consumed=consumed,
                    code=restricted_code,
                    correlation=correlation,
                    phases=phases,
                )
            except BaseException as error:
                phases = self._phases(error)
                raise
            phases = self._phases(outcome)
            guard()
            if not (
                outcome.local_outcome == "committed"
                and outcome.finalization_outcome == "committed"
                and outcome.token_outcome == "issued"
                and outcome.cleanup_released is True
                and type(outcome.tokens) is AuthTokenPair
            ):
                return self._create_restricted_navigation(
                    snapshot=snapshot,
                    guard=guard,
                    browser_scope=browser_scope,
                    consumed=consumed,
                    code=CasdoorErrorCode.AUTHORIZATION_PENDING,
                    correlation=correlation,
                    phases=phases,
                )
            self._success_event = SafetyEvent(RequestAction.CALLBACK, SafetyResultCode.SUCCESS, correlation)
            return _CompleteResult(
                snapshot.web_origin + consumed.context.return_path,
                cookies,
                None,
                302,
                correlation,
                outcome.tokens,
                phases,
                provenance=outcome.provenance,
            )
        except Exception as error:
            public = self._public_error(error, correlation, RequestAction.CALLBACK)
            return _CompleteResult(None, cookies, public, public.status, correlation, phases=phases)
        except BaseException as error:
            # Preserve the exact primary cancellation. Transport owns application
            # of these safe directives and must re-raise; no session revoke here.
            error._casdoor_clear_cookies = cookies
            error._casdoor_phase_facts = phases
            raise
