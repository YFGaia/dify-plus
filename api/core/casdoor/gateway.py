"""Controlled, synchronous Casdoor HTTP boundary; never a login/claims/graph owner.

One GatewayOperation is owned by one callback and shared by both facades. Its
trusted redirect comes from the registered Console callback, never request data.
Directory subjects come only from the later ID-token validation owner. Raw data
is transient internal input for that owner/graph validation, not public DTOs.
Deployment proofs are server-owned evidence references, not activation decisions.
"""

import base64
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, override
from urllib.parse import quote, quote_plus, urlsplit

from casdoor import CasdoorSDK

from core.casdoor.auth_transactions import CookiePolicy
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.errors import CasdoorErrorCode
from core.helper import ssrf_proxy

JSON_LIMIT = 256 * 1024
ROLES_LIMIT = 1024 * 1024
_TOKEN_PATH = "/api/login/oauth/access_token"
_AUTHORIZE_PATH = "/login/oauth/authorize"
_USERINFO_PATH = "/api/userinfo"
_JWKS_PATH = "/.well-known/jwks"


class GatewayError(Exception):
    """Only stable codes/reasons, never provider exception text or payloads."""

    def __init__(self, code: CasdoorErrorCode, reason: str, *, termination_confirmed: bool = True):
        self.code = code
        self.reason = reason
        self.retry_allowed = False
        self.termination_confirmed = termination_confirmed
        super().__init__(f"casdoor_gateway_{reason}")


class _SigningDiscoveryUnsupported(Exception):
    """Only an absent application discovery route permits global fallback."""


def _text(value: object, limit: int = 255) -> bool:
    try:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value.encode("utf-8")) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except UnicodeError:
        return False


def _directory_metadata_text(value: object) -> bool:
    """Bounded Casdoor envelope labels, including the provider's empty defaults."""
    try:
        return (
            isinstance(value, str)
            and len(value.encode("utf-8")) <= 255
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except UnicodeError:
        return False


def _url(value: str, *, root: bool = False, allow_loopback_http: bool = False) -> str:
    try:
        p = urlsplit(value)
        port = p.port
        if (
            len(value) > 2048
            or (p.scheme != "https" and not (p.scheme == "http" and allow_loopback_http))
            or not p.hostname
            or p.username is not None
            or p.password is not None
            or p.query
            or "?" in value
            or "#" in value
            or "\\" in value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or port == 0
            or (root and p.path not in ("", "/"))
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise GatewayError(CasdoorErrorCode.CONFIG_CONFLICT, "url_policy") from None
    if p.scheme == "http":
        try:
            CookiePolicy(f"{p.scheme}://{p.netloc}", allow_loopback_http=allow_loopback_http)
        except Exception:
            raise GatewayError(CasdoorErrorCode.CONFIG_CONFLICT, "url_policy") from None
    return value.rstrip("/") if root else value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_constant(value: str) -> Any:
    raise ValueError


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


@dataclass(frozen=True)
class ValidatedEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str | None
    jwks_uri: str | None


@dataclass(frozen=True, repr=False)
class RawTokens:
    """Unverified tokens; 3.2 must verify ID Token before using any identity."""

    payload: dict[str, Any]

    def __repr__(self) -> str:
        return "RawTokens(<redacted>)"


@dataclass(frozen=True)
class DirectoryDeploymentProof:
    """Evidence linkage only; three GETs cannot establish non-DCR creation."""

    expected_issuer: str
    organization: str
    application: str
    client_id: str
    release_fingerprint: str
    creator_proof_fingerprint: str
    credential_proof_fingerprint: str

    def matches(self, config: CasdoorConfiguration) -> bool:
        return (self.expected_issuer, self.organization, self.application, self.client_id) == (
            config.expected_issuer,
            config.organization,
            config.application,
            config.client_id,
        ) and all(
            re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            for fingerprint in (
                self.release_fingerprint,
                self.creator_proof_fingerprint,
                self.credential_proof_fingerprint,
            )
        )


class DirectoryCredentialStrategy(Protocol):
    """Server-owned credential encoding; never an admin-selected auth mode.

    Query credentials are unsupported. UserInfo Bearer is not a directory
    credential and must never be supplied through this interface.
    """

    def authorization(self, client_id: str, client_secret: str) -> str: ...


@dataclass(frozen=True, repr=False)
class CasdoorBasicDirectoryCredentialStrategy:
    """Built-in Casdoor organization-directory credential profile.

    This is executable server policy, not evidence of deployment provenance.
    The real diagnostic and login calls must still validate the returned user,
    organization, complete role graph and authorization mapping.
    """

    client_id: str

    def authorization(self, client_id: str, client_secret: str) -> str:
        if type(self.client_id) is not str or client_id != self.client_id or ":" in client_id:
            raise ValueError("invalid Casdoor client id")
        if (
            type(client_secret) is not str
            or not 1 <= len(client_secret) <= 16 * 1024
            or not client_secret.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in client_secret)
        ):
            raise ValueError("invalid Casdoor client secret")
        return "Basic " + base64.b64encode((client_id + ":" + client_secret).encode("ascii")).decode("ascii")

    def __repr__(self) -> str:
        return "CasdoorBasicDirectoryCredentialStrategy(<redacted>)"


@dataclass(repr=False)
class GatewayOperation:
    """One callback budget and fail-closed latch. Never reuse for another callback.

    Loopback HTTP permission is trusted server input for the callback only;
    provider URLs always require HTTPS. The caller owns development authority.
    """

    config: CasdoorConfiguration
    client_secret: str = field(repr=False)
    registered_redirect_uri: str
    deadline: float = field(default_factory=lambda: time.monotonic() + 45.0)
    allow_loopback_http: bool = field(default=False, kw_only=True)
    _halted: bool = field(default=False, init=False, repr=False)
    _exchange_started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.allow_loopback_http) is not bool:
            self.fail("url_policy", CasdoorErrorCode.CONFIG_CONFLICT)
        self._backend = _url(self.config.backend_api_url, root=True)
        self._frontend = _url(self.config.browser_frontend_url, root=True)
        _url(self.config.expected_issuer)
        _url(self.registered_redirect_uri, allow_loopback_http=self.allow_loopback_http)
        if not _text(self.client_secret, 16 * 1024):
            self.fail("credential_invalid", CasdoorErrorCode.CONFIG_CONFLICT)
        now = time.monotonic()
        if (
            type(self.deadline) not in (float, int)
            or not math.isfinite(self.deadline)
            or self.deadline <= now
            or self.deadline > now + 45.0
        ):
            self.fail("deadline", CasdoorErrorCode.PROVIDER_UNAVAILABLE)

    def fail(
        self,
        reason: str,
        code: CasdoorErrorCode = CasdoorErrorCode.PROVIDER_UNAVAILABLE,
        *,
        termination_confirmed: bool = True,
    ) -> Any:
        self._halted = True
        raise GatewayError(code, reason, termination_confirmed=termination_confirmed) from None

    def _check(self) -> None:
        if self._halted:
            self.fail("operation_stopped")
        if time.monotonic() >= self.deadline:
            self.fail("deadline")

    def remaining_seconds(self) -> float:
        self._check()
        return self.deadline - time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        limit: int = JSON_LIMIT,
        directory: bool = False,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        authorization: str | None = None,
        signing_keys: bool = False,
        missing_route: bool = False,
    ) -> dict[str, Any]:
        self._check()
        code = CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN if directory else CasdoorErrorCode.PROVIDER_UNAVAILABLE
        # Private methods are still guarded: no unknown route/query/auth path.
        allowed = {
            "/.well-known/openid-configuration": ("GET", set()),
            _TOKEN_PATH: ("POST", set()),
            _USERINFO_PATH: ("GET", set()),
            "/api/get-user": ("GET", {"owner", "userId"}),
            "/api/get-roles": ("GET", {"owner"}),
            "/api/get-organization": ("GET", {"id"}),
        }
        if signing_keys:
            app = quote(self.config.application, safe="")
            allowed.update(
                {
                    f"/.well-known/{app}/openid-configuration": ("GET", set()),
                    f"/.well-known/{app}/jwks": ("GET", set()),
                    _JWKS_PATH: ("GET", set()),
                }
            )
            if authorization is not None or data is not None or params:
                self.fail("request_policy", code)
        if path not in allowed or (method, set(params or {})) != allowed[path]:
            self.fail("request_policy", code)
        if params and (
            ("owner" in params and params["owner"] != self.config.organization)
            or ("id" in params and params["id"] != f"admin/{self.config.organization}")
        ):
            self.fail("request_policy", code)
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if authorization is not None:
            if not _text(authorization, 32 * 1024) or not authorization.isascii():
                self.fail("credential_invalid", code)
            headers["Authorization"] = authorization
        try:
            response = ssrf_proxy.make_request_with_deadline(
                method,
                self._backend + path,
                deadline=self.deadline,
                max_response_bytes=limit,
                request_timeout=15.0,
                max_retries=0,
                follow_redirects=False,
                ssl_verify=True,
                headers=headers,
                params=params,
                data=data,
            )
        except ssrf_proxy.RequestTerminationUnconfirmedError:
            self.fail("termination_unconfirmed", code, termination_confirmed=False)
        except ssrf_proxy.RequestDeadlineExceededError:
            self.fail("deadline", code)
        except ssrf_proxy.ResponseLimitError:
            self.fail("response_bounds", code)
        except Exception:
            if signing_keys:
                # A confirmed transient GET failure may use a bounded trusted
                # cache. Do not latch the rest of this authentication operation.
                raise GatewayError(code, "signing_keys_transport") from None
            self.fail("transport", code)
        self._check()
        if missing_route and signing_keys and response.status_code in (404, 405):
            raise _SigningDiscoveryUnsupported()
        if response.status_code != 200:
            if signing_keys and 500 <= response.status_code <= 599:
                raise GatewayError(code, "signing_keys_temporary")
            self.fail("http_status", code)
        if response.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
            self.fail("response_encoding", code)
        content_type = response.headers.get("content-type", "").strip().lower()
        if not re.fullmatch(r'application/json(?:\s*;\s*charset\s*=\s*"?utf-8"?)?', content_type):
            self.fail("response_media_type", code)
        body = response.content
        if len(body) > limit:
            self.fail("response_bounds", code)
        try:
            raw = json.loads(
                body.decode("utf-8", errors="strict"),
                object_pairs_hook=_pairs,
                parse_constant=_bad_constant,
                parse_float=_finite_float,
            )
        except (UnicodeError, ValueError, RecursionError):
            self.fail("response_json", code)
        if not isinstance(raw, dict) or "error" in raw:
            self.fail("response_schema", code)
        self._check()
        return raw

    def _discover_signing_metadata(self, profile: str | None = None) -> tuple[str, dict[str, Any], str]:
        """Select only fixed application/global metadata and public key routes.

        A persisted profile never falls back. No credentials accompany these
        requests; Token headers and discovery-provided arbitrary URLs are ignored.
        """
        if profile not in (None, "application", "global"):
            self.fail("discovery_profile", CasdoorErrorCode.CONFIG_CONFLICT)
        app = quote(self.config.application, safe="")
        selected = profile or "application"
        path = (
            f"/.well-known/{app}/openid-configuration"
            if selected == "application"
            else "/.well-known/openid-configuration"
        )
        try:
            raw = self._request("GET", path, signing_keys=True, missing_route=profile is None)
        except _SigningDiscoveryUnsupported:
            selected = "global"
            raw = self._request("GET", "/.well-known/openid-configuration", signing_keys=True)
        if raw.get("issuer") != self.config.expected_issuer:
            self.fail("discovery_issuer")
        expected = {
            "authorization_endpoint": self._frontend + _AUTHORIZE_PATH,
            "token_endpoint": self._backend + _TOKEN_PATH,
            "userinfo_endpoint": self._backend + _USERINFO_PATH,
        }
        if any(raw.get(key) != expected[key] for key in ("authorization_endpoint", "token_endpoint")) or (
            "userinfo_endpoint" in raw and raw["userinfo_endpoint"] != expected["userinfo_endpoint"]
        ):
            self.fail("discovery_endpoint")
        key_path = f"/.well-known/{app}/jwks" if selected == "application" else _JWKS_PATH
        source = self._backend + key_path
        if raw.get("jwks_uri") != source:
            self.fail("discovery_endpoint")
        return source, raw, selected

    def discover_signing_keys(self, profile: str | None = None) -> tuple[str, dict[str, Any], str]:
        source, _, selected = self._discover_signing_metadata(profile)
        keys = self._request("GET", source[len(self._backend) :], signing_keys=True)
        return source, keys, selected

    def discover(self) -> ValidatedEndpoints:
        """Metadata is advisory, never a source of arbitrary dispatch URLs."""
        if self.config.schema_version == 2:
            source, raw, _ = self._discover_signing_metadata()
            return ValidatedEndpoints(
                raw["authorization_endpoint"], raw["token_endpoint"], raw.get("userinfo_endpoint"), source
            )
        raw = self._request("GET", "/.well-known/openid-configuration")
        if raw.get("issuer") != self.config.expected_issuer:
            self.fail("discovery_issuer")
        expected = {
            "authorization_endpoint": self._frontend + _AUTHORIZE_PATH,
            "token_endpoint": self._backend + _TOKEN_PATH,
            "userinfo_endpoint": self._backend + _USERINFO_PATH,
            "jwks_uri": self._backend + _JWKS_PATH,
        }
        for key, url in expected.items():
            if key in raw and raw[key] != url:
                self.fail("discovery_endpoint")
        if any(key not in raw for key in ("authorization_endpoint", "token_endpoint")):
            self.fail("discovery_schema")
        return ValidatedEndpoints(
            expected["authorization_endpoint"],
            expected["token_endpoint"],
            raw.get("userinfo_endpoint"),
            raw.get("jwks_uri"),
        )

    def userinfo(self, access_token: str) -> dict[str, Any]:
        """Profile-only raw input; later claims owner must compare verified sub."""
        if not _text(access_token, JSON_LIMIT):
            self.fail("token_invalid")
        raw = self._request("GET", _USERINFO_PATH, authorization="Bearer " + access_token)
        if not _text(raw.get("sub")):
            self.fail("userinfo_schema")
        return raw


class _ControlledSDK(CasdoorSDK):
    """Private instance seam. Facade does not expose SDK network convenience APIs."""

    def __init__(self, operation: GatewayOperation):
        self._operation = operation
        super().__init__(
            endpoint=operation._backend,
            client_id=operation.config.client_id,
            client_secret=operation.client_secret,
            certificate="",
            org_name=operation.config.organization,
            application_name=operation.config.application,
            front_endpoint=operation._frontend,
        )

    @override
    def _oauth_token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        op = self._operation
        if (
            set(payload) != {"grant_type", "code", "redirect_uri", "code_verifier"}
            or payload["grant_type"] != "authorization_code"
            or payload["redirect_uri"] != op.registered_redirect_uri
            or not _text(payload["code"], 4096)
            or not isinstance(payload["code_verifier"], str)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", payload["code_verifier"])
        ):
            op.fail("exchange_input")
        credentials = f"{quote_plus(self.client_id)}:{quote_plus(self.client_secret)}".encode("ascii")
        authorization = "Basic " + base64.b64encode(credentials).decode("ascii")
        return op._request("POST", _TOKEN_PATH, data=payload, authorization=authorization)


class CasdoorTokenGateway:
    """Only authorization-code exchange, at most once per callback operation."""

    def __init__(self, operation: GatewayOperation):
        self._operation = operation
        self._sdk = _ControlledSDK(operation)

    def exchange_code(self, code: str, code_verifier: str) -> RawTokens:
        op = self._operation
        op._check()
        if op._exchange_started:
            op.fail("exchange_already_started")
        if (
            not _text(code, 4096)
            or not isinstance(code_verifier, str)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier)
        ):
            op.fail("exchange_input")
        op._exchange_started = True
        raw = self._sdk._oauth_token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": op.registered_redirect_uri,
                "code_verifier": code_verifier,
            }
        )
        if not _text(raw.get("id_token"), JSON_LIMIT) or not _text(raw.get("access_token"), JSON_LIMIT):
            op.fail("token_schema")
        if not isinstance(raw.get("token_type"), str) or raw["token_type"].lower() != "bearer":
            op.fail("token_schema")
        return RawTokens(raw)


class CasdoorDirectoryGateway:
    """Fixed reads bound to configured org and the claims owner's verified sub.

    This facade checks envelope integrity and online user fields only; it never
    declares graph/visibility complete or interprets missing relation defaults.
    Casdoor's optional sub/name envelope labels are metadata, never identity or
    authorization inputs. Additional data2/data3 payloads must remain absent/null.
    """

    def __init__(
        self,
        operation: GatewayOperation,
        *,
        verified_subject: str,
        credential_strategy: DirectoryCredentialStrategy | None = None,
    ):
        self._operation = operation
        if not _text(verified_subject):
            operation.fail("subject_invalid", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        self._subject = verified_subject
        self._strategy = credential_strategy

    def _read(self, path: str, params: dict[str, str], *, limit: int = JSON_LIMIT) -> Any:
        op = self._operation
        op._check()
        if self._strategy is None:
            op.fail("directory_credential_unsupported", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        try:
            proof = getattr(self._strategy, "proof", None)
            if proof is not None:
                if not isinstance(proof, DirectoryDeploymentProof) or not proof.matches(op.config):
                    op.fail("directory_proof", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
            elif (
                type(self._strategy) is not CasdoorBasicDirectoryCredentialStrategy
                or self._strategy.client_id != op.config.client_id
            ):
                op.fail("directory_credential_unsupported", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
            authorization = self._strategy.authorization(op.config.client_id, op.client_secret)
            # End-user Bearer may be Self-filtered and cannot substitute app identity.
            if not isinstance(authorization, str) or authorization.lower().startswith("bearer "):
                op.fail("directory_credential_invalid", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        except GatewayError:
            raise
        except Exception:
            op.fail("directory_credential_invalid", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        raw = op._request("GET", path, params=params, limit=limit, directory=True, authorization=authorization)
        if (
            set(raw) - {"status", "msg", "sub", "name", "data", "data2", "data3"}
            or raw.get("status") != "ok"
            or "data" not in raw
            or raw.get("data2") is not None
            or raw.get("data3") is not None
            or ("msg" in raw and not isinstance(raw["msg"], str))
            or any(key in raw and not _directory_metadata_text(raw[key]) for key in ("sub", "name"))
        ):
            op.fail("directory_envelope", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        return raw["data"]

    @property
    def deployment_proof(self) -> DirectoryDeploymentProof | None:
        proof = getattr(self._strategy, "proof", None) if self._strategy is not None else None
        return proof if isinstance(proof, DirectoryDeploymentProof) else None

    def get_verified_user(self) -> dict[str, Any]:
        op = self._operation
        raw = self._read("/api/get-user", {"owner": op.config.organization, "userId": self._subject})
        if (
            not isinstance(raw, dict)
            or raw.get("owner") != op.config.organization
            or raw.get("id") != self._subject
            or not _text(raw.get("name"))
            or type(raw.get("isForbidden")) is not bool
            or type(raw.get("isDeleted")) is not bool
        ):
            op.fail("user_schema", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        return raw

    def get_complete_roles(self) -> list[dict[str, Any]]:
        """Raw candidate full response; completeness must be proven by graph owner."""
        op = self._operation
        raw = self._read("/api/get-roles", {"owner": op.config.organization}, limit=ROLES_LIMIT)
        if not isinstance(raw, list) or any(
            not isinstance(role, dict) or role.get("owner") != op.config.organization or not _text(role.get("name"))
            for role in raw
        ):
            op.fail("roles_schema", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        return raw

    def get_organization_visibility(self) -> dict[str, Any]:
        op = self._operation
        raw = self._read("/api/get-organization", {"id": f"admin/{op.config.organization}"})
        if not isinstance(raw, dict) or raw.get("owner") != "admin" or raw.get("name") != op.config.organization:
            op.fail("organization_schema", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        # Retain presence and values without filling unknown fields with defaults.
        return {key: raw[key] for key in ("owner", "name", "accountItems", "viewRule") if key in raw}
