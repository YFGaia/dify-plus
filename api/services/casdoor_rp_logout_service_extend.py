"""Optional, provider-only RP protocol owner with single-use browser workflows.

An exact signed release profile is only a prerequisite. A real diagnostic token
exchange/claims/online check and browser-bound returned state produce the separate
observation. No Dify token, workspace mutation, login assertion or public pass flag
is accepted here. Provider navigation is returned only to the private controller
as a redacted object; never serialize its Location into Dify JSON or logs.
"""

import base64
import hashlib
import hmac
import json
import math
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from core.casdoor.auth_transactions import (
    AuthMode,
    ConsumedAuthTransaction,
    CookieDirective,
    CookiePolicy,
    CurrentAuthContext,
)
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import (
    CasdoorCrypto,
    EncryptionContext,
    EncryptionPurpose,
)
from core.casdoor.deployment_evidence import (
    AcceptedDeploymentPolicy,
    DeploymentBinding,
    ReviewedRPLogoutCapability,
)
from core.casdoor.gateway import CasdoorDirectoryGateway, GatewayOperation, RawTokens
from core.casdoor.redis_runtime import CasdoorRedisRuntimeFactory
from repositories.casdoor_rp_logout_repository_extend import (
    CasdoorRPLogoutRepository,
    canonical_opaque,
)

from services.casdoor_signing_validator_service_extend import create_claims_validator

RP_CALLBACK_PATH = "/console/api/auth/casdoor/logout/callback"

RP_HANDOFF_PATH = "/console/api/auth/casdoor/logout/"
RP_RETRY_PATH = "/console/api/auth/casdoor/logout/retry"
_OPTIONAL_SECONDS = 2.0
_WINDOW_SECONDS = 300


class RPLogoutUnavailable(ValueError):
    def __init__(self):
        super().__init__("casdoor_rp_logout_unavailable")


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(canonical_opaque(value).encode("ascii")).hexdigest()


@dataclass(frozen=True, repr=False)
class RPLogoutHandoff:
    """Opaque local navigation and host-only Cookies; never the provider target."""

    handoff_path: str
    cookies: tuple[CookieDirective, ...]


@dataclass(frozen=True, repr=False)
class RPLogoutNavigation:
    """Private 303 input containing a hint only in the trusted IdP target."""

    location: str
    cookies: tuple[CookieDirective, ...]


@dataclass(frozen=True, repr=False)
class RPLogoutReturn:
    """Anonymous completion only, never a login or provider-session assertion."""

    completed: bool
    cookies: tuple[CookieDirective, ...]


@dataclass(frozen=True, repr=False)
class RPProtocolObservation:
    binding: DeploymentBinding
    proof_fingerprint: str
    checked_at: datetime
    expires_at: datetime


@dataclass(frozen=True, repr=False)
class RPLogoutTokenSnapshot:
    """Private original native slot, supplied by the completed normal claims owner."""

    binding: DeploymentBinding
    proof_fingerprint: str
    id_token: str
    expires_at: float


class CasdoorRPLogoutService:
    def __init__(
        self,
        *,
        settings,
        secret_key,
        redis_runtime_factory,
        current_policy,
        diagnostic_guard,
        clock=time.time,
    ):
        self._settings = settings
        self._secret_key = secret_key
        self._runtime = redis_runtime_factory
        self._current_policy = current_policy
        self._diagnostic_guard = diagnostic_guard
        self._clock = clock

    @classmethod
    def for_production(cls, settings, *, current_policy, diagnostic_guard):
        """Lazy optional construction; no DB/provider/Redis IO or startup gate."""
        try:
            return cls(
                settings=settings,
                secret_key=settings.SECRET_KEY,
                redis_runtime_factory=CasdoorRedisRuntimeFactory(settings),
                current_policy=current_policy,
                diagnostic_guard=diagnostic_guard,
            )
        except Exception:
            return None

    @property
    def _crypto(self):
        return CasdoorCrypto(secret_key=self._secret_key, key_version="v1")

    def _cookie_policy(self):
        return CookiePolicy(
            self._settings.CONSOLE_API_URL,
            allow_loopback_http=self._settings.DEPLOY_ENV == "DEVELOPMENT",
        )

    def _cookie(self, kind, opaque, value, ttl):
        policy = self._cookie_policy()
        name = ("__Secure-" if policy.secure else "") + "casdoor_rp_" + kind + "_" + _hash(opaque)[:24]
        path = (
            RP_CALLBACK_PATH
            if kind == "state"
            else (RP_RETRY_PATH if kind == "retry" else RP_HANDOFF_PATH + canonical_opaque(opaque))
        )
        return CookieDirective(name, value, ttl, path, policy.secure)

    def _retry_cookies(self, opaque, browser, ttl):
        policy = self._cookie_policy()
        handle_name = ("__Secure-" if policy.secure else "") + "casdoor_rp_retry_handle"
        return (
            CookieDirective(handle_name, opaque if ttl else "", ttl, RP_RETRY_PATH, policy.secure),
            self._cookie("retry", opaque, browser, ttl),
        )

    def retry_cookie_names(self, opaque):
        cookies = self._retry_cookies(opaque, "", 0)
        return cookies[0].name, cookies[1].name

    def handoff_cookie_name(self, opaque):
        return self._cookie("handoff", opaque, "", 0).name

    def callback_cookie_name(self, state):
        return self._cookie("state", state, "", 0).name

    def clear_callback_cookie(self, state):
        return self._cookie("state", state, "", 0)

    def _reviewed(self, binding, fingerprint=None):
        if type(binding) is not DeploymentBinding:
            raise RPLogoutUnavailable()
        policy = self._current_policy(binding)
        now = datetime.fromtimestamp(self._clock(), UTC)
        if (
            type(policy) is not AcceptedDeploymentPolicy
            or policy.binding != binding
            or not policy.issued_at <= now < policy.expires_at
            or (fingerprint is not None and not hmac.compare_digest(policy.proof_fingerprint, fingerprint))
            or type(policy.rp_logout) is not ReviewedRPLogoutCapability
        ):
            raise RPLogoutUnavailable()
        cookie_policy = self._cookie_policy()
        if policy.rp_logout.post_logout_redirect_uri != cookie_policy.backend_origin.rstrip("/") + RP_CALLBACK_PATH:
            raise RPLogoutUnavailable()
        return policy

    def _run(self, work, *, deadline=None):
        # Do not retry ambiguous Redis commands. Late/unclean delivery is denied.
        deadline = min(
            deadline if deadline is not None else math.inf,
            time.monotonic() + _OPTIONAL_SECONDS,
        )
        scope, result, closed = None, None, False
        try:
            if time.monotonic() >= deadline:
                raise RPLogoutUnavailable()
            scope = self._runtime.open(deadline=deadline)
            result = work(CasdoorRPLogoutRepository(scope.client))
        except Exception:
            result = None
        finally:
            if scope is not None:
                try:
                    closed = scope.finish() is True
                except BaseException:
                    closed = False
        return result if closed and time.monotonic() < deadline else None

    @staticmethod
    def _context(kind, opaque, binding):
        return EncryptionContext(
            EncryptionPurpose.LOGOUT_ID_TOKEN,
            UUID(binding.namespace_id),
            UUID(binding.revision_id),
            "rp-" + kind + ":" + canonical_opaque(opaque),
        )

    def _encode(self, kind, opaque, binding, data):
        return _json(
            {
                "version": 1,
                "namespace_id": binding.namespace_id,
                "revision_id": binding.revision_id,
                "envelope": self._crypto.encrypt(
                    _json({"binding": binding.model_dump(), **data}),
                    context=self._context(kind, opaque, binding),
                ),
            }
        )

    def _decode(self, kind, opaque, raw):
        outer = json.loads(raw)
        if (
            set(outer) != {"version", "namespace_id", "revision_id", "envelope"}
            or type(outer["version"]) is not int
            or outer["version"] != 1
        ):
            raise RPLogoutUnavailable()
        context = EncryptionContext(
            EncryptionPurpose.LOGOUT_ID_TOKEN,
            UUID(outer["namespace_id"]),
            UUID(outer["revision_id"]),
            "rp-" + kind + ":" + canonical_opaque(opaque),
        )
        data = json.loads(self._crypto.decrypt(outer["envelope"], context=context))
        binding = DeploymentBinding.model_validate(data.pop("binding"))
        if (binding.namespace_id, binding.revision_id) != (
            outer["namespace_id"],
            outer["revision_id"],
        ):
            raise RPLogoutUnavailable()
        expiry = data.get("expires_at")
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= self._clock():
            raise RPLogoutUnavailable()
        return binding, data

    def _guard_grant(self, binding, grant):
        base_keys = {
            "proof_fingerprint",
            "expires_at",
            "id_token",
            "purpose",
            "source",
        }
        if set(grant) not in (base_keys, base_keys | {"retry_browser_digest", "attempts", "active_handoff"}):
            raise RPLogoutUnavailable()
        policy = self._reviewed(binding, grant["proof_fingerprint"])
        if grant["purpose"] == "diagnostic":
            if set(grant) != base_keys or type(grant["source"]) is not dict or self._diagnostic_guard is None:
                raise RPLogoutUnavailable()
            # The owner rechecks the exact draft and original request-refresh/access
            # mapping. The persisted projection is input, never authority itself.
            self._guard_diagnostic(binding, grant["source"])
        elif grant["purpose"] != "logout" or grant["source"] is not None:
            raise RPLogoutUnavailable()
        elif (
            set(grant) != base_keys | {"retry_browser_digest", "attempts", "active_handoff"}
            or type(grant["attempts"]) is not int
            or not 0 <= grant["attempts"] <= 5
            or type(grant["retry_browser_digest"]) is not str
            or len(grant["retry_browser_digest"]) != 64
        ):
            raise RPLogoutUnavailable()
        if type(grant["id_token"]) is not str or not 1 <= len(grant["id_token"]) <= 32 * 1024:
            raise RPLogoutUnavailable()
        return policy

    def _guard_diagnostic(self, binding, projection):
        if (
            self._diagnostic_guard is None
            or type(projection) is not dict
            or projection.get("rp_logout_diagnostic") is not True
        ):
            raise RPLogoutUnavailable()
        current = self._diagnostic_guard(binding, projection)
        if type(current) is not CurrentAuthContext or current.projection() != {
            key: value for key, value in projection.items() if key not in ("return_path", "locale", "timezone")
        }:
            raise RPLogoutUnavailable()

    def start_protocol(self, *, configuration, binding, consumed, raw_tokens, operation):
        """Provider-only diagnostic owner; keep its normal login session untouched.

        The mounted caller must use an explicitly RP-marked diagnostic transaction,
        not route an ordinary diagnostic or logout request here. No public token,
        synthetic claims or `passed` input can create a protocol observation.
        """
        try:
            if (
                type(configuration) is not CasdoorConfiguration
                or not configuration.rp_logout
                or type(consumed) is not ConsumedAuthTransaction
                or consumed.context.mode is not AuthMode.DIAGNOSTIC
                or consumed.context.diagnostic_binding is None
                or consumed.context.reauth_diagnostic
                or not consumed.context.rp_logout_diagnostic
                or type(raw_tokens) is not RawTokens
                or type(operation) is not GatewayOperation
                or not operation._exchange_started
                or operation.config != configuration
                or (
                    str(consumed.context.namespace_id),
                    str(consumed.context.revision_id),
                )
                != (binding.namespace_id, binding.revision_id)
                or configuration.config_digest() != binding.configuration_digest
            ):
                raise RPLogoutUnavailable()
            policy = self._reviewed(binding)
            source = consumed.context.public_projection()
            if self._diagnostic_guard is None:
                raise RPLogoutUnavailable()
            self._guard_diagnostic(binding, source)
            operation._check()
            validator = create_claims_validator(
                operation,
                namespace_id=UUID(binding.namespace_id),
                revision_id=UUID(binding.revision_id),
                diagnostic=False,
                redis_client=None,
            )
            bundle = validator.verify_token_bundle(
                raw_tokens,
                expected_nonce=consumed.nonce,
                auth_started_at=consumed.auth_started_at,
                contract=policy.native_token_contract,
                now=datetime.fromtimestamp(self._clock(), UTC),
            )
            validator.verify_userinfo(
                operation.userinfo(raw_tokens.payload["access_token"]),
                identity=bundle.identity,
            )
            directory = CasdoorDirectoryGateway(
                operation,
                verified_subject=bundle.identity.subject,
                credential_strategy=policy.credential_strategy,
            )
            validator.verify_online_user(directory.get_verified_user(), identity=bundle.identity)
            operation._check()
            self._guard_diagnostic(binding, source)
            policy = self._reviewed(binding, policy.proof_fingerprint)
            return self._issue_verified(
                policy=policy,
                id_token=raw_tokens.payload["id_token"],
                token_expires_at=bundle.identity.expires_at,
                purpose="diagnostic",
                source=source,
                deadline=operation.deadline,
            )
        except Exception:
            return None

    def _issue_verified(self, *, policy, id_token, token_expires_at, purpose, source, deadline=None):
        """Private owner input after exact-slot claims verification (or source CAS).

        B's local-success caller may use this only after consuming its private
        prepared source; controllers never accept these arguments from a payload.
        """
        ttl = math.floor(
            min(
                _WINDOW_SECONDS,
                policy.expires_at.timestamp() - self._clock(),
                token_expires_at - self._clock(),
            )
        )
        if ttl <= 0:
            return None
        grant_id, handoff_id, browser = (secrets.token_urlsafe(32) for _ in range(3))
        expiry = self._clock() + ttl
        grant = {
            "proof_fingerprint": policy.proof_fingerprint,
            "expires_at": expiry,
            "id_token": id_token,
            "purpose": purpose,
            "source": source,
        }
        retry_browser = secrets.token_urlsafe(32)
        if purpose == "logout":
            grant.update(retry_browser_digest=_hash(retry_browser), attempts=0, active_handoff=handoff_id)
        handoff = {
            "grant_id": grant_id,
            "browser_digest": _hash(browser),
            "expires_at": expiry,
        }

        def issue(repo):
            self._guard_grant(policy.binding, grant)
            if not repo.create_pair(
                first_kind="retry",
                first_id=grant_id,
                first_raw=self._encode("retry", grant_id, policy.binding, grant),
                second_kind="handoff",
                second_id=handoff_id,
                second_raw=self._encode("handoff", handoff_id, policy.binding, handoff),
                ttl=ttl,
            ):
                raise RPLogoutUnavailable()
            cookies = (self._cookie("handoff", handoff_id, browser, ttl),)
            if purpose == "logout":
                cookies += self._retry_cookies(grant_id, retry_browser, ttl)
            return RPLogoutHandoff(RP_HANDOFF_PATH + handoff_id, cookies)

        return self._run(issue, deadline=deadline)

    def navigate(self, *, opaque, browser_cookie):
        """Consume local handoff once before returning the trusted IdP 303 target."""

        def navigate(repo):
            raw = repo.read("handoff", opaque)
            binding, handoff = self._decode("handoff", opaque, raw)
            if set(handoff) != {
                "grant_id",
                "browser_digest",
                "expires_at",
            } or not hmac.compare_digest(handoff["browser_digest"], _hash(browser_cookie)):
                raise RPLogoutUnavailable()
            grant_raw = repo.read("retry", handoff["grant_id"])
            grant_binding, grant = self._decode("retry", handoff["grant_id"], grant_raw)
            if grant_binding != binding:
                raise RPLogoutUnavailable()
            policy = self._guard_grant(binding, grant)
            state, state_cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            expiry = min(
                handoff["expires_at"],
                grant["expires_at"],
                policy.expires_at.timestamp(),
            )
            ttl = math.floor(expiry - self._clock())
            data = {
                "grant_id": handoff["grant_id"],
                "browser_digest": _hash(state_cookie),
                "expires_at": expiry,
            }
            if grant["purpose"] == "logout":
                data["attempt"] = grant["attempts"]
            if not repo.transition(
                source_kind="handoff",
                source_id=opaque,
                expected=raw,
                target_kind="state",
                target_id=state,
                target_raw=self._encode("state", state, binding, data),
                ttl=ttl,
            ):
                raise RPLogoutUnavailable()
            location = (
                policy.rp_logout.end_session_endpoint
                + "?"
                + urlencode(
                    {
                        "id_token_hint": grant["id_token"],
                        "post_logout_redirect_uri": policy.rp_logout.post_logout_redirect_uri,
                        "state": state,
                    }
                )
            )
            return RPLogoutNavigation(
                location,
                (
                    self._cookie("handoff", opaque, "", 0),
                    self._cookie("state", state, state_cookie, ttl),
                ),
            )

        return self._run(navigate)

    @staticmethod
    def _observation_id(fingerprint):
        return base64.urlsafe_b64encode(bytes.fromhex(fingerprint)).rstrip(b"=").decode("ascii")

    def complete_callback(self, *, state, browser_cookie, provider_error=False):
        """Returned state proves only the reviewed RP round trip, never global logout."""
        source_to_publish = None
        checked_epoch, expiry_epoch = None, None
        deadline = time.monotonic() + _OPTIONAL_SECONDS

        def complete(repo):
            nonlocal source_to_publish, checked_epoch, expiry_epoch
            raw = repo.read("state", state)
            binding, data = self._decode("state", state, raw)
            base_keys = {"grant_id", "browser_digest", "expires_at"}
            if set(data) not in (base_keys, base_keys | {"attempt"}) or not hmac.compare_digest(
                data["browser_digest"], _hash(browser_cookie)
            ):
                raise RPLogoutUnavailable()
            grant_raw = repo.read("retry", data["grant_id"])
            grant_binding, grant = self._decode("retry", data["grant_id"], grant_raw)
            if grant_binding != binding:
                raise RPLogoutUnavailable()
            if grant["purpose"] == "logout" and (
                type(data.get("attempt")) is not int or data["attempt"] != grant.get("attempts")
            ):
                raise RPLogoutUnavailable()
            if type(provider_error) is not bool:
                raise RPLogoutUnavailable()
            if provider_error:
                if not repo.consume("state", state, raw):
                    raise RPLogoutUnavailable()
                if grant["purpose"] == "logout":
                    return RPLogoutReturn(False, (self._cookie("state", state, "", 0),))
                repo.consume("retry", data["grant_id"], grant_raw)
                return None
            policy = self._guard_grant(binding, grant)
            if grant["purpose"] == "logout":
                if not repo.consume("state", state, raw):
                    raise RPLogoutUnavailable()
                repo.consume("retry", data["grant_id"], grant_raw)
                return RPLogoutReturn(
                    True, (self._cookie("state", state, "", 0), *self._retry_cookies(data["grant_id"], "", 0))
                )
            now, expiry = (
                self._clock(),
                min(data["expires_at"], policy.expires_at.timestamp()),
            )
            if not repo.consume("state", state, raw):
                raise RPLogoutUnavailable()
            repo.consume("retry", data["grant_id"], grant_raw)
            source_to_publish = grant["source"]
            checked_epoch, expiry_epoch = now, expiry
            return RPProtocolObservation(
                binding,
                policy.proof_fingerprint,
                datetime.fromtimestamp(now, UTC),
                datetime.fromtimestamp(expiry, UTC),
            )

        result = self._run(complete, deadline=deadline)
        if type(result) is not RPProtocolObservation:
            return result

        def publish(repo):
            # The real callback validation/CAS scope has now confirmed cleanup.
            # A failed/late first scope cannot leave a usable observation. The
            # separate optional persistence scope never retroactively supplies
            # callback, source-session or deployment authority.
            self._reviewed(result.binding, result.proof_fingerprint)
            self._guard_diagnostic(result.binding, source_to_publish)
            observation = {
                "proof_fingerprint": result.proof_fingerprint,
                # Preserve the original NumericDate scalars. A datetime round
                # trip rounds to microseconds and can move `checked_at` slightly
                # into the future under a fixed clock, or extend a short expiry.
                "checked_at": checked_epoch,
                "expires_at": expiry_epoch,
            }
            opaque = self._observation_id(result.proof_fingerprint)
            if not repo.publish_observation(
                opaque,
                self._encode("observation", opaque, result.binding, observation),
                math.floor(expiry_epoch - self._clock()),
            ):
                raise RPLogoutUnavailable()
            return result

        return self._run(publish, deadline=deadline)

    def retry(self, *, opaque, browser_cookie):
        """Explicit anonymous retry derived only from the original accepted grant.

        Never extend the source window or return authentication. A new generation
        invalidates old handoffs/states; five attempts share the original browser
        transaction window, independently of provider capability.
        """

        def retry(repo):
            raw = repo.read("retry", opaque)
            binding, grant = self._decode("retry", opaque, raw)
            self._guard_grant(binding, grant)
            if (
                grant["purpose"] != "logout"
                or not hmac.compare_digest(grant["retry_browser_digest"], _hash(browser_cookie))
                or grant["attempts"] >= 5
            ):
                raise RPLogoutUnavailable()
            new_handoff, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            replacement = {**grant, "attempts": grant["attempts"] + 1, "active_handoff": new_handoff}
            handoff = {"grant_id": opaque, "browser_digest": _hash(browser), "expires_at": grant["expires_at"]}
            ttl = math.floor(grant["expires_at"] - self._clock())
            if not repo.retry(
                retry_id=opaque,
                expected=raw,
                replacement=self._encode("retry", opaque, binding, replacement),
                old_handoff=grant["active_handoff"],
                new_handoff=new_handoff,
                handoff_raw=self._encode("handoff", new_handoff, binding, handoff),
                ttl=ttl,
            ):
                raise RPLogoutUnavailable()
            return RPLogoutHandoff(RP_HANDOFF_PATH + new_handoff, (self._cookie("handoff", new_handoff, browser, ttl),))

        return self._run(retry)

    def observation(self, binding, *, deadline=None):
        """Only this producer's encrypted, exact-policy callback record can pass."""

        def observe(repo):
            policy = self._reviewed(binding)
            observation_id = self._observation_id(policy.proof_fingerprint)
            observed_binding, data = self._decode(
                "observation", observation_id, repo.read("observation", observation_id)
            )
            if observed_binding != binding or set(data) != {
                "proof_fingerprint",
                "checked_at",
                "expires_at",
            }:
                raise RPLogoutUnavailable()
            self._reviewed(binding, data["proof_fingerprint"])
            checked = data["checked_at"]
            if (
                type(checked) not in (int, float)
                or not math.isfinite(checked)
                or not checked <= self._clock() < data["expires_at"]
            ):
                raise RPLogoutUnavailable()
            return RPProtocolObservation(
                binding,
                policy.proof_fingerprint,
                datetime.fromtimestamp(checked, UTC),
                datetime.fromtimestamp(data["expires_at"], UTC),
            )

        return self._run(observe, deadline=deadline)

    def accept_token_snapshot(self, snapshot, *, deadline=None):
        """Default denied: signature without an actual RP observation stores no hint."""
        try:
            if (
                type(snapshot) is not RPLogoutTokenSnapshot
                or type(snapshot.id_token) is not str
                or not snapshot.id_token.isascii()
                or not 0 < len(snapshot.id_token) <= 32 * 1024
                or type(snapshot.expires_at) not in (float, int)
                or not math.isfinite(snapshot.expires_at)
                or snapshot.expires_at <= self._clock()
            ):
                return None
            policy = self._reviewed(snapshot.binding, snapshot.proof_fingerprint)
            observation = self.observation(snapshot.binding, deadline=deadline)
            if (
                type(observation) is not RPProtocolObservation
                or observation.proof_fingerprint != snapshot.proof_fingerprint
            ):
                return None
            expiry = min(snapshot.expires_at, policy.expires_at.timestamp(), observation.expires_at.timestamp())
            self._reviewed(snapshot.binding, snapshot.proof_fingerprint)
            return RPLogoutTokenSnapshot(snapshot.binding, snapshot.proof_fingerprint, snapshot.id_token, expiry)
        except Exception:
            return None

    def current_token_snapshot(self, snapshot):
        """Fresh policy fence immediately before source writes, without provider IO."""
        try:
            if type(snapshot) is not RPLogoutTokenSnapshot or snapshot.expires_at <= self._clock():
                return None
            self._reviewed(snapshot.binding, snapshot.proof_fingerprint)
            return snapshot
        except Exception:
            return None

    def issue_local_success(self, snapshot, *, deadline=None):
        """Only the session owner's exact prepared-record CAS calls this after logout.

        This is internal typed input, never a payload, flag, token or public opaque
        authorization endpoint. Revocation/expiry still stops new navigation.
        """
        try:
            accepted = self.accept_token_snapshot(snapshot, deadline=deadline)
            if accepted is None:
                return None
            policy = self._reviewed(accepted.binding, accepted.proof_fingerprint)
            return self._issue_verified(
                policy=policy,
                id_token=accepted.id_token,
                token_expires_at=accepted.expires_at,
                purpose="logout",
                source=None,
                deadline=deadline,
            )
        except Exception:
            return None
