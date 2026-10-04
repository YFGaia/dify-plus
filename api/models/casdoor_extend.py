"""Independent Casdoor storage; no legacy provider tables or session formats change.

JSON fields are canonical, schema-versioned snapshots owned by the configuration
and provisioning services. Storage alone does not validate mapping policy, prove
remote termination, or grant a Console session. UTC timestamps are naive, matching
the existing database convention. No table is seeded by the migration.
"""

import hashlib
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

import sqlalchemy as sa
from libs.datetime_utils import naive_utc_now
from sqlalchemy.dialects.mysql import VARCHAR as MYSQL_VARCHAR
from sqlalchemy.orm import Mapped, mapped_column, validates

from .base import Base
from .types import EnumText, LongText, StringUUID


class CasdoorNamespaceLifecycle(StrEnum):
    ACTIVE = "active"
    FENCING = "fencing"
    ARCHIVED = "archived"


class CasdoorValidationKind(StrEnum):
    STATIC = "static"
    PROTOCOL = "protocol"
    DIAGNOSTIC = "diagnostic"
    DEPLOYMENT = "deployment"


class CasdoorValidationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CasdoorMembershipOwnership(StrEnum):
    MANAGED = "managed"
    LOCAL_OVERRIDE = "local_override"
    RELEASED = "released"


class CasdoorMembershipSource(StrEnum):
    MAPPING = "mapping"
    FALLBACK = "fallback"
    ADOPT = "adopt"


class CasdoorFinalizationState(StrEnum):
    PENDING = "pending"
    FINALIZED = "finalized"
    MANUAL_RECOVERY = "manual_recovery"


class CasdoorIntentKind(StrEnum):
    ROLE_REPLACE = "role_replace"
    MEMBER_REMOVE = "member_remove"
    RESOURCE_GRANT = "resource_grant"
    RESOURCE_REVOKE = "resource_revoke"
    INVITATION_FINALIZE = "invitation_finalize"
    PROFILE_AVATAR = "profile_avatar"


class CasdoorOperationState(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"
    APPLIED = "applied"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CasdoorTerminationState(StrEnum):
    NOT_STARTED = "not_started"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    MANUAL_RECOVERY = "manual_recovery"


def _enum_check(column: str, enum: type[StrEnum], name: str) -> sa.CheckConstraint:
    choices = ", ".join(f"'{item.value}'" for item in enum)
    return sa.CheckConstraint(f"{column} IN ({choices})", name=name)


def _nonnegative(column: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} >= 0", name=name.replace("_nonnegative", "_nn"))


class _CasdoorCreatedFields:
    id: Mapped[str] = mapped_column(StringUUID, primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime, nullable=False, default=naive_utc_now, server_default=sa.func.current_timestamp()
    )


class _CasdoorUpdatedFields(_CasdoorCreatedFields):
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime,
        nullable=False,
        default=naive_utc_now,
        server_default=sa.func.current_timestamp(),
        onupdate=naive_utc_now,
    )


class CasdoorIntegrationExtend(_CasdoorUpdatedFields, Base):
    """Lazy singleton. Pointer ownership and etag CAS belong to the repository."""

    __tablename__ = "casdoor_integration_extend"
    __table_args__ = (
        sa.UniqueConstraint("slot", name="casdoor_integration_slot_key"),
        sa.CheckConstraint("slot = 1", name="singleton_slot"),
        _nonnegative("etag", "etag_nonnegative"),
    )

    slot: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1, server_default=sa.text("1"))
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    # No circular FK: repository validates both pointers against this integration.
    active_revision_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    draft_revision_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    etag: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default=sa.text("0"))
    updated_by: Mapped[str | None] = mapped_column(StringUUID, nullable=True)


class CasdoorNamespaceExtend(_CasdoorCreatedFields, Base):
    """Stable identity namespace; equal tuple fingerprints never imply reuse."""

    __tablename__ = "casdoor_namespace_extend"
    __table_args__ = (
        _enum_check("lifecycle", CasdoorNamespaceLifecycle, "lifecycle"),
        _nonnegative("fence_epoch", "fence_epoch_nonnegative"),
        sa.Index("casdoor_namespace_integration_lifecycle_idx", "integration_id", "lifecycle"),
    )

    integration_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_integration_extend.id", ondelete="RESTRICT"), nullable=False
    )
    expected_issuer: Mapped[str] = mapped_column(sa.Text, nullable=False)
    organization: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    application: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    client_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    core_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    lifecycle: Mapped[CasdoorNamespaceLifecycle] = mapped_column(
        EnumText(CasdoorNamespaceLifecycle, 24),
        nullable=False,
        default=CasdoorNamespaceLifecycle.ACTIVE,
        server_default=sa.text("'active'"),
    )
    fence_epoch: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)


class CasdoorConfigRevisionExtend(_CasdoorCreatedFields, Base):
    """Immutable configuration snapshot; mutable diagnostics use validation rows.

    The configuration repository creates whole revisions after typed validation;
    it must never update snapshots through bulk SQL (which bypasses ORM guards).
    """

    __tablename__ = "casdoor_config_revision_extend"
    __table_args__ = (
        sa.UniqueConstraint("integration_id", "revision_number", name="casdoor_revision_number_key"),
        sa.CheckConstraint("revision_number > 0", name="revision_number_positive"),
        sa.CheckConstraint("schema_version = 1", name="schema_version"),
        sa.Index("casdoor_revision_namespace_idx", "namespace_id"),
    )

    integration_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_integration_extend.id", ondelete="RESTRICT"), nullable=False
    )
    namespace_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_namespace_extend.id", ondelete="RESTRICT"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1, server_default=sa.text("1"))
    config_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    browser_frontend_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    backend_api_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expected_issuer: Mapped[str] = mapped_column(sa.Text, nullable=False)
    organization: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    application: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    client_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    button_text: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    default_workspace_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    encrypted_secret: Mapped[str | None] = mapped_column(LongText, nullable=True)
    certificates_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    policy_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mappings_json: Mapped[str] = mapped_column(LongText, nullable=False)
    created_by: Mapped[str | None] = mapped_column(StringUUID, nullable=True)


@sa.event.listens_for(CasdoorConfigRevisionExtend, "before_update")
def _reject_revision_update(mapper: object, connection: object, target: CasdoorConfigRevisionExtend) -> None:
    if any(attribute.history.has_changes() for attribute in sa.inspect(target).attrs):
        raise ValueError("Casdoor configuration revisions are immutable; create a new revision")


class CasdoorValidationExtend(_CasdoorCreatedFields, Base):
    """Expiring proof summaries, without directory payloads or credentials."""

    __tablename__ = "casdoor_validation_extend"
    __table_args__ = (
        _enum_check("kind", CasdoorValidationKind, "kind"),
        _enum_check("status", CasdoorValidationStatus, "status"),
        sa.Index("casdoor_validation_revision_kind_idx", "revision_id", "kind", "checked_at"),
    )

    revision_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_config_revision_extend.id", ondelete="RESTRICT"), nullable=False
    )
    config_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    kind: Mapped[CasdoorValidationKind] = mapped_column(EnumText(CasdoorValidationKind, 24), nullable=False)
    status: Mapped[CasdoorValidationStatus] = mapped_column(
        EnumText(CasdoorValidationStatus, 24),
        nullable=False,
        default=CasdoorValidationStatus.PENDING,
        server_default=sa.text("'pending'"),
    )
    rbac_mode: Mapped[str | None] = mapped_column(sa.String(8), nullable=True)
    proof_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    summary_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor_account_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    correlation_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)


class CasdoorIdentityExtend(_CasdoorUpdatedFields, Base):
    """Bidirectional identity key; repository also compares exact raw subject.

    Digest avoids collation-dependent subject uniqueness. ORM assignment rejects
    overlong UTF-8 rather than truncating; bulk writers must apply the same rule.
    """

    __tablename__ = "casdoor_identity_extend"
    __table_args__ = (
        sa.UniqueConstraint("namespace_id", "subject_digest", name="casdoor_identity_subject_key"),
        sa.UniqueConstraint("namespace_id", "account_id", name="casdoor_identity_account_key"),
        _nonnegative("sync_generation", "sync_generation_nonnegative"),
    )

    namespace_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_namespace_extend.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    issuer: Mapped[str] = mapped_column(sa.Text, nullable=False)
    organization: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_digest: Mapped[str] = mapped_column(
        sa.String(64).with_variant(MYSQL_VARCHAR(64, charset="ascii", collation="ascii_bin"), "mysql"), nullable=False
    )
    remote_email: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    email_verified: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    sync_generation: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default=sa.text("0"))
    remote_profile_version: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    last_applied_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    profile_sync_json: Mapped[str] = mapped_column(sa.Text, nullable=False)

    @validates("subject")
    def _validate_subject(self, key: str, value: str) -> str:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 255:
            raise ValueError("Casdoor subject must contain 1 to 255 UTF-8 bytes")
        if self.subject is not None and value != self.subject:
            raise ValueError("Casdoor subjects are immutable; bind a new identity explicitly")
        self.subject_digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return value

    @validates("subject_digest")
    def _validate_subject_digest(self, key: str, value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("Casdoor subject digest must be lowercase SHA-256 hex")
        if self.subject is not None and value != hashlib.sha256(self.subject.encode("utf-8")).hexdigest():
            raise ValueError("Casdoor subject digest does not match the exact subject")
        return value


class CasdoorManagedMembershipExtend(_CasdoorUpdatedFields, Base):
    """Ownership tombstones survive join deletion and old namespace archival."""

    __tablename__ = "casdoor_managed_membership_extend"
    __table_args__ = (
        sa.UniqueConstraint("namespace_id", "account_id", "workspace_id", name="casdoor_membership_scope_key"),
        _enum_check("ownership", CasdoorMembershipOwnership, "ownership"),
        _enum_check("source", CasdoorMembershipSource, "source"),
        _enum_check("finalization", CasdoorFinalizationState, "finalization"),
        _nonnegative("ownership_epoch", "ownership_epoch_nonnegative"),
        _nonnegative("desired_generation", "desired_generation_nonnegative"),
        sa.Index("casdoor_membership_identity_idx", "identity_id"),
        sa.Index("casdoor_membership_join_idx", "join_id"),
    )

    namespace_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_namespace_extend.id", ondelete="RESTRICT"), nullable=False
    )
    # Historical reference survives a fully fenced, reconciled identity unlink.
    identity_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    account_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    workspace_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    join_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    ownership: Mapped[CasdoorMembershipOwnership] = mapped_column(
        EnumText(CasdoorMembershipOwnership, 24), nullable=False
    )
    ownership_epoch: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default=sa.text("0"))
    source: Mapped[CasdoorMembershipSource] = mapped_column(EnumText(CasdoorMembershipSource, 24), nullable=False)
    desired_generation: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default=sa.text("0")
    )
    revision_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_config_revision_extend.id", ondelete="RESTRICT"), nullable=False
    )
    last_applied_roles_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    last_applied_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    desired_roles_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    baseline_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    finalization: Mapped[CasdoorFinalizationState] = mapped_column(
        EnumText(CasdoorFinalizationState, 24),
        nullable=False,
        default=CasdoorFinalizationState.PENDING,
        server_default=sa.text("'pending'"),
    )
    tombstone: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())


class CasdoorSyncIntentExtend(_CasdoorUpdatedFields, Base):
    """Shared durable role/resource/profile outbox with separate termination proof.

    Scope and epoch checks, authorization barriers, and reliable remote completion
    belong to the saga owner; an applied state alone does not finalize access.
    """

    __tablename__ = "casdoor_sync_intent_extend"
    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="casdoor_intent_idempotency_key"),
        _enum_check("kind", CasdoorIntentKind, "kind"),
        _enum_check("operation_state", CasdoorOperationState, "operation_state"),
        _enum_check("termination_state", CasdoorTerminationState, "termination_state"),
        *(
            _nonnegative(field, f"{field}_nonnegative")
            for field in ("generation", "ownership_epoch", "fence_epoch", "attempt_count")
        ),
        sa.Index("casdoor_intent_pending_retry_idx", "operation_state", "retry_at"),
        sa.Index("casdoor_intent_scope_generation_idx", "namespace_id", "scope_digest", "generation"),
        sa.Index("casdoor_intent_membership_idx", "membership_id"),
        sa.Index(
            "casdoor_avatar_initial_scan_idx", "kind", "operation_state", "termination_state", "attempt_count", "id"
        ),
    )

    namespace_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_namespace_extend.id", ondelete="RESTRICT"), nullable=False
    )
    # Historical reference survives a fully fenced, reconciled identity unlink.
    identity_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    account_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    membership_id: Mapped[str | None] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_managed_membership_extend.id", ondelete="RESTRICT"), nullable=True
    )
    revision_id: Mapped[str] = mapped_column(
        StringUUID, sa.ForeignKey("casdoor_config_revision_extend.id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    ownership_epoch: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    fence_epoch: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    kind: Mapped[CasdoorIntentKind] = mapped_column(EnumText(CasdoorIntentKind, 32), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    scope_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    desired_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    operation_state: Mapped[CasdoorOperationState] = mapped_column(
        EnumText(CasdoorOperationState, 24),
        nullable=False,
        default=CasdoorOperationState.PENDING,
        server_default=sa.text("'pending'"),
    )
    termination_state: Mapped[CasdoorTerminationState] = mapped_column(
        EnumText(CasdoorTerminationState, 24),
        nullable=False,
        default=CasdoorTerminationState.NOT_STARTED,
        server_default=sa.text("'not_started'"),
    )
    attempt_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    attempt_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default=sa.text("0"))
    lease_owner: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    readback_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    termination_proof_kind: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    proof_ref: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class CasdoorAuditExtend(_CasdoorCreatedFields, Base):
    """Non-secret lifecycle evidence; references deliberately survive unlink."""

    __tablename__ = "casdoor_audit_extend"
    __table_args__ = (
        sa.Index("casdoor_audit_namespace_created_idx", "namespace_id", "created_at"),
        sa.Index("casdoor_audit_correlation_idx", "correlation_id"),
    )

    namespace_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    revision_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    identity_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    account_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    actor_account_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    result_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    summary_json: Mapped[str] = mapped_column(sa.Text, nullable=False)


class CasdoorAvatarDispatchCursorExtend(Base):
    """Navigation only: one finite UUID sweep, with caller-owned version CAS.

    Idle has neither key. A sweep has an upper key and optionally a last key;
    the keys carry no eligibility, execution, publication or completion proof.
    """

    __tablename__ = "casdoor_avatar_dispatch_cursor_extend"
    __table_args__ = (
        sa.CheckConstraint("slot = 1", name="singleton_slot"),
        sa.CheckConstraint("version >= 0 AND version <= 9223372036854775807", name="version_range"),
        sa.CheckConstraint(
            "(sweep_upper_id IS NULL AND last_id IS NULL) OR "
            "(sweep_upper_id IS NOT NULL AND (last_id IS NULL OR last_id <= sweep_upper_id))",
            name="sweep_shape",
        ),
    )

    slot: Mapped[int] = mapped_column(sa.SmallInteger, primary_key=True, autoincrement=False)
    last_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    sweep_upper_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    version: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default=sa.text("0"))
