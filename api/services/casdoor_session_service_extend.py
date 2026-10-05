"""Optional Casdoor provenance around the original Dify session owners.

Each observation owns a separate finite private Redis scope. Provider hints are
retained only with exact reviewed RP authority and an actual protocol observation.
Refresh/optional cleanup failure clears provenance, never the original token pair.
"""

import json
import logging
import math
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from core.casdoor.auth_transactions import CookieDirective, CookiePolicy
from core.casdoor.crypto import CasdoorCrypto, EncryptionContext, EncryptionPurpose
from core.casdoor.redis_runtime import CasdoorRedisRuntimeFactory
from repositories.casdoor_session_repository_extend import CasdoorSessionRepository

from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_rp_logout_service_extend import RPLogoutTokenSnapshot

logger = logging.getLogger(__name__)
_OPTIONAL_SECONDS = 2.0
SOURCE_COOKIE_NAME = "casdoor_source"


def _unavailable():
    # No exception string, opaque ID, account, token or source metadata is logged.
    try:
        logger.warning("casdoor_session_provenance_unavailable")
    except Exception:
        # An optional logging handler must not replace the original session result.
        pass


@dataclass(frozen=True, repr=False)
class SessionProvenanceSeed:
    account_id: UUID
    namespace_id: UUID
    revision_id: UUID
    id_token_expires_at: float
    rp_logout: RPLogoutTokenSnapshot | None = None


@dataclass(frozen=True)
class SessionObservation:
    source: Literal["casdoor", "local_only"] = "local_only"
    verified: bool = False
    rp_logout_available: bool = False
    expires_at: datetime | None = None


@dataclass(frozen=True, repr=False)
class _Record:
    raw: str
    namespace_id: UUID
    revision_id: UUID
    metadata: dict


@dataclass(frozen=True, repr=False)
class _PreparedLogout:
    service: object
    opaque: str
    record: _Record


class CasdoorRefreshObserver:
    """One request's optional transition, not a source authentication assertion."""

    def __init__(self, service, opaque):
        self._service, self._opaque = service, opaque
        self._deadline = time.monotonic() + _OPTIONAL_SECONDS
        self._record = None
        self.clear_cookie = bool(opaque)

    def before_rotation(self, *, refresh_token, account_id):
        self._record = self._service._run(
            lambda client: self._service._matching(client, self._opaque, refresh_token, account_id),
            deadline=self._deadline,
        )

    def after_rotation(self, *, refresh_token, account_id):
        if self._record is None:
            return
        old = self._record

        def migrate(client):
            self._service._valid_refresh(client, refresh_token, account_id)
            metadata = {**old.metadata, "refresh_digest": self._service._crypto.refresh_source_digest(refresh_token)}
            raw = self._service._encode(self._opaque, old.namespace_id, old.revision_id, metadata)
            if not CasdoorSessionRepository(client).replace(self._opaque, old.raw, raw):
                raise ValueError("casdoor_source_changed")
            self._service._matching(client, self._opaque, refresh_token, account_id)
            return True

        if self._service._run(migrate, deadline=self._deadline) is True:
            self.clear_cookie = False

    def unavailable(self):
        self.clear_cookie = bool(self._opaque)
        self._record = None


class CasdoorSessionService:
    def __init__(self, *, settings, secret_key, redis_runtime_factory, rp_logout_service=None):
        self._settings = settings
        self._secret_key = secret_key
        self._runtime = redis_runtime_factory
        self._rp_logout_service = rp_logout_service

    @classmethod
    def for_production(cls, settings, *, rp_logout_service=None):
        """Optional composition must not prevent other authentication owners."""
        try:
            return cls(
                settings=settings,
                secret_key=settings.SECRET_KEY,
                redis_runtime_factory=CasdoorRedisRuntimeFactory(settings),
                rp_logout_service=rp_logout_service,
            )
        except Exception:
            _unavailable()
            return None

    @property
    def _crypto(self):
        return CasdoorCrypto(secret_key=self._secret_key, key_version="v1")

    def _policy(self):
        return CookiePolicy(
            self._settings.CONSOLE_API_URL,
            allow_loopback_http=self._settings.DEPLOY_ENV == "DEVELOPMENT",
        )

    def cookie_name(self):
        return ("__Host-" if self._policy().secure else "") + SOURCE_COOKIE_NAME

    def clear_cookie(self):
        return CookieDirective(self.cookie_name(), "", 0, "/", self._policy().secure)

    def _run(self, work, *, deadline=None):
        deadline = min(deadline if deadline is not None else math.inf, time.monotonic() + _OPTIONAL_SECONDS)
        scope, value = None, None
        closed = False
        try:
            if self._runtime is None or time.monotonic() >= deadline:
                raise ValueError("casdoor_source_unavailable")
            scope = self._runtime.open(deadline=deadline)
            value = work(scope.client)
        except Exception:
            _unavailable()
        finally:
            if scope is not None:
                try:
                    closed = scope.finish() is True
                except BaseException:
                    _unavailable()
        if scope is not None and not closed:
            _unavailable()
        return value if closed and time.monotonic() < deadline else None

    @staticmethod
    def _valid_refresh(client, refresh_token, account_id):
        if type(refresh_token) is not str or not refresh_token or len(refresh_token) > 1024:
            raise ValueError("casdoor_source_invalid")
        # The original mapping is authoritative. Never inspect account latest.
        if RedisAccountSessionGateway(redis=client).resolve_refresh_token(refresh_token) != str(account_id):
            raise ValueError("casdoor_source_invalid")

    @staticmethod
    def _context(opaque, namespace, revision):
        return EncryptionContext(EncryptionPurpose.SESSION_PROVENANCE, namespace, revision, opaque)

    def _encode(self, opaque, namespace, revision, metadata):
        return json.dumps(
            {
                "version": 1,
                "namespace_id": str(namespace),
                "revision_id": str(revision),
                "envelope": self._crypto.encrypt(
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    context=self._context(opaque, namespace, revision),
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _decode(self, opaque, raw):
        data = json.loads(raw)
        if (
            set(data) != {"version", "namespace_id", "revision_id", "envelope"}
            or type(data["version"]) is not int
            or data["version"] != 1
        ):
            raise ValueError("casdoor_source_invalid")
        namespace, revision = UUID(data["namespace_id"]), UUID(data["revision_id"])
        metadata = json.loads(
            self._crypto.decrypt(data["envelope"], context=self._context(opaque, namespace, revision))
        )
        if set(metadata) not in (
            {"account_id", "refresh_digest", "expires_at"},
            {"account_id", "refresh_digest", "expires_at", "rp_logout"},
        ):
            raise ValueError("casdoor_source_invalid")
        UUID(metadata["account_id"])
        expiry = metadata["expires_at"]
        if type(expiry) not in (float, int) or not math.isfinite(expiry) or expiry <= time.time():
            raise ValueError("casdoor_source_expired")
        return _Record(raw, namespace, revision, metadata)

    def _matching(self, client, opaque, refresh_token, account_id):
        self._valid_refresh(client, refresh_token, account_id)
        raw = CasdoorSessionRepository(client).read(opaque)
        if raw is None:
            raise ValueError("casdoor_source_missing")
        record = self._decode(opaque, raw)
        if record.metadata["account_id"] != str(account_id) or not self._crypto.matches_refresh_source(
            refresh_token, record.metadata["refresh_digest"]
        ):
            raise ValueError("casdoor_source_invalid")
        return record

    def create(self, *, seed, refresh_token, deadline):
        """Called only after confirmed mandatory login/runtime cleanup.

        No arbitrary provenance TTL: the actual request-refresh TTL and verified
        ID-token usability window bound both the record and browser Cookie.
        """
        if type(seed) is not SessionProvenanceSeed:
            return None
        accepted_rp = None
        if self._rp_logout_service is not None and seed.rp_logout is not None:
            try:
                if time.monotonic() < deadline:
                    accepted_rp = self._rp_logout_service.accept_token_snapshot(seed.rp_logout, deadline=deadline)
            except Exception:
                _unavailable()

        def create(client):
            self._valid_refresh(client, refresh_token, seed.account_id)
            refresh_ttl = client.ttl(RedisAccountSessionGateway._refresh_token_key(refresh_token))
            if type(refresh_ttl) is not int or refresh_ttl <= 0:
                raise ValueError("casdoor_source_invalid")
            now = time.time()
            current_rp = None
            if accepted_rp is not None:
                try:
                    current_rp = self._rp_logout_service.current_token_snapshot(accepted_rp)
                except Exception:
                    _unavailable()
            ttl = math.floor(min(refresh_ttl, seed.id_token_expires_at - now))
            if current_rp is not None:
                ttl = min(ttl, math.floor(current_rp.expires_at - now))
            if ttl <= 0:
                return None
            # The displayed/checked window must not outlive the integer Redis TTL.
            expiry = now + ttl
            opaque = secrets.token_urlsafe(32)
            metadata = {
                "account_id": str(seed.account_id),
                "refresh_digest": self._crypto.refresh_source_digest(refresh_token),
                "expires_at": expiry,
            }
            if current_rp is not None:
                metadata["rp_logout"] = {
                    "binding": current_rp.binding.model_dump(),
                    "proof_fingerprint": current_rp.proof_fingerprint,
                    "id_token": current_rp.id_token,
                    "expires_at": min(current_rp.expires_at, expiry),
                }
            record = self._encode(opaque, seed.namespace_id, seed.revision_id, metadata)
            if not CasdoorSessionRepository(client).create(opaque, record, ttl):
                raise ValueError("casdoor_source_changed")
            return CookieDirective(self.cookie_name(), opaque, ttl, "/", self._policy().secure)

        return self._run(create, deadline=deadline)

    def observe(self, *, account_id, refresh_token, opaque):
        if not account_id or not refresh_token or not opaque:
            return SessionObservation()
        deadline = time.monotonic() + _OPTIONAL_SECONDS
        record = self._run(lambda client: self._matching(client, opaque, refresh_token, account_id), deadline=deadline)
        if record is None:
            return SessionObservation()
        available = self._accepted_rp(record, deadline=deadline) is not None
        return SessionObservation(
            "casdoor", True, available, datetime.fromtimestamp(record.metadata["expires_at"], UTC)
        )

    def refresh_observer(self, opaque):
        return CasdoorRefreshObserver(self, opaque) if opaque else None

    def consume(self, *, account_id, refresh_token, opaque):
        if not account_id or not refresh_token or not opaque:
            return

        def consume(client):
            record = self._matching(client, opaque, refresh_token, account_id)
            if not CasdoorSessionRepository(client).consume(opaque, record.raw):
                raise ValueError("casdoor_source_changed")
            return True

        self._run(consume)

    def _accepted_rp(self, record, *, deadline=None):
        if self._rp_logout_service is None or "rp_logout" not in record.metadata:
            return None
        try:
            from core.casdoor.deployment_evidence import DeploymentBinding

            data = record.metadata["rp_logout"]
            if type(data) is not dict or set(data) != {"binding", "proof_fingerprint", "id_token", "expires_at"}:
                return None
            binding = DeploymentBinding.model_validate(data["binding"])
            if (binding.namespace_id, binding.revision_id) != (str(record.namespace_id), str(record.revision_id)):
                return None
            if data["expires_at"] > record.metadata["expires_at"]:
                return None
            snapshot = RPLogoutTokenSnapshot(binding, data["proof_fingerprint"], data["id_token"], data["expires_at"])
            return self._rp_logout_service.accept_token_snapshot(snapshot, deadline=deadline)
        except Exception:
            return None

    def prepare_logout(self, *, account_id, refresh_token, opaque):
        """Read exact source using the original request-refresh before revocation.

        No consumption, provider I/O or public authorization assertion happens
        here. If the original local owner fails, this prepared record is unused.
        """
        if not account_id or not refresh_token or not opaque:
            return None
        record = self._run(lambda client: self._matching(client, opaque, refresh_token, account_id))
        return _PreparedLogout(self, opaque, record) if type(record) is _Record else None

    def complete_local_logout(self, prepared):
        """Exact-value CAS only AFTER original local logout succeeds.

        Refresh was legitimately revoked; do not try to resolve that mapping
        again. Optional CAS/policy/runtime failure preserves original success.
        """
        if type(prepared) is not _PreparedLogout or prepared.service is not self:
            return None
        deadline = time.monotonic() + _OPTIONAL_SECONDS

        def consume(client):
            if prepared.record.metadata["expires_at"] <= time.time():
                return None
            if not CasdoorSessionRepository(client).consume(prepared.opaque, prepared.record.raw):
                raise ValueError("casdoor_source_changed")
            return prepared.record

        record = self._run(consume, deadline=deadline)
        if record is None:
            return None
        accepted = self._accepted_rp(record, deadline=deadline)
        if accepted is None:
            return None
        try:
            return self._rp_logout_service.issue_local_success(accepted, deadline=deadline)
        except Exception:
            _unavailable()
            return None
