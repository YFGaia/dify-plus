"""Casdoor endpoint DTO foundation, without route registration or business actions.

Future controllers must use these schemas for both parsing and Swagger and must
translate validation errors without Pydantic ``input``/``ctx`` or raw provider
messages. Current-account actions derive account_id from the authenticated owner;
membership/retry references are hints to reconstruct DB scope, never authority.
Safe navigation fields are local paths to controlled one-shot handlers, never
provider URLs containing Tokens. CSRF, ETag CAS and capability proofs belong to
their owners and are not established by constructing a DTO.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from core.casdoor.configuration import (
    ButtonText,
    CasdoorConfiguration,
    CertificateKid,
    ExactName,
    RoleRef,
    TargetRole,
    _utf8_text,
)
from core.casdoor.errors import CasdoorDecisionReason, CasdoorErrorCode
from fields.base import ResponseModel
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

ETag = Annotated[int, Field(strict=True, ge=0)]


class CasdoorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_default=True)


class CasdoorResponse(ResponseModel):
    # Fail closed on accidental fields instead of silently dropping PII or tokens.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_default=True)


class CasdoorLocalMembershipTarget(CasdoorPayload):
    identity_id: UUID
    workspace_id: UUID


class CasdoorLocalMembershipReviewPayload(CasdoorLocalMembershipTarget):
    operation: Literal["release", "adopt"]
    etag: ETag


class CasdoorLocalMembershipMutationPayload(CasdoorPayload):
    review_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
    etag: ETag


class CasdoorLocalMembershipInspectionResponse(CasdoorResponse):
    identity_id: UUID
    workspace_id: UUID
    account_id: UUID
    etag: ETag
    current_role: TargetRole
    ownership: Literal["managed", "local_override", "released", "unmanaged"]
    local_no_intent: StrictBool


class CasdoorLocalMembershipReviewResponse(CasdoorResponse):
    review_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
    etag: ETag
    operation: Literal["release", "adopt"]
    current_role: TargetRole
    target_role: TargetRole
    expires_in: Literal[60]


class CasdoorLocalMembershipMutationResponse(CasdoorResponse):
    status: Literal["released", "adopted"]
    membership_id: UUID
    ownership_epoch: ETag


class CasdoorLocalMembershipListQuery(CasdoorPayload):
    after_identity_id: UUID | None = None
    after_workspace_id: UUID | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20

    @model_validator(mode="after")
    def paired_cursor(self):
        if (self.after_identity_id is None) != (self.after_workspace_id is None):
            raise ValueError("cursor_pair_required")
        return self


class CasdoorLocalMembershipCandidateResponse(CasdoorResponse):
    identity_id: UUID
    workspace_id: UUID
    account_id: UUID
    account_name: ExactName
    workspace_name: ExactName
    current_role: Literal["owner", "admin", "editor", "normal", "dataset_operator"]


class CasdoorLocalMembershipListResponse(CasdoorResponse):
    items: Annotated[list[CasdoorLocalMembershipCandidateResponse], Field(max_length=50)]
    has_more: StrictBool
    next_identity_id: UUID | None
    next_workspace_id: UUID | None


class CasdoorDisplayResponse(CasdoorResponse):
    enabled: StrictBool = False
    button_text: ButtonText = "Casdoor"
    start_path: Literal["/console/api/auth/casdoor/login"] = "/console/api/auth/casdoor/login"


class CasdoorPermissionsResponse(CasdoorResponse):
    can_manage_casdoor: StrictBool


class CasdoorSaveConfigurationPayload(CasdoorPayload):
    etag: ETag
    configuration: CasdoorConfiguration
    secret: SecretStr | None = Field(
        default=None, exclude=True, repr=False, json_schema_extra={"x-max-utf8-bytes": 4096}
    )

    @field_validator("secret")
    @classmethod
    def bounded_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().encode("utf-8")) > 4 * 1024:
            raise ValueError("secret exceeds 4096 UTF-8 bytes")
        if value is not None and value.get_secret_value() == "********":
            raise ValueError("masked display text is not a replacement secret")
        return value

    @property
    def secret_action(self) -> Literal["keep", "replace"]:
        """The owner reads SecretStr directly only for replace; dump is not storage input."""
        if self.secret is None or self.secret.get_secret_value() == "":
            return "keep"
        return "replace"


class CasdoorRevisionPayload(CasdoorPayload):
    etag: ETag
    revision_id: UUID


class CasdoorClearSecretPayload(CasdoorRevisionPayload):
    """Explicit draft-only action; empty Secret in save means retain, not clear."""


class CasdoorDisablePayload(CasdoorPayload):
    etag: ETag


class CasdoorResetNamespacePayload(CasdoorPayload):
    etag: ETag
    namespace_id: UUID
    confirm_management_review: StrictBool


class CasdoorValidationSummaryResponse(CasdoorResponse):
    revision_id: UUID
    kind: Literal["validation", "diagnostic"]
    status: Literal["not_run", "passed", "failed", "unknown", "expired"]
    checked_at: datetime | None = None
    expires_at: datetime | None = None
    code: CasdoorErrorCode | None = None
    correlation_id: UUID | None = None


class CasdoorCertificateSummaryResponse(CasdoorResponse):
    fingerprint: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    kid: CertificateKid | None = None
    not_before: datetime
    accept_until: datetime


class CasdoorRevisionResponse(CasdoorResponse):
    revision_id: UUID
    namespace_id: UUID
    configuration: CasdoorConfiguration
    secret_configured: StrictBool
    certificate_summaries: tuple[CasdoorCertificateSummaryResponse, ...] = Field(default=(), max_length=2)
    validation: tuple[CasdoorValidationSummaryResponse, ...] = Field(default=(), max_length=2)
    diagnostic: "CasdoorDraftDiagnosticPreviewResponse | None" = None


class CasdoorConfigurationResponse(CasdoorResponse):
    enabled: StrictBool = False
    etag: ETag = 0
    active_revision_id: UUID | None = None
    draft_revision_id: UUID | None = None
    active: CasdoorRevisionResponse | None = None
    draft: CasdoorRevisionResponse | None = None

    @model_validator(mode="after")
    def exact_revision_pointers(self) -> "CasdoorConfigurationResponse":
        for revision_id, revision in ((self.active_revision_id, self.active), (self.draft_revision_id, self.draft)):
            if revision is not None and revision.revision_id != revision_id:
                raise ValueError("revision response must match its pointer")
        return self


class CasdoorDisableResponse(CasdoorResponse):
    configuration: CasdoorConfigurationResponse
    reconciliation_required: StrictBool


class CasdoorRoleSelectorResponse(CasdoorResponse):
    role_ref: RoleRef
    enabled: StrictBool


class CasdoorRolesResponse(CasdoorResponse):
    revision_id: UUID
    roles: tuple[CasdoorRoleSelectorResponse, ...] = Field(max_length=2000)


class CasdoorWorkspaceResponse(CasdoorResponse):
    workspace_id: UUID
    # Local Tenant.name is String(255) characters, not an IdP identifier.
    name: StrictStr = Field(max_length=255)
    available: StrictBool


class CasdoorWorkspaceSelectionResponse(CasdoorWorkspaceResponse):
    """Management selector adds persisted creation evidence to the workspace DTO."""

    created_at: datetime


class CasdoorWorkspacesQuery(CasdoorPayload):
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=100)


class CasdoorWorkspacesResponse(CasdoorResponse):
    workspaces: tuple[CasdoorWorkspaceSelectionResponse, ...]
    page: int = Field(ge=1)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_more: StrictBool
    earliest_created_workspace: CasdoorWorkspaceSelectionResponse | None
    earliest_created_ambiguous: StrictBool


class CasdoorStaticValidationResponse(CasdoorResponse):
    revision_id: UUID
    etag: ETag
    kind: Literal["static"] = "static"
    status: Literal["passed"] = "passed"
    static_only: Literal[True] = True
    checked_at: datetime
    certificate_summaries: tuple[CasdoorCertificateSummaryResponse, ...] = Field(min_length=1, max_length=2)


class CasdoorTestLoginResponse(CasdoorResponse):
    status: Literal["blocked", "started"]
    reason: Literal["deployment_proof_missing", "live_test_not_wired"] | None = None
    handoff: "CasdoorNavigationResponse | None" = None

    @model_validator(mode="after")
    def consistent_navigation(self) -> "CasdoorTestLoginResponse":
        if self.status == "started":
            if self.handoff is None or self.reason is not None:
                raise ValueError("started requires only local navigation")
        elif self.reason is None or self.handoff is not None:
            raise ValueError("blocked requires only a safe reason")
        return self


class CasdoorManagementErrorResponse(CasdoorResponse):
    code: StrictStr
    reason: Literal["deployment_proof_missing", "live_test_not_wired"] | None = None
    message: Literal["Casdoor management request failed."] = "Casdoor management request failed."
    correlation_id: UUID


class CasdoorMembershipScopePayload(CasdoorPayload):
    namespace_id: UUID
    identity_id: UUID
    workspace_id: UUID


class CasdoorAdoptMembershipPayload(CasdoorMembershipScopePayload):
    confirm_permission_difference: StrictBool


class CasdoorReleaseMembershipPayload(CasdoorMembershipScopePayload):
    confirm_management_transfer: StrictBool


class CasdoorRetrySyncPayload(CasdoorPayload):
    intent_id: UUID


class CasdoorWorkspaceDecisionResponse(CasdoorResponse):
    """Only complete known snapshots may express successful mapping/fallback."""

    workspace_id: UUID
    target_role: TargetRole
    reason: CasdoorDecisionReason
    snapshot_state: Literal["known"]
    ownership: Literal["managed", "local_override", "released", "unmanaged"]
    finalization: Literal["pending", "finalized", "manual_recovery"]

    @model_validator(mode="after")
    def fallback_is_normal(self) -> "CasdoorWorkspaceDecisionResponse":
        if self.reason == CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK and self.target_role != "normal":
            raise ValueError("default fallback can only grant normal")
        return self


class CasdoorResultResponse(CasdoorResponse):
    code: CasdoorErrorCode
    correlation_id: UUID
    retry_allowed: StrictBool


class CasdoorRestrictedResultResponse(CasdoorResponse):
    """Anonymous, consumed display result; never a session or retry grant."""

    code: Literal[
        CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN,
        CasdoorErrorCode.WORKSPACE_UNAVAILABLE,
        CasdoorErrorCode.AUTHORIZATION_PENDING,
    ]
    correlation_id: UUID
    retry_allowed: Literal[False]

    @field_validator("correlation_id", mode="before")
    @classmethod
    def canonical_correlation(cls, value: object) -> str:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("canonical correlation required")
        return value

    @field_validator("retry_allowed", mode="before")
    @classmethod
    def no_retry(cls, value: object) -> bool:
        if value is not False:
            raise ValueError("display result cannot authorize retry")
        return False


class CasdoorCallbackQuery(CasdoorPayload):
    state: StrictStr = Field(min_length=1, max_length=512, repr=False)
    code: StrictStr | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    error: StrictStr | None = Field(default=None, min_length=1, max_length=255, repr=False)
    error_description: StrictStr | None = Field(default=None, max_length=4096, repr=False)

    @model_validator(mode="after")
    def callback_outcome(self) -> "CasdoorCallbackQuery":
        if (self.code is None) == (self.error is None):
            raise ValueError("callback requires exactly one of code or error")
        if self.error_description is not None and self.error is None:
            raise ValueError("error_description requires error")
        return self


class CasdoorResultQuery(CasdoorPayload):
    handoff: StrictStr = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)


class CasdoorDiagnosticStageResponse(CasdoorResponse):
    stage: Literal["configuration", "protocol", "identity", "online_status", "role_snapshot", "workspace", "rbac"]
    status: Literal["not_run", "passed", "failed", "unknown"]
    code: CasdoorErrorCode | None = None


class CasdoorDiagnosticResponse(CasdoorResponse):
    revision_id: UUID
    correlation_id: UUID
    stages: tuple[CasdoorDiagnosticStageResponse, ...] = Field(max_length=7)
    decisions: tuple[CasdoorWorkspaceDecisionResponse, ...] = Field(default=(), max_length=100)


class CasdoorDraftWorkspaceTargetResponse(CasdoorResponse):
    """Desired permission preview; never claims local membership or a grant."""

    workspace_id: UUID
    target_role: TargetRole
    reason: CasdoorDecisionReason


class CasdoorDraftDiagnosticPreviewResponse(CasdoorResponse):
    revision_id: UUID
    namespace_id: UUID
    correlation_id: UUID
    effective_role_count: int = Field(ge=0, le=2000)
    stages: tuple[CasdoorDiagnosticStageResponse, ...] = Field(max_length=7)
    targets: tuple[CasdoorDraftWorkspaceTargetResponse, ...] = Field(max_length=100)


class CasdoorProfilePayload(CasdoorPayload):
    """Name-only local request; role/email/password/quota remain separate owners."""

    name: ExactName


class CasdoorLinkIdentityPayload(CasdoorPayload):
    """Empty request: source account comes only from the current session owner."""


class CasdoorUnlinkIdentityPayload(CasdoorPayload):
    """Proof is bound server-side to the current account, identity and action."""


class CasdoorIdentityActionsResponse(CasdoorResponse):
    link: StrictBool
    reauthenticate: StrictBool
    unlink: StrictBool
    reason: (
        Literal[
            "reauthentication_unavailable",
            "reauthentication_required",
            "other_login_unavailable",
            "managed_history_requires_release",
        ]
        | None
    ) = None


class CasdoorIdentityUnlinkedResponse(CasdoorResponse):
    status: Literal["unlinked"]


class CasdoorProfileSyncResponse(CasdoorResponse):
    field: Literal["name", "avatar"]
    status: Literal["unchanged", "applied", "skipped", "local_override", "failed", "disabled"]
    synced_at: datetime | None = None


class CasdoorIdentityResponse(CasdoorResponse):
    bound: StrictBool
    identity_id: UUID | None = None
    namespace_id: UUID | None = None
    # No raw email/name/avatar URL/subject; server computes a difference boolean.
    remote_email_differs: StrictBool = False
    remote_email_verified: StrictBool | None = None
    profile_sync: tuple[CasdoorProfileSyncResponse, ...] = Field(default=(), max_length=2)
    self_unlink_available: StrictBool = False


class CasdoorSessionResponse(CasdoorResponse):
    source: Literal["casdoor", "local_only"]
    verified: StrictBool
    rp_logout_available: StrictBool = False
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def verified_source(self) -> "CasdoorSessionResponse":
        if self.source == "casdoor" and not self.verified:
            raise ValueError("Casdoor source requires verified session provenance")
        if self.source == "local_only" and self.rp_logout_available:
            raise ValueError("RP logout requires verified Casdoor provenance")
        return self


class CasdoorNavigationResponse(CasdoorResponse):
    """Opaque local handler path; the server validates/consumes the handoff."""

    handoff_path: StrictStr = Field(max_length=2048, min_length=1)

    @field_validator("handoff_path")
    @classmethod
    def local_handoff(cls, value: str) -> str:
        _utf8_text(value, 2048)
        if (
            not value.startswith("/console/api/auth/casdoor/")
            or any(char in value for char in "?#\\%")
            or "//" in value
            or any(segment in (".", "..") for segment in value.split("/"))
        ):
            raise ValueError("handoff must be a local Casdoor handler path without query or fragment")
        if any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid handoff path")
        return value


class CasdoorLogoutResponse(CasdoorResponse):
    status: Literal["local_only", "handoff_ready"]
    handoff: CasdoorNavigationResponse | None = None

    @model_validator(mode="after")
    def matching_handoff(self) -> "CasdoorLogoutResponse":
        if (self.status == "handoff_ready") != (self.handoff is not None):
            raise ValueError("logout status and handoff must agree")
        return self


class CasdoorConsoleLogoutResponse(CasdoorResponse):
    result: Literal["success"] = "success"
    casdoor_logout: CasdoorLogoutResponse | None = None


class CasdoorRPLogoutDiagnosticResponse(CasdoorResponse):
    revision_id: UUID
    namespace_id: UUID
    profile_available: StrictBool = False
    status: Literal["not_run", "passed", "unknown"] = "unknown"
    checked_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def matching_observation(self):
        if self.status == "passed":
            if (
                not self.profile_available
                or self.checked_at is None
                or self.expires_at is None
                or self.checked_at >= self.expires_at
            ):
                raise ValueError("passed requires a bounded protocol observation")
        elif self.checked_at is not None or self.expires_at is not None:
            raise ValueError("unavailable observation has no freshness claim")
        return self


class CasdoorRPLogoutStatusQuery(CasdoorPayload):
    revision_id: UUID


# Resolve late-defined wire models here, independently of controller registration
# or a prior test importing/rebuilding the same classes in its process.
CasdoorRevisionResponse.model_rebuild(_types_namespace=globals())
CasdoorConfigurationResponse.model_rebuild(_types_namespace=globals())
CasdoorTestLoginResponse.model_rebuild(_types_namespace=globals())


class CasdoorNamespaceResetReviewResponse(CasdoorResponse):
    review_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
    namespace_id: UUID
    etag: ETag
    expires_in: Literal[60]
    credential_check: Literal["format_only"]


class CasdoorNamespaceResetMutationPayload(CasdoorPayload):
    review_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
    etag: ETag
