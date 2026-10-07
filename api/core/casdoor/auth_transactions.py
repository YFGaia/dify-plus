"""Short-lived, single-use Casdoor transactions, without HTTP/session/DB owners.

I20 must confirm the browser scope cookie before creating transactions. The short
initialization record confirms one scope+handle only; concurrent cookieless
requests have no shared browser identity. The five-slot limit is per scope, not
a global abuse limiter.
Source contexts are projections of already authenticated session-owner results;
HMAC linkage alone never authenticates a session. A fresh guard is required both
before and after consume; DB configuration changes are not atomic with Redis.
"""

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from core.casdoor.crypto import CasdoorCrypto, EncryptionContext, EncryptionPurpose
from core.casdoor.errors import CasdoorErrorCode

TTL_SECONDS = 300
MAX_TRANSACTIONS = 5
SCOPE_COOKIE_NAME = "casdoor_browser_scope"
COOKIE_PATH = "/console/api/auth/casdoor"
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")

CREATE_INITIALIZATION_SCRIPT = """
if not redis.call('SET', KEYS[1], ARGV[1], 'EX', 60, 'NX') then return 0 end
return 1
"""

CONSUME_INITIALIZATION_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw or redis.call('TTL', KEYS[1]) <= 0 then return false end
if type(raw) ~= 'string' or #raw == 0 or #raw > 16384 then return false end
local ok, record = pcall(cjson.decode, raw)
if not ok or type(record) ~= 'table' then return false end
local fields = {schema_version=true, owner=true, context=true, data=true}
local count = 0
for key, _ in pairs(record) do
    if not fields[key] then return false end
    count = count + 1
end
if count ~= 4 or type(record.schema_version) ~= 'number'
    or (record.schema_version ~= 1 and record.schema_version ~= 2)
    or type(record.owner) ~= 'string' or record.owner ~= ARGV[1]
    or type(record.context) ~= 'string' or record.context ~= ARGV[2]
    or type(record.data) ~= 'string' or #record.data == 0 or #record.data > 16384 then
    return false
end
redis.call('DEL', KEYS[1])
return raw
"""

CREATE_RESTRICTED_RESULT_SCRIPT = """
if not redis.call('SET', KEYS[1], ARGV[1], 'EX', 300, 'NX') then return 0 end
return 1
"""

CONSUME_RESTRICTED_RESULT_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw or redis.call('TTL', KEYS[1]) <= 0 then return false end
if type(raw) ~= 'string' or #raw == 0 or #raw > 16384 then return false end
local ok, record = pcall(cjson.decode, raw)
if not ok or type(record) ~= 'table' then return false end
local fields = {schema_version=true, owner=true, scope=true, context=true,
    issued_at=true, expires_at=true, data=true}
local count = 0
for key, _ in pairs(record) do
    if not fields[key] then return false end
    count = count + 1
end
local now = tonumber(ARGV[4])
if count ~= 7 or type(record.schema_version) ~= 'number' or record.schema_version ~= 1
    or type(record.owner) ~= 'string' or record.owner ~= ARGV[1]
    or type(record.scope) ~= 'string' or record.scope ~= ARGV[2]
    or type(record.context) ~= 'string' or record.context ~= ARGV[3]
    or type(record.issued_at) ~= 'number' or record.issued_at < 0
    or record.issued_at ~= math.floor(record.issued_at)
    or type(record.expires_at) ~= 'number' or record.expires_at > 253402300799
    or record.expires_at ~= math.floor(record.expires_at)
    or record.expires_at ~= record.issued_at + 300
    or not now or not (record.issued_at <= now and now < record.expires_at)
    or type(record.data) ~= 'string' or #record.data == 0 or #record.data > 16384 then
    return false
end
redis.call('DEL', KEYS[1])
return raw
"""

# Authorization keys share a browser-digest hash tag for both two-key scripts.
# TIME is Redis-owned slot expiry; the UTC claims clock is independently injected.
CREATE_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZCARD', KEYS[2]) >= 5 then return 0 end
if not redis.call('SET', KEYS[1], ARGV[1], 'EX', 300, 'NX') then return -1 end
redis.call('ZADD', KEYS[2], now + 300, ARGV[2])
redis.call('EXPIRE', KEYS[2], 300)
return 1
"""

CONSUME_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw or redis.call('TTL', KEYS[1]) <= 0 then return false end
local ok, record = pcall(cjson.decode, raw)
if not ok or record.owner ~= ARGV[1] or record.context ~= ARGV[2] then return false end
if not redis.call('ZSCORE', KEYS[2], ARGV[3]) then return false end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[3])
return record.data
"""


class AuthTransactionError(Exception):
    """Fixed non-sensitive reasons, never a Redis/provider/raw-input message."""

    code = CasdoorErrorCode.INVALID_TRANSACTION
    retry_allowed = False

    def __init__(self, reason: str = "invalid") -> None:
        self.reason = reason
        super().__init__("casdoor_transaction_" + reason)


class AuthMode(StrEnum):
    LOGIN = "login"
    LINK = "link"
    DIAGNOSTIC = "diagnostic"
    REAUTH_UNLINK = "reauth_unlink"


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise AuthTransactionError()
    try:
        if len(value.encode("utf-8")) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise AuthTransactionError()
    except UnicodeError:
        raise AuthTransactionError() from None
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise AuthTransactionError()
    return value


def _opaque(value: object) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise AuthTransactionError()
    # Canonical encoding of exactly 32 random bytes, not alternate base64 spelling.
    decoded = base64.urlsafe_b64decode(value + "=")
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise AuthTransactionError()
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_action_cookie_name(state: str) -> str:
    return "casdoor_action_" + _hash(_opaque(state))[:24]


def source_initialization_cookie_name(handle: str) -> str:
    return "casdoor_action_init_" + _hash(_opaque(handle))[:24]


def diagnostic_initialization_cookie_name(handle: str) -> str:
    return "casdoor_diagnostic_init_" + _hash(_opaque(handle))[:24]


def diagnostic_cookie_name(state: str) -> str:
    """Server-created routing marker only; the stored transaction is authority."""
    return "casdoor_diagnostic_" + _hash(_opaque(state))[:24]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise AuthTransactionError("clock_invalid")
    return value


def _url(value: str):
    try:
        result = urlsplit(_text(value, 2048))
        # urlsplit delays port validation; reject malformed ports as stable errors.
        _port = result.port
        return result
    except ValueError:
        raise AuthTransactionError() from None


@dataclass(frozen=True, repr=False)
class SourceSessionContext:
    """Trusted internal input, only after refresh validity/access/account checks.

    The caller must invoke the original session owner again for each callback and
    recheck diagnostic management permission. These fields do not prove validity.
    Never substitute account latest-refresh index lookup for request authentication.
    """

    account_id: UUID
    refresh_account_id: UUID
    access_account_id: UUID
    refresh_digest: str
    management_authorized: bool = False

    def __post_init__(self) -> None:
        if not (_uuid(self.account_id) == _uuid(self.refresh_account_id) == _uuid(self.access_account_id)):
            raise AuthTransactionError()
        if not isinstance(self.refresh_digest, str) or not _DIGEST.fullmatch(self.refresh_digest):
            raise AuthTransactionError()
        if type(self.management_authorized) is not bool:
            raise AuthTransactionError()

    def projection(self) -> dict[str, object]:
        return {
            "account_id": str(self.account_id),
            "refresh_digest": self.refresh_digest,
            "management_authorized": self.management_authorized,
        }


@dataclass(frozen=True, repr=False)
class TrustedAuthContext:
    """Start-owner validated configuration and navigation, never callback query.

    For login/link/reauth revision is the active revision; diagnostic is the exact
    requested immutable draft. Invite validity and supported locale/timezone are
    resolved by the start owner. This boundary additionally enforces strict limits.
    """

    namespace_id: UUID
    revision_id: UUID
    mode: AuthMode
    registered_redirect_uri: str
    return_path: str = "/apps"
    invite: str | None = None
    locale: str | None = None
    timezone: str | None = None
    source: SourceSessionContext | None = None
    identity_id: UUID | None = None
    action: str | None = None
    diagnostic_binding: str | None = None
    action_binding: str | None = None
    reauth_diagnostic: bool = False
    rp_logout_diagnostic: bool = False

    def __post_init__(self) -> None:
        _uuid(self.namespace_id)
        _uuid(self.revision_id)
        if not isinstance(self.mode, AuthMode):
            raise AuthTransactionError()
        url = _url(self.registered_redirect_uri)
        if (
            url.scheme not in ("https", "http")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path != COOKIE_PATH + "/callback"
        ):
            raise AuthTransactionError()
        path = _text(self.return_path, 2048)
        if (
            not path.startswith("/")
            or any(c in path for c in "?#\\%")
            or "//" in path
            or any(segment in (".", "..") for segment in path.split("/"))
        ):
            raise AuthTransactionError()
        for value, limit in ((self.invite, 512), (self.locale, 64), (self.timezone, 64)):
            if value is not None:
                _text(value, limit)
        if self.mode == AuthMode.LOGIN:
            if self.source is not None or self.identity_id is not None or self.action is not None:
                raise AuthTransactionError()
        elif not isinstance(self.source, SourceSessionContext):
            raise AuthTransactionError()
        if self.mode == AuthMode.DIAGNOSTIC and not self.source.management_authorized:
            raise AuthTransactionError()
        if self.diagnostic_binding is not None and (
            self.mode != AuthMode.DIAGNOSTIC or not _DIGEST.fullmatch(self.diagnostic_binding)
        ):
            raise AuthTransactionError()
        if type(self.reauth_diagnostic) is not bool or (self.reauth_diagnostic and self.mode != AuthMode.DIAGNOSTIC):
            raise AuthTransactionError()
        if type(self.rp_logout_diagnostic) is not bool or (
            self.rp_logout_diagnostic and (self.mode != AuthMode.DIAGNOSTIC or self.reauth_diagnostic)
        ):
            raise AuthTransactionError()
        if self.action_binding is not None and (
            self.mode == AuthMode.LOGIN or not _DIGEST.fullmatch(self.action_binding)
        ):
            raise AuthTransactionError()
        if self.mode == AuthMode.REAUTH_UNLINK:
            _uuid(self.identity_id)
            if self.action != "unlink":
                raise AuthTransactionError()
        elif self.identity_id is not None or self.action is not None:
            raise AuthTransactionError()

    def public_projection(self) -> dict[str, object]:
        result = {
            "namespace_id": str(self.namespace_id),
            "revision_id": str(self.revision_id),
            "mode": self.mode.value,
            "registered_redirect_uri": self.registered_redirect_uri,
            "return_path": self.return_path,
            "locale": self.locale,
            "timezone": self.timezone,
            "source": self.source.projection() if self.source else None,
            "identity_id": str(self.identity_id) if self.identity_id else None,
            "action": self.action,
        }
        if self.mode == AuthMode.DIAGNOSTIC:
            result["diagnostic_binding"] = self.diagnostic_binding
            if self.reauth_diagnostic:
                result["reauth_diagnostic"] = True
            if self.rp_logout_diagnostic:
                result["rp_logout_diagnostic"] = True
        if self.action_binding is not None:
            result["action_binding"] = self.action_binding
        return result


@dataclass(frozen=True, repr=False)
class CurrentAuthContext:
    """Fresh guard result, supplied by configuration/session owners, not the UI.

    allowed must cover enabled/active namespace lifecycle for ordinary modes. For
    diagnostic it covers the exact draft plus current management/session validity;
    explicit disable/revocation must set allowed=False. Saving an unrelated draft
    must leave the active projection unchanged. Only diagnostic compares drafts.
    """

    namespace_id: UUID
    revision_id: UUID
    mode: AuthMode
    registered_redirect_uri: str
    source: SourceSessionContext | None
    identity_id: UUID | None = None
    action: str | None = None
    diagnostic_binding: str | None = None
    action_binding: str | None = None
    reauth_diagnostic: bool = False
    rp_logout_diagnostic: bool = False
    allowed: bool = field(kw_only=True)

    def projection(self) -> dict[str, object]:
        if type(self.allowed) is not bool or not self.allowed:
            raise AuthTransactionError("context_changed")
        _uuid(self.namespace_id)
        _uuid(self.revision_id)
        if not isinstance(self.mode, AuthMode):
            raise AuthTransactionError()
        if self.mode == AuthMode.LOGIN:
            if self.source is not None:
                raise AuthTransactionError()
        elif not isinstance(self.source, SourceSessionContext):
            raise AuthTransactionError()
        if self.mode == AuthMode.DIAGNOSTIC and not self.source.management_authorized:
            raise AuthTransactionError()
        if type(self.reauth_diagnostic) is not bool or (self.reauth_diagnostic and self.mode != AuthMode.DIAGNOSTIC):
            raise AuthTransactionError()
        if type(self.rp_logout_diagnostic) is not bool or (
            self.rp_logout_diagnostic and (self.mode != AuthMode.DIAGNOSTIC or self.reauth_diagnostic)
        ):
            raise AuthTransactionError()
        if self.action_binding is not None and (
            self.mode == AuthMode.LOGIN or not _DIGEST.fullmatch(self.action_binding)
        ):
            raise AuthTransactionError()
        if self.mode == AuthMode.REAUTH_UNLINK:
            _uuid(self.identity_id)
            if self.action != "unlink":
                raise AuthTransactionError()
        elif self.identity_id is not None or self.action is not None:
            raise AuthTransactionError()
        result = {
            "namespace_id": str(self.namespace_id),
            "revision_id": str(self.revision_id),
            "mode": self.mode.value,
            "registered_redirect_uri": _text(self.registered_redirect_uri, 2048),
            "source": self.source.projection() if self.source else None,
            "identity_id": str(self.identity_id) if self.identity_id else None,
            "action": self.action,
        }
        if self.mode == AuthMode.DIAGNOSTIC:
            if self.diagnostic_binding is not None and not _DIGEST.fullmatch(self.diagnostic_binding):
                raise AuthTransactionError()
            result["diagnostic_binding"] = self.diagnostic_binding
            if self.reauth_diagnostic:
                result["reauth_diagnostic"] = True
            if self.rp_logout_diagnostic:
                result["rp_logout_diagnostic"] = True
        elif self.diagnostic_binding is not None:
            raise AuthTransactionError()
        if self.action_binding is not None:
            result["action_binding"] = self.action_binding
        return result


def _context_projection(context: TrustedAuthContext) -> dict[str, object]:
    public = context.public_projection()
    return {key: value for key, value in public.items() if key not in ("return_path", "locale", "timezone")}


@dataclass(frozen=True)
class CookiePolicy:
    """Host-only cookies. HTTP is allowed only for explicit loopback development."""

    backend_origin: str
    allow_loopback_http: bool = False

    def __post_init__(self) -> None:
        url = _url(self.backend_origin)
        if (
            url.username is not None
            or url.password is not None
            or not url.hostname
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or type(self.allow_loopback_http) is not bool
        ):
            raise AuthTransactionError()
        if url.scheme != "https" and not (
            url.scheme == "http" and self.allow_loopback_http and url.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise AuthTransactionError()

    @property
    def secure(self) -> bool:
        return urlsplit(self.backend_origin).scheme == "https"


@dataclass(frozen=True, repr=False)
class CookieDirective:
    name: str
    value: str
    max_age: int
    path: str
    secure: bool
    httponly: bool = True
    samesite: str = "Lax"
    domain: None = None


def new_browser_scope(policy: CookiePolicy) -> CookieDirective:
    """Bootstrap only; I20 must confirm this cookie round-trip before create."""
    return CookieDirective(SCOPE_COOKIE_NAME, secrets.token_urlsafe(32), TTL_SECONDS, COOKIE_PATH, policy.secure)


def transaction_cookie_name(state: str) -> str:
    return "casdoor_tx_" + _hash(_opaque(state))


def result_cookie_name(handoff: str) -> str:
    return "casdoor_result_" + _hash(_opaque(handoff))


@dataclass(frozen=True, repr=False, slots=True)
class RestrictedResultBinding:
    """Server snapshot projection, never consumed-callback or authentication proof.

    The service's guard closure must freshly verify fence, digest, both origins
    and runtime policy in addition to the original CurrentAuthContext projection.
    Only the supplied trusted CookiePolicy can enable loopback HTTP at use time.
    """

    namespace_id: UUID
    revision_id: UUID
    fence_epoch: int
    config_digest: str
    backend_origin: str
    web_origin: str
    mode: AuthMode = AuthMode.LOGIN

    def __post_init__(self) -> None:
        if type(self.namespace_id) is not UUID or type(self.revision_id) is not UUID:
            raise AuthTransactionError()
        if type(self.fence_epoch) is not int or self.fence_epoch < 0:
            raise AuthTransactionError()
        if type(self.config_digest) is not str or not _DIGEST.fullmatch(self.config_digest):
            raise AuthTransactionError()
        if self.mode is not AuthMode.LOGIN:
            raise AuthTransactionError()
        for origin in (self.backend_origin, self.web_origin):
            if type(origin) is not str:
                raise AuthTransactionError()
            CookiePolicy(origin, allow_loopback_http=True)

    def projection(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "namespace_id": str(self.namespace_id),
            "revision_id": str(self.revision_id),
            "fence_epoch": self.fence_epoch,
            "config_digest": self.config_digest,
            "backend_origin": self.backend_origin,
            "web_origin": self.web_origin,
            "mode": self.mode.value,
        }


@dataclass(frozen=True, repr=False, slots=True)
class RestrictedResultPayload:
    """Closed display-only result; no identity, navigation or session authority."""

    code: CasdoorErrorCode
    correlation_id: str
    retry_allowed: bool = False

    def __post_init__(self) -> None:
        if type(self.code) is not CasdoorErrorCode or self.code not in (
            CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN,
            CasdoorErrorCode.WORKSPACE_UNAVAILABLE,
            CasdoorErrorCode.AUTHORIZATION_PENDING,
        ):
            raise AuthTransactionError()
        try:
            if type(self.correlation_id) is not str or str(UUID(self.correlation_id)) != self.correlation_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise AuthTransactionError() from None
        if self.retry_allowed is not False:
            raise AuthTransactionError()

    def public_projection(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "code": self.code.value,
            "correlation_id": self.correlation_id,
            "retry_allowed": False,
        }


@dataclass(frozen=True, repr=False, slots=True)
class CreatedRestrictedResult:
    handoff: str
    result_cookie: CookieDirective
    scope_cookie: CookieDirective


@dataclass(frozen=True, repr=False, slots=True)
class ConsumedRestrictedResult:
    payload: RestrictedResultPayload
    clear_cookie: CookieDirective


@dataclass(frozen=True, repr=False)
class CreatedAuthorization:
    state: str
    nonce: str
    code_challenge: str
    cookie: CookieDirective
    scope_cookie: CookieDirective
    mode: AuthMode
    reauth_diagnostic: bool = False

    @property
    def authorization_parameters(self) -> dict[str, str]:
        result = {
            "response_type": "code",
            "scope": "openid profile email",
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
        }
        if self.mode == AuthMode.REAUTH_UNLINK or self.reauth_diagnostic:
            result.update(prompt="login", max_age="0")
        return result


@dataclass(frozen=True, repr=False)
class ConsumedAuthTransaction:
    context: TrustedAuthContext
    nonce: str
    code_verifier: str
    auth_started_at: datetime
    clear_cookie: CookieDirective


class TransactionRedisClient(Protocol):
    """Existing wrapper's eval delegates raw; script keys are explicitly prefixed."""

    def _get_prefix(self) -> str: ...
    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any: ...


class AuthTransactionStore:
    def __init__(
        self,
        redis_client: TransactionRedisClient,
        crypto: CasdoorCrypto,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        key_serializer: Callable[[str, str], str] | None = None,
    ) -> None:
        if key_serializer is None:
            # Production reuses the actual wrapper owner; import lazily so importing
            # this domain module itself never initializes application configuration.
            from extensions.redis_names import serialize_redis_name

            key_serializer = serialize_redis_name
        self._redis = redis_client
        self._crypto = crypto
        self._clock = clock
        self._key_serializer = key_serializer

    def _keys(self, state: str, browser_scope: str) -> tuple[str, str]:
        prefix = self._redis._get_prefix()
        scope_digest = _hash(_opaque(browser_scope))
        return (
            self._key_serializer(f"casdoor:auth:{{{scope_digest}}}:{_hash(_opaque(state))}", prefix),
            self._key_serializer(f"casdoor:auth-browser:{{{scope_digest}}}", prefix),
        )

    def _eval(self, script: str, keys: tuple[str, ...], *args: str) -> Any:
        signal = None
        try:
            from libs.sensitive_redis import sensitive_redis_call

            with sensitive_redis_call():
                result = self._redis.eval(script, len(keys), *keys, *args)
            return result
        except KeyboardInterrupt:
            signal = "interrupt"
        except SystemExit:
            signal = "exit"
        except BaseException:
            signal = "error"
        # Raise outside the handler: `from None` alone retains raw __context__.
        # Lost reply may mean consumed/created: never automatically retry.
        del script, keys, args
        result = None
        if signal == "interrupt":
            raise KeyboardInterrupt("sensitive Redis call interrupted") from None
        if signal == "exit":
            raise SystemExit(1) from None
        raise AuthTransactionError("storage_uncertain") from None

    def _guard(self, guard: Callable[[], CurrentAuthContext]) -> dict[str, object]:
        try:
            snapshot = guard()
            if not isinstance(snapshot, CurrentAuthContext):
                raise AuthTransactionError("context_changed")
            return snapshot.projection()
        except AuthTransactionError:
            raise
        except Exception:
            raise AuthTransactionError("guard_unavailable") from None

    def _initialization_key(self, handle: str, browser_scope: str) -> tuple[str]:
        prefix = self._redis._get_prefix()
        return (self._key_serializer(f"casdoor:auth-init:{{{_hash(browser_scope)}}}:{_hash(handle)}", prefix),)

    @staticmethod
    def _initialization_context(context: TrustedAuthContext, policy: CookiePolicy) -> None:
        if not isinstance(context, TrustedAuthContext) or not isinstance(policy, CookiePolicy):
            raise AuthTransactionError()
        # Revalidate even internal frozen instances before any storage operation.
        context.__post_init__()
        policy.__post_init__()
        if context.mode != AuthMode.LOGIN or any(
            value is not None for value in (context.source, context.identity_id, context.action)
        ):
            raise AuthTransactionError()
        callback, origin = (
            urlsplit(context.registered_redirect_uri),
            urlsplit(policy.backend_origin),
        )
        if (callback.scheme, callback.netloc) != (origin.scheme, origin.netloc):
            raise AuthTransactionError("context_changed")

    @staticmethod
    def _initialization_json(raw: str | bytes) -> dict[str, Any]:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result

        def constant(value: str) -> Any:
            raise ValueError("nonfinite")

        if isinstance(raw, bytes):
            if len(raw) > 16384:
                raise ValueError("size")
            raw = raw.decode("utf-8", errors="strict")
        if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 16384:
            raise ValueError("size")
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if type(result) is not dict or _json(result) != raw:
            raise ValueError("noncanonical")
        return result

    def create_initialization(
        self,
        context: TrustedAuthContext,
        *,
        browser_scope: str,
        policy: CookiePolicy,
        guard: Callable[[], CurrentAuthContext],
    ) -> str:
        """Store ordinary LOGIN navigation for one scope-cookie round-trip.

        This allocates no OAuth transaction, verifier or browser-index slot. A
        failed postguard leaves only an EX60 orphan and returns no handle.
        """
        self._initialization_context(context, policy)
        _opaque(browser_scope)
        expected = _context_projection(context)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        handle = _opaque(secrets.token_urlsafe(32))
        # Ordinary legacy navigation retains its exact v1 shape. Invitation
        # authority travels only inside authenticated encryption, bound to this
        # one initialization handle and domain separated from OAuth records.
        version, data = 1, _json(context.public_projection())
        if context.invite is not None:
            encryption = EncryptionContext(
                EncryptionPurpose.AUTH_VERIFIER,
                context.namespace_id,
                context.revision_id,
                "login-init:" + handle,
            )
            data = _json(
                {
                    "context": context.public_projection(),
                    "encrypted_invite": self._crypto.encrypt(_json({"invite": context.invite}), context=encryption),
                }
            )
            version = 2
        record = _json(
            {
                "schema_version": version,
                "owner": _hash(browser_scope),
                "context": _hash(_json(expected)),
                "data": data,
            }
        )
        if len(record.encode("ascii")) > 16384:
            raise AuthTransactionError()
        result = self._eval(
            CREATE_INITIALIZATION_SCRIPT,
            self._initialization_key(handle, browser_scope),
            record,
        )
        if type(result) is not int or result not in (0, 1):
            raise AuthTransactionError("storage_uncertain")
        if result == 0:
            raise AuthTransactionError("collision")
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return handle

    def consume_initialization(
        self,
        handle: str,
        *,
        browser_scope: str,
        policy: CookiePolicy,
        guard: Callable[[], CurrentAuthContext],
    ) -> TrustedAuthContext:
        """Spend a matching scope+handle once, then validate the full envelope.

        Decode or postguard failures stay spent. This confirms only this pair,
        not browser identity shared by concurrent requests without a cookie.
        """
        _opaque(handle)
        _opaque(browser_scope)
        if not isinstance(policy, CookiePolicy):
            raise AuthTransactionError()
        policy.__post_init__()
        initial = self._guard(guard)
        if initial["mode"] != AuthMode.LOGIN.value or any(
            initial[key] is not None for key in ("source", "identity_id", "action")
        ):
            raise AuthTransactionError("context_changed")
        self._initialization_context(
            TrustedAuthContext(
                UUID(str(initial["namespace_id"])),
                UUID(str(initial["revision_id"])),
                AuthMode.LOGIN,
                str(initial["registered_redirect_uri"]),
            ),
            policy,
        )
        owner, digest = _hash(browser_scope), _hash(_json(initial))
        raw = self._eval(
            CONSUME_INITIALIZATION_SCRIPT,
            self._initialization_key(handle, browser_scope),
            owner,
            digest,
        )
        if raw is None:
            raise AuthTransactionError()
        if not isinstance(raw, str | bytes):
            raise AuthTransactionError("storage_uncertain")
        # Lua returns the full envelope so no outer protocol checks are trusted
        # solely to cjson's permissive parser. No path restores a spent record.
        try:
            record = self._initialization_json(raw)
            if (
                set(record) != {"schema_version", "owner", "context", "data"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] not in (1, 2)
                or record["owner"] != owner
                or record["context"] != digest
                or not isinstance(record["data"], str)
            ):
                raise ValueError("envelope")
            stored = self._initialization_json(record["data"])
            invite = None
            if record["schema_version"] == 2:
                if set(stored) != {"context", "encrypted_invite"} or type(stored["context"]) is not dict:
                    raise ValueError("private_envelope")
                encryption = EncryptionContext(
                    EncryptionPurpose.AUTH_VERIFIER,
                    UUID(str(initial["namespace_id"])),
                    UUID(str(initial["revision_id"])),
                    "login-init:" + handle,
                )
                private = self._initialization_json(
                    self._crypto.decrypt(stored["encrypted_invite"], context=encryption)
                )
                if set(private) != {"invite"} or type(private["invite"]) is not str:
                    raise ValueError("invitation")
                invite, stored = private["invite"], stored["context"]
            context = TrustedAuthContext(
                UUID(stored["namespace_id"]),
                UUID(stored["revision_id"]),
                AuthMode(stored["mode"]),
                stored["registered_redirect_uri"],
                stored["return_path"],
                locale=stored["locale"],
                timezone=stored["timezone"],
                invite=invite,
            )
            self._initialization_context(context, policy)
            if stored != context.public_projection() or _context_projection(context) != initial:
                raise ValueError("projection")
        except Exception:
            raise AuthTransactionError("record_invalid") from None
        if self._guard(guard) != initial:
            raise AuthTransactionError("context_changed")
        return context

    def _restricted_result_key(self, handoff: str, browser_scope: str) -> tuple[str]:
        prefix = self._redis._get_prefix()
        logical = f"casdoor:restricted-result:{{{_hash(browser_scope)}}}:{_hash(handoff)}"
        return (self._key_serializer(logical, prefix),)

    @staticmethod
    def _restricted_result_context(binding: RestrictedResultBinding, policy: CookiePolicy) -> dict[str, object]:
        if type(binding) is not RestrictedResultBinding or type(policy) is not CookiePolicy:
            raise AuthTransactionError()
        binding.__post_init__()
        policy.__post_init__()
        for origin in (binding.backend_origin, binding.web_origin):
            CookiePolicy(origin, allow_loopback_http=policy.allow_loopback_http)
        if binding.backend_origin != policy.backend_origin:
            raise AuthTransactionError("context_changed")
        return CurrentAuthContext(
            binding.namespace_id,
            binding.revision_id,
            AuthMode.LOGIN,
            binding.backend_origin.rstrip("/") + COOKIE_PATH + "/callback",
            None,
            allowed=True,
        ).projection()

    def _restricted_result_now(self) -> int:
        try:
            now = int(_utc(self._clock()).timestamp())
            if not 0 <= now <= 253402300799:
                raise ValueError()
            return now
        except Exception:
            raise AuthTransactionError("clock_invalid") from None

    def create_restricted_result(
        self,
        *,
        browser_scope: str,
        context_binding: RestrictedResultBinding,
        payload: RestrictedResultPayload,
        policy: CookiePolicy,
        guard: Callable[[], CurrentAuthContext],
    ) -> CreatedRestrictedResult:
        """Create one display result after the service's same-call callback guard.

        This store cannot establish callback admission. No verifier, authorization
        slot or session is created. A failed postguard leaves an EX300 orphan and
        returns no handle or Cookie directives; uncertain storage is never retried.
        """
        _opaque(browser_scope)
        expected = self._restricted_result_context(context_binding, policy)
        if type(payload) is not RestrictedResultPayload:
            raise AuthTransactionError()
        data = _json(payload.public_projection())
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        issued_at = self._restricted_result_now()
        if issued_at > 253402300799 - TTL_SECONDS:
            raise AuthTransactionError("clock_invalid")
        handoff, nonce = (_opaque(secrets.token_urlsafe(32)) for _ in range(2))
        record = _json(
            {
                "schema_version": 1,
                "owner": _hash(nonce),
                "scope": _hash(browser_scope),
                "context": _hash(_json(context_binding.projection())),
                "issued_at": issued_at,
                "expires_at": issued_at + TTL_SECONDS,
                "data": data,
            }
        )
        if len(record.encode("ascii")) > 16384:
            raise AuthTransactionError()
        result = self._eval(
            CREATE_RESTRICTED_RESULT_SCRIPT,
            self._restricted_result_key(handoff, browser_scope),
            record,
        )
        if type(result) is not int or result not in (0, 1):
            raise AuthTransactionError("storage_uncertain")
        if result == 0:
            raise AuthTransactionError("collision")
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        if not issued_at <= self._restricted_result_now() < issued_at + TTL_SECONDS:
            raise AuthTransactionError("clock_invalid")
        return CreatedRestrictedResult(
            handoff,
            CookieDirective(
                result_cookie_name(handoff),
                nonce,
                TTL_SECONDS,
                COOKIE_PATH + "/result",
                policy.secure,
            ),
            CookieDirective(
                SCOPE_COOKIE_NAME,
                browser_scope,
                TTL_SECONDS,
                COOKIE_PATH,
                policy.secure,
            ),
        )

    def consume_restricted_result(
        self,
        handoff: str,
        *,
        browser_scope: str,
        result_cookie: str,
        context_binding: RestrictedResultBinding,
        policy: CookiePolicy,
        guard: Callable[[], CurrentAuthContext],
    ) -> ConsumedRestrictedResult:
        """Atomically spend a browser/nonce/current-context match, then validate.

        Python canonical-envelope, clock and postguard rejection remains spent.
        Return only a closed display payload and this result's clear directive.
        """
        _opaque(handoff)
        _opaque(browser_scope)
        _opaque(result_cookie)
        expected = self._restricted_result_context(context_binding, policy)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        owner, scope, context = (
            _hash(result_cookie),
            _hash(browser_scope),
            _hash(_json(context_binding.projection())),
        )
        raw = self._eval(
            CONSUME_RESTRICTED_RESULT_SCRIPT,
            self._restricted_result_key(handoff, browser_scope),
            owner,
            scope,
            context,
            str(self._restricted_result_now()),
        )
        if raw is None:
            raise AuthTransactionError()
        if not isinstance(raw, str | bytes):
            raise AuthTransactionError("storage_uncertain")
        try:
            record = self._initialization_json(raw)
            if (
                set(record)
                != {
                    "schema_version",
                    "owner",
                    "scope",
                    "context",
                    "issued_at",
                    "expires_at",
                    "data",
                }
                or type(record["schema_version"]) is not int
                or record["schema_version"] != 1
                or record["owner"] != owner
                or record["scope"] != scope
                or record["context"] != context
                or type(record["issued_at"]) is not int
                or type(record["expires_at"]) is not int
                or not 0 <= record["issued_at"] < record["expires_at"] <= 253402300799
                or record["expires_at"] != record["issued_at"] + TTL_SECONDS
                or not record["issued_at"] <= self._restricted_result_now() < record["expires_at"]
                or type(record["data"]) is not str
            ):
                raise ValueError("envelope")
            stored = self._initialization_json(record["data"])
            if set(stored) != {"code", "correlation_id", "retry_allowed"} or type(stored["code"]) is not str:
                raise ValueError("payload")
            payload = RestrictedResultPayload(
                CasdoorErrorCode(stored["code"]),
                stored["correlation_id"],
                stored["retry_allowed"],
            )
            if stored != payload.public_projection():
                raise ValueError("payload")
        except Exception:
            raise AuthTransactionError("record_invalid") from None
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return ConsumedRestrictedResult(
            payload,
            CookieDirective(
                result_cookie_name(handoff),
                "",
                0,
                COOKIE_PATH + "/result",
                policy.secure,
            ),
        )

    def create(
        self,
        context: TrustedAuthContext,
        *,
        browser_scope: str,
        policy: CookiePolicy,
        guard: Callable[[], CurrentAuthContext],
    ) -> CreatedAuthorization:
        if not isinstance(context, TrustedAuthContext) or not isinstance(policy, CookiePolicy):
            raise AuthTransactionError()
        _opaque(browser_scope)
        callback = urlsplit(context.registered_redirect_uri)
        origin = urlsplit(policy.backend_origin)
        if (callback.scheme, callback.netloc) != (origin.scheme, origin.netloc):
            raise AuthTransactionError()
        expected = _context_projection(context)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        started = _utc(self._clock())
        record_id = uuid4()
        state, cookie, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        encryption = EncryptionContext(
            EncryptionPurpose.AUTH_VERIFIER, context.namespace_id, context.revision_id, str(record_id)
        )
        # Invite may be a capability token: keep it encrypted alongside verifier.
        envelope = self._crypto.encrypt(_json({"verifier": verifier, "invite": context.invite}), context=encryption)
        data = _json(
            {
                "record_id": str(record_id),
                "context": context.public_projection(),
                "nonce": nonce,
                "encrypted_verifier": envelope,
                "auth_started_at": started.isoformat(),
            }
        )
        record = _json({"owner": _hash(cookie), "context": _hash(_json(expected)), "data": data})
        result = self._eval(CREATE_SCRIPT, self._keys(state, browser_scope), record, _hash(state))
        if result != 1:
            raise AuthTransactionError("limit" if result == 0 else "collision")
        if self._guard(guard) != expected:
            # Kept short-lived and unusable; no directives/authorization returned.
            raise AuthTransactionError("context_changed")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
        return CreatedAuthorization(
            state,
            nonce,
            challenge.decode("ascii"),
            CookieDirective(
                transaction_cookie_name(state), cookie, TTL_SECONDS, COOKIE_PATH + "/callback", policy.secure
            ),
            CookieDirective(SCOPE_COOKIE_NAME, browser_scope, TTL_SECONDS, COOKIE_PATH, policy.secure),
            context.mode,
            context.reauth_diagnostic,
        )

    @staticmethod
    def _diagnostic_context(stored: dict[str, Any]) -> TrustedAuthContext:
        """Decode a navigation hint only; callers still reconstruct authority."""
        try:
            source = stored["source"]
            account = UUID(source["account_id"])
            context = TrustedAuthContext(
                UUID(stored["namespace_id"]),
                UUID(stored["revision_id"]),
                AuthMode.DIAGNOSTIC,
                stored["registered_redirect_uri"],
                stored["return_path"],
                source=SourceSessionContext(
                    account, account, account, source["refresh_digest"], source["management_authorized"]
                ),
                diagnostic_binding=stored["diagnostic_binding"],
                rp_logout_diagnostic=stored.get("rp_logout_diagnostic", False),
            )
            if stored != context.public_projection() or context.diagnostic_binding is None:
                raise ValueError()
            return context
        except Exception:
            raise AuthTransactionError("record_invalid") from None

    def diagnostic_hint(self, state: str, *, browser_scope: str, transaction_cookie: str) -> TrustedAuthContext | None:
        """Browser-bound routing hint, never authorization or consumption.

        An ordinary LOGIN record returns None. No code, verifier or provider
        token is decrypted or returned, and the actual consume keeps all guards.
        """
        _opaque(state)
        _opaque(browser_scope)
        _opaque(transaction_cookie)
        raw = self._eval("return redis.call('GET', KEYS[1])", (self._keys(state, browser_scope)[0],))
        if raw is None:
            return None
        record = self._initialization_json(raw)
        if record.get("owner") != _hash(transaction_cookie):
            raise AuthTransactionError()
        data = self._initialization_json(record["data"])
        stored = data.get("context")
        if not isinstance(stored, dict):
            raise AuthTransactionError()
        if stored.get("mode") != AuthMode.DIAGNOSTIC.value:
            return None
        return self._diagnostic_context(stored)

    def create_diagnostic_initialization(
        self, context: TrustedAuthContext, *, browser_scope: str, guard: Callable[[], CurrentAuthContext]
    ) -> str:
        self._diagnostic_context(context.public_projection())
        _opaque(browser_scope)
        expected = _context_projection(context)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        handle = _opaque(secrets.token_urlsafe(32))
        record = _json(
            {
                "schema_version": 1,
                "owner": _hash(browser_scope),
                "context": _hash(_json(expected)),
                "data": _json(context.public_projection()),
            }
        )
        if self._eval(CREATE_INITIALIZATION_SCRIPT, self._initialization_key(handle, browser_scope), record) != 1:
            raise AuthTransactionError("collision")
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return handle

    def diagnostic_initialization_hint(self, handle: str, *, browser_scope: str) -> TrustedAuthContext:
        _opaque(handle)
        _opaque(browser_scope)
        raw = self._eval("return redis.call('GET', KEYS[1])", self._initialization_key(handle, browser_scope))
        record = self._initialization_json(raw)
        if record.get("owner") != _hash(browser_scope):
            raise AuthTransactionError()
        return self._diagnostic_context(self._initialization_json(record["data"]))

    def consume_diagnostic_initialization(
        self, handle: str, *, browser_scope: str, guard: Callable[[], CurrentAuthContext]
    ) -> TrustedAuthContext:
        context = self.diagnostic_initialization_hint(handle, browser_scope=browser_scope)
        expected = _context_projection(context)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        raw = self._eval(
            CONSUME_INITIALIZATION_SCRIPT,
            self._initialization_key(handle, browser_scope),
            _hash(browser_scope),
            _hash(_json(expected)),
        )
        record = self._initialization_json(raw)
        consumed = self._diagnostic_context(self._initialization_json(record["data"]))
        if consumed != context or self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return consumed

    @staticmethod
    def _source_action_context(stored: dict[str, Any]) -> TrustedAuthContext:
        """Navigation-only record reconstruction; no refresh/session authority."""
        try:
            source = stored["source"]
            account = UUID(source["account_id"])
            context = TrustedAuthContext(
                UUID(stored["namespace_id"]),
                UUID(stored["revision_id"]),
                AuthMode(stored["mode"]),
                stored["registered_redirect_uri"],
                stored["return_path"],
                source=SourceSessionContext(
                    account, account, account, source["refresh_digest"], source["management_authorized"]
                ),
                identity_id=UUID(stored["identity_id"]) if stored["identity_id"] else None,
                action=stored["action"],
                diagnostic_binding=stored.get("diagnostic_binding"),
                action_binding=stored.get("action_binding"),
                reauth_diagnostic=stored.get("reauth_diagnostic", False),
            )
            if (
                stored != context.public_projection()
                or context.action_binding is None
                or not (context.mode in (AuthMode.LINK, AuthMode.REAUTH_UNLINK) or context.reauth_diagnostic)
            ):
                raise ValueError()
            return context
        except Exception:
            raise AuthTransactionError("record_invalid") from None

    def source_action_hint(self, state: str, *, browser_scope: str, transaction_cookie: str) -> TrustedAuthContext:
        _opaque(state)
        _opaque(browser_scope)
        _opaque(transaction_cookie)
        raw = self._eval("return redis.call('GET', KEYS[1])", (self._keys(state, browser_scope)[0],))
        record = self._initialization_json(raw)
        if record.get("owner") != _hash(transaction_cookie):
            raise AuthTransactionError()
        data = self._initialization_json(record["data"])
        return self._source_action_context(data["context"])

    def create_source_initialization(self, context, *, browser_scope, guard):
        self._source_action_context(context.public_projection())
        _opaque(browser_scope)
        expected = _context_projection(context)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        handle = _opaque(secrets.token_urlsafe(32))
        record = _json(
            {
                "schema_version": 1,
                "owner": _hash(browser_scope),
                "context": _hash(_json(expected)),
                "data": _json(context.public_projection()),
            }
        )
        if self._eval(CREATE_INITIALIZATION_SCRIPT, self._initialization_key(handle, browser_scope), record) != 1:
            raise AuthTransactionError("collision")
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return handle

    def source_initialization_hint(self, handle, *, browser_scope):
        _opaque(handle)
        _opaque(browser_scope)
        record = self._initialization_json(
            self._eval("return redis.call('GET', KEYS[1])", self._initialization_key(handle, browser_scope))
        )
        if record.get("owner") != _hash(browser_scope):
            raise AuthTransactionError()
        return self._source_action_context(self._initialization_json(record["data"]))

    def consume_source_initialization(self, handle, *, browser_scope, guard):
        context = self.source_initialization_hint(handle, browser_scope=browser_scope)
        expected = _context_projection(context)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        record = self._initialization_json(
            self._eval(
                CONSUME_INITIALIZATION_SCRIPT,
                self._initialization_key(handle, browser_scope),
                _hash(browser_scope),
                _hash(_json(expected)),
            )
        )
        consumed = self._source_action_context(self._initialization_json(record["data"]))
        if consumed != context or self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return consumed

    def _unlink_proof_key(self, handle, browser_scope):
        _opaque(handle)
        _opaque(browser_scope)
        return (
            self._key_serializer(
                f"casdoor:unlink-proof:{{{_hash(browser_scope)}}}:{_hash(handle)}", self._redis._get_prefix()
            ),
        )

    def create_unlink_proof(self, context, *, browser_scope, auth_time, guard):
        """Only the actual signed reauthentication caller may produce this proof.

        Separate keyspace prevents initialization handles authorizing an unlink.
        The source session and exact identity are always freshly checked again.
        """
        if context.mode != AuthMode.REAUTH_UNLINK:
            raise AuthTransactionError()
        self._source_action_context(context.public_projection())
        expected = _context_projection(context)
        now = _utc(self._clock()).timestamp()
        if type(auth_time) not in (int, float) or not now - 300 <= auth_time <= now:
            raise AuthTransactionError()
        handle = _opaque(secrets.token_urlsafe(32))
        record = _json(
            {
                "schema_version": 1,
                "owner": _hash(browser_scope),
                "context": _hash(_json(expected)),
                "data": _json({"context": context.public_projection(), "auth_time": auth_time}),
            }
        )
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        if self._eval(CREATE_INITIALIZATION_SCRIPT, self._unlink_proof_key(handle, browser_scope), record) != 1:
            raise AuthTransactionError("collision")
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return handle

    def unlink_proof_hint(self, handle, *, browser_scope):
        record = self._initialization_json(
            self._eval("return redis.call('GET', KEYS[1])", self._unlink_proof_key(handle, browser_scope))
        )
        if record.get("owner") != _hash(browser_scope):
            raise AuthTransactionError()
        data = self._initialization_json(record["data"])
        if set(data) != {"context", "auth_time"} or type(data["auth_time"]) not in (int, float):
            raise AuthTransactionError()
        now = _utc(self._clock()).timestamp()
        if not now - 300 <= data["auth_time"] <= now:
            raise AuthTransactionError()
        context = self._source_action_context(data["context"])
        if context.mode != AuthMode.REAUTH_UNLINK:
            raise AuthTransactionError()
        return context

    def consume_unlink_proof(self, handle, *, browser_scope, guard):
        context = self.unlink_proof_hint(handle, browser_scope=browser_scope)
        expected = _context_projection(context)
        if self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        record = self._initialization_json(
            self._eval(
                CONSUME_INITIALIZATION_SCRIPT,
                self._unlink_proof_key(handle, browser_scope),
                _hash(browser_scope),
                _hash(_json(expected)),
            )
        )
        data = self._initialization_json(record["data"])
        if self._source_action_context(data["context"]) != context or self._guard(guard) != expected:
            raise AuthTransactionError("context_changed")
        return context

    def consume(
        self,
        state: str,
        *,
        transaction_cookie: str,
        browser_scope: str,
        policy: CookiePolicy,
        guard: Callable[[], CurrentAuthContext],
    ) -> ConsumedAuthTransaction:
        _opaque(state)
        _opaque(transaction_cookie)
        _opaque(browser_scope)
        if not isinstance(policy, CookiePolicy):
            raise AuthTransactionError()
        initial = self._guard(guard)
        callback = urlsplit(str(initial["registered_redirect_uri"]))
        origin = urlsplit(policy.backend_origin)
        if (callback.scheme, callback.netloc) != (origin.scheme, origin.netloc):
            raise AuthTransactionError("context_changed")
        raw = self._eval(
            CONSUME_SCRIPT,
            self._keys(state, browser_scope),
            _hash(transaction_cookie),
            _hash(_json(initial)),
            _hash(state),
        )
        if not isinstance(raw, str | bytes) or not raw or len(raw) > 16384:
            raise AuthTransactionError()
        # The record is permanently spent before decryption, late guards or exchange.
        try:
            data = json.loads(raw)
            stored = data["context"]
            source = stored["source"]
            source_context = (
                SourceSessionContext(
                    UUID(source["account_id"]),
                    UUID(source["account_id"]),
                    UUID(source["account_id"]),
                    source["refresh_digest"],
                    source["management_authorized"],
                )
                if source
                else None
            )
            encryption = EncryptionContext(
                EncryptionPurpose.AUTH_VERIFIER,
                UUID(stored["namespace_id"]),
                UUID(stored["revision_id"]),
                str(UUID(data["record_id"])),
            )
            private = json.loads(self._crypto.decrypt(data["encrypted_verifier"], context=encryption))
            context = TrustedAuthContext(
                UUID(stored["namespace_id"]),
                UUID(stored["revision_id"]),
                AuthMode(stored["mode"]),
                stored["registered_redirect_uri"],
                stored["return_path"],
                private["invite"],
                stored["locale"],
                stored["timezone"],
                source_context,
                UUID(stored["identity_id"]) if stored["identity_id"] else None,
                stored["action"],
                stored.get("diagnostic_binding"),
                stored.get("action_binding"),
                stored.get("reauth_diagnostic", False),
                stored.get("rp_logout_diagnostic", False),
            )
            started = _utc(datetime.fromisoformat(data["auth_started_at"]))
            now = _utc(self._clock())
            if not started <= now < started + timedelta(seconds=TTL_SECONDS):
                raise AuthTransactionError()
            nonce, verifier = _opaque(data["nonce"]), _opaque(private["verifier"])
            if self._guard(guard) != initial or _context_projection(context) != initial:
                raise AuthTransactionError("context_changed")
            callback, origin = urlsplit(context.registered_redirect_uri), urlsplit(policy.backend_origin)
            if (callback.scheme, callback.netloc) != (origin.scheme, origin.netloc):
                raise AuthTransactionError("context_changed")
        except AuthTransactionError:
            raise
        except Exception:
            raise AuthTransactionError("record_invalid") from None
        return ConsumedAuthTransaction(
            context,
            nonce,
            verifier,
            started,
            CookieDirective(transaction_cookie_name(state), "", 0, COOKIE_PATH + "/callback", policy.secure),
        )
