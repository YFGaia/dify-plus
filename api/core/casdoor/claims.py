"""Pure, pinned token and raw identity validation; no login or account mutation.

ID Token and native access JWT have distinct entry points and supported shapes.
Native layout requires server-owned evidence linkage; the SDK does not establish
that layout. Raw directory roles never become authorization in this module.
"""

import base64
import binascii
import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from casdoor import CasdoorSDK

from core.casdoor.crypto import CertificateTrustStore, CryptoError
from core.casdoor.errors import CasdoorErrorCode

if TYPE_CHECKING:
    from core.casdoor.gateway import RawTokens

MAX_TOKEN_BYTES = 96 * 1024
MAX_HEADER_BYTES = 4 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 1024
MAX_RAW_BYTES = 256 * 1024
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_MAILBOX = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class ClaimsError(ValueError):
    """Fixed internal reasons and public codes; never embed input/provider text."""

    def __init__(self, reason: str, code: CasdoorErrorCode = CasdoorErrorCode.INVALID_TRANSACTION):
        self.code = code
        self.reason = reason
        self.retry_allowed = False
        super().__init__(f"casdoor_claims_{reason}")


def _fail(reason: str, code: CasdoorErrorCode = CasdoorErrorCode.INVALID_TRANSACTION) -> Any:
    raise ClaimsError(reason, code) from None


def _text(value: object, limit: int = 255) -> bool:
    try:
        return (
            type(value) is str
            and bool(value.strip())
            and len(value.encode("utf-8")) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except UnicodeError:
        return False


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_number(value: str) -> Any:
    raise ValueError


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _json(data: bytes, limit: int) -> dict[str, Any]:
    if not data or len(data) > limit:
        _fail("json_invalid")
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_number, parse_float=_float
        )
    except (ValueError, UnicodeError, RecursionError):
        _fail("json_invalid")
    if type(value) is not dict:
        _fail("json_invalid")
    return value


def _raw(value: dict[str, Any] | bytes) -> dict[str, Any]:
    """Gateway parsed dictionaries or bounded raw JSON; no SDK default objects."""
    if type(value) is bytes:
        return _json(value, MAX_RAW_BYTES)
    if type(value) is not dict:
        _fail("raw_schema_unknown", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
    try:
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError):
        _fail("raw_schema_unknown", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
    return _json(data, MAX_RAW_BYTES)


def _decode(value: str, limit: int) -> bytes:
    if not _BASE64URL.fullmatch(value) or len(value) > ((limit + 2) // 3) * 4:
        _fail("jws_invalid")
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        _fail("jws_invalid")
    if len(result) > limit or base64.urlsafe_b64encode(result).rstrip(b"=").decode("ascii") != value:
        _fail("jws_invalid")
    return result


@dataclass(frozen=True, repr=False)
class VerifiedIDToken:
    issuer: str
    subject: str
    client_id: str
    issued_at: float | int
    expires_at: float | int

    def __repr__(self) -> str:
        return "VerifiedIDToken(<redacted>)"


class NativeTokenSchema(StrEnum):
    FLAT_USER_V1 = "flat_user_v1"


@dataclass(frozen=True)
class NativeTokenContract:
    """Evidence references, not automatic proof/activation or a public setting.

    G0-B must independently attest the actual release and this exact flat layout.
    No supported contract is inferred from SDK examples or an unverified token.
    """

    schema: NativeTokenSchema
    expected_issuer: str
    organization: str
    application: str
    client_id: str
    release_fingerprint: str
    schema_proof_fingerprint: str


@dataclass(frozen=True, repr=False)
class VerifiedNativeAccessToken:
    subject: str
    organization: str
    application: str
    issued_at: float | int
    expires_at: float | int

    def __repr__(self) -> str:
        return "VerifiedNativeAccessToken(<redacted>)"


@dataclass(frozen=True, repr=False)
class VerifiedTokenBundle:
    identity: VerifiedIDToken
    native_access: VerifiedNativeAccessToken

    def __repr__(self) -> str:
        return "VerifiedTokenBundle(<redacted>)"


@dataclass(frozen=True)
class StructuredUserRef:
    owner: str
    name: str


@dataclass(frozen=True, repr=False)
class VerifiedOnlineUser:
    subject: str
    user_ref: StructuredUserRef

    def __repr__(self) -> str:
        return "VerifiedOnlineUser(<redacted>)"


@dataclass(frozen=True, repr=False)
class VerifiedProfile:
    """Snapshot only. Nothing here updates Account.email, quota or passwords."""

    subject: str
    email: str | None
    email_verified: bool | None
    name: str | None
    locale: str | None
    zoneinfo: str | None
    picture: str | None = None

    def __repr__(self) -> str:
        return "VerifiedProfile(<redacted>)"

    def eligible_for_first_admission(self) -> bool:
        """Necessary mailbox condition only; never grants account/login policy."""
        return self.email_verified is True and _legal_email(self.email)


def _legal_email(value: str | None) -> bool:
    # Conservative ASCII dot-atom/DNS mailbox subset; no DNS/network or coercion.
    if not _text(value, 254) or not value.isascii() or value.count("@") != 1:
        return False
    local, domain = value.split("@")
    if (
        len(local) > 64
        or not _MAILBOX.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and len(domain) <= 253 and all(_DOMAIN_LABEL.fullmatch(label) for label in labels)


class ClaimsValidator:
    """Explicit trusted config, trust store and caller clock; never network or DB.

    The caller reconstructs auth_started_at/nonce from the consumed transaction.
    The SDK re-verifies the selected public pin and issuer/client audience; strict
    JSON/types and deterministic NumericDate checks are owned here, not coerced by
    PyJWT. Only parse_jwt_token is called on the private public-certificate SDK.
    """

    def __init__(
        self,
        *,
        trust_store: CertificateTrustStore,
        expected_issuer: str,
        organization: str,
        application: str,
        client_id: str,
        leeway: int = 60,
    ):
        if (
            not isinstance(trust_store, CertificateTrustStore)
            or not _text(expected_issuer, 2048)
            or not all(_text(v) for v in (organization, application, client_id))
            or type(leeway) is not int
            or not 0 <= leeway <= 60
        ):
            _fail("configuration_invalid", CasdoorErrorCode.CONFIG_CONFLICT)
        self._trust_store = trust_store
        self._issuer = expected_issuer
        self._organization = organization
        self._application = application
        self._client_id = client_id
        self._leeway = leeway

    def _clock(self, now: datetime, auth_started_at: datetime) -> tuple[float, float]:
        if any(not isinstance(t, datetime) or t.utcoffset() != timedelta(0) for t in (now, auth_started_at)):
            _fail("clock_invalid")
        current, started = now.timestamp(), auth_started_at.timestamp()
        if started > current:
            _fail("clock_invalid")
        return current, started

    def _token(self, token: str, *, required: list[str], now: datetime) -> dict[str, Any]:
        if not _text(token, MAX_TOKEN_BYTES) or not token.isascii():
            _fail("jws_invalid")
        parts = token.split(".")
        if len(parts) != 3:
            _fail("jws_invalid")
        header = _json(_decode(parts[0], MAX_HEADER_BYTES), MAX_HEADER_BYTES)
        claims = _json(_decode(parts[1], MAX_PAYLOAD_BYTES), MAX_PAYLOAD_BYTES)
        signature = _decode(parts[2], MAX_SIGNATURE_BYTES)
        if (
            set(header) - {"alg", "typ", "kid"}
            or header.get("alg") != "RS256"
            or ("typ" in header and header["typ"] != "JWT")
            or ("kid" in header and not _text(header["kid"], 128))
        ):
            _fail("header_invalid")
        try:
            pin = self._trust_store.verify_rs256(
                (parts[0] + "." + parts[1]).encode("ascii"), signature, kid=header.get("kid"), now=now
            )
        except CryptoError:
            _fail("signature_invalid")
        # No secret and explicit frontend; this instance is never used for HTTP.
        sdk = CasdoorSDK(
            endpoint="https://unused.invalid",
            front_endpoint="https://unused.invalid",
            client_id=self._client_id,
            client_secret="",
            certificate=pin.pem,
            org_name=self._organization,
            application_name=self._application,
        )
        try:
            sdk.parse_jwt_token(
                token,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": required, "verify_exp": False, "verify_iat": False, "verify_nbf": False},
            )
        except Exception:
            _fail("token_invalid")
        return claims

    def _standard(self, claims: dict[str, Any], *, current: float, started: float) -> None:
        if claims.get("iss") != self._issuer:
            _fail("issuer_invalid")
        audience = claims.get("aud")
        if type(audience) is str:
            audiences = [audience]
        elif type(audience) is list:
            audiences = audience
        else:
            _fail("audience_invalid")
        if not audiences or not all(_text(a) for a in audiences) or len(set(audiences)) != len(audiences):
            _fail("audience_invalid")
        if self._client_id not in audiences:
            _fail("audience_invalid")
        if (len(audiences) > 1 and "azp" not in claims) or ("azp" in claims and claims["azp"] != self._client_id):
            _fail("authorized_party_invalid")
        for key in ("exp", "iat", "nbf"):
            if key not in claims:
                if key == "nbf":
                    continue
                _fail("time_invalid")
            value = claims[key]
            if type(value) not in (int, float):
                _fail("time_invalid")
            try:
                if not math.isfinite(value):
                    _fail("time_invalid")
            except OverflowError:
                _fail("time_invalid")
        issued, expiry = claims["iat"], claims["exp"]
        if (
            issued < started - self._leeway
            or issued > current + self._leeway
            or expiry <= issued
            or expiry <= current - self._leeway
            or ("nbf" in claims and (claims["nbf"] > current + self._leeway or claims["nbf"] >= expiry))
        ):
            _fail("time_invalid")

    def verify_id_token(
        self, token: str, *, expected_nonce: str, auth_started_at: datetime, now: datetime | None = None
    ) -> VerifiedIDToken:
        checked_at = now if now is not None else datetime.now(UTC)
        current, started = self._clock(checked_at, auth_started_at)
        if not _text(expected_nonce, 255):
            _fail("nonce_invalid")
        claims = self._token(token, required=["iss", "sub", "aud", "exp", "iat", "nonce"], now=checked_at)
        self._standard(claims, current=current, started=started)
        if not _text(claims.get("sub")):
            _fail("subject_invalid")
        nonce = claims.get("nonce")
        if not _text(nonce, 255) or not hmac.compare_digest(nonce.encode("utf-8"), expected_nonce.encode("utf-8")):
            _fail("nonce_invalid")
        return VerifiedIDToken(self._issuer, claims["sub"], self._client_id, claims["iat"], claims["exp"])

    def _identity(self, identity: VerifiedIDToken) -> None:
        if not isinstance(identity, VerifiedIDToken) or (identity.issuer, identity.client_id) != (
            self._issuer,
            self._client_id,
        ):
            _fail("identity_invalid")

    def verify_native_access_token(
        self,
        token: str,
        *,
        identity: VerifiedIDToken,
        auth_started_at: datetime,
        contract: NativeTokenContract | None = None,
        now: datetime | None = None,
    ) -> VerifiedNativeAccessToken:
        self._identity(identity)
        if (
            not isinstance(contract, NativeTokenContract)
            or type(contract.schema) is not NativeTokenSchema
            or contract.schema != NativeTokenSchema.FLAT_USER_V1
            or (contract.expected_issuer, contract.organization, contract.application, contract.client_id)
            != (self._issuer, self._organization, self._application, self._client_id)
            or not all(
                type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v)
                for v in (contract.release_fingerprint, contract.schema_proof_fingerprint)
            )
        ):
            _fail("native_contract_unknown", CasdoorErrorCode.CONFIG_CONFLICT)
        checked_at = now if now is not None else datetime.now(UTC)
        current, started = self._clock(checked_at, auth_started_at)
        claims = self._token(token, required=["iss", "aud", "exp", "iat", "owner", "id"], now=checked_at)
        self._standard(claims, current=current, started=started)
        if claims.get("owner") != self._organization or not _text(claims.get("id")) or claims["id"] != identity.subject:
            _fail("native_identity_invalid")
        if ("sub" in claims and claims["sub"] != identity.subject) or (
            "application" in claims and claims["application"] != self._application
        ):
            _fail("native_identity_invalid")
        return VerifiedNativeAccessToken(
            identity.subject, self._organization, self._application, claims["iat"], claims["exp"]
        )

    def verify_token_bundle(
        self,
        raw_tokens: "RawTokens",
        *,
        expected_nonce: str,
        auth_started_at: datetime,
        contract: NativeTokenContract | None = None,
        now: datetime | None = None,
    ) -> VerifiedTokenBundle:
        """Fixed token-response slots, never fall back from id_token to access_token.

        JWT custom fields cannot cryptographically prove token purpose. The
        validated OAuth response slots, nonce and independent native contract
        establish the supported protocol boundary; live release proof is G0-B.
        """
        from core.casdoor.gateway import RawTokens

        if not isinstance(raw_tokens, RawTokens) or type(raw_tokens.payload) is not dict:
            _fail("token_response_invalid")
        payload = raw_tokens.payload
        if not _text(payload.get("id_token"), MAX_TOKEN_BYTES) or not _text(
            payload.get("access_token"), MAX_TOKEN_BYTES
        ):
            _fail("token_response_invalid")
        checked_at = now if now is not None else datetime.now(UTC)
        identity = self.verify_id_token(
            payload["id_token"], expected_nonce=expected_nonce, auth_started_at=auth_started_at, now=checked_at
        )
        native = self.verify_native_access_token(
            payload["access_token"],
            identity=identity,
            auth_started_at=auth_started_at,
            contract=contract,
            now=checked_at,
        )
        return VerifiedTokenBundle(identity, native)

    def verify_userinfo(self, raw: dict[str, Any] | bytes, *, identity: VerifiedIDToken) -> VerifiedProfile:
        self._identity(identity)
        data = _raw(raw)
        if not _text(data.get("sub")) or data["sub"] != identity.subject:
            _fail("userinfo_subject_invalid")
        if "email_verified" in data and type(data["email_verified"]) is not bool:
            _fail("profile_schema_invalid")
        name = data.get("name")
        if name is not None:
            try:
                valid_name = (
                    type(name) is str
                    and len(name.encode("utf-8")) <= 255
                    and not any(ord(c) < 32 or ord(c) == 127 for c in name)
                )
            except UnicodeError:
                valid_name = False
            if not valid_name:
                _fail("profile_schema_invalid")
            if not name.strip():
                name = None
        limits = {"email": 254, "locale": 64, "zoneinfo": 64}
        for key, limit in limits.items():
            if key in data and not _text(data[key], limit):
                _fail("profile_schema_invalid")
        # Optional OIDC UserInfo candidate only, after exact subject validation.
        # Invalid optional pictures never invalidate an otherwise valid login.
        picture = data.get("picture")
        try:
            if (
                type(picture) is not str
                or not picture.strip()
                or len(picture.encode("utf-8")) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in picture)
            ):
                picture = None
        except UnicodeError:
            picture = None
        return VerifiedProfile(
            identity.subject,
            data.get("email"),
            data.get("email_verified"),
            name,
            data.get("locale"),
            data.get("zoneinfo"),
            picture,
        )

    def verify_online_user(self, raw: dict[str, Any] | bytes, *, identity: VerifiedIDToken) -> VerifiedOnlineUser:
        self._identity(identity)
        data = _raw(raw)
        if not all(_text(data.get(key)) for key in ("owner", "id", "name")) or any(
            type(data.get(key)) is not bool for key in ("isForbidden", "isDeleted")
        ):
            _fail("online_schema_unknown", CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN)
        if data["owner"] != self._organization or data["id"] != identity.subject:
            _fail("online_identity_invalid")
        if data["isForbidden"] or data["isDeleted"]:
            _fail("online_disabled", CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED)
        return VerifiedOnlineUser(identity.subject, StructuredUserRef(data["owner"], data["name"]))
