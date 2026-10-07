"""Read-only deployment review authority, separate from live protocol validation.

An operator-pinned Ed25519 authority attests a fixed deployment profile and its
evidence references. Neither a matching hash nor database validation rows create
authority. No real accepted artifact ships here; synthetic fixtures are tests
only. Callers must re-resolve before use; this object is not a durable grant.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from core.casdoor.claims import NativeTokenContract, NativeTokenSchema
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import DirectoryDeploymentProof
from core.casdoor.role_graph import DirectorySnapshotContract

MAX_EVIDENCE_BYTES = 32 * 1024
DEPLOYMENT_CAPABILITIES = frozenset(
    {"release_identity", "non_dcr_org_admin_application", "directory_credential_transport", "role_effects"}
)
Fingerprint = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
RecordID = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]
BoundedText = Annotated[StrictStr, Field(min_length=1, max_length=2048)]


class DeploymentEvidenceError(ValueError):
    """Fixed safe errors only; no parser exception, path, token or Secret."""

    code = CasdoorErrorCode.CONFIG_CONFLICT
    retry_allowed = False

    def __init__(self, reason: Literal["deployment_proof_missing", "deployment_proof_invalid"]):
        self.reason = reason
        super().__init__(reason)


def _invalid() -> DeploymentEvidenceError:
    return DeploymentEvidenceError("deployment_proof_invalid")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, validate_default=True)


class DeploymentBinding(_Model):
    """Caller reconstructs namespace/revision through the complete DB owner chain."""

    namespace_id: StrictStr
    revision_id: StrictStr
    config_digest: Fingerprint
    configuration_digest: Fingerprint
    rbac_mode: Literal["off"]
    deployment_edition: Literal["COMMUNITY"]
    browser_frontend_url: BoundedText
    backend_api_url: BoundedText
    expected_issuer: BoundedText
    organization: BoundedText
    application: BoundedText
    client_id: BoundedText

    @field_validator("namespace_id", "revision_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("invalid binding")
        return value


class EvidenceReference(_Model):
    """Opaque audit record and digest only: no raw directory, URLs or credentials.

    The signed reviewer attests these records describe actual deployment work.
    This owner does not fetch records, infer provenance from digests, or claim
    that the attestation substitutes for independent live protocol checks.
    """

    record_id: RecordID
    sha256: Fingerprint


class DeploymentEvidenceReferences(_Model):
    release: EvidenceReference
    image: EvidenceReference
    non_dcr_creation: EvidenceReference
    organization_admin_scope: EvidenceReference
    directory_visibility: EvidenceReference
    directory_schema: EvidenceReference
    native_token_layout: EvidenceReference
    directory_authentication: EvidenceReference
    local_role_effects: EvidenceReference
    # Reviewed release prerequisites, never successful live validation statuses.
    pkce_s256_enforcement: EvidenceReference
    id_token_contract: EvidenceReference
    nonce_contract: EvidenceReference
    userinfo_subject_contract: EvidenceReference


class ReviewedReauthenticationCapability(_Model):
    """Exact-release reviewed interactive enforcement, never a protocol pass."""

    profile: Literal["prompt_login_max_age_zero_auth_time_v1"]
    prompt_login_enforcement: EvidenceReference
    max_age_zero_enforcement: EvidenceReference
    signed_auth_time_contract: EvidenceReference


class ReviewedRPLogoutCapability(_Model):
    """Reviewed RP round trip with an unexpired, verified native ID-token hint.

    This attests endpoint semantics and registration for the exact release. An
    actual browser-bound protocol observation is separately required; discovery
    or a reviewer signature alone never supplies that observation.
    """

    profile: Literal["oidc_rp_initiated_logout_v1"]
    end_session_endpoint: BoundedText
    post_logout_redirect_uri: BoundedText
    endpoint_semantics: EvidenceReference
    registered_post_logout_redirect: EvidenceReference
    state_round_trip: EvidenceReference

    @field_validator("end_session_endpoint", "post_logout_redirect_uri")
    @classmethod
    def exact_registered_url(cls, value: str, info) -> str:
        parsed = urlsplit(value)
        loopback_callback = (
            info.field_name == "post_logout_redirect_uri"
            and parsed.scheme == "http"
            and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        )
        if (
            (parsed.scheme != "https" and not loopback_callback)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port == 0
            or any(char in value for char in "?#\\%")
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or "//" in parsed.path
            or any(part in (".", "..") for part in parsed.path.split("/"))
        ):
            raise ValueError("invalid RP URL")
        return value

    @field_validator("post_logout_redirect_uri")
    @classmethod
    def fixed_callback(cls, value: str) -> str:
        if urlsplit(value).path != "/console/api/auth/casdoor/logout/callback":
            raise ValueError("invalid RP callback")
        return value


class _Manifest(_Model):
    schema_version: Literal[1]
    authority_id: RecordID
    reviewed_source: Literal["actual_deployment"]
    binding: DeploymentBinding
    issued_at: datetime
    expires_at: datetime
    casdoor_release: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    image_digest: Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    native_token_schema: Literal["flat_user_v1"]
    directory_schema: Literal["flat_directory_v1"]
    directory_authentication: Literal["basic_header"]
    role_effects_profile: Literal["community_local_v1"]
    evidence: DeploymentEvidenceReferences
    reauthentication: ReviewedReauthenticationCapability | None = None
    rp_logout: ReviewedRPLogoutCapability | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid version")
        return value

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def timestamp_shape(cls, value: object) -> object:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|\+00:00)", value):
            raise ValueError("invalid window")
        return value

    @field_validator("casdoor_release")
    @classmethod
    def release_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", value):
            raise ValueError("invalid release")
        return value

    @model_validator(mode="after")
    def bounded_window(self) -> "_Manifest":
        if (
            self.issued_at.utcoffset() != timedelta(0)
            or self.expires_at.utcoffset() != timedelta(0)
            or not self.issued_at < self.expires_at
        ):
            raise ValueError("invalid window")
        if self.rp_logout is not None:
            endpoint = urlsplit(self.rp_logout.end_session_endpoint)
            issuer = urlsplit(self.binding.expected_issuer)
            if (endpoint.scheme, endpoint.hostname, endpoint.port or 443) != (
                issuer.scheme, issuer.hostname, issuer.port or 443
            ):
                raise ValueError("invalid RP issuer origin")
        return self


class _Authority(_Model):
    schema_version: Literal[1]
    authority_id: RecordID
    public_key: StrictStr
    accepted_manifest_sha256: Fingerprint

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid version")
        return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _json(document: bytes) -> dict:
    if type(document) is not bytes or not document or len(document) > MAX_EVIDENCE_BYTES:
        raise _invalid()
    try:
        parsed = json.loads(document.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if type(parsed) is not dict:
        raise _invalid()
    return parsed


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _b64(value: str, length: int) -> bytes:
    if type(value) is not str or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise _invalid()
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise _invalid() from None
    if len(decoded) != length or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise _invalid()
    return decoded


@dataclass(frozen=True, repr=False)
class ReviewedBasicDirectoryCredentialStrategy:
    """Only a reviewed release profile permits Basic; never retain a Secret.

    The caller passes the revision owner's short-lived plaintext at the existing
    gateway boundary. Query credentials and end-user Bearer are unsupported.
    """

    proof: DirectoryDeploymentProof

    def authorization(self, client_id: str, client_secret: str) -> str:
        if client_id != self.proof.client_id or ":" in client_id:
            raise _invalid()
        if (
            type(client_secret) is not str
            or not 1 <= len(client_secret) <= 16 * 1024
            or not client_secret.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in client_secret)
        ):
            raise _invalid()
        return "Basic " + base64.b64encode((client_id + ":" + client_secret).encode("ascii")).decode("ascii")


@dataclass(frozen=True, repr=False)
class AcceptedDeploymentPolicy:
    binding: DeploymentBinding
    authority_id: str
    proof_fingerprint: str
    issued_at: datetime
    expires_at: datetime
    evidence_references: DeploymentEvidenceReferences
    native_token_contract: NativeTokenContract
    directory_snapshot_contract: DirectorySnapshotContract
    directory_deployment_proof: DirectoryDeploymentProof
    credential_strategy: ReviewedBasicDirectoryCredentialStrategy
    deployment_capabilities: frozenset[str] = DEPLOYMENT_CAPABILITIES
    reauthentication: ReviewedReauthenticationCapability | None = None
    rp_logout: ReviewedRPLogoutCapability | None = None

    def __repr__(self) -> str:
        return "AcceptedDeploymentPolicy(<redacted>)"


def accept_deployment_evidence(
    *,
    authority_document: bytes,
    envelope_document: bytes,
    configuration: CasdoorConfiguration,
    namespace_id: UUID,
    revision_id: UUID,
    config_digest: str,
    rbac_mode: str,
    deployment_edition: str,
    now: datetime,
) -> AcceptedDeploymentPolicy:
    """Authenticate exact signed profile against current operator trust and binding.

    Ed25519 signs canonical JSON of the envelope's manifest (ASCII, sorted keys,
    compact separators). The operator's SHA256 pin hashes these same bytes.
    Removing either file/key/pin revokes subsequent resolutions. Accepted
    deployment capabilities never create PROTOCOL or DIAGNOSTIC validation.
    """
    try:
        authority = _Authority.model_validate(_json(authority_document))
        envelope = _json(envelope_document)
        if set(envelope) != {"manifest", "signature"} or type(envelope["manifest"]) is not dict:
            raise _invalid()
        payload = _canonical(envelope["manifest"])
        manifest = _Manifest.model_validate(envelope["manifest"])
        fingerprint = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(fingerprint, authority.accepted_manifest_sha256):
            raise _invalid()
        Ed25519PublicKey.from_public_bytes(_b64(authority.public_key, 32)).verify(
            _b64(envelope["signature"], 64), payload
        )
        if (
            not isinstance(configuration, CasdoorConfiguration)
            or type(config_digest) is not str
            or not isinstance(now, datetime)
            or now.utcoffset() != timedelta(0)
            or not manifest.issued_at <= now < manifest.expires_at
            or authority.authority_id != manifest.authority_id
            or configuration.organization == "built-in"
            or configuration.application == "built-in"
            or not configuration.client_id.isascii()
            or ":" in configuration.client_id
            or any(ord(char) <= 32 or ord(char) == 127 for char in configuration.client_id)
        ):
            raise _invalid()
        binding = DeploymentBinding(
            namespace_id=str(UUID(str(namespace_id))),
            revision_id=str(UUID(str(revision_id))),
            config_digest=config_digest,
            configuration_digest=configuration.config_digest(),
            rbac_mode=rbac_mode,
            deployment_edition=deployment_edition,
            **{
                field: getattr(configuration, field)
                for field in (
                    "browser_frontend_url",
                    "backend_api_url",
                    "expected_issuer",
                    "organization",
                    "application",
                    "client_id",
                )
            },
        )
        if manifest.binding != binding:
            raise _invalid()
        evidence = manifest.evidence
        proof = DirectoryDeploymentProof(
            expected_issuer=configuration.expected_issuer,
            organization=configuration.organization,
            application=configuration.application,
            client_id=configuration.client_id,
            release_fingerprint=evidence.release.sha256,
            creator_proof_fingerprint=evidence.non_dcr_creation.sha256,
            credential_proof_fingerprint=evidence.directory_authentication.sha256,
        )
        return AcceptedDeploymentPolicy(
            binding=binding,
            authority_id=authority.authority_id,
            proof_fingerprint=fingerprint,
            issued_at=manifest.issued_at,
            expires_at=manifest.expires_at,
            evidence_references=evidence,
            reauthentication=manifest.reauthentication,
            rp_logout=manifest.rp_logout,
            native_token_contract=NativeTokenContract(
                schema=NativeTokenSchema.FLAT_USER_V1,
                expected_issuer=configuration.expected_issuer,
                organization=configuration.organization,
                application=configuration.application,
                client_id=configuration.client_id,
                release_fingerprint=proof.release_fingerprint,
                schema_proof_fingerprint=evidence.native_token_layout.sha256,
            ),
            directory_snapshot_contract=DirectorySnapshotContract(
                deployment_proof=proof,
                schema_proof_fingerprint=evidence.directory_schema.sha256,
                visibility_proof_fingerprint=evidence.directory_visibility.sha256,
            ),
            directory_deployment_proof=proof,
            credential_strategy=ReviewedBasicDirectoryCredentialStrategy(proof),
        )
    except (ValidationError, InvalidSignature, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise _invalid() from None
