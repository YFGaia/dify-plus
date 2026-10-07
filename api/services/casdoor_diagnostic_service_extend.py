"""Browser-bound draft diagnostics, with no account, membership or session writes.

All provider work shares the original controlled gateway's 45-second deadline,
outside SQL transactions. Protocol and diagnostic success require this
callback's actual exchange, pinned claims, UserInfo and complete online reads.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from urllib.parse import urlencode
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.auth_transactions import (
    COOKIE_PATH,
    AuthMode,
    AuthTransactionError,
    AuthTransactionStore,
    CookieDirective,
    CookiePolicy,
    CurrentAuthContext,
    SourceSessionContext,
    TrustedAuthContext,
    diagnostic_cookie_name,
    diagnostic_initialization_cookie_name,
    new_browser_scope,
)
from core.casdoor.claims import NativeTokenContract, NativeTokenSchema
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.deployment_evidence import AcceptedDeploymentPolicy, DeploymentEvidenceError
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import CasdoorBasicDirectoryCredentialStrategy, CasdoorTokenGateway, GatewayOperation
from core.casdoor.mapping import (
    BuiltinResolution,
    MappingIdentityContext,
    ServerWorkspaceAvailability,
    WorkspaceAvailability,
    WorkspaceState,
    resolve_workspace_plan,
)
from core.casdoor.redis_runtime import CasdoorRedisRuntimeFactory, CasdoorRedisUnavailable
from core.casdoor.request_safety import CasdoorRequestLimiter, RequestAction, TrustedRateLimitScope
from core.casdoor.role_graph import DirectorySnapshotContract, OnlineRoleSnapshotLoader
from models.account import Account, Tenant, TenantStatus
from models.casdoor_extend import (
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorValidationKind,
    CasdoorValidationStatus,
)
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from repositories.casdoor_validation_repository_extend import CasdoorValidationRepository, ValidationBinding

from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_rp_logout_service_extend import RPLogoutHandoff, RPProtocolObservation
from services.casdoor_signing_validator_service_extend import create_claims_validator

RETURN_PATH = "/system-manage-extend/system-integration"


@dataclass(frozen=True, repr=False)
class DiagnosticNavigation:
    redirect: str
    cookies: tuple[CookieDirective, ...]
    correlation_id: UUID
    rp_logout: bool = False


@dataclass(frozen=True, repr=False)
class DiagnosticPrepared:
    reason: str | None = None
    navigation: DiagnosticNavigation | None = None


@dataclass(frozen=True, repr=False)
class _Draft:
    binding: ValidationBinding
    configuration: CasdoorConfiguration
    encrypted_secret: str
    policy: AcceptedDeploymentPolicy | None
    native_token_contract: NativeTokenContract
    directory_snapshot_contract: DirectorySnapshotContract
    credential_strategy: CasdoorBasicDirectoryCredentialStrategy

    @property
    def digest(self):
        return hashlib.sha256(
            json.dumps(
                {
                    "etag": self.binding.etag,
                    "namespace_id": str(self.binding.namespace_id),
                    "revision_id": str(self.binding.revision_id),
                    "fence_epoch": self.binding.fence_epoch,
                    "config_digest": self.binding.config_digest,
                    "policy": self.policy.proof_fingerprint if self.policy is not None else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


class CasdoorDiagnosticService:
    def __init__(
        self,
        *,
        session_factory,
        configuration_service,
        deployment_policy_service,
        settings,
        redis_runtime_factory=None,
        rp_logout_service=None,
    ):
        self._session_factory = session_factory
        self._configuration_service = configuration_service
        self._deployment_policy_service = deployment_policy_service
        self._settings = settings
        self._redis_runtime_factory = redis_runtime_factory
        self._rp_logout_service = rp_logout_service

    def _policy(self):
        return CookiePolicy(
            self._settings.CONSOLE_API_URL, allow_loopback_http=self._settings.DEPLOY_ENV == "DEVELOPMENT"
        )

    def _return_url(self):
        web = CookiePolicy(self._settings.CONSOLE_WEB_URL, allow_loopback_http=self._policy().allow_loopback_http)
        return web.backend_origin.rstrip("/") + RETURN_PATH + "?tab=casdoor"

    def _base(self, revision_id: UUID, etag: int | None = None):
        if self._settings.RBAC_ENABLED is not False or self._configuration_service._rbac_enabled is not False:
            raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "local_mode_required")
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            integration = owner._integration()
            if (
                integration is None
                or integration.draft_revision_id != str(revision_id)
                or (etag is not None and integration.etag != etag)
            ):
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "draft_pointer_mismatch")
            revision = owner._revision(integration.id, str(revision_id))
            namespace = session.get(CasdoorNamespaceExtend, revision.namespace_id)
            if namespace is None or namespace.lifecycle is not CasdoorNamespaceLifecycle.ACTIVE:
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "namespace_fenced")
            binding = ValidationBinding(
                UUID(integration.id),
                UUID(namespace.id),
                UUID(revision.id),
                integration.etag,
                namespace.fence_epoch,
                revision.config_digest,
            )
            return binding, owner._configuration(revision), revision.encrypted_secret

    def _draft(self, revision_id: UUID, etag: int | None = None):
        binding, config, secret = self._base(revision_id, etag)
        accepted = None
        # The common configuration/diagnostic/login profile is selected by
        # server code and tested against each real response. Optional RP logout
        # keeps its separate reviewed-release capability gate.
        if config.rp_logout and self._deployment_policy_service is not None:
            try:
                accepted = self._deployment_policy_service.resolve(
                    configuration=config,
                    namespace_id=binding.namespace_id,
                    revision_id=binding.revision_id,
                    config_digest=binding.config_digest,
                    rbac_mode="off",
                )
            except DeploymentEvidenceError:
                # Only the optional RP-logout profile uses reviewed deployment
                # evidence. Ordinary diagnostics continue on the built-in
                # protocol and must pass their real token/role checks.
                accepted = None
        if not secret:
            raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "required_secret_missing")
        return _Draft(
            binding,
            config,
            secret,
            accepted,
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

    def _crypto(self):
        return CasdoorCrypto(secret_key=self._configuration_service._secret_key, key_version="v1")

    def _source(self, account, refresh_token, client):
        self._configuration_service.require_management(account)
        with self._session_factory() as session, session.begin():
            fresh = session.get(Account, account.id)
            self._configuration_service.require_management(fresh)
        if type(refresh_token) is not str or not refresh_token or len(refresh_token) > 1024:
            raise AuthTransactionError()
        resolved = RedisAccountSessionGateway(redis=client).resolve_refresh_token(refresh_token)
        if resolved != account.id:
            raise AuthTransactionError("source_session_invalid")
        account_id = UUID(account.id)
        return SourceSessionContext(
            account_id, UUID(resolved), account_id, self._crypto().refresh_source_digest(refresh_token), True
        )

    def _guard(self, draft, source, account, refresh_token, client, deadline, *, rp_logout=False):
        if type(rp_logout) is not bool:
            raise AuthTransactionError()
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")
        current = self._draft(draft.binding.revision_id, draft.binding.etag)
        if current != draft or self._source(account, refresh_token, client) != source:
            raise AuthTransactionError("context_changed")
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")
        return CurrentAuthContext(
            draft.binding.namespace_id,
            draft.binding.revision_id,
            AuthMode.DIAGNOSTIC,
            self._policy().backend_origin.rstrip("/") + COOKIE_PATH + "/callback",
            source,
            diagnostic_binding=draft.digest,
            allowed=True,
            rp_logout_diagnostic=rp_logout,
        )

    def guard_rp_logout(self, binding, projection):
        """Actual access-account/management/request-refresh owner, never a public flag."""
        from flask import request
        from libs.login import current_user
        from libs.token import extract_access_token, extract_refresh_token, is_admin_api_key_request

        if is_admin_api_key_request(request) or not extract_access_token(request):
            raise AuthTransactionError("source_session_invalid")
        account = current_user._get_current_object()
        if not isinstance(account, Account) or not account.is_authenticated:
            raise AuthTransactionError("source_session_invalid")
        refresh_token = extract_refresh_token(request)

        def run(client, deadline):
            draft = self._draft(UUID(binding.revision_id))
            if draft.policy is None or draft.policy.binding != binding:
                raise AuthTransactionError("context_changed")
            source = self._source(account, refresh_token, client)
            return self._guard(draft, source, account, refresh_token, client, deadline, rp_logout=True)

        return self._run(RequestAction.CALLBACK, request.remote_addr, run, deadline=time.monotonic() + 2.0)

    def prepare_rp_logout(self, account, *, refresh_token, etag, revision_id, browser_scope, server_ip):
        """Fresh native-token diagnostic; does not overwrite any activation gate."""
        self._configuration_service.require_management(account)

        def run(client, deadline):
            source = self._source(account, refresh_token, client)
            draft = self._draft(revision_id, etag)
            if (
                self._rp_logout_service is None
                or draft.policy is None
                or not draft.configuration.rp_logout
                or draft.policy.rp_logout is None
            ):
                return DiagnosticPrepared(reason="live_test_not_wired")
            self._rp_logout_service._reviewed(draft.policy.binding, draft.policy.proof_fingerprint)
            guard = partial(self._guard, draft, source, account, refresh_token, client, deadline, rp_logout=True)
            guard()
            initializer = new_browser_scope(self._policy()).value
            context = TrustedAuthContext(
                draft.binding.namespace_id,
                draft.binding.revision_id,
                AuthMode.DIAGNOSTIC,
                guard().registered_redirect_uri,
                RETURN_PATH,
                source=source,
                diagnostic_binding=draft.digest,
                rp_logout_diagnostic=True,
            )
            store = AuthTransactionStore(client, self._crypto())
            handle = store.create_diagnostic_initialization(context, browser_scope=initializer, guard=guard)
            path = COOKIE_PATH + "/diagnostic/" + handle
            cookie = CookieDirective(
                diagnostic_initialization_cookie_name(handle), initializer, 60, path, self._policy().secure
            )
            return DiagnosticPrepared(navigation=DiagnosticNavigation(path, (cookie,), uuid4()))

        return self._run(RequestAction.START, server_ip, run)

    def rp_logout_status(self, account, *, refresh_token, revision_id, server_ip):
        self._configuration_service.require_management(account)

        def run(client, deadline):
            source = self._source(account, refresh_token, client)
            binding, config, _ = self._base(revision_id)
            result = {
                "revision_id": revision_id,
                "namespace_id": binding.namespace_id,
                "profile_available": False,
                "status": "unknown",
            }
            if self._rp_logout_service is None or not config.rp_logout:
                return result
            try:
                draft = self._draft(revision_id)
                if draft.policy is None:
                    return result
                self._rp_logout_service._reviewed(draft.policy.binding, draft.policy.proof_fingerprint)
                observation = self._rp_logout_service.observation(draft.policy.binding, deadline=deadline)
                guard = partial(self._guard, draft, source, account, refresh_token, client, deadline)
                guard()
                result.update(profile_available=True, status="not_run")
                if type(observation) is RPProtocolObservation:
                    result.update(status="passed", checked_at=observation.checked_at, expires_at=observation.expires_at)
                return result
            except Exception:
                self._source(account, refresh_token, client)
                return result

        return self._run(RequestAction.DIAGNOSTIC, server_ip, run, deadline=time.monotonic() + 2.0)

    def _final_refresh(self, source, refresh_token, client):
        """One original owner GET; no nested SQL or provider work."""
        resolved = RedisAccountSessionGateway(redis=client).resolve_refresh_token(refresh_token)
        if resolved != str(source.account_id) or not self._crypto().matches_refresh_source(
            refresh_token, source.refresh_digest
        ):
            raise AuthTransactionError("source_session_invalid")

    def _write(
        self,
        draft,
        account,
        correlation,
        rows,
        *,
        guard,
        final_refresh_check,
        deadline,
        preview=None,
        signing_keys=None,
    ):
        # Refresh/Redis and the fresh full source guard run before SQL locks.
        # The final O(1) private Redis GET below is the documented consistency
        # exception to api/AGENTS.md's no-I/O rule. It observes source revocation
        # after row flush, inside these short locks; no provider HTTP is in SQL.
        # Redis and SQL remain non-atomic after that final observation.
        guard()
        # The locked owner barrier and all rows share one rollback boundary.
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            writer = CasdoorValidationRepository(owner)
            writer.require_current(draft.binding)
            fresh = session.scalar(sa.select(Account).where(Account.id == account.id).with_for_update())
            self._configuration_service.require_management(fresh)
            accepted = draft.policy
            if accepted is not None:
                current = self._deployment_policy_service.resolve(
                    configuration=draft.configuration,
                    namespace_id=draft.binding.namespace_id,
                    revision_id=draft.binding.revision_id,
                    config_digest=draft.binding.config_digest,
                    rbac_mode="off",
                )
                if current != accepted:
                    raise AuthTransactionError("context_changed")
            now = datetime.now(UTC)
            for kind, status in rows:
                writer.record(
                    draft.binding,
                    kind=kind,
                    status=status,
                    actor_account_id=UUID(account.id),
                    correlation_id=correlation,
                    policy=accepted,
                    now=now,
                    preview=preview if kind is CasdoorValidationKind.DIAGNOSTIC else None,
                    signing_keys=signing_keys if kind is CasdoorValidationKind.PROTOCOL else None,
                )
            final_refresh_check()
            fresh = session.scalar(
                sa.select(Account)
                .where(Account.id == account.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            self._configuration_service.require_management(fresh)
            if accepted is not None:
                final_policy = self._deployment_policy_service.resolve(
                    configuration=draft.configuration,
                    namespace_id=draft.binding.namespace_id,
                    revision_id=draft.binding.revision_id,
                    config_digest=draft.binding.config_digest,
                    rbac_mode="off",
                )
                if final_policy != accepted:
                    raise AuthTransactionError("context_changed")
            if time.monotonic() >= deadline:
                raise AuthTransactionError("deadline")

    def _runtime(self, deadline, action):
        return (self._redis_runtime_factory or CasdoorRedisRuntimeFactory(self._settings)).open(deadline=deadline)

    def _run(self, action, server_ip, operation, *, deadline=None):
        deadline = time.monotonic() + 45.0 if deadline is None else deadline
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")
        runtime = self._runtime(deadline, action)
        try:
            client = runtime.client
            CasdoorRequestLimiter(client).check_and_increment(
                action, TrustedRateLimitScope.for_ip(server_ip), deadline=deadline
            )
            result = operation(client, deadline)
        except BaseException:
            runtime.finish()
            raise
        if not runtime.finish():
            raise CasdoorRedisUnavailable()
        return result

    @staticmethod
    def _charge_action(client, action, *, server_ip=None, account_id=None, deadline):
        """One logical action scope charge, outside repeated source guards."""
        scope = (
            TrustedRateLimitScope.for_ip(server_ip)
            if account_id is None
            else TrustedRateLimitScope.for_account(account_id)
        )
        CasdoorRequestLimiter(client).check_and_increment(action, scope, deadline=deadline)

    def prepare(self, account, *, refresh_token, etag, revision_id, browser_scope, server_ip):
        deadline = time.monotonic() + 45.0
        self._configuration_service.require_management(account)

        def run(client, deadline):
            correlation = uuid4()
            self._charge_action(client, RequestAction.DIAGNOSTIC, server_ip=server_ip, deadline=deadline)
            source = self._source(account, refresh_token, client)
            self._charge_action(client, RequestAction.DIAGNOSTIC, account_id=source.account_id, deadline=deadline)
            draft = self._draft(revision_id, etag)
            guard = partial(self._guard, draft, source, account, refresh_token, client, deadline)
            final_refresh_check = partial(self._final_refresh, source, refresh_token, client)
            guard()
            # STATIC is produced only by the existing real static owner.
            self._configuration_service.validate_static(account, etag=etag, revision_id=revision_id)
            self._write(
                draft,
                account,
                correlation,
                (
                    (CasdoorValidationKind.PROTOCOL, CasdoorValidationStatus.PENDING),
                    (CasdoorValidationKind.DIAGNOSTIC, CasdoorValidationStatus.PENDING),
                ),
                guard=guard,
                final_refresh_check=final_refresh_check,
                deadline=deadline,
            )
            # This management POST cannot receive the auth-path shared scope.
            # A per-handle owner cookie binds its local GET without resetting
            # previously created LOGIN/LINK/DIAGNOSTIC browser transactions.
            initializer = new_browser_scope(self._policy()).value
            context = TrustedAuthContext(
                draft.binding.namespace_id,
                draft.binding.revision_id,
                AuthMode.DIAGNOSTIC,
                guard().registered_redirect_uri,
                RETURN_PATH,
                source=source,
                diagnostic_binding=draft.digest,
            )
            store = AuthTransactionStore(client, self._crypto())
            handle = store.create_diagnostic_initialization(context, browser_scope=initializer, guard=guard)
            path = COOKIE_PATH + "/diagnostic/" + handle
            cookie = CookieDirective(
                diagnostic_initialization_cookie_name(handle), initializer, 60, path, self._policy().secure
            )
            return DiagnosticPrepared(navigation=DiagnosticNavigation(path, (cookie,), correlation))

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    def start(self, account, *, refresh_token, handle, browser_scope, initialization_cookie, server_ip):
        def run(client, deadline):
            store = AuthTransactionStore(client, self._crypto())
            hint = store.diagnostic_initialization_hint(handle, browser_scope=initialization_cookie)
            draft = self._draft(hint.revision_id)
            source = self._source(account, refresh_token, client)
            guard = partial(
                self._guard,
                draft,
                source,
                account,
                refresh_token,
                client,
                deadline,
                rp_logout=hint.rp_logout_diagnostic,
            )
            if hint.diagnostic_binding != draft.digest or hint.source != source:
                raise AuthTransactionError("context_changed")
            context = store.consume_diagnostic_initialization(handle, browser_scope=initialization_cookie, guard=guard)
            scope = new_browser_scope(self._policy()).value if browser_scope is None else browser_scope
            created = store.create(context, browser_scope=scope, policy=self._policy(), guard=guard)
            operation = self._operation(draft, deadline)
            endpoint = operation.discover().authorization_endpoint
            guard()
            parameters = created.authorization_parameters | {
                "client_id": draft.configuration.client_id,
                "redirect_uri": context.registered_redirect_uri,
            }
            return DiagnosticNavigation(
                endpoint + "?" + urlencode(parameters),
                (
                    created.cookie,
                    created.scope_cookie,
                    CookieDirective(
                        diagnostic_cookie_name(created.state),
                        "diagnostic",
                        300,
                        COOKIE_PATH + "/callback",
                        self._policy().secure,
                    ),
                    CookieDirective(
                        diagnostic_initialization_cookie_name(handle),
                        "",
                        0,
                        COOKIE_PATH + "/diagnostic/" + handle,
                        self._policy().secure,
                    ),
                ),
                uuid4(),
            )

        return self._run(RequestAction.START, server_ip, run)

    def _operation(self, draft, deadline):
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            revision = owner._revision(str(draft.binding.integration_id), str(draft.binding.revision_id))
            if (
                revision.config_digest != draft.binding.config_digest
                or revision.encrypted_secret != draft.encrypted_secret
            ):
                raise AuthTransactionError("context_changed")
            secret = owner.crypto.decrypt(
                revision.encrypted_secret, context=owner._secret_context(revision.namespace_id, revision.id)
            )
        return GatewayOperation(
            config=draft.configuration,
            client_secret=secret,
            registered_redirect_uri=self._policy().backend_origin.rstrip("/") + COOKIE_PATH + "/callback",
            deadline=deadline,
            allow_loopback_http=self._policy().allow_loopback_http,
        )

    def complete_if_diagnostic(
        self,
        account_provider,
        *,
        refresh_token,
        state,
        transaction_cookie,
        browser_scope,
        code,
        provider_error,
        server_ip,
    ):
        # Empty browser ownership cannot identify a diagnostic record. The
        # ordinary callback still applies its original parsing/error behavior.
        if browser_scope is None or transaction_cookie is None:
            return None

        def run(client, deadline):
            store = AuthTransactionStore(client, self._crypto())
            hint = store.diagnostic_hint(state, browser_scope=browser_scope, transaction_cookie=transaction_cookie)
            if hint is None:
                raise AuthTransactionError()
            account = account_provider()  # existing libs.login owner, never self-decoded JWT
            draft = self._draft(hint.revision_id)
            source = self._source(account, refresh_token, client)
            guard = partial(
                self._guard,
                draft,
                source,
                account,
                refresh_token,
                client,
                deadline,
                rp_logout=hint.rp_logout_diagnostic,
            )
            final_refresh_check = partial(self._final_refresh, source, refresh_token, client)
            if hint.diagnostic_binding != draft.digest or hint.source != source:
                raise AuthTransactionError("context_changed")
            consumed = store.consume(
                state,
                transaction_cookie=transaction_cookie,
                browser_scope=browser_scope,
                policy=self._policy(),
                guard=guard,
            )
            correlation = uuid4()
            if consumed.context.rp_logout_diagnostic:
                clear = (
                    consumed.clear_cookie,
                    CookieDirective(
                        diagnostic_cookie_name(state), "", 0, COOKIE_PATH + "/callback", self._policy().secure
                    ),
                )
                if provider_error is not None:
                    return DiagnosticNavigation(self._return_url(), clear, correlation)
                if self._rp_logout_service is None or draft.policy is None:
                    raise AuthTransactionError("optional_capability_unknown")
                guard()
                operation = self._operation(draft, deadline)
                tokens = CasdoorTokenGateway(operation).exchange_code(code, consumed.code_verifier)
                handoff = self._rp_logout_service.start_protocol(
                    configuration=draft.configuration,
                    binding=draft.policy.binding,
                    consumed=consumed,
                    raw_tokens=tokens,
                    operation=operation,
                )
                if type(handoff) is not RPLogoutHandoff:
                    raise AuthTransactionError("optional_capability_unknown")
                guard()
                return DiagnosticNavigation(
                    handoff.handoff_path, (*clear, *handoff.cookies), correlation, rp_logout=True
                )
            if provider_error is not None:
                self._write(
                    draft,
                    account,
                    correlation,
                    (
                        (CasdoorValidationKind.PROTOCOL, CasdoorValidationStatus.FAILED),
                        (CasdoorValidationKind.DIAGNOSTIC, CasdoorValidationStatus.UNKNOWN),
                    ),
                    guard=guard,
                    final_refresh_check=final_refresh_check,
                    deadline=deadline,
                )
                return DiagnosticNavigation(
                    self._return_url(),
                    (
                        consumed.clear_cookie,
                        CookieDirective(
                            diagnostic_cookie_name(state), "", 0, COOKIE_PATH + "/callback", self._policy().secure
                        ),
                    ),
                    correlation,
                )
            try:
                guard()
                operation = self._operation(draft, deadline)
                tokens = CasdoorTokenGateway(operation).exchange_code(code, consumed.code_verifier)
                validator = create_claims_validator(
                    operation,
                    namespace_id=draft.binding.namespace_id,
                    revision_id=draft.binding.revision_id,
                    diagnostic=True,
                    redis_client=client,
                )
                bundle = validator.verify_token_bundle(
                    tokens,
                    expected_nonce=consumed.nonce,
                    auth_started_at=consumed.auth_started_at,
                    contract=draft.native_token_contract,
                )
                validator.verify_userinfo(operation.userinfo(tokens.payload["access_token"]), identity=bundle.identity)
                snapshot = OnlineRoleSnapshotLoader(
                    operation,
                    identity=bundle.identity,
                    claims_validator=validator,
                    leases=_ReadGuard(guard),
                    credential_strategy=draft.credential_strategy,
                    contract=draft.directory_snapshot_contract,
                ).load()
                preview = self._preview(draft, snapshot, source, correlation)
                guard()
                self._write(
                    draft,
                    account,
                    correlation,
                    (
                        (CasdoorValidationKind.PROTOCOL, CasdoorValidationStatus.PASSED),
                        (CasdoorValidationKind.DIAGNOSTIC, CasdoorValidationStatus.PASSED),
                    ),
                    guard=guard,
                    final_refresh_check=final_refresh_check,
                    deadline=deadline,
                    preview=preview,
                    signing_keys=validator.signing_key_metadata(),
                )
            except Exception as primary:
                # Never persist raw provider errors. A changed draft rolls back
                # this whole writer UoW; its new revision has no inherited proof.
                try:
                    self._write(
                        draft,
                        account,
                        correlation,
                        (
                            (CasdoorValidationKind.PROTOCOL, CasdoorValidationStatus.FAILED),
                            (CasdoorValidationKind.DIAGNOSTIC, CasdoorValidationStatus.UNKNOWN),
                        ),
                        guard=guard,
                        final_refresh_check=final_refresh_check,
                        deadline=deadline,
                    )
                except Exception:
                    # Invalid source/context leaves the already committed new
                    # PENDING rows in place; never revives an older pass.
                    raise primary from None
                # Trusted source/config still valid: show the safe failed
                # summary on the configuration page and keep its Dify session.
            return DiagnosticNavigation(
                self._return_url(),
                (
                    consumed.clear_cookie,
                    CookieDirective(
                        diagnostic_cookie_name(state), "", 0, COOKIE_PATH + "/callback", self._policy().secure
                    ),
                ),
                correlation,
            )

        return self._run(RequestAction.CALLBACK, server_ip, run)

    def _preview(self, draft, snapshot, source, correlation):
        config = draft.configuration
        ids = {config.default_workspace_id} | {mapping.workspace_id for mapping in config.workspace_mappings}
        with self._session_factory() as session, session.begin():
            rows = session.scalars(sa.select(Tenant).where(Tenant.id.in_([str(value) for value in ids]))).all()
            availability = ServerWorkspaceAvailability(
                tuple(
                    WorkspaceAvailability(
                        UUID(row.id),
                        WorkspaceState.NORMAL if row.status == TenantStatus.NORMAL else WorkspaceState.UNAVAILABLE,
                        tuple(BuiltinResolution(role, role) for role in ("admin", "editor", "normal")),
                    )
                    for row in rows
                )
            )
        context = MappingIdentityContext(
            draft.binding.integration_id,
            draft.binding.revision_id,
            draft.binding.namespace_id,
            uuid4(),
            source.account_id,
            draft.binding.config_digest,
            config.expected_issuer,
            config.organization,
            config.application,
            config.client_id,
            snapshot.subject,
        )
        plan = resolve_workspace_plan(
            configuration=config, snapshot=snapshot, context=context, availability=availability
        )
        # Preview IDs never create an identity. No native roles/PII or raw graph
        # is persisted; these are bounded desired targets, not granted access.
        return {
            "revision_id": str(draft.binding.revision_id),
            "namespace_id": str(draft.binding.namespace_id),
            "correlation_id": str(correlation),
            "effective_role_count": len(snapshot.effective_roles),
            "stages": [
                {"stage": stage, "status": "passed"}
                for stage in ("configuration", "protocol", "identity", "online_status", "role_snapshot", "workspace")
            ],
            "targets": [
                {
                    "workspace_id": str(target.workspace_id),
                    "target_role": target.target_role,
                    "reason": target.reason.value,
                }
                for target in plan.targets
            ],
        }


class _ReadGuard:
    """Reuses the draft/source fence for read-only diagnostic graph ownership.

    No business writer is dispatched; the online snapshot is transient. This
    cannot be reused by login writers in place of their actual sorted leases.
    """

    def __init__(self, guard):
        self._guard = guard

    def ensure_owned(self, *, renew=False):
        self._guard()
