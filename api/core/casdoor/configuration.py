"""Pure, non-secret Casdoor revision policy and exact role-reference DTOs.

No environment, database, URL fetching or certificate parsing occurs here. The
deployment owner supplies the explicit development HTTP exception as validation
context; it is never a field an administrator can enable in a payload. Legacy
pins are validated by their crypto owner; v2 keys are discovered by the service.
Revision, namespace, ETag and encrypted Secret belong to storage.
"""

import hashlib
import ipaddress
import json
from datetime import datetime, timedelta
from operator import itemgetter
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)


def _utf8_text(value: str, limit: int) -> str:
    if not value or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError(f"must be non-empty and at most {limit} UTF-8 bytes")
    return value


ExactName = Annotated[
    StrictStr,
    Field(min_length=1, max_length=255, json_schema_extra={"x-max-utf8-bytes": 255}),
    AfterValidator(lambda value: _utf8_text(value, 255)),
]
ButtonText = Annotated[ExactName, Field(max_length=120)]
CertificatePEM = Annotated[
    StrictStr,
    Field(min_length=1, max_length=16384, json_schema_extra={"x-max-utf8-bytes": 16384}),
    AfterValidator(lambda value: _utf8_text(value, 16 * 1024)),
]
CertificateKid = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, json_schema_extra={"x-max-utf8-bytes": 128}),
    AfterValidator(lambda value: _utf8_text(value, 128)),
]
TargetRole = Literal["admin", "editor", "normal"]


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, validate_default=True)


class RoleRef(PolicyModel):
    """Two exact strings; names are never split, normalized or case-folded."""

    organization: ExactName
    name: ExactName


class WorkspaceRoleMapping(PolicyModel):
    workspace_id: UUID
    admin: RoleRef | None = None
    editor: RoleRef | None = None
    normal: RoleRef | None = None

    @model_validator(mode="after")
    def unique_role_refs(self) -> "WorkspaceRoleMapping":
        refs = [ref for ref in (self.admin, self.editor, self.normal) if ref is not None]
        if len(set(refs)) != len(refs):
            raise ValueError("one roleRef may target only one role in a workspace")
        return self


class PublicCertificatePolicy(PolicyModel):
    """Public pin declaration only; cryptographic authenticity is owned by I02."""

    pem: CertificatePEM
    kid: CertificateKid | None = None
    not_before: datetime
    accept_until: datetime

    @field_validator("pem")
    @classmethod
    def public_only(cls, value: str) -> str:
        if "PRIVATE KEY" in value or not value.strip().startswith("-----BEGIN CERTIFICATE-----"):
            raise ValueError("only a public X.509 certificate declaration is allowed")
        return value

    @model_validator(mode="after")
    def utc_window(self) -> "PublicCertificatePolicy":
        if self.not_before.utcoffset() != timedelta(0) or self.accept_until.utcoffset() != timedelta(0):
            raise ValueError("certificate window must use UTC")
        if self.not_before >= self.accept_until:
            raise ValueError("certificate window must be non-empty")
        return self


class CasdoorConfiguration(PolicyModel):
    schema_version: Literal[1, 2] = 1
    signing_key_mode: Literal["automatic"] | None = None
    browser_frontend_url: StrictStr = Field(max_length=2048, min_length=1)
    backend_api_url: StrictStr = Field(max_length=2048, min_length=1)
    expected_issuer: StrictStr = Field(max_length=2048, min_length=1)
    organization: ExactName
    application: ExactName
    client_id: ExactName
    button_text: ButtonText = "Casdoor"
    scope: Literal["openid email profile"] = "openid email profile"
    default_workspace_id: UUID
    workspace_mappings: tuple[WorkspaceRoleMapping, ...] = Field(default=(), max_length=100)
    default_normal_fallback: Literal[True] = True
    name_sync: Literal["off", "fill_empty", "managed"] = "fill_empty"
    # Draft intent only. Capability/evidence owners must gate effective behavior
    # and activation; accepting True here is not proof an option is supported.
    avatar_sync: StrictBool = False
    # Ownership intent when enabled, independent of name_sync. Neither mode
    # claims existing local values or enables runtime work by itself. UI off
    # maps to avatar_sync=False; the other choices set True plus this mode.
    avatar_mode: Literal["fill_empty", "managed"] = "fill_empty"
    rp_logout: StrictBool = False
    self_unlink: StrictBool = False
    certificates: tuple[PublicCertificatePolicy, ...] = Field(default=(), max_length=2)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("default_normal_fallback", "avatar_sync", "rp_logout", "self_unlink", mode="before")
    @classmethod
    def boolean_policies(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("policy flag must be a boolean")
        return value

    @field_validator("browser_frontend_url", "backend_api_url", "expected_issuer")
    @classmethod
    def trusted_url_shape(cls, value: str, info: ValidationInfo) -> str:
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError("invalid URL characters")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("invalid URL") from None
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "#" in value
            or (port is not None and port == 0)
        ):
            raise ValueError("URL requires http(s), a host, and no userinfo or fragment")
        if info.field_name == "expected_issuer" and "?" in value:
            raise ValueError("issuer must not contain a query")
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            context = info.context if isinstance(info.context, dict) else {}
            if not loopback or context.get("allow_development_loopback_http") is not True:
                raise ValueError("HTTP requires the deployment owner's explicit loopback development exception")
        return value

    @model_validator(mode="after")
    def exact_mapping_scope(self) -> "CasdoorConfiguration":
        if self.schema_version == 1 and self.signing_key_mode is not None:
            raise ValueError("legacy configurations cannot select automatic keys")
        if self.schema_version == 2:
            if self.certificates:
                raise ValueError("automatic configurations cannot contain certificate pins")
            if self.signing_key_mode != "automatic":
                raise ValueError("automatic configurations require the server signing key policy")
        workspace_ids = [mapping.workspace_id for mapping in self.workspace_mappings]
        if len(set(workspace_ids)) != len(workspace_ids):
            raise ValueError("workspace mappings must be unique")
        if len(set(workspace_ids) | {self.default_workspace_id}) > 100:
            raise ValueError("configuration may target at most 100 workspaces including the default")
        for mapping in self.workspace_mappings:
            for ref in (mapping.admin, mapping.editor, mapping.normal):
                if ref is not None and ref.organization != self.organization:
                    raise ValueError("roleRef organization must exactly match the configured organization")
        kids = [certificate.kid for certificate in self.certificates if certificate.kid is not None]
        if len(kids) != len(set(kids)):
            raise ValueError("certificate kids must be unique")
        return self

    def canonical_json(self) -> str:
        """Stable JSON over typed policy only; semantically unordered rows are sorted."""
        data = self.model_dump(mode="json")
        # Historical immutable revision digests must remain byte-for-byte stable.
        if self.schema_version == 1:
            data.pop("signing_key_mode")
        data["workspace_mappings"] = sorted(data["workspace_mappings"], key=itemgetter("workspace_id"))
        data["certificates"] = sorted(
            data["certificates"], key=lambda pin: (pin["kid"] or "", pin["pem"], pin["not_before"], pin["accept_until"])
        )
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def config_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
