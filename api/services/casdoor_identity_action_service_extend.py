"""Mounted source-session identity actions; no ordinary admission or session issuer.

The inherited diagnostic owner supplies only its bounded private runtime, controlled
provider operation and read-only workspace mapping. This owner supplies active/draft
binding, actual source authentication, subject/account leases and lifecycle writes.
Provider HTTP always precedes SQL. The final O(1) original refresh GET is the same
documented consistency exception as D04; Redis and SQL remain separate systems.
"""

import hashlib
import json
import sys
import time
from dataclasses import dataclass, replace
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
    CurrentAuthContext,
    SourceSessionContext,
    TrustedAuthContext,
    new_browser_scope,
    source_action_cookie_name,
    source_initialization_cookie_name,
)
from core.casdoor.claims import ClaimsValidator, NativeTokenContract, NativeTokenSchema
from core.casdoor.crypto import CertificateTrustStore, TrustedCertificate
from core.casdoor.deployment_evidence import DeploymentEvidenceError
from core.casdoor.gateway import CasdoorBasicDirectoryCredentialStrategy, CasdoorTokenGateway
from core.casdoor.leases import CasdoorLeases, CasdoorLeaseScope
from core.casdoor.reauthentication import verify_recent_auth_time
from core.casdoor.request_safety import RequestAction
from core.casdoor.role_graph import DirectorySnapshotContract, OnlineRoleSnapshotLoader
from models.account import Account, AccountStatus
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorValidationExtend,
    CasdoorValidationKind,
    CasdoorValidationStatus,
)
from repositories.casdoor_identity_lifecycle_repository_extend import CasdoorIdentityLifecycleRepository
from repositories.casdoor_identity_repository_extend import CasdoorIdentityRepository, VerifiedIdentityKey
from repositories.casdoor_validation_repository_extend import CasdoorValidationRepository, ValidationBinding

from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_diagnostic_service_extend import (
    CasdoorDiagnosticService,
    DiagnosticNavigation,
    _Draft,
    _ReadGuard,
)

PROOF_COOKIE_NAME = "casdoor_unlink_proof"
PROOF_SCOPE_COOKIE_NAME = "casdoor_unlink_scope"
IDENTITY_PATH = "/console/api/account/casdoor-identity"
RETURN_PATH = "/account"


@dataclass(frozen=True, repr=False)
class IdentityActionPrepared:
    path: str
    cookies: tuple[CookieDirective, ...]


class CasdoorIdentityActionService(CasdoorDiagnosticService):
    def _run(self, action, server_ip, operation, *, deadline=None):
        def bounded(client, owned_deadline):
            result = operation(client, owned_deadline)
            if time.monotonic() >= owned_deadline:
                raise AuthTransactionError("deadline")
            return result

        return super()._run(action, server_ip, bounded, deadline=deadline)

    def _return_url(self):
        return self._settings.CONSOLE_WEB_URL.rstrip("/") + RETURN_PATH + "?casdoor_identity=complete"

    def _reauth_draft(self, revision_id, etag=None):
        """Load the exact draft and require its reviewed reauthentication capability."""
        draft = super()._draft(revision_id, etag)
        policy = draft.policy
        if policy is None:
            if self._deployment_policy_service is None:
                raise AuthTransactionError("reauthentication_unavailable")
            try:
                policy = self._deployment_policy_service.resolve(
                    configuration=draft.configuration,
                    namespace_id=draft.binding.namespace_id,
                    revision_id=draft.binding.revision_id,
                    config_digest=draft.binding.config_digest,
                    rbac_mode="off",
                )
            except DeploymentEvidenceError:
                raise AuthTransactionError("reauthentication_unavailable") from None
        if policy.reauthentication is None:
            raise AuthTransactionError("reauthentication_unavailable")
        return replace(draft, policy=policy)

    def _account(self, account, *, management=False):
        if not isinstance(account, Account) or account.status != AccountStatus.ACTIVE:
            raise AuthTransactionError("source_session_invalid")
        with self._session_factory() as session, session.begin():
            fresh = session.get(Account, account.id)
            if fresh is None or fresh.status != AccountStatus.ACTIVE:
                raise AuthTransactionError("source_session_invalid")
            if management:
                self._configuration_service.require_management(fresh)
        return UUID(account.id)

    def _source(self, account, refresh_token, client, *, management=False):
        account_id = self._account(account, management=management)
        if type(refresh_token) is not str or not refresh_token or len(refresh_token) > 1024:
            raise AuthTransactionError("source_session_invalid")
        resolved = RedisAccountSessionGateway(redis=client).resolve_refresh_token(refresh_token)
        if resolved != str(account_id):
            raise AuthTransactionError("source_session_invalid")
        return SourceSessionContext(
            account_id, account_id, account_id, self._crypto().refresh_source_digest(refresh_token), management
        )

    def _active(self):
        if self._settings.RBAC_ENABLED is not False or self._configuration_service._rbac_enabled is not False:
            raise AuthTransactionError("local_mode_required")
        with self._session_factory() as session, session.begin():
            owner = self._configuration_service._repository(session)
            integration = owner._integration()
            if integration is None or integration.enabled is not True or not integration.active_revision_id:
                raise AuthTransactionError("inactive")
            revision = owner._revision(integration.id, integration.active_revision_id)
            namespace = session.get(CasdoorNamespaceExtend, revision.namespace_id)
            if namespace is None or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE:
                raise AuthTransactionError("namespace_fenced")
            config, secret = owner._configuration(revision), revision.encrypted_secret
            # Draft saves/ETag updates do not invalidate an active transaction.
            binding = ValidationBinding(
                UUID(integration.id),
                UUID(namespace.id),
                UUID(revision.id),
                0,
                namespace.fence_epoch,
                revision.config_digest,
            )
        policy = self._deployment_policy_service.resolve(
            config, binding.namespace_id, binding.revision_id, binding.config_digest, "off"
        )
        if not secret:
            raise AuthTransactionError("required_secret_missing")
        return _Draft(
            binding,
            config,
            secret,
            policy,
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

    def _identity(self, snapshot, account_id):
        with self._session_factory() as session, session.begin():
            row = CasdoorIdentityLifecycleRepository(session).identity(snapshot.binding.namespace_id, account_id)
            if (
                row.issuer != snapshot.configuration.expected_issuer
                or row.organization != snapshot.configuration.organization
            ):
                raise AuthTransactionError("identity_unavailable")
            return UUID(row.id), row.subject, row.sync_generation

    @staticmethod
    def _binding(snapshot, identity=None):
        value = {
            "namespace": str(snapshot.binding.namespace_id),
            "revision": str(snapshot.binding.revision_id),
            "digest": snapshot.binding.config_digest,
            "fence": snapshot.binding.fence_epoch,
            "policy": snapshot.policy.proof_fingerprint,
            "identity": [str(identity[0]), identity[2]] if identity else None,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _guard_action(self, snapshot, context, account, refresh_token, client, deadline):
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")
        current = self._reauth_draft(context.revision_id) if context.reauth_diagnostic else self._active()
        identity = (
            self._identity(current, context.source.account_id) if context.mode == AuthMode.REAUTH_UNLINK else None
        )
        source = self._source(account, refresh_token, client, management=context.reauth_diagnostic)
        if (
            current != snapshot
            or source != context.source
            or self._binding(current, identity) != context.action_binding
        ):
            raise AuthTransactionError("context_changed")
        if context.reauth_diagnostic or context.mode == AuthMode.REAUTH_UNLINK:
            if current.policy.reauthentication is None:
                raise AuthTransactionError("reauthentication_unavailable")
        if context.mode == AuthMode.REAUTH_UNLINK and (
            current.configuration.self_unlink is not True or identity[0] != context.identity_id
        ):
            raise AuthTransactionError("reauthentication_unavailable")
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")
        return CurrentAuthContext(
            context.namespace_id,
            context.revision_id,
            context.mode,
            context.registered_redirect_uri,
            source,
            identity_id=context.identity_id,
            action=context.action,
            diagnostic_binding=context.diagnostic_binding,
            action_binding=context.action_binding,
            reauth_diagnostic=context.reauth_diagnostic,
            allowed=True,
        )

    def prepare_action(self, account, *, mode, refresh_token, browser_scope, server_ip, revision_id=None, etag=None):
        deadline = time.monotonic() + 45

        def run(client, deadline):
            diagnostic = mode == AuthMode.DIAGNOSTIC
            if mode not in (AuthMode.LINK, AuthMode.REAUTH_UNLINK, AuthMode.DIAGNOSTIC):
                raise AuthTransactionError()
            action = (
                RequestAction.DIAGNOSTIC
                if diagnostic
                else RequestAction.LINK
                if mode == AuthMode.LINK
                else RequestAction.REAUTH
            )
            self._charge_action(client, action, server_ip=server_ip, deadline=deadline)
            source = self._source(account, refresh_token, client, management=diagnostic)
            self._charge_action(client, action, account_id=source.account_id, deadline=deadline)
            snapshot = (
                self._reauth_draft(revision_id, etag) if diagnostic else self._active()
            )
            identity = self._identity(snapshot, source.account_id) if mode == AuthMode.REAUTH_UNLINK else None
            if mode != AuthMode.LINK and snapshot.policy.reauthentication is None:
                raise AuthTransactionError("reauthentication_unavailable")
            if identity:
                self._require_safe(snapshot, source.account_id, identity)
                if snapshot.configuration.self_unlink is not True:
                    raise AuthTransactionError("reauthentication_unavailable")
            context = TrustedAuthContext(
                snapshot.binding.namespace_id,
                snapshot.binding.revision_id,
                mode,
                self._policy().backend_origin.rstrip("/") + COOKIE_PATH + "/callback",
                RETURN_PATH,
                source=source,
                identity_id=identity[0] if identity else None,
                action="unlink" if identity else None,
                diagnostic_binding=snapshot.digest if diagnostic else None,
                action_binding=self._binding(snapshot, identity),
                reauth_diagnostic=diagnostic,
            )
            guard = partial(self._guard_action, snapshot, context, account, refresh_token, client, deadline)
            # Account/admin POST paths cannot receive the auth-path scope. Use
            # a separate per-handle initializer owner; only the local auth GET
            # reads/preserves the shared scope, so concurrent tabs coexist.
            initializer = new_browser_scope(self._policy()).value
            store = AuthTransactionStore(client, self._crypto())
            if diagnostic:
                self._record_reauth_diagnostic(
                    snapshot, context, account, refresh_token, client, deadline, uuid4(), guard, None
                )
            handle = store.create_source_initialization(context, browser_scope=initializer, guard=guard)
            path = COOKIE_PATH + "/identity/" + handle
            cookie = CookieDirective(
                source_initialization_cookie_name(handle), initializer, 60, path, self._policy().secure
            )
            return IdentityActionPrepared(path, (cookie,))

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    def start_action(self, account, *, refresh_token, handle, browser_scope, initialization_cookie, server_ip):
        def run(client, deadline):
            store = AuthTransactionStore(client, self._crypto())
            context = store.source_initialization_hint(handle, browser_scope=initialization_cookie)
            snapshot = self._reauth_draft(context.revision_id) if context.reauth_diagnostic else self._active()
            guard = partial(self._guard_action, snapshot, context, account, refresh_token, client, deadline)
            store.consume_source_initialization(handle, browser_scope=initialization_cookie, guard=guard)
            scope = new_browser_scope(self._policy()).value if browser_scope is None else browser_scope
            created = store.create(context, browser_scope=scope, policy=self._policy(), guard=guard)
            operation = self._operation(snapshot, deadline)
            endpoints = operation.discover()
            guard()
            parameters = created.authorization_parameters | {
                "client_id": snapshot.configuration.client_id,
                "redirect_uri": context.registered_redirect_uri,
            }
            marker = CookieDirective(
                source_action_cookie_name(created.state),
                "identity",
                300,
                COOKIE_PATH + "/callback",
                self._policy().secure,
            )
            return DiagnosticNavigation(
                endpoints.authorization_endpoint + "?" + urlencode(parameters),
                (
                    created.cookie,
                    created.scope_cookie,
                    marker,
                    CookieDirective(
                        source_initialization_cookie_name(handle),
                        "",
                        0,
                        COOKIE_PATH + "/identity/" + handle,
                        self._policy().secure,
                    ),
                ),
                uuid4(),
            )

        return self._run(RequestAction.START, server_ip, run)

    def _require_safe(self, snapshot, account_id, identity):
        with self._session_factory() as session, session.begin():
            from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

            CasdoorLocalLifecycleRepository(session)._unlink_parent_union(
                account_id=account_id, namespace_id=snapshot.binding.namespace_id, identity_id=identity[0],
            )
            self._locked_binding(session, snapshot)
            repo = CasdoorIdentityLifecycleRepository(session)
            row = repo.identity(snapshot.binding.namespace_id, account_id)
            if (UUID(row.id), row.subject, row.sync_generation) != identity:
                raise AuthTransactionError("identity_unavailable")
            repo.require_unlink_safe(row, repo.account(account_id))

    def _identity_lease_scope(self, namespace_id, subject, account_id, *, lifecycle=False):
        members = ()
        if lifecycle:
            with self._session_factory() as session, session.begin():
                members = CasdoorIdentityLifecycleRepository(session).member_lease_scopes(account_id)
        return CasdoorLeaseScope(namespace_id, subject, account_ids=(account_id,), members=members)

    def _identity_action_leases(self, client, namespace_id, subject, account_id, *, lifecycle, deadline):
        """One original acquire/cleanup owner over all retained subject/member keys."""
        if not lifecycle:
            return CasdoorLeases(client, self._identity_lease_scope(namespace_id, subject, account_id), deadline=deadline)
        from core.casdoor.leases import WorkspaceMemberScope
        from models.casdoor_extend import CasdoorIdentityExtend as Identity, CasdoorManagedMembershipExtend as History
        from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

        with self._session_factory() as session, session.begin():
            owner = CasdoorLocalLifecycleRepository(session)
            identities = owner._archived_rows(Identity, Identity.account_id == str(account_id))
            histories = owner._archived_rows(History, sa.or_(
                History.account_id == str(account_id), History.identity_id.in_([r["id"] for r in identities])))
            if any(r["account_id"] != str(account_id) for r in histories):
                raise AuthTransactionError("managed_history_requires_release")
            members = tuple(WorkspaceMemberScope(UUID(r["workspace_id"]), account_id) for r in histories)
            current = CasdoorLeaseScope(namespace_id, subject, account_ids=(account_id,), members=members)
            subjects = tuple(CasdoorLeaseScope(UUID(r["namespace_id"]), r["subject"]) for r in identities
                             if (r["namespace_id"], r["subject"]) != (str(namespace_id), subject))
        return CasdoorLeases.for_scopes(client, (current, *subjects), deadline=deadline)

    def _final_source(self, session, source, refresh_token, client, snapshot, deadline):
        self._final_refresh(source, refresh_token, client)
        fresh = CasdoorIdentityLifecycleRepository(session).account(source.account_id)
        if source.management_authorized:
            self._configuration_service.require_management(fresh)
        current = self._deployment_policy_service.resolve(
            snapshot.configuration,
            snapshot.binding.namespace_id,
            snapshot.binding.revision_id,
            snapshot.binding.config_digest,
            "off",
        )
        if current != snapshot.policy:
            raise AuthTransactionError("context_changed")
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")

    def _locked_binding(self, session, snapshot, *, diagnostic=False):
        integration = session.scalar(
            sa.select(CasdoorIntegrationExtend)
            .where(CasdoorIntegrationExtend.id == str(snapshot.binding.integration_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        namespace = session.scalar(
            sa.select(CasdoorNamespaceExtend)
            .where(CasdoorNamespaceExtend.id == str(snapshot.binding.namespace_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        pointer = (
            integration.draft_revision_id
            if diagnostic and integration
            else integration.active_revision_id
            if integration
            else None
        )
        if (
            integration is None
            or pointer != str(snapshot.binding.revision_id)
            or (not diagnostic and integration.enabled is not True)
            or namespace is None
            or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE
            or namespace.fence_epoch != snapshot.binding.fence_epoch
            or (diagnostic and integration.etag != snapshot.binding.etag)
        ):
            raise AuthTransactionError("context_changed")

    def _write_binding(
        self, snapshot, context, identity, account, client, refresh_token, deadline, correlation, guard, leases
    ):
        guard()
        leases.ensure_owned()
        with self._session_factory() as session, session.begin():
            self._locked_binding(session, snapshot)
            repo = CasdoorIdentityLifecycleRepository(session)
            repo.account(context.source.account_id)
            bound = CasdoorIdentityRepository(session).bind(
                identity, account_id=context.source.account_id, expected_fence_epoch=snapshot.binding.fence_epoch
            )
            session.add(
                CasdoorAuditExtend(
                    namespace_id=str(context.namespace_id),
                    revision_id=str(context.revision_id),
                    identity_id=str(bound.identity_id),
                    account_id=account.id,
                    actor_account_id=account.id,
                    action="identity_link",
                    result_code="success",
                    correlation_id=str(correlation),
                    summary_json="{}",
                )
            )
            session.flush()
            leases.ensure_owned()
            self._final_source(session, context.source, refresh_token, client, snapshot, deadline)

    def _record_reauth_diagnostic(
        self, snapshot, context, account, refresh_token, client, deadline, correlation, guard, passed
    ):
        guard()
        with self._session_factory() as session, session.begin():
            self._locked_binding(session, snapshot, diagnostic=True)
            writer = CasdoorValidationRepository(self._configuration_service._repository(session))
            writer.record(
                snapshot.binding,
                kind=CasdoorValidationKind.PROTOCOL,
                status=CasdoorValidationStatus.PENDING
                if passed is None
                else CasdoorValidationStatus.PASSED
                if passed
                else CasdoorValidationStatus.FAILED,
                actor_account_id=context.source.account_id,
                correlation_id=correlation,
                policy=snapshot.policy,
                now=datetime.now(UTC),
            )
            # This optional capability is written only after actual prompt/PKCE/nonce/
            # claims/UserInfo/online directory and signed auth_time validation.
            row = session.scalar(
                sa.select(CasdoorValidationExtend).where(CasdoorValidationExtend.correlation_id == str(correlation))
            )
            summary = json.loads(row.summary_json)
            summary["capabilities"]["reauth_unlink"] = "pending" if passed is None else "passed" if passed else "failed"
            row.summary_json = json.dumps(summary, sort_keys=True, separators=(",", ":"))
            session.flush()
            self._final_source(session, context.source, refresh_token, client, snapshot, deadline)

    def complete_action(
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
        def run(client, deadline):
            store = AuthTransactionStore(client, self._crypto())
            context = store.source_action_hint(
                state, browser_scope=browser_scope, transaction_cookie=transaction_cookie
            )
            account = account_provider()
            snapshot = self._reauth_draft(context.revision_id) if context.reauth_diagnostic else self._active()
            guard = partial(self._guard_action, snapshot, context, account, refresh_token, client, deadline)
            consumed = store.consume(
                state,
                transaction_cookie=transaction_cookie,
                browser_scope=browser_scope,
                policy=self._policy(),
                guard=guard,
            )
            correlation = uuid4()
            cookies = (
                consumed.clear_cookie,
                CookieDirective(
                    source_action_cookie_name(state), "", 0, COOKIE_PATH + "/callback", self._policy().secure
                ),
            )
            if provider_error is not None:
                guard()
                if context.reauth_diagnostic:
                    self._record_reauth_diagnostic(
                        snapshot, context, account, refresh_token, client, deadline, correlation, guard, False
                    )
                return DiagnosticNavigation(self._result_url(context, "cancelled"), cookies, correlation)
            leases = None
            try:
                operation = self._operation(snapshot, deadline)
                tokens = CasdoorTokenGateway(operation).exchange_code(code, consumed.code_verifier)
                config = snapshot.configuration
                validator = ClaimsValidator(
                    trust_store=CertificateTrustStore(
                        tuple(TrustedCertificate(**pin.model_dump()) for pin in config.certificates)
                    ),
                    expected_issuer=config.expected_issuer,
                    organization=config.organization,
                    application=config.application,
                    client_id=config.client_id,
                )
                bundle = validator.verify_token_bundle(
                    tokens,
                    expected_nonce=consumed.nonce,
                    auth_started_at=consumed.auth_started_at,
                    contract=snapshot.native_token_contract,
                )
                validator.verify_userinfo(operation.userinfo(tokens.payload["access_token"]), identity=bundle.identity)
                guard()
                if not context.reauth_diagnostic:
                    leases = self._identity_action_leases(
                        client, context.namespace_id, bundle.identity.subject, context.source.account_id,
                        lifecycle=context.mode is AuthMode.REAUTH_UNLINK, deadline=deadline,
                    )
                    leases.acquire()
                snapshot_roles = OnlineRoleSnapshotLoader(
                    operation,
                    identity=bundle.identity,
                    claims_validator=validator,
                    leases=leases if leases is not None else _ReadGuard(guard),
                    credential_strategy=snapshot.credential_strategy,
                    contract=snapshot.directory_snapshot_contract,
                ).load()
                self._preview(snapshot, snapshot_roles, context.source, correlation)
                guard()
                if context.mode == AuthMode.LINK:
                    key = VerifiedIdentityKey(
                        context.namespace_id, config.expected_issuer, config.organization, bundle.identity.subject
                    )
                    self._write_binding(
                        snapshot, context, key, account, client, refresh_token, deadline, correlation, guard, leases
                    )
                else:
                    recent = verify_recent_auth_time(
                        validator,
                        tokens.payload["id_token"],
                        expected_nonce=consumed.nonce,
                        auth_started_at=consumed.auth_started_at,
                        now=datetime.now(UTC),
                    )
                    guard()
                    if context.reauth_diagnostic:
                        self._record_reauth_diagnostic(
                            snapshot, context, account, refresh_token, client, deadline, correlation, guard, True
                        )
                    else:
                        identity = self._identity(snapshot, context.source.account_id)
                        if identity[1] != bundle.identity.subject:
                            raise AuthTransactionError("identity_unavailable")
                        self._require_safe(snapshot, context.source.account_id, identity)
                        proof = store.create_unlink_proof(
                            context, browser_scope=browser_scope, auth_time=recent.auth_time, guard=guard
                        )
                        cookies += (
                            CookieDirective(PROOF_COOKIE_NAME, proof, 60, IDENTITY_PATH, self._policy().secure),
                            CookieDirective(
                                PROOF_SCOPE_COOKIE_NAME, browser_scope, 60, IDENTITY_PATH, self._policy().secure
                            ),
                        )
                return DiagnosticNavigation(
                    self._result_url(context, "ready" if context.mode == AuthMode.REAUTH_UNLINK else "linked"),
                    cookies,
                    correlation,
                )
            except Exception:
                guard()  # Invalid source/context rejects; never archives a success.
                if context.reauth_diagnostic:
                    self._record_reauth_diagnostic(
                        snapshot, context, account, refresh_token, client, deadline, correlation, guard, False
                    )
                return DiagnosticNavigation(self._result_url(context, "failed"), cookies, correlation)
            finally:
                if leases is not None:
                    self._release_leases(leases)

        return self._run(RequestAction.CALLBACK, server_ip, run)

    def _result_url(self, context, result):
        if context.reauth_diagnostic:
            return super()._return_url()
        return self._settings.CONSOLE_WEB_URL.rstrip("/") + RETURN_PATH + "?casdoor_identity=" + result

    def action_status(self, account, *, refresh_token, browser_scope, proof, server_ip):
        def run(client, deadline):
            source = self._source(account, refresh_token, client)
            snapshot = self._active()
            result = {"link": True, "reauthenticate": False, "unlink": False, "reason": None}
            try:
                identity = self._identity(snapshot, source.account_id)
                result["link"] = False
                if snapshot.policy.reauthentication is None or snapshot.configuration.self_unlink is not True:
                    result["reason"] = "reauthentication_unavailable"
                    return result
                self._require_safe(snapshot, source.account_id, identity)
                result["reauthenticate"] = True
                if proof is not None and browser_scope is not None:
                    store = AuthTransactionStore(client, self._crypto())
                    context = store.unlink_proof_hint(proof, browser_scope=browser_scope)
                    self._guard_action(snapshot, context, account, refresh_token, client, deadline)
                    result["unlink"] = True
            except AuthTransactionError as error:
                if error.reason == "identity_unavailable":
                    return result
                result["reason"] = (
                    error.reason
                    if error.reason
                    in ("other_login_unavailable", "managed_history_requires_release", "reauthentication_unavailable")
                    else "reauthentication_required"
                )
            if time.monotonic() >= deadline:
                raise AuthTransactionError("deadline")
            return result

        return self._run(RequestAction.START, server_ip, run)

    def unlink_action(self, account, *, refresh_token, browser_scope, proof, server_ip):
        def run(client, deadline):
            self._charge_action(client, RequestAction.REAUTH, server_ip=server_ip, deadline=deadline)
            source = self._source(account, refresh_token, client)
            self._charge_action(client, RequestAction.REAUTH, account_id=source.account_id, deadline=deadline)
            store = AuthTransactionStore(client, self._crypto())
            context = store.unlink_proof_hint(proof, browser_scope=browser_scope)
            snapshot = self._active()
            guard = partial(self._guard_action, snapshot, context, account, refresh_token, client, deadline)
            store.consume_unlink_proof(proof, browser_scope=browser_scope, guard=guard)
            identity = self._identity(snapshot, context.source.account_id)
            leases = self._identity_action_leases(
                client, snapshot.binding.namespace_id, identity[1], context.source.account_id,
                lifecycle=True, deadline=deadline,
            )
            try:
                leases.acquire()
                guard()
                with self._session_factory() as session, session.begin():
                    from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

                    CasdoorLocalLifecycleRepository(session)._unlink_parent_union(
                        account_id=context.source.account_id, namespace_id=context.namespace_id, identity_id=context.identity_id,
                    )
                    self._locked_binding(session, snapshot)
                    repo = CasdoorIdentityLifecycleRepository(session)
                    fresh = repo.account(context.source.account_id)
                    row = repo.identity(context.namespace_id, context.source.account_id)
                    if (UUID(row.id), row.subject, row.sync_generation) != identity or UUID(
                        row.id
                    ) != context.identity_id:
                        raise AuthTransactionError("identity_unavailable")
                    repo.unlink(row, fresh)
                    session.add(
                        CasdoorAuditExtend(
                            namespace_id=str(context.namespace_id),
                            revision_id=str(context.revision_id),
                            identity_id=str(context.identity_id),
                            account_id=account.id,
                            actor_account_id=account.id,
                            action="identity_unlink",
                            result_code="success",
                            correlation_id=str(uuid4()),
                            summary_json="{}",
                        )
                    )
                    session.flush()
                    leases.ensure_owned()
                    self._final_source(session, context.source, refresh_token, client, snapshot, deadline)
                return (
                    CookieDirective(PROOF_COOKIE_NAME, "", 0, IDENTITY_PATH, self._policy().secure),
                    CookieDirective(PROOF_SCOPE_COOKIE_NAME, "", 0, IDENTITY_PATH, self._policy().secure),
                )
            finally:
                self._release_leases(leases)

        return self._run(RequestAction.CALLBACK, server_ip, run)

    @staticmethod
    def _release_leases(leases):
        # Preserve the original cancellation/failure, including BaseException;
        # uncertain cleanup on a normal result must still refuse navigation.
        primary = sys.exc_info()[1]
        try:
            if leases.release() is not True:
                raise AuthTransactionError("lease_cleanup_uncertain")
        except BaseException:
            if primary is None:
                raise
