"""Casdoor-only request limits, public errors and reference-only logging.

The caller verifies proxy provenance before supplying an IP and reconstructs
local UUIDs from authenticated/database state. Shape validation is not trust.
RateLimiter's sliding-window check and increment are separate operations: these
limits are best-effort abuse controls, not atomic global traffic ceilings.
Deadline guards reject late results but cannot interrupt synchronous Redis I/O.
The default shared Redis client has 5s connect/read timeouts and three retries
with backoff; a bounded runtime client remains the deployment owner's obligation.
"""

import hashlib
import ipaddress
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypedDict
from uuid import UUID, uuid4

from libs.helper import RateLimiter, _RateLimiterRedisClient

from core.casdoor.crypto import CryptoError
from core.casdoor.errors import CasdoorErrorCode

logger = logging.getLogger(__name__)


class RequestAction(StrEnum):
    START = "start"
    CALLBACK = "callback"
    DIAGNOSTIC = "diagnostic"
    LINK = "link"
    REAUTH = "reauth"


RATE_LIMITS = MappingProxyType(
    {
        RequestAction.START: 20,
        RequestAction.CALLBACK: 40,
        RequestAction.DIAGNOSTIC: 5,
        RequestAction.LINK: 5,
        RequestAction.REAUTH: 5,
    }
)
RATE_WINDOW_SECONDS = 60


class ScopeKind(StrEnum):
    IP = "ip"
    ACCOUNT = "account"


class SafetyFailure(StrEnum):
    INVALID_INPUT = "invalid_input"
    RATE_LIMITED = "rate_limited"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    DEADLINE = "deadline"


class RequestSafetyError(Exception):
    """Fixed failure only, without Redis error text or input/cause serialization."""

    def __init__(self, failure: SafetyFailure):
        self.failure = failure if type(failure) is SafetyFailure else SafetyFailure.INVALID_INPUT
        self.code = (
            CasdoorErrorCode.PROVIDER_UNAVAILABLE
            if self.failure in (SafetyFailure.STORAGE_UNAVAILABLE, SafetyFailure.DEADLINE)
            else CasdoorErrorCode.INVALID_TRANSACTION
        )
        super().__init__(self.failure.value)


@dataclass(frozen=True, repr=False)
class TrustedRateLimitScope:
    kind: ScopeKind
    digest: str

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not ScopeKind
            or type(self.digest) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", self.digest)
        ):
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT)

    @classmethod
    def for_ip(cls, verified_server_ip: str) -> "TrustedRateLimitScope":
        """Validate syntax only; never call extract_remote_ip to establish trust."""
        try:
            if type(verified_server_ip) is not str or len(verified_server_ip) > 45 or "%" in verified_server_ip:
                raise ValueError
            canonical = ipaddress.ip_address(verified_server_ip).compressed
        except ValueError:
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT) from None
        return cls(ScopeKind.IP, hashlib.sha256(canonical.encode("ascii")).hexdigest())

    @classmethod
    def for_account(cls, local_account_id: UUID) -> "TrustedRateLimitScope":
        if type(local_account_id) is not UUID:
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
        return cls(ScopeKind.ACCOUNT, hashlib.sha256(local_account_id.bytes).hexdigest())


class CasdoorRequestLimiter:
    """Caller injects Redis policy; no global client or old provider is modified."""

    def __init__(self, redis_client: _RateLimiterRedisClient, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._limits = {
            action: RateLimiter(
                f"casdoor:request:v1:{action.value}", count, RATE_WINDOW_SECONDS, redis_client=redis_client
            )
            for action, count in RATE_LIMITS.items()
        }

    def _deadline(self, deadline: float) -> None:
        try:
            if type(deadline) not in (float, int) or not math.isfinite(deadline):
                raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
            if self._clock() >= deadline:
                raise RequestSafetyError(SafetyFailure.DEADLINE)
        except RequestSafetyError:
            raise
        except Exception:
            raise RequestSafetyError(SafetyFailure.STORAGE_UNAVAILABLE) from None

    def check_and_increment(self, action: RequestAction, scope: TrustedRateLimitScope, *, deadline: float) -> None:
        """Charge every admitted attempt, including later failures; never refund.

        Use verified-IP ingress controls for routes, and additionally local-account
        scopes for diagnostic/link/reauth after the original session-owner guard.
        Check/increment races can exceed the nominal threshold under concurrency.
        A partial increment or late reply is rejected without retrying this call.
        """
        if type(action) is not RequestAction or type(scope) is not TrustedRateLimitScope:
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
        scope.__post_init__()
        identity = f"{scope.kind.value}:{scope.digest}"
        limiter = self._limits[action]
        self._deadline(deadline)
        try:
            limited = limiter.is_rate_limited(identity)
        except Exception:
            raise RequestSafetyError(SafetyFailure.STORAGE_UNAVAILABLE) from None
        self._deadline(deadline)
        if limited:
            raise RequestSafetyError(SafetyFailure.RATE_LIMITED)
        try:
            limiter.increment_rate_limit(identity)
        except Exception:
            raise RequestSafetyError(SafetyFailure.STORAGE_UNAVAILABLE) from None
        self._deadline(deadline)


class PublicErrorPayload(TypedDict):
    code: str
    correlation_id: str
    retry_allowed: bool


@dataclass(frozen=True)
class PublicError:
    code: CasdoorErrorCode
    correlation_id: UUID
    status: int
    message: str
    retry_allowed: bool = False
    retry_after_seconds: int | None = None

    def payload(self) -> PublicErrorPayload:
        """Frozen R02 result shape; controller owns status/text/header transport."""
        return {"code": self.code.value, "correlation_id": str(self.correlation_id), "retry_allowed": False}


_PUBLIC_POLICIES = MappingProxyType(
    {
        CasdoorErrorCode.NOT_CONFIGURED: (503, "Casdoor authorization is unavailable."),
        CasdoorErrorCode.INVALID_TRANSACTION: (400, "Start a new authorization request."),
        CasdoorErrorCode.IDENTITY_CONFLICT: (409, "This authorization could not be completed."),
        CasdoorErrorCode.INVITATION_MISMATCH: (409, "This authorization could not be completed."),
        CasdoorErrorCode.WORKSPACE_UNAVAILABLE: (503, "This authorization could not be completed."),
        CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN: (503, "Start a new authorization request."),
        CasdoorErrorCode.AUTHORIZATION_PENDING: (409, "Authorization is pending. Start a new authorization request."),
        CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED: (403, "This authorization is not permitted."),
        CasdoorErrorCode.PROVIDER_UNAVAILABLE: (503, "Start a new authorization request."),
        CasdoorErrorCode.CONFIG_CONFLICT: (409, "The authorization configuration has changed."),
    }
)
_CRYPTO_CODES = MappingProxyType(
    {
        "casdoor_crypto_invalid": CasdoorErrorCode.INVALID_TRANSACTION,
        "casdoor_crypto_version_unsupported": CasdoorErrorCode.CONFIG_CONFLICT,
        "casdoor_crypto_key_version_unknown": CasdoorErrorCode.CONFIG_CONFLICT,
        "casdoor_crypto_decryption_failed": CasdoorErrorCode.INVALID_TRANSACTION,
        "casdoor_certificate_invalid": CasdoorErrorCode.CONFIG_CONFLICT,
        "casdoor_signature_invalid": CasdoorErrorCode.INVALID_TRANSACTION,
    }
)


def format_public_error(error: CasdoorErrorCode | Exception, *, correlation_id: UUID | None = None) -> PublicError:
    """Only typed enums and known CryptoError strings map to public codes.

    Domain owners pass their validated CasdoorErrorCode explicitly. Unknown
    exceptions, validation errors and arbitrary .code properties are never read,
    formatted or logged, including their causes/contexts/tracebacks.
    """
    if correlation_id is not None and type(correlation_id) is not UUID:
        raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
    code = CasdoorErrorCode.PROVIDER_UNAVAILABLE
    rate_limited = False
    if type(error) is CasdoorErrorCode:
        code = error
    elif type(error) is RequestSafetyError:
        code = error.code if type(error.code) is CasdoorErrorCode else code
        rate_limited = error.failure is SafetyFailure.RATE_LIMITED
    elif type(error) is CryptoError and type(error.code) is str:
        code = _CRYPTO_CODES.get(error.code, code)
    status, message = _PUBLIC_POLICIES[code]
    if rate_limited:
        status, message = 429, "Too many authorization requests. Start a new request after the wait."
    return PublicError(
        code, correlation_id or uuid4(), status, message, retry_after_seconds=60 if rate_limited else None
    )


class SafetyResultCode(StrEnum):
    SUCCESS = "success"


@dataclass(frozen=True)
class LocalReferences:
    namespace_id: UUID | None = None
    revision_id: UUID | None = None
    identity_id: UUID | None = None
    account_id: UUID | None = None
    actor_account_id: UUID | None = None
    workspace_id: UUID | None = None

    def values(self) -> dict[str, str]:
        result = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if value is not None:
                if type(value) is not UUID:
                    raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
                result[name] = str(value)
        return result


@dataclass(frozen=True)
class AuditSummary:
    """Exact counts only; no raw profile, query, subject or arbitrary payload."""

    workspace_count: int = 0
    role_count: int = 0
    intent_count: int = 0

    def values(self) -> dict[str, int]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        if any(type(value) is not int or not 0 <= value <= 20000 for value in values.values()):
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
        return values


@dataclass(frozen=True)
class SafetyEvent:
    action: RequestAction
    result_code: CasdoorErrorCode | SafetyResultCode
    correlation_id: UUID
    references: LocalReferences = field(default_factory=LocalReferences)
    summary: AuditSummary = field(default_factory=AuditSummary)

    def values(self) -> dict[str, str | dict[str, str] | dict[str, int]]:
        if (
            type(self.action) is not RequestAction
            or type(self.result_code) not in (CasdoorErrorCode, SafetyResultCode)
            or type(self.correlation_id) is not UUID
            or type(self.references) is not LocalReferences
            or type(self.summary) is not AuditSummary
        ):
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
        return {
            "action": self.action.value,
            "result_code": self.result_code.value,
            "correlation_id": str(self.correlation_id),
            "references": self.references.values(),
            "summary": self.summary.values(),
        }


def record_safety_event(event: SafetyEvent) -> None:
    if type(event) is not SafetyEvent:
        raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
    values = event.values()
    # The deployed text formatter does not serialize LogRecord extras. Keep the
    # bounded public result and correlation visible there as well as structured
    # logs, without rendering exception text or authentication payloads.
    logger.info(
        "casdoor_request_result action=%s result_code=%s correlation_id=%s",
        values["action"],
        values["result_code"],
        values["correlation_id"],
        extra={"casdoor_event": values},
        exc_info=False,
        stack_info=False,
    )


class ProfileAuditAction(StrEnum):
    PROFILE_SYNC = "profile_sync"


class ProfileAuditResult(StrEnum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    LOCAL_OVERRIDE = "local_override"
    DISABLED = "disabled"


class ProfileNameReason(StrEnum):
    DISABLED = "disabled"
    EMPTY_REMOTE_NAME = "empty_remote_name"
    FILLED_EMPTY = "filled_empty"
    CREATED_BASELINE = "created_baseline"
    MANAGED_UPDATE = "managed_update"
    SAME_NAME = "same_name"
    LOCAL_NAME_PRESENT = "local_name_present"
    UNOWNED_LOCAL_NAME = "unowned_local_name"
    LOCAL_OVERRIDE = "local_override"
    STALE_PROFILE_ATTEMPT = "stale_profile_attempt"
    AMBIGUOUS_PROFILE_ATTEMPT = "ambiguous_profile_attempt"


@dataclass(frozen=True, repr=False)
class ProfileAuditSummary:
    generation: int
    name_status: ProfileAuditResult
    name_reason: ProfileNameReason
    email_changed: bool
    email_differs: bool

    def values(self) -> dict[str, int | str | bool]:
        if (
            type(self.generation) is not int
            or not 0 <= self.generation <= 2**63 - 1
            or type(self.name_status) is not ProfileAuditResult
            or type(self.name_reason) is not ProfileNameReason
            or type(self.email_changed) is not bool
            or type(self.email_differs) is not bool
        ):
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
        return {
            "generation": self.generation,
            "name_status": self.name_status.value,
            "name_reason": self.name_reason.value,
            "email_changed": self.email_changed,
            "email_differs": self.email_differs,
        }


@dataclass(frozen=True, repr=False)
class ProfileAuditEvent:
    action: ProfileAuditAction
    result_code: ProfileAuditResult
    correlation_id: UUID
    references: LocalReferences
    summary: ProfileAuditSummary

    def values(self) -> dict[str, object]:
        if (
            type(self.action) is not ProfileAuditAction
            or type(self.result_code) is not ProfileAuditResult
            or type(self.correlation_id) is not UUID
            or type(self.references) is not LocalReferences
            or type(self.summary) is not ProfileAuditSummary
            or self.result_code is not self.summary.name_status
        ):
            raise RequestSafetyError(SafetyFailure.INVALID_INPUT)
        return {
            "action": self.action.value,
            "result_code": self.result_code.value,
            "correlation_id": str(self.correlation_id),
            "references": self.references.values(),
            "summary": self.summary.values(),
        }
