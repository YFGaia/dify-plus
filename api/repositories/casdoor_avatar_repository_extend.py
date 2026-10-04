"""Persist pending avatar intents and bounded DB-only worker attempts.

For persist_pending, the actual C1 caller owns prior lock ordering, successful
profile authentication lineage, B3 generation and whole-root rollback. Public
DTOs grant no authority. That pending producer rechecks state and writes only
avatar outbox plus closed audit; it makes no avatar/file write, generation
allocation, commit, lease, dispatch or remote I/O.

The additive worker claim/finish methods reconstruct their own parent locks and
reserve a database lease only, with no Redis lease acquisition or renewal. They
record unknown completion; callers commit or roll back the entire explicit root.
Worker attachment, SSRF/image validation and durable cleanup remain separate work.
"""

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.avatar_termination import _consume_pre_storage
from core.casdoor.claims import VerifiedProfile
from core.casdoor.crypto import CryptoError, EncryptionContext, EncryptionPurpose
from core.casdoor.mapping import (
    MappingError,
    MappingIdentityContext,
    validate_mapping_context,
)
from core.casdoor.request_safety import ProfileNameReason
from extensions.storage.storage_type import StorageType
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
    TenantStatus,
)
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import (
    CasdoorAvatarDispatchCursorExtend as AvatarDispatchCursor,
)
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.enums import CreatorUserRole
from models.model import UploadFile
from pydantic import ValidationError
from services.file_service import (
    AvatarReservation,
    FileService,
    NormalizedAvatar,
    _avatar_valid_normalized,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransactionOrigin

from repositories.casdoor_audit_repository_extend import (
    _AVATAR_PRE_STORAGE_SUMMARY_BYTES,
    CasdoorAuditRepository,
    _AvatarAttachmentAudit,
    _AvatarAttemptAudit,
    _AvatarPendingAudit,
    _AvatarPreStorageAudit,
    _pre_storage_summary_v2,
)
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
    CasdoorConfigurationRepository,
)
from repositories.casdoor_profile_repository_extend import (
    CasdoorProfileConflict,
    ProfilePersistenceOutcome,
    _clock,
    _dump,
    _generation,
    _pairs,
    _parse_time,
    _snapshot,
    _time,
)

MAX_DESIRED_BYTES = 16384
URL_TTL_SECONDS = 300
_SCHEMA_KEYS = {
    "schema_version",
    "namespace_id",
    "revision_id",
    "identity_id",
    "account_id",
    "generation",
    "fence_epoch",
    "auth_started_at",
    "correlation_id",
    "policy",
    "baseline",
    "url_sha256",
    "url_ciphertext",
    "url_created_at",
    "url_expires_at",
    "result_file_id",
    "cleanup_state",
    "reservations",
}
# Future worker contract: at most three immutable per-attempt reservations. No
# arbitrary metadata, source URL, exception text or claims may enter this schema.
_RESERVATION_KEYS = {"attempt_id", "file_id", "storage_key", "cleanup_state"}
_CLEANUP_STATES = {"none", "pending", "complete", "unknown"}


class CasdoorAvatarConflict(ValueError):
    def __init__(self):
        super().__init__("casdoor_avatar_conflict")


@dataclass(frozen=True, repr=False)
class AvatarPendingOutcome:
    intent_id: UUID | None
    reason: str


def _uuid(value: object) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _candidate(value: object) -> str | None:
    """Conservative public HTTPS syntax, without DNS or trust in later redirects."""
    try:
        if (
            type(value) is not str
            or not value
            or len(value.encode("utf-8")) > 4096
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or "\\" in value
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            return None
        url = urlsplit(value)
        host = url.hostname
        decoded = unquote(value, errors="strict")
        if (
            url.scheme != "https"
            or not host
            or "%" in host
            or url.username is not None
            or url.password is not None
            or "#" in value
            or url.port not in (None, 443)
            or len(url.path.encode("utf-8")) > 2048
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or "\\" in decoded
        ):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (
                not host.isascii()
                or len(host) > 253
                or "." not in host
                or host.endswith((".localhost", ".local", ".internal"))
                or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x) for x in host.split("."))
                or host.split(".")[-1].isdigit()
            ):
                return None
        else:
            if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified:
                return None
        return value
    except (ValueError, UnicodeError):
        return None


def _scope(identity_id: UUID) -> str:
    return hashlib.sha256(f"avatar:{identity_id}".encode()).hexdigest()


def _key(identity_id: UUID, generation: int, digest: str) -> str:
    return hashlib.sha256(f"{identity_id}:{generation}:{digest}".encode()).hexdigest()


def _desired(row: Intent) -> dict:
    """Validate canonical closed schema and its immutable column correspondence."""
    try:
        raw = row.desired_json
        if type(raw) is not str or len(raw.encode("utf-8")) > MAX_DESIRED_BYTES:
            raise CasdoorAvatarConflict()
        value = json.loads(raw, object_pairs_hook=_pairs)
        if type(value) is not dict or set(value) != _SCHEMA_KEYS or _dump(value) != raw:
            raise CasdoorAvatarConflict()
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise CasdoorAvatarConflict()
        for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
            if not _uuid(value[field]) or value[field] != getattr(row, field):
                raise CasdoorAvatarConflict()
        if not _uuid(row.id) or not _uuid(value["correlation_id"]):
            raise CasdoorAvatarConflict()
        for field in ("generation", "fence_epoch"):
            if not _generation(value[field]) or value[field] != getattr(row, field):
                raise CasdoorAvatarConflict()
        if (
            row.kind != CasdoorIntentKind.PROFILE_AVATAR
            or row.workspace_id is not None
            or row.membership_id is not None
            or row.resource_type is not None
            or row.ownership_epoch != 0
            or row.scope_digest != _scope(UUID(row.identity_id))
            or type(value["url_sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", value["url_sha256"])
            or row.idempotency_key != _key(UUID(row.identity_id), row.generation, value["url_sha256"])
            or value["policy"] not in ("fill_empty", "managed")
            or (value["baseline"] not in (None, "") and not _uuid(value["baseline"]))
            or (value["result_file_id"] is not None and not _uuid(value["result_file_id"]))
            or value["cleanup_state"] not in _CLEANUP_STATES
            or type(value["url_ciphertext"]) is not str
            or len(value["url_ciphertext"]) > 12000
        ):
            raise CasdoorAvatarConflict()
        created, expires = _parse_time(value["url_created_at"]), _parse_time(value["url_expires_at"])
        if not _parse_time(value["auth_started_at"]) <= created < expires <= created + timedelta(seconds=300):
            raise CasdoorAvatarConflict()
        reservations = value["reservations"]
        if type(reservations) is not list or len(reservations) > 3:
            raise CasdoorAvatarConflict()
        attempts, files = set(), set()
        for item in reservations:
            if (
                type(item) is not dict
                or set(item) != _RESERVATION_KEYS
                or not _uuid(item["attempt_id"])
                or not _uuid(item["file_id"])
                or item["cleanup_state"] not in _CLEANUP_STATES
                or item["storage_key"] != f"casdoor-avatar/{row.id}/{item['attempt_id']}/{item['file_id']}.png"
                or item["attempt_id"] in attempts
                or item["file_id"] in files
            ):
                raise CasdoorAvatarConflict()
            attempts.add(item["attempt_id"])
            files.add(item["file_id"])
        if row.operation_state == CasdoorOperationState.APPLIED:
            if (
                not _uuid(value["result_file_id"])
                or row.resource_id != value["result_file_id"]
                or value["result_file_id"] not in files
                or row.attempt_count != len(reservations)
                or row.termination_state != CasdoorTerminationState.CONFIRMED
            ):
                raise CasdoorAvatarConflict()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise CasdoorAvatarConflict() from None


@dataclass(frozen=True, repr=False)
class _AvatarAttemptRef:
    intent_id: UUID
    attempt_id: UUID
    lease_owner: str


@dataclass(frozen=True, repr=False)
class _AvatarFetchSource:
    namespace_id: UUID
    revision_id: UUID
    intent_id: UUID
    ciphertext: str
    sha256: str
    expires_at: datetime


@dataclass(frozen=True, repr=False)
class _AvatarClaim:
    code: Literal["reserved", "replayed", "not_due", "busy", "closed", "unknown"]
    attempt: _AvatarAttemptRef | None = None
    source: _AvatarFetchSource | None = None
    reservation: AvatarReservation | None = None
    result_file_id: UUID | None = None


@dataclass(frozen=True, repr=False)
class _AvatarAttemptResult:
    code: Literal["unknown"]


_FINISH_REASONS = frozenset(
    {
        "fetch_rejected",
        "fetch_failed",
        "fetch_cancelled",
        "fetch_unknown",
        "image_rejected",
        "storage_unknown",
        "attachment_lost",
        "lease_expired",
        "commit_unknown",
    }
)
_WORKER_BOUNDS = {
    Audit: {"summary_json": _AVATAR_PRE_STORAGE_SUMMARY_BYTES},
    Intent: {"desired_json": MAX_DESIRED_BYTES},
    UploadFile: {"source_url": 0},
    Namespace: {"expected_issuer": 8192},
    Identity: {"issuer": 8192, "subject": 255, "profile_sync_json": 4096, "last_applied_json": 4096},
    Revision: {
        "policy_json": 4096,
        "mappings_json": 1048576,
        "certificates_json": 262144,
        "encrypted_secret": 92160,
        "browser_frontend_url": 8192,
        "backend_api_url": 8192,
        "expected_issuer": 8192,
    },
}


def _worker_time(value):
    if type(value) is not datetime or value.tzinfo is not None:
        raise CasdoorAvatarConflict()
    try:
        return _clock(value.replace(tzinfo=UTC))
    except (ValueError, OverflowError, OSError):
        raise CasdoorAvatarConflict() from None


def _worker_state(row):
    """Supplement frozen v1 desired parsing with exact worker column coherence."""
    try:
        for field in ("generation", "fence_epoch", "ownership_epoch", "attempt_count"):
            if not _generation(row[field]):
                raise CasdoorAvatarConflict()
        data = _desired(SimpleNamespace(**row))
        count = row["attempt_count"]
        if count != len(data["reservations"]) or not 0 <= count <= 3:
            raise CasdoorAvatarConflict()
        state, termination = row["operation_state"], row["termination_state"]
        if state not in set(CasdoorOperationState) or termination not in set(CasdoorTerminationState):
            raise CasdoorAvatarConflict()
        for key in ("lease_expires_at", "sent_at", "acknowledged_at", "readback_at", "terminated_at", "retry_at"):
            if row[key] is not None:
                _worker_time(row[key])
        if count == 0:
            if (
                state != "pending"
                or termination != "not_started"
                or data["cleanup_state"] != "none"
                or any(
                    row[key] is not None
                    for key in (
                        "attempt_id",
                        "lease_owner",
                        "lease_expires_at",
                        "sent_at",
                        "acknowledged_at",
                        "readback_at",
                        "terminated_at",
                        "termination_proof_kind",
                        "proof_ref",
                        "retry_at",
                        "error_code",
                    )
                )
            ):
                raise CasdoorAvatarConflict()
        else:
            if row["attempt_id"] != data["reservations"][-1]["attempt_id"]:
                raise CasdoorAvatarConflict()
            if state != "applied" or row["lease_owner"] is not None:
                if (
                    type(row["lease_owner"]) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", row["lease_owner"])
                    or row["lease_expires_at"] is None
                ):
                    raise CasdoorAvatarConflict()
            elif row["lease_expires_at"] is not None:
                raise CasdoorAvatarConflict()
            if state == "in_flight":
                if (
                    termination != "unconfirmed"
                    or data["cleanup_state"] != "none"
                    or any(item["cleanup_state"] != "none" for item in data["reservations"])
                    or any(
                        row[key] is not None
                        for key in (
                            "retry_at",
                            "terminated_at",
                            "termination_proof_kind",
                            "proof_ref",
                            "error_code",
                            "sent_at",
                            "acknowledged_at",
                            "readback_at",
                        )
                    )
                ):
                    raise CasdoorAvatarConflict()
            elif state == "unknown":
                if (
                    termination != "manual_recovery"
                    or data["cleanup_state"] != "unknown"
                    or data["reservations"][-1]["cleanup_state"] != "unknown"
                    or row["error_code"] not in _FINISH_REASONS
                    or any(
                        row[key] is not None
                        for key in ("retry_at", "terminated_at", "termination_proof_kind", "proof_ref")
                    )
                ):
                    raise CasdoorAvatarConflict()
            elif state in ("pending", "failed", "cancelled"):
                # Recognize but never produce/admit a retry without the later T owner.
                if (
                    termination != "confirmed"
                    or row["terminated_at"] is None
                    or type(row["termination_proof_kind"]) is not str
                    or not 1 <= len(row["termination_proof_kind"]) <= 32
                    or (state == "pending" and row["retry_at"] is None)
                ):
                    raise CasdoorAvatarConflict()
            elif state != "applied":
                raise CasdoorAvatarConflict()
        if state != "applied" and (row["resource_id"] is not None or data["result_file_id"] is not None):
            raise CasdoorAvatarConflict()
        if row["termination_proof_kind"] == "avatar_pre_storage":
            _pre_storage_state(row, data)
        return data
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CasdoorAvatarConflict() from None


def _pre_storage_state(row, data):
    """Validate the exact new terminal shape independently of receipt possession."""
    if (
        row["operation_state"] != "failed"
        or row["termination_state"] != "confirmed"
        or type(row["attempt_count"]) is not int
        or row["attempt_count"] != 1
        or len(data["reservations"]) != 1
        or data["cleanup_state"] != "complete"
        or data["reservations"][0]["cleanup_state"] != "complete"
        or data["reservations"][0]["attempt_id"] != row["attempt_id"]
        or data["result_file_id"] is not None
        or not _uuid(row["proof_ref"])
        or row["error_code"] not in ("fetch_rejected", "fetch_failed", "image_rejected")
        or row["updated_at"] != row["terminated_at"]
        or any(row[key] is not None for key in ("resource_id", "sent_at", "acknowledged_at", "readback_at", "retry_at"))
    ):
        raise CasdoorAvatarConflict()
    terminated = _worker_time(row["terminated_at"])
    if not _parse_time(data["url_created_at"]) <= terminated < _parse_time(data["url_expires_at"]):
        raise CasdoorAvatarConflict()
    if terminated >= _worker_time(row["lease_expires_at"]):
        raise CasdoorAvatarConflict()


# Frozen model-order protocol: adding/reordering a scalar requires explicit review.
_PRE_STORAGE_INTENT_FIELDS = (
    "namespace_id",
    "identity_id",
    "account_id",
    "workspace_id",
    "membership_id",
    "revision_id",
    "generation",
    "ownership_epoch",
    "fence_epoch",
    "kind",
    "resource_type",
    "resource_id",
    "scope_digest",
    "idempotency_key",
    "desired_json",
    "operation_state",
    "termination_state",
    "attempt_id",
    "attempt_count",
    "lease_owner",
    "lease_expires_at",
    "sent_at",
    "acknowledged_at",
    "readback_at",
    "terminated_at",
    "termination_proof_kind",
    "proof_ref",
    "retry_at",
    "error_code",
    "updated_at",
    "id",
    "created_at",
)
_PRE_STORAGE_DIGEST_DOMAIN = b"dify-plus:casdoor:avatar-pre-storage:terminal-intent:v2\x00"


def _pre_storage_intent_digest(row: dict) -> str:
    """Bind every exact SQL scalar with ordered names and explicit value tags."""
    if (
        tuple(Intent.__table__.columns.keys()) != _PRE_STORAGE_INTENT_FIELDS
        or type(row) is not dict
        or set(row) != set(_PRE_STORAGE_INTENT_FIELDS)
    ):
        raise CasdoorAvatarConflict()
    values = []
    for key in _PRE_STORAGE_INTENT_FIELDS:
        value = row[key]
        if value is None:
            tagged = ["null"]
        elif type(value) is bool:
            tagged = ["bool", value]
        elif type(value) is int and -(2**63) <= value <= 2**63 - 1:
            tagged = ["int", value]
        elif type(value) is str:
            tagged = ["str", value]
        elif type(value) is datetime and value.tzinfo is None:
            _worker_time(value)
            tagged = ["datetime", value.isoformat(timespec="microseconds")]
        else:
            raise CasdoorAvatarConflict()
        values.append([key, tagged])
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_PRE_STORAGE_DIGEST_DOMAIN + encoded).hexdigest()


@dataclass(frozen=True, repr=False)
class _AvatarPreStorageRecord:
    intent_id: UUID
    intent_values: tuple
    audit_values: tuple


@dataclass(frozen=True, repr=False)
class _AvatarAttachmentResult:
    code: Literal["attached", "replayed"]
    result_file_id: UUID


@dataclass(frozen=True, repr=False)
class _AvatarAttachmentReconciliation:
    code: Literal["applied", "unknown"]
    result_file_id: UUID | None = None


@dataclass(frozen=True, repr=False)
class _AvatarIOAuthority:
    attempt: _AvatarAttemptRef
    source: _AvatarFetchSource
    reservation: AvatarReservation
    lease_expires_at: datetime


@dataclass(frozen=True, repr=False)
class _AvatarIntentAttachment:
    code: Literal["applied", "not_applied", "unknown"]
    result_file_id: UUID | None = None


_DISPATCH_ABSENT_FIELDS = (
    "attempt_id",
    "lease_owner",
    "lease_expires_at",
    "resource_type",
    "resource_id",
    "sent_at",
    "acknowledged_at",
    "readback_at",
    "terminated_at",
    "termination_proof_kind",
    "proof_ref",
    "retry_at",
    "error_code",
)


@dataclass(frozen=True, repr=False)
class _AvatarDispatchPage:
    intent_ids: tuple[UUID, ...]
    sweep_complete: bool


class CasdoorAvatarRepository:
    def __init__(self, session: Session, *, configuration_repository: CasdoorConfigurationRepository):
        if (
            type(configuration_repository) is not CasdoorConfigurationRepository
            or configuration_repository.session is not session
        ):
            raise CasdoorAvatarConflict()
        self._session = session
        self._configuration_repository = configuration_repository

    def persist_pending(
        self,
        context: MappingIdentityContext,
        profile: VerifiedProfile,
        profile_outcome: ProfilePersistenceOutcome,
        *,
        expected_generation: int,
        expected_fence_epoch: int,
        auth_started_at: datetime,
        correlation_id: UUID,
        now: datetime,
    ) -> AvatarPendingOutcome:
        session = self._session
        if (
            not session.is_active
            or not session.in_transaction()
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise CasdoorAvatarConflict()
        try:
            validate_mapping_context(context)
            started, current = _clock(auth_started_at), _clock(now)
        except (MappingError, CasdoorProfileConflict, TypeError, AttributeError):
            raise CasdoorAvatarConflict() from None
        if (
            type(profile) is not VerifiedProfile
            or profile.subject != context.subject
            or type(profile_outcome) is not ProfilePersistenceOutcome
            or started > current
            or not _generation(expected_generation)
            or not _generation(expected_fence_epoch)
            or type(correlation_id) is not UUID
            or profile_outcome.identity_id != context.identity_id
            or profile_outcome.account_id != context.account_id
            or profile_outcome.generation != expected_generation
        ):
            raise CasdoorAvatarConflict()
        # Parent locks were acquired before B2. Reread DB scalar state, including
        # identity-map parents before the original configuration owner uses get().
        integration = session.execute(
            sa.select(Integration.enabled, Integration.active_revision_id)
            .where(Integration.id == str(context.integration_id), Integration.slot == 1)
            .with_for_update()
        ).one_or_none()
        namespace = session.scalar(
            sa.select(Namespace)
            .where(Namespace.id == str(context.namespace_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        revision = session.scalar(
            sa.select(Revision).where(Revision.id == str(context.revision_id)).execution_options(populate_existing=True)
        )
        if (
            integration is None
            or integration.enabled is not True
            or integration.active_revision_id != str(context.revision_id)
            or namespace is None
            or revision is None
            or namespace.integration_id != str(context.integration_id)
            or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE
            or namespace.fence_epoch != expected_fence_epoch
            or revision.namespace_id != namespace.id
            or revision.config_digest != context.config_digest
        ):
            raise CasdoorAvatarConflict()
        try:
            verified = self._configuration_repository._revision(str(context.integration_id), str(context.revision_id))
            configuration = self._configuration_repository._configuration(verified)
        except (CasdoorConfigurationError, ValidationError, ValueError, TypeError, RecursionError):
            raise CasdoorAvatarConflict() from None
        if any(
            getattr(configuration, field) != getattr(context, target)
            for field, target in (
                ("expected_issuer", "issuer"),
                ("organization", "organization"),
                ("application", "application"),
                ("client_id", "client_id"),
            )
        ):
            raise CasdoorAvatarConflict()
        if configuration.avatar_sync is False:
            return AvatarPendingOutcome(None, "disabled")
        account = session.execute(
            sa.select(Account.avatar, Account.status, Account.initialized_at)
            .where(Account.id == str(context.account_id))
            .with_for_update()
        ).one_or_none()
        identity = session.execute(
            sa.select(*Identity.__table__.columns)
            .where(
                Identity.id == str(context.identity_id),
                Identity.namespace_id == str(context.namespace_id),
                Identity.account_id == str(context.account_id),
                Identity.issuer == context.issuer,
                Identity.organization == context.organization,
                Identity.subject == context.subject,
                Identity.subject_digest == hashlib.sha256(context.subject.encode()).hexdigest(),
                Identity.sync_generation == expected_generation,
            )
            .with_for_update()
        ).one_or_none()
        if (
            account is None
            or account.status != AccountStatus.ACTIVE
            or account.initialized_at is None
            or identity is None
        ):
            raise CasdoorAvatarConflict()
        if profile_outcome.name_reason in (
            ProfileNameReason.STALE_PROFILE_ATTEMPT,
            ProfileNameReason.AMBIGUOUS_PROFILE_ATTEMPT,
        ):
            return AvatarPendingOutcome(None, "profile_not_accepted")
        try:
            sync = _snapshot(identity.profile_sync_json, baseline=False, generation=expected_generation)
            if (
                not sync
                or sync["generation"] != expected_generation
                or _parse_time(sync["auth_started_at"]) != started
                or sync["correlation_id"] != str(correlation_id)
                or _parse_time(sync["last_sync_at"]) != profile_outcome.synced_at
                or _parse_time(sync["last_sync_at"]) > current
            ):
                raise CasdoorAvatarConflict()
        except CasdoorProfileConflict:
            raise CasdoorAvatarConflict() from None
        url = _candidate(profile.picture)
        if url is None:
            return AvatarPendingOutcome(None, "candidate_unavailable")
        digest = hashlib.sha256(url.encode()).hexdigest()
        key = _key(context.identity_id, expected_generation, digest)
        scope = (
            Intent.kind == CasdoorIntentKind.PROFILE_AVATAR,
            Intent.identity_id == str(context.identity_id),
            Intent.namespace_id == str(context.namespace_id),
            Intent.account_id == str(context.account_id),
        )
        latest = session.scalars(
            sa.select(Intent)
            .where(*scope)
            .order_by(Intent.generation.desc(), Intent.id)
            .limit(2)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        existing = session.scalar(
            sa.select(Intent)
            .where(Intent.idempotency_key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if latest:
            data = _desired(latest[0])
            if latest[0].generation > expected_generation or (
                len(latest) > 1 and latest[0].generation == latest[1].generation
            ):
                raise CasdoorAvatarConflict()
            prior = _parse_time(data["auth_started_at"])
            if started < prior or (started == prior and data["correlation_id"] != str(correlation_id)):
                return AvatarPendingOutcome(None, "stale_avatar_attempt")
            if latest[0].generation == expected_generation and latest[0].idempotency_key != key:
                raise CasdoorAvatarConflict()
        baseline = account.avatar
        if baseline not in (None, ""):
            if configuration.avatar_mode == "fill_empty" or not _uuid(baseline):
                return AvatarPendingOutcome(None, "unowned_local_avatar")
            applied = session.scalars(
                sa.select(Intent)
                .where(*scope, Intent.operation_state == CasdoorOperationState.APPLIED)
                .order_by(Intent.generation.desc(), Intent.id)
                .limit(2)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            if not applied or (len(applied) > 1 and applied[0].generation == applied[1].generation):
                return AvatarPendingOutcome(None, "unowned_local_avatar")
            try:
                prior_data = _desired(applied[0])
            except CasdoorAvatarConflict:
                return AvatarPendingOutcome(None, "unowned_local_avatar")
            if prior_data["result_file_id"] != baseline:
                return AvatarPendingOutcome(None, "local_override")
            owned = session.scalar(
                sa.select(UploadFile.id).where(
                    UploadFile.id == baseline,
                    UploadFile.created_by == str(context.account_id),
                    UploadFile.created_by_role == CreatorUserRole.ACCOUNT,
                )
            )
            if owned is None:
                return AvatarPendingOutcome(None, "unowned_local_avatar")
        if existing is not None:
            data = _desired(existing)
            expected = dict(
                namespace_id=str(context.namespace_id),
                revision_id=str(context.revision_id),
                identity_id=str(context.identity_id),
                account_id=str(context.account_id),
                generation=expected_generation,
                fence_epoch=expected_fence_epoch,
                auth_started_at=_time(started),
                correlation_id=str(correlation_id),
                policy=configuration.avatar_mode,
                baseline=baseline,
                url_sha256=digest,
            )
            if (
                any(data[k] != v for k, v in expected.items())
                or existing.operation_state != CasdoorOperationState.PENDING
                or existing.termination_state != CasdoorTerminationState.NOT_STARTED
                or existing.attempt_count != 0
                or existing.resource_id is not None
                or any(
                    getattr(existing, k) is not None
                    for k in (
                        "attempt_id",
                        "lease_owner",
                        "lease_expires_at",
                        "sent_at",
                        "acknowledged_at",
                        "readback_at",
                        "terminated_at",
                        "termination_proof_kind",
                        "proof_ref",
                        "retry_at",
                        "error_code",
                    )
                )
                or data["reservations"]
                or data["result_file_id"] is not None
                or data["cleanup_state"] != "none"
                or _parse_time(data["url_created_at"]) > current
            ):
                raise CasdoorAvatarConflict()
            if _parse_time(data["url_expires_at"]) <= current:
                return AvatarPendingOutcome(None, "expired_url")
            try:
                plaintext = self._configuration_repository.crypto.decrypt(
                    data["url_ciphertext"],
                    context=EncryptionContext(
                        EncryptionPurpose.AVATAR_URL, context.namespace_id, context.revision_id, existing.id
                    ),
                )
            except CryptoError:
                raise CasdoorAvatarConflict() from None
            if plaintext != url:
                raise CasdoorAvatarConflict()
            return AvatarPendingOutcome(UUID(existing.id), "replayed")
        intent_id = uuid4()
        data = dict(
            schema_version=1,
            namespace_id=str(context.namespace_id),
            revision_id=str(context.revision_id),
            identity_id=str(context.identity_id),
            account_id=str(context.account_id),
            generation=expected_generation,
            fence_epoch=expected_fence_epoch,
            auth_started_at=_time(started),
            correlation_id=str(correlation_id),
            policy=configuration.avatar_mode,
            baseline=baseline,
            url_sha256=digest,
            url_ciphertext=self._configuration_repository.crypto.encrypt(
                url,
                context=EncryptionContext(
                    EncryptionPurpose.AVATAR_URL, context.namespace_id, context.revision_id, str(intent_id)
                ),
            ),
            url_created_at=_time(current),
            url_expires_at=_time(current + timedelta(seconds=URL_TTL_SECONDS)),
            result_file_id=None,
            cleanup_state="none",
            reservations=[],
        )
        row = Intent(
            id=str(intent_id),
            namespace_id=str(context.namespace_id),
            revision_id=str(context.revision_id),
            identity_id=str(context.identity_id),
            account_id=str(context.account_id),
            generation=expected_generation,
            fence_epoch=expected_fence_epoch,
            ownership_epoch=0,
            kind=CasdoorIntentKind.PROFILE_AVATAR,
            scope_digest=_scope(context.identity_id),
            idempotency_key=key,
            desired_json=_dump(data),
            operation_state=CasdoorOperationState.PENDING,
            termination_state=CasdoorTerminationState.NOT_STARTED,
        )
        _desired(row)
        session.add(row)
        session.flush()
        CasdoorAuditRepository(session)._append_avatar(
            _AvatarPendingAudit(
                context.namespace_id,
                context.revision_id,
                context.identity_id,
                context.account_id,
                intent_id,
                correlation_id,
                expected_generation,
                expected_fence_epoch,
            )
        )
        return AvatarPendingOutcome(intent_id, "pending")

    def _worker_begin(self, now):
        """No autobegin, savepoint or implicit flush can enter the worker owner."""
        session = self._session
        transaction = session.get_transaction() if isinstance(session, Session) else None
        if (
            transaction is None
            or not transaction.is_active
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or not session.is_active
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise CasdoorAvatarConflict()
        try:
            return _clock(now)
        except (ValueError, TypeError, OverflowError, OSError):
            raise CasdoorAvatarConflict() from None

    def _worker_bounds(self, model, names):
        """SQL byte predicates shared by scalar and ORM fresh reads."""
        columns = model.__table__.columns
        bounds = []
        for name, maximum in _WORKER_BOUNDS.get(model, {}).items():
            if name not in names:
                continue
            column = columns[name]
            length = (
                sa.func.length(sa.cast(column, sa.LargeBinary))
                if self._session.get_bind().dialect.name == "sqlite"
                else sa.func.octet_length(column)
            )
            bounded = length.between(0, maximum)
            bounds.append(sa.or_(column.is_(None), bounded) if column.nullable else bounded)
        return bounds

    def _worker_read(self, model, where, *, fields=None, lock=True):
        """Bound all selected TEXT in SQL before scalar hydration, on every read."""
        columns = model.__table__.columns
        names = tuple(fields) if fields else tuple(columns.keys())
        bounds = self._worker_bounds(model, names)
        if bounds:
            size = self._session.execute(
                sa.select(*(sa.cast(bound, sa.Boolean) for bound in bounds)).where(*where).limit(2)
            ).all()
            if len(size) > 1 or (size and not all(value is True for value in size[0])):
                raise CasdoorAvatarConflict()
        projection = []
        for name in names:
            column = columns[name]
            if isinstance(column.type, sa.Boolean):
                column = (
                    sa.type_coerce(column, sa.Integer)
                    if self._session.get_bind().dialect.name == "sqlite"
                    else sa.cast(column, sa.Integer)
                )
            elif name in (
                "operation_state",
                "termination_state",
                "kind",
                "lifecycle",
                "status",
                "role",
                "created_by_role",
            ):
                column = sa.cast(column, sa.String)
            projection.append(column.label(name))
        statement = sa.select(*projection).where(*where, *bounds).limit(2)
        if lock:
            statement = statement.with_for_update()
        rows = self._session.execute(statement).mappings().all()
        if len(rows) > 1 or (bounds and size and not rows):
            raise CasdoorAvatarConflict()
        return dict(rows[0]) if rows else None

    def _worker_root(self, intent_id, *, lock=True):
        """Discovery grants no authority; lock parents first, then compare pointers."""
        pointer_fields = ("id", "namespace_id", "revision_id", "identity_id", "account_id")
        pointers = self._worker_read(Intent, (Intent.id == str(intent_id),), fields=pointer_fields, lock=False)
        if pointers is None or any(not _uuid(value) for value in pointers.values()):
            raise CasdoorAvatarConflict()
        revision_pointer = self._worker_read(
            Revision,
            (Revision.id == pointers["revision_id"],),
            fields=("id", "integration_id", "namespace_id", "default_workspace_id"),
            lock=False,
        )
        if revision_pointer is not None and any(not _uuid(value) for value in revision_pointer.values()):
            raise CasdoorAvatarConflict()
        integration = None
        if revision_pointer is not None:
            integration = self._worker_read(
                Integration,
                (Integration.id == revision_pointer["integration_id"],),
                fields=("id", "slot", "enabled", "active_revision_id"),
                lock=lock,
            )
        namespace = self._worker_read(Namespace, (Namespace.id == pointers["namespace_id"],), lock=lock)
        account = self._worker_read(
            Account,
            (Account.id == pointers["account_id"],),
            fields=("id", "avatar", "status", "initialized_at"),
            lock=lock,
        )
        identity = self._worker_read(
            Identity,
            (Identity.id == pointers["identity_id"],),
            fields=(
                "id",
                "namespace_id",
                "account_id",
                "issuer",
                "organization",
                "subject",
                "subject_digest",
                "sync_generation",
                "last_applied_json",
                "profile_sync_json",
            ),
            lock=lock,
        )
        tenant, join = None, None
        if revision_pointer is not None:
            tenant = self._worker_read(
                Tenant,
                (Tenant.id == revision_pointer["default_workspace_id"],),
                fields=("id", "status"),
                lock=lock,
            )
            join = self._worker_read(
                TenantAccountJoin,
                (
                    TenantAccountJoin.tenant_id == revision_pointer["default_workspace_id"],
                    TenantAccountJoin.account_id == pointers["account_id"],
                ),
                fields=("id", "tenant_id", "account_id", "role"),
                lock=lock,
            )
        row = self._worker_read(Intent, (Intent.id == str(intent_id),), lock=lock)
        revision = self._worker_read(Revision, (Revision.id == pointers["revision_id"],), lock=False)
        if (
            row is None
            or any(row[key] != value for key, value in pointers.items())
            or (revision_pointer is None) != (revision is None)
            or (revision is not None and any(revision[key] != value for key, value in revision_pointer.items()))
        ):
            raise CasdoorAvatarConflict()
        _worker_state(row)
        return dict(
            intent=row,
            integration=integration,
            namespace=namespace,
            account=account,
            identity=identity,
            revision=revision,
            tenant=tenant,
            join=join,
        )

    def _worker_eligible(self, root, now):
        """Current DB/config/profile authority; references and reason strings prove nothing."""
        row = root["intent"]
        data = _worker_state(row)
        integration, namespace, account, identity, revision, tenant, join = (
            root[key] for key in ("integration", "namespace", "account", "identity", "revision", "tenant", "join")
        )
        if any(value is None for value in (integration, namespace, account, identity, revision, tenant, join)):
            return False
        if (
            type(integration["enabled"]) is not int
            or integration["enabled"] != 1
            or type(integration["slot"]) is not int
            or integration["slot"] != 1
            or integration["active_revision_id"] != row["revision_id"]
            or namespace["integration_id"] != integration["id"]
            or namespace["lifecycle"] != "active"
            or not _generation(namespace["fence_epoch"])
            or namespace["fence_epoch"] != row["fence_epoch"]
            or revision["namespace_id"] != namespace["id"]
            or revision["integration_id"] != integration["id"]
            or type(revision["schema_version"]) is not int
            or revision["schema_version"] != 1
            or not _generation(revision["revision_number"])
            or revision["revision_number"] < 1
            or account["status"] != AccountStatus.ACTIVE
            or account["initialized_at"] is None
            or identity["namespace_id"] != namespace["id"]
            or identity["account_id"] != account["id"]
            or identity["issuer"] != namespace["expected_issuer"]
            or identity["organization"] != namespace["organization"]
            or type(identity["subject"]) is not str
            or not identity["subject"]
            or identity["subject_digest"] != hashlib.sha256(identity["subject"].encode()).hexdigest()
            or not _generation(identity["sync_generation"])
            or identity["sync_generation"] != row["generation"]
            or tenant["status"] != TenantStatus.NORMAL
            or tenant["id"] != revision["default_workspace_id"]
            or not _uuid(join["id"])
            or join["role"] not in set(TenantAccountRole)
            or join["tenant_id"] != tenant["id"]
            or join["account_id"] != account["id"]
            or (row["lease_owner"] is not None and row["lease_owner"][:32] != UUID(join["id"]).hex)
            or account["avatar"] != data["baseline"]
            or not _parse_time(data["url_created_at"]) <= now < _parse_time(data["url_expires_at"])
        ):
            return False
        _worker_time(account["initialized_at"])
        try:
            # Feed the original validation owner only freshly bounded ORM rows.
            # Exact scalar predicates repeat the bounded snapshot at materialization.
            for model, values in ((Namespace, namespace), (Revision, revision)):
                predicates = [getattr(model, key) == value for key, value in values.items()]
                fresh = self._session.scalar(
                    sa.select(model)
                    .where(*predicates, *self._worker_bounds(model, values))
                    .execution_options(populate_existing=True)
                )
                if fresh is None:
                    raise CasdoorAvatarConflict()
                if model is Namespace:
                    fresh_namespace = fresh  # retain it in the identity map for _revision()
                else:
                    fresh_revision = fresh
            verified = self._configuration_repository._revision(integration["id"], fresh_revision.id)
            configuration = self._configuration_repository._configuration(verified)
            if fresh_namespace.id != verified.namespace_id:
                raise CasdoorAvatarConflict()
            sync = _snapshot(identity["profile_sync_json"], baseline=False, generation=row["generation"])
            _snapshot(identity["last_applied_json"], baseline=True, generation=row["generation"])
            if (
                not configuration.avatar_sync
                or configuration.avatar_mode != data["policy"]
                or str(configuration.default_workspace_id) != tenant["id"]
                or not sync
                or sync["generation"] != row["generation"]
                or sync["auth_started_at"] != data["auth_started_at"]
                or sync["correlation_id"] != data["correlation_id"]
                or _parse_time(sync["last_sync_at"]) > now
                or sync["name_reason"] in ("stale_profile_attempt", "ambiguous_profile_attempt")
            ):
                return False
        except (
            CasdoorConfigurationError,
            CasdoorProfileConflict,
            ValidationError,
            ValueError,
            TypeError,
            RecursionError,
        ):
            return False
        latest = self._session.execute(
            sa.select(Intent.id, Intent.generation)
            .where(
                Intent.namespace_id == row["namespace_id"],
                Intent.identity_id == row["identity_id"],
                Intent.account_id == row["account_id"],
                Intent.kind == CasdoorIntentKind.PROFILE_AVATAR,
            )
            .order_by(Intent.generation.desc(), Intent.id)
            .limit(2)
        ).all()
        if (
            not latest
            or latest[0].id != row["id"]
            or any(not _uuid(item.id) or not _generation(item.generation) for item in latest)
            or (len(latest) == 2 and latest[0].generation == latest[1].generation)
        ):
            return False
        if data["baseline"] not in (None, ""):
            if data["policy"] != "managed":
                return False
            prior_ids = self._session.scalars(
                sa.select(Intent.id)
                .where(
                    Intent.namespace_id == row["namespace_id"],
                    Intent.identity_id == row["identity_id"],
                    Intent.account_id == row["account_id"],
                    Intent.kind == CasdoorIntentKind.PROFILE_AVATAR,
                    Intent.operation_state == CasdoorOperationState.APPLIED,
                )
                .order_by(Intent.generation.desc(), Intent.id)
                .limit(2)
            ).all()
            prior = [self._worker_read(Intent, (Intent.id == key,), lock=False) for key in prior_ids]
            if (
                not prior
                or any(item is None for item in prior)
                or (len(prior) == 2 and prior[0]["generation"] == prior[1]["generation"])
            ):
                return False
            if self._worker_applied(prior[0], revision) != data["baseline"]:
                return False
        return True

    def _worker_applied(self, row, revision):
        """Retain an existing APPLIED file even after later config/avatar drift."""
        data = _worker_state(row)
        if revision is None or revision["namespace_id"] != row["namespace_id"]:
            raise CasdoorAvatarConflict()
        reservation = next(item for item in data["reservations"] if item["file_id"] == data["result_file_id"])
        file = self._worker_read(
            UploadFile,
            (UploadFile.id == data["result_file_id"],),
            fields=(
                "id",
                "tenant_id",
                "created_by",
                "created_by_role",
                "key",
                "hash",
                "size",
                "extension",
                "mime_type",
                "source_url",
            ),
            lock=False,
        )
        if (
            file is None
            or file["tenant_id"] != revision["default_workspace_id"]
            or file["created_by"] != row["account_id"]
            or file["created_by_role"] != CreatorUserRole.ACCOUNT
            or file["key"] != reservation["storage_key"]
            or type(file["hash"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", file["hash"])
            or type(file["size"]) is not int
            or not 0 < file["size"] <= 2 * 1024 * 1024
            or file["extension"] != "png"
            or file["mime_type"] != "image/png"
            or file["source_url"] != ""
        ):
            raise CasdoorAvatarConflict()
        return file["id"]

    def _worker_write(self, root, data, *, reason, now, **changes):
        """Business and closed audit flush followed by independent full scalar reread."""
        original = root["intent"]
        serialized = _dump(data)
        if len(serialized.encode()) > MAX_DESIRED_BYTES:
            raise CasdoorAvatarConflict()
        changes["updated_at"] = now.replace(tzinfo=None)
        expected = dict(original, desired_json=serialized, **changes)
        _worker_state(expected)
        statement = (
            sa.update(Intent)
            .where(
                Intent.id == original["id"],
                Intent.desired_json == original["desired_json"],
                Intent.operation_state == original["operation_state"],
                Intent.attempt_id == original["attempt_id"],
                Intent.lease_owner == original["lease_owner"],
            )
            .values(desired_json=serialized, **changes)
            .execution_options(synchronize_session=False)
        )
        if self._session.execute(statement).rowcount != 1:
            raise CasdoorAvatarConflict()
        self._session.flush()
        last = data["reservations"][-1]
        CasdoorAuditRepository(self._session)._append_avatar_attempt(
            _AvatarAttemptAudit(
                namespace_id=UUID(original["namespace_id"]),
                revision_id=UUID(original["revision_id"]),
                identity_id=UUID(original["identity_id"]),
                account_id=UUID(original["account_id"]),
                intent_id=UUID(original["id"]),
                correlation_id=UUID(data["correlation_id"]),
                attempt_id=UUID(last["attempt_id"]),
                file_id=UUID(last["file_id"]),
                generation=original["generation"],
                fence_epoch=original["fence_epoch"],
                count=expected["attempt_count"],
                action="avatar_claim" if reason == "claimed" else "avatar_finish",
                result="reserved" if reason == "claimed" else "unknown",
                reason=reason,
            )
        )
        fresh = self._worker_root(UUID(original["id"]), lock=False)
        if any(fresh[key] != root[key] for key in root if key != "intent"):
            raise CasdoorAvatarConflict()
        if any(fresh["intent"][key] != value for key, value in expected.items()):
            raise CasdoorAvatarConflict()
        if reason == "claimed" and not self._worker_eligible(fresh, now):
            raise CasdoorAvatarConflict()

    def claim_and_reserve(self, intent_id: UUID, *, now: datetime) -> _AvatarClaim:
        """Reserve initial work only; caller commits/releases before any external I/O."""
        current = self._worker_begin(now)
        if type(intent_id) is not UUID:
            raise CasdoorAvatarConflict()
        try:
            with self._session.no_autoflush:
                root = self._worker_root(intent_id)
                row = root["intent"]
                data = _worker_state(row)
                if row["operation_state"] == "applied":
                    return _AvatarClaim("replayed", result_file_id=UUID(self._worker_applied(row, root["revision"])))
                if row["operation_state"] == "unknown":
                    return _AvatarClaim("unknown")
                if row["operation_state"] == "in_flight":
                    if _worker_time(row["lease_expires_at"]) > current and self._worker_eligible(root, current):
                        return _AvatarClaim("busy")
                    self._worker_unknown(root, data, reason="lease_expired", now=current)
                    return _AvatarClaim("unknown")
                if row["operation_state"] != "pending":
                    return _AvatarClaim("closed")
                if row["termination_state"] != "not_started":
                    return _AvatarClaim("not_due")  # T's confirmed pre-storage retry producer is held.
                if not self._worker_eligible(root, current):
                    return _AvatarClaim("closed")
                attempt_id, file_id = uuid4(), uuid4()
                lease = UUID(root["join"]["id"]).hex + uuid4().hex
                key = f"casdoor-avatar/{intent_id}/{attempt_id}/{file_id}.png"
                data["reservations"].append(
                    dict(attempt_id=str(attempt_id), file_id=str(file_id), storage_key=key, cleanup_state="none")
                )
                self._worker_write(
                    root,
                    data,
                    reason="claimed",
                    now=current,
                    attempt_id=str(attempt_id),
                    attempt_count=1,
                    lease_owner=lease,
                    lease_expires_at=(current + timedelta(seconds=60)).replace(tzinfo=None),
                    operation_state=CasdoorOperationState.IN_FLIGHT,
                    termination_state=CasdoorTerminationState.UNCONFIRMED,
                )
                return _AvatarClaim(
                    "reserved",
                    _AvatarAttemptRef(intent_id, attempt_id, lease),
                    _AvatarFetchSource(
                        UUID(row["namespace_id"]),
                        UUID(row["revision_id"]),
                        intent_id,
                        data["url_ciphertext"],
                        data["url_sha256"],
                        _parse_time(data["url_expires_at"]),
                    ),
                    AvatarReservation(
                        str(intent_id),
                        str(attempt_id),
                        str(file_id),
                        row["account_id"],
                        root["revision"]["default_workspace_id"],
                        key,
                    ),
                )
        except (SQLAlchemyError, ValueError, TypeError, OverflowError, RecursionError):
            raise CasdoorAvatarConflict() from None

    def _worker_unknown(self, root, data, *, reason, now):
        data["cleanup_state"] = "unknown"
        data["reservations"][-1]["cleanup_state"] = "unknown"
        self._worker_write(
            root,
            data,
            reason=reason,
            now=now,
            operation_state=CasdoorOperationState.UNKNOWN,
            termination_state=CasdoorTerminationState.MANUAL_RECOVERY,
            error_code=reason,
        )

    def finish_attempt(self, attempt: _AvatarAttemptRef, *, reason: str, now: datetime) -> _AvatarAttemptResult:
        """A reason cannot prove I/O termination: exact claimed work becomes unknown."""
        current = self._worker_begin(now)
        if (
            type(attempt) is not _AvatarAttemptRef
            or type(attempt.intent_id) is not UUID
            or type(attempt.attempt_id) is not UUID
            or type(attempt.lease_owner) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", attempt.lease_owner)
            or type(reason) is not str
            or reason not in _FINISH_REASONS
        ):
            raise CasdoorAvatarConflict()
        try:
            with self._session.no_autoflush:
                root = self._worker_root(attempt.intent_id)
                row = root["intent"]
                data = _worker_state(row)
                if (
                    row["attempt_id"] != str(attempt.attempt_id)
                    or row["lease_owner"] != attempt.lease_owner
                    or row["operation_state"] not in ("in_flight", "unknown")
                ):
                    raise CasdoorAvatarConflict()
                # Recheck surviving authority, but never elevate a failure to cancellation,
                # retry or success. Missing/unlinked parents still permit conservative finish.
                self._worker_eligible(root, current)
                if row["operation_state"] != "unknown":
                    self._worker_unknown(root, data, reason=reason, now=current)
                return _AvatarAttemptResult("unknown")
        except (SQLAlchemyError, ValueError, TypeError, OverflowError, RecursionError):
            raise CasdoorAvatarConflict() from None

    def _attachment_ref(self, attempt):
        if (
            type(attempt) is not _AvatarAttemptRef
            or type(attempt.intent_id) is not UUID
            or type(attempt.attempt_id) is not UUID
            or type(attempt.lease_owner) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", attempt.lease_owner)
        ):
            raise CasdoorAvatarConflict()

    def _attachment_file(self, row, revision, reservation, *, digest=None, size=None):
        """Bound actual file text in SQL before hydration; never infer storage authority."""
        if (
            revision is None
            or revision["id"] != row["revision_id"]
            or revision["namespace_id"] != row["namespace_id"]
            or not _uuid(revision["default_workspace_id"])
        ):
            raise CasdoorAvatarConflict()
        columns = UploadFile.__table__.columns
        expected = dict(
            id=reservation["file_id"],
            tenant_id=revision["default_workspace_id"],
            created_by=row["account_id"],
            used_by=row["account_id"],
        )
        if not all(_uuid(value) for value in expected.values()):
            raise CasdoorAvatarConflict()
        bounds = []
        for key, maximum in dict(
            key=255, name=255, hash=64, extension=255, mime_type=255, storage_type=255, source_url=0, created_by_role=32
        ).items():
            column = columns[key]
            length = (
                sa.func.length(sa.cast(column, sa.LargeBinary))
                if self._session.get_bind().dialect.name == "sqlite"
                else sa.func.octet_length(column)
            )
            bounds.append(length.between(0, maximum))
        where = [columns[key] == value for key, value in expected.items()]
        projection = []
        for column in columns:
            if column.name == "used":
                column = (
                    sa.type_coerce(column, sa.Integer)
                    if self._session.get_bind().dialect.name == "sqlite"
                    else sa.cast(column, sa.Integer)
                )
                column = column.label("used")
            projection.append(column)
        files = self._session.execute(sa.select(*projection).where(*where, *bounds).limit(2)).mappings().all()
        if len(files) != 1:
            raise CasdoorAvatarConflict()
        file = dict(files[0])
        if (
            file["key"] != reservation["storage_key"]
            or file["name"] != "avatar.png"
            or file["extension"] != "png"
            or file["mime_type"] != "image/png"
            or file["created_by_role"] != CreatorUserRole.ACCOUNT
            or file["storage_type"] not in set(StorageType)
            or file["source_url"] != ""
            or type(file["used"]) is not int
            or file["used"] != 1
            or type(file["hash"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", file["hash"])
            or type(file["size"]) is not int
            or not 1 <= file["size"] <= 2097152
            or (digest is not None and file["hash"] != digest)
            or (size is not None and file["size"] != size)
        ):
            raise CasdoorAvatarConflict()
        _worker_time(file["created_at"])
        _worker_time(file["used_at"])
        return file

    def _attachment_applied(self, row, revision, attempt, *, digest=None, size=None):
        """Strict new DB-proof shape, supplemented before and after the frozen reader."""
        self._attachment_ref(attempt)
        data = _worker_state(row)
        if (
            row["id"] != str(attempt.intent_id)
            or row["attempt_id"] != str(attempt.attempt_id)
            or row["lease_owner"] != attempt.lease_owner
            or row["operation_state"] != "applied"
            or row["termination_state"] != "confirmed"
            or not data["reservations"]
            or row["attempt_count"] != len(data["reservations"])
            or data["reservations"][-1]["attempt_id"] != row["attempt_id"]
            or data["result_file_id"] != data["reservations"][-1]["file_id"]
            or row["resource_id"] != data["result_file_id"]
            or row["termination_proof_kind"] != "avatar_attachment_db"
            or row["proof_ref"] != data["result_file_id"]
            or row["readback_at"] != row["terminated_at"]
            or row["updated_at"] != row["terminated_at"]
            or data["cleanup_state"] != "none"
            or any(item["cleanup_state"] != "none" for item in data["reservations"])
            or any(row[key] is not None for key in ("sent_at", "acknowledged_at", "retry_at", "error_code"))
        ):
            raise CasdoorAvatarConflict()
        terminated = _worker_time(row["terminated_at"])
        if not _parse_time(data["url_created_at"]) <= terminated < _worker_time(row["lease_expires_at"]):
            raise CasdoorAvatarConflict()
        if terminated >= _parse_time(data["url_expires_at"]):
            raise CasdoorAvatarConflict()
        reservation = data["reservations"][-1]
        file = self._attachment_file(row, revision, reservation, digest=digest, size=size)
        if self._worker_applied(row, revision) != file["id"]:
            raise CasdoorAvatarConflict()
        if self._attachment_file(row, revision, reservation, digest=digest, size=size) != file:
            raise CasdoorAvatarConflict()
        return file

    def _attachment_prior(self, root, data):
        """Managed baselines require strict provenance, beyond frozen R1 eligibility."""
        if data["baseline"] in (None, ""):
            return
        row = root["intent"]
        ids = self._session.scalars(
            sa.select(Intent.id)
            .where(
                Intent.namespace_id == row["namespace_id"],
                Intent.identity_id == row["identity_id"],
                Intent.account_id == row["account_id"],
                Intent.kind == CasdoorIntentKind.PROFILE_AVATAR,
                Intent.operation_state == CasdoorOperationState.APPLIED,
            )
            .order_by(Intent.generation.desc(), Intent.id)
            .limit(2)
        ).all()
        prior = [self._worker_read(Intent, (Intent.id == key,), lock=False) for key in ids]
        if (
            not prior
            or any(item is None for item in prior)
            or (len(prior) == 2 and prior[0]["generation"] == prior[1]["generation"])
        ):
            raise CasdoorAvatarConflict()
        old = prior[0]
        revision = self._worker_read(Revision, (Revision.id == old["revision_id"],), lock=False)
        attempt = _AvatarAttemptRef(UUID(old["id"]), UUID(old["attempt_id"]), old["lease_owner"])
        file = self._attachment_applied(old, revision, attempt)
        if file["id"] != data["baseline"]:
            raise CasdoorAvatarConflict()
        return old, revision, attempt, file

    def recheck_and_attach(
        self, attempt: _AvatarAttemptRef, normalized: NormalizedAvatar, *, now: datetime
    ) -> _AvatarAttachmentResult:
        """DB attachment only. Future T must prove actual storage readback before calling.

        Any conflict requires caller rollback of the entire explicit root, including
        earlier flushed caller writes. This method never commits or rolls back.
        """
        current = self._worker_begin(now)
        self._attachment_ref(attempt)
        try:
            if not _avatar_valid_normalized(normalized):
                raise CasdoorAvatarConflict()
            with self._session.no_autoflush:
                root = self._worker_root(attempt.intent_id)
                row = root["intent"]
                data = _worker_state(row)
                if row["operation_state"] == "applied":
                    file = self._attachment_applied(
                        row, root["revision"], attempt, digest=normalized.sha3_256, size=len(normalized.content)
                    )
                    return _AvatarAttachmentResult("replayed", UUID(file["id"]))
                if (
                    row["attempt_id"] != str(attempt.attempt_id)
                    or row["lease_owner"] != attempt.lease_owner
                    or row["operation_state"] != "in_flight"
                    or row["termination_state"] != "unconfirmed"
                    or _worker_time(row["lease_expires_at"]) <= current
                ):
                    raise CasdoorAvatarConflict()
                prior = self._attachment_prior(root, data)
                if not self._worker_eligible(root, current):
                    raise CasdoorAvatarConflict()
                last = data["reservations"][-1]
                reservation = AvatarReservation(
                    row["id"],
                    row["attempt_id"],
                    last["file_id"],
                    row["account_id"],
                    root["revision"]["default_workspace_id"],
                    last["storage_key"],
                )
                inserted = FileService.insert_reserved_avatar(self._session, reservation, normalized)
                if (
                    inserted.code != "inserted"
                    or inserted.upload_file is None
                    or inserted.upload_file.id != last["file_id"]
                ):
                    raise CasdoorAvatarConflict()
                file = self._attachment_file(
                    row, root["revision"], last, digest=normalized.sha3_256, size=len(normalized.content)
                )
                timestamp = current.replace(tzinfo=None)
                changed = self._session.execute(
                    sa.update(Account)
                    .where(
                        Account.id == row["account_id"],
                        Account.avatar == data["baseline"],
                    )
                    .values(avatar=last["file_id"], updated_at=timestamp)
                    .execution_options(synchronize_session=False)
                )
                if changed.rowcount != 1:
                    raise CasdoorAvatarConflict()
                data["result_file_id"] = last["file_id"]
                changes = dict(
                    desired_json=_dump(data),
                    operation_state=CasdoorOperationState.APPLIED,
                    termination_state=CasdoorTerminationState.CONFIRMED,
                    resource_id=last["file_id"],
                    termination_proof_kind="avatar_attachment_db",
                    proof_ref=last["file_id"],
                    terminated_at=timestamp,
                    readback_at=timestamp,
                    updated_at=timestamp,
                )
                expected = dict(row, **changes)
                changed = self._session.execute(
                    sa.update(Intent)
                    .where(*(getattr(Intent, key) == value for key, value in row.items()))
                    .values(**changes)
                    .execution_options(synchronize_session=False)
                )
                if changed.rowcount != 1:
                    raise CasdoorAvatarConflict()
                self._session.flush()
                CasdoorAuditRepository(self._session)._append_avatar_attachment(
                    _AvatarAttachmentAudit(
                        namespace_id=UUID(row["namespace_id"]),
                        revision_id=UUID(row["revision_id"]),
                        identity_id=UUID(row["identity_id"]),
                        account_id=UUID(row["account_id"]),
                        intent_id=UUID(row["id"]),
                        correlation_id=UUID(data["correlation_id"]),
                        attempt_id=attempt.attempt_id,
                        file_id=UUID(last["file_id"]),
                        generation=row["generation"],
                        fence_epoch=row["fence_epoch"],
                        count=row["attempt_count"],
                    )
                )
                fresh = self._worker_root(attempt.intent_id, lock=False)
                if any(fresh[key] != root[key] for key in root if key not in ("intent", "account")):
                    raise CasdoorAvatarConflict()
                expected_account = dict(root["account"], avatar=last["file_id"])
                actual_account = self._worker_read(
                    Account, (Account.id == row["account_id"],), fields=(*expected_account, "updated_at"), lock=False
                )
                if (
                    fresh["intent"] != expected
                    or fresh["account"] != expected_account
                    or actual_account != dict(expected_account, updated_at=timestamp)
                ):
                    raise CasdoorAvatarConflict()
                if (
                    self._attachment_applied(
                        fresh["intent"],
                        fresh["revision"],
                        attempt,
                        digest=normalized.sha3_256,
                        size=len(normalized.content),
                    )
                    != file
                ):
                    raise CasdoorAvatarConflict()
                if prior is not None:
                    prior_row, prior_revision, prior_attempt, prior_file = prior
                    reread = self._worker_read(Intent, (Intent.id == prior_row["id"],), lock=False)
                    reread_revision = self._worker_read(Revision, (Revision.id == prior_revision["id"],), lock=False)
                    if reread != prior_row or reread_revision != prior_revision:
                        raise CasdoorAvatarConflict()
                    if self._attachment_applied(reread, reread_revision, prior_attempt) != prior_file:
                        raise CasdoorAvatarConflict()
                return _AvatarAttachmentResult("attached", UUID(last["file_id"]))
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
            RuntimeError,
        ):
            raise CasdoorAvatarConflict() from None

    def reconcile_attachment(
        self,
        attempt: _AvatarAttemptRef,
        *,
        now: datetime,
        expected_sha3_256: str | None = None,
        expected_size: int | None = None,
    ) -> _AvatarAttachmentReconciliation:
        """Read in a fresh caller Session; retain committed ownership despite current drift.

        Unknown grants no result and makes no state change. Caller rolls back failed
        read transactions; no commit, retry, deletion or replacement occurs here.
        """
        self._worker_begin(now)
        try:
            self._attachment_ref(attempt)
            if (expected_sha3_256 is None) != (expected_size is None):
                raise CasdoorAvatarConflict()
            if expected_sha3_256 is not None and (
                type(expected_sha3_256) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha3_256)
                or type(expected_size) is not int
                or not 1 <= expected_size <= 2097152
            ):
                raise CasdoorAvatarConflict()
            with self._session.no_autoflush:
                row = self._worker_read(Intent, (Intent.id == str(attempt.intent_id),), lock=False)
                if row is None:
                    raise CasdoorAvatarConflict()
                revision = self._worker_read(Revision, (Revision.id == row["revision_id"],), lock=False)
                file = self._attachment_applied(row, revision, attempt, digest=expected_sha3_256, size=expected_size)
                if self._worker_read(Intent, (Intent.id == row["id"],), lock=False) != row:
                    raise CasdoorAvatarConflict()
                if self._worker_read(Revision, (Revision.id == row["revision_id"],), lock=False) != revision:
                    raise CasdoorAvatarConflict()
                return _AvatarAttachmentReconciliation("applied", UUID(file["id"]))
        except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            return _AvatarAttachmentReconciliation("unknown")

    def recheck_attempt_for_io(self, attempt: _AvatarAttemptRef, *, now: datetime) -> _AvatarIOAuthority:
        """Return past DB authority only; caller must end this root before any I/O.

        No future lock, cancellation or storage proof is granted. Conflicts require
        caller rollback of the whole root, including earlier flushed caller writes.
        """
        current = self._worker_begin(now)
        self._attachment_ref(attempt)
        try:
            with self._session.no_autoflush:
                root = self._worker_root(attempt.intent_id)
                row = root["intent"]
                data = _worker_state(row)
                if (
                    row["attempt_id"] != str(attempt.attempt_id)
                    or row["lease_owner"] != attempt.lease_owner
                    or row["operation_state"] != "in_flight"
                    or row["termination_state"] != "unconfirmed"
                    or _worker_time(row["lease_expires_at"]) <= current
                ):
                    raise CasdoorAvatarConflict()
                prior = self._attachment_prior(root, data)
                if not self._worker_eligible(root, current):
                    raise CasdoorAvatarConflict()
                last = data["reservations"][-1]
                reservation = AvatarReservation(
                    row["id"],
                    row["attempt_id"],
                    last["file_id"],
                    row["account_id"],
                    root["revision"]["default_workspace_id"],
                    last["storage_key"],
                )
                if self._worker_root(attempt.intent_id, lock=False) != root:
                    raise CasdoorAvatarConflict()
                if prior is not None:
                    prior_row, prior_revision, prior_attempt, prior_file = prior
                    reread = self._worker_read(Intent, (Intent.id == prior_row["id"],), lock=False)
                    reread_revision = self._worker_read(Revision, (Revision.id == prior_revision["id"],), lock=False)
                    if reread != prior_row or reread_revision != prior_revision:
                        raise CasdoorAvatarConflict()
                    if self._attachment_applied(reread, reread_revision, prior_attempt) != prior_file:
                        raise CasdoorAvatarConflict()
                return _AvatarIOAuthority(
                    attempt,
                    _AvatarFetchSource(
                        UUID(row["namespace_id"]),
                        UUID(row["revision_id"]),
                        attempt.intent_id,
                        data["url_ciphertext"],
                        data["url_sha256"],
                        _parse_time(data["url_expires_at"]),
                    ),
                    reservation,
                    _worker_time(row["lease_expires_at"]),
                )
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
            RuntimeError,
        ):
            raise CasdoorAvatarConflict() from None

    def reconcile_intent_attachment(self, intent_id: UUID, *, now: datetime) -> _AvatarIntentAttachment:
        """Strict retained ownership only; nonapplied state never authorizes I/O.

        Historical APPLIED is reconciled by R2 without current eligibility. Caller
        owns this read transaction; ambiguous state grants no retry or replacement.
        """
        self._worker_begin(now)
        try:
            if type(intent_id) is not UUID:
                raise CasdoorAvatarConflict()
            with self._session.no_autoflush:
                row = self._worker_read(Intent, (Intent.id == str(intent_id),), lock=False)
                if row is None:
                    raise CasdoorAvatarConflict()
                _worker_state(row)
                if row["operation_state"] == "applied":
                    attempt = _AvatarAttemptRef(intent_id, UUID(row["attempt_id"]), row["lease_owner"])
                    result = self.reconcile_attachment(attempt, now=now)
                    if result.code != "applied":
                        raise CasdoorAvatarConflict()
                    outcome = _AvatarIntentAttachment("applied", result.result_file_id)
                else:
                    outcome = _AvatarIntentAttachment("not_applied")
                if self._worker_read(Intent, (Intent.id == str(intent_id),), lock=False) != row:
                    raise CasdoorAvatarConflict()
                return outcome
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
            RuntimeError,
        ):
            return _AvatarIntentAttachment("unknown")

    def _dispatch_key_projection(self, column):
        """Do not hydrate an unbounded/corrupt UUID ordering key.

        Compare the original StringUUID column, never the cast: PostgreSQL uses
        native UUID ordering; MySQL/SQLite store canonical lowercase UUID text.
        """
        value = sa.cast(column, sa.String)
        if self._session.get_bind().dialect.name == "sqlite":
            valid = sa.and_(sa.func.typeof(column) == "text", sa.func.length(sa.cast(column, sa.LargeBinary)) == 36)
        else:
            valid = sa.func.octet_length(value) == 36
        return sa.case((valid, value), else_=None)

    def _dispatch_initial_filters(self):
        """Structural navigation only; the original worker owns all authority."""
        return (
            Intent.kind == CasdoorIntentKind.PROFILE_AVATAR,
            Intent.operation_state == CasdoorOperationState.PENDING,
            Intent.termination_state == CasdoorTerminationState.NOT_STARTED,
            Intent.attempt_count == 0,
            *(getattr(Intent, name).is_(None) for name in _DISPATCH_ABSENT_FIELDS),
        )

    def _dispatch_cursor(self):
        """Lock the sole slot and reject damage; never repair or reset corruption."""
        table = AvatarDispatchCursor.__table__
        # SQLite permits oversized TEXT/BLOB in INTEGER columns after corruption.
        # Project only native integers; do not coerce floats or hydrate raw bytes.
        numeric = {}
        for name in ("slot", "version"):
            column = table.c[name]
            if self._session.get_bind().dialect.name == "sqlite":
                column = sa.case((sa.func.typeof(column) == "integer", column), else_=None)
            numeric[name] = column.label(name)
        projection = (
            numeric["slot"],
            numeric["version"],
            table.c.last_id.is_(None).label("last_null"),
            table.c.sweep_upper_id.is_(None).label("upper_null"),
            self._dispatch_key_projection(table.c.last_id).label("last_id"),
            self._dispatch_key_projection(table.c.sweep_upper_id).label("sweep_upper_id"),
        )
        statement = sa.select(*projection).limit(2).with_for_update()
        rows = self._session.execute(statement).mappings().all()
        if not rows:
            # A uniqueness race is a whole-root failure, not an insertion receipt.
            self._session.execute(sa.insert(table).values(slot=1, version=0))
            rows = self._session.execute(statement).mappings().all()
        if len(rows) != 1:
            raise CasdoorAvatarConflict()
        row = dict(rows[0])
        if (
            type(row["slot"]) is not int
            or row["slot"] != 1
            or type(row["version"]) is not int
            or not 0 <= row["version"] <= 9223372036854775807
            or any(
                (row[null] is not True and not _uuid(row[key])) or (row[null] is True and row[key] is not None)
                for key, null in (("last_id", "last_null"), ("sweep_upper_id", "upper_null"))
            )
            or (row["sweep_upper_id"] is None and row["last_id"] is not None)
            or (row["last_id"] is not None and row["last_id"] > row["sweep_upper_id"])
        ):
            raise CasdoorAvatarConflict()
        return {key: row[key] for key in ("slot", "version", "last_id", "sweep_upper_id")}

    def _scan_initial_dispatch_page(self, *, limit: int = 100, now: datetime) -> _AvatarDispatchPage:
        """Advance at most limit examined UUIDs; caller must commit and close.

        Empty tail ends the finite sweep. A following invocation starts the next
        sweep; this operation never performs a second scan or grants execution.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CasdoorAvatarConflict()
        self._worker_begin(now)
        try:
            with self._session.no_autoflush:
                row = self._dispatch_cursor()
                if row["version"] == 9223372036854775807:
                    raise CasdoorAvatarConflict()
                upper, last = row["sweep_upper_id"], row["last_id"]
                filters = self._dispatch_initial_filters()
                key = self._dispatch_key_projection(Intent.id)
                if upper is None:
                    maximum = self._session.execute(
                        sa.select(key).where(*filters).order_by(Intent.id.desc()).limit(1)
                    ).first()
                    if maximum is not None:
                        upper = maximum[0]
                        if not _uuid(upper):
                            raise CasdoorAvatarConflict()
                ids = []
                if upper is not None:
                    bounds = [Intent.id <= upper]
                    if last is not None:
                        bounds.append(Intent.id > last)
                    ids = list(
                        self._session.scalars(sa.select(key).where(*filters, *bounds).order_by(Intent.id).limit(limit))
                    )
                    previous = last
                    for value in ids:
                        if not _uuid(value) or value > upper or (previous is not None and value <= previous):
                            raise CasdoorAvatarConflict()
                        previous = value
                changes = dict(
                    last_id=ids[-1] if ids else None,
                    sweep_upper_id=upper if ids else None,
                    version=row["version"] + 1,
                )
                table = AvatarDispatchCursor.__table__
                changed = self._session.execute(
                    sa.update(table).where(*(table.c[name] == value for name, value in row.items())).values(**changes)
                )
                if changed.rowcount != 1:
                    raise CasdoorAvatarConflict()
                # A trigger or concurrent writer must not replace the expected cursor.
                if self._dispatch_cursor() != dict(row, **changes):
                    raise CasdoorAvatarConflict()
                return _AvatarDispatchPage(tuple(UUID(value) for value in ids), not ids)
        except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise CasdoorAvatarConflict() from None

    def _initial_dispatch_candidate(self, intent_id: UUID, *, now: datetime) -> UUID | None:
        """Read initial eligibility through original owners; never claim or decrypt.

        Returned UUID is a past snapshot only. The consumer must independently
        claim and recheck; the caller must end and close this root before publish.
        """
        current = self._worker_begin(now)
        if type(intent_id) is not UUID:
            return None
        try:
            with self._session.no_autoflush:
                root = self._worker_root(intent_id)
                row = root["intent"]
                data = _worker_state(row)
                if (
                    row["kind"] != "profile_avatar"
                    or row["operation_state"] != "pending"
                    or row["termination_state"] != "not_started"
                    or type(row["attempt_count"]) is not int
                    or row["attempt_count"] != 0
                    or any(row[name] is not None for name in _DISPATCH_ABSENT_FIELDS)
                    or data["reservations"]
                    or data["result_file_id"] is not None
                    or data["cleanup_state"] != "none"
                ):
                    return None
                prior = self._attachment_prior(root, data)
                if not self._worker_eligible(root, current):
                    return None
                if self._worker_root(intent_id, lock=False) != root:
                    return None
                if prior is not None:
                    old, revision, attempt, file = prior
                    if self._worker_read(Intent, (Intent.id == old["id"],), lock=False) != old:
                        return None
                    if self._worker_read(Revision, (Revision.id == revision["id"],), lock=False) != revision:
                        return None
                    if self._attachment_applied(old, revision, attempt) != file:
                        return None
                return intent_id
        except SQLAlchemyError:
            raise CasdoorAvatarConflict() from None
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            return None

    def confirm_pre_storage_failure(self, attempt: _AvatarAttemptRef, capability, *, now: datetime):
        """One normal owner failure before storage; caller owns whole-root outcome.

        Capability consumption is irreversible even if SQL rolls back. It proves
        this invocation never entered storage, never physical object absence.
        """
        current = self._worker_begin(now)
        self._attachment_ref(attempt)
        try:
            binding = _consume_pre_storage(capability, attempt)
            with self._session.no_autoflush:
                root = self._worker_root(attempt.intent_id)
                row = root["intent"]
                data = _worker_state(row)
                if (
                    row["operation_state"] != "in_flight"
                    or row["termination_state"] != "unconfirmed"
                    or row["attempt_id"] != str(attempt.attempt_id)
                    or row["lease_owner"] != attempt.lease_owner
                    or row["attempt_count"] != 1
                    or _worker_time(row["lease_expires_at"]) <= current
                ):
                    raise CasdoorAvatarConflict()
                prior = self._attachment_prior(root, data)
                if not self._worker_eligible(root, current):
                    raise CasdoorAvatarConflict()
                last = data["reservations"][-1]
                actual_reservation = AvatarReservation(
                    row["id"],
                    row["attempt_id"],
                    last["file_id"],
                    row["account_id"],
                    root["revision"]["default_workspace_id"],
                    last["storage_key"],
                )
                actual_source = _AvatarFetchSource(
                    UUID(row["namespace_id"]),
                    UUID(row["revision_id"]),
                    attempt.intent_id,
                    data["url_ciphertext"],
                    data["url_sha256"],
                    _parse_time(data["url_expires_at"]),
                )
                if binding.reservation != actual_reservation or binding.source != actual_source:
                    raise CasdoorAvatarConflict()
                timestamp = current.replace(tzinfo=None)
                data["cleanup_state"] = "complete"
                last["cleanup_state"] = "complete"
                changes = dict(
                    operation_state=CasdoorOperationState.FAILED.value,
                    termination_state=CasdoorTerminationState.CONFIRMED.value,
                    terminated_at=timestamp,
                    termination_proof_kind="avatar_pre_storage",
                    proof_ref=str(binding.proof_ref),
                    error_code=binding.reason,
                    updated_at=timestamp,
                    desired_json=_dump(data),
                )
                expected = dict(row, **changes)
                _worker_state(expected)
                digest = _pre_storage_intent_digest(expected)
                changed = self._session.execute(
                    sa.update(Intent)
                    .where(*(getattr(Intent, key) == value for key, value in row.items()))
                    .values(**changes)
                    .execution_options(synchronize_session=False)
                )
                if changed.rowcount != 1:
                    raise CasdoorAvatarConflict()
                self._session.flush()
                audit = CasdoorAuditRepository(self._session)._append_avatar_pre_storage(
                    _AvatarPreStorageAudit(
                        namespace_id=UUID(row["namespace_id"]),
                        revision_id=UUID(row["revision_id"]),
                        identity_id=UUID(row["identity_id"]),
                        account_id=UUID(row["account_id"]),
                        intent_id=attempt.intent_id,
                        correlation_id=UUID(data["correlation_id"]),
                        attempt_id=attempt.attempt_id,
                        file_id=UUID(last["file_id"]),
                        generation=row["generation"],
                        fence_epoch=row["fence_epoch"],
                        count=row["attempt_count"],
                        reason=binding.reason,
                        proof_ref=binding.proof_ref,
                        terminal_intent_sha256=digest,
                    )
                )
                audit_values = tuple((column.name, getattr(audit, column.name)) for column in Audit.__table__.columns)
                fresh = self._worker_root(attempt.intent_id, lock=False)
                if fresh["intent"] != expected or any(fresh[key] != root[key] for key in root if key != "intent"):
                    raise CasdoorAvatarConflict()
                if prior is not None:
                    old, revision, old_attempt, file = prior
                    if self._worker_read(Intent, (Intent.id == old["id"],), lock=False) != old:
                        raise CasdoorAvatarConflict()
                    if self._worker_read(Revision, (Revision.id == revision["id"],), lock=False) != revision:
                        raise CasdoorAvatarConflict()
                    if self._attachment_applied(old, revision, old_attempt) != file:
                        raise CasdoorAvatarConflict()
                result = _AvatarPreStorageRecord(attempt.intent_id, tuple(expected.items()), audit_values)
                if not self.reconcile_pre_storage_failure(result, now=now):
                    raise CasdoorAvatarConflict()
                return result
        except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            raise CasdoorAvatarConflict() from None

    def reconcile_pre_storage_failure(self, record, *, now: datetime) -> bool:
        """Exact retained SQL proof only; no retry, cleanup, storage or new grant."""
        self._worker_begin(now)
        try:
            if type(record) is not _AvatarPreStorageRecord or type(record.intent_id) is not UUID:
                return False
            row = self._worker_read(Intent, (Intent.id == str(record.intent_id),), lock=False)
            if row is None:
                return False
            _worker_state(row)
            if row["termination_proof_kind"] != "avatar_pre_storage" or tuple(row.items()) != record.intent_values:
                return False
            # Equality predicates bound the expected audit without hydrating corrupt TEXT.
            audit = self._session.execute(
                sa.select(Audit.id)
                .where(*(getattr(Audit, key) == value for key, value in record.audit_values))
                .limit(2)
            ).all()
            if len(audit) != 1:
                return False
            return self._worker_read(Intent, (Intent.id == str(record.intent_id),), lock=False) == row
        except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            return False

    def recover_pre_storage_failure(self, intent_id: UUID, *, now: datetime) -> _AvatarPreStorageRecord | None:
        """Fresh-root SQL-only v2 evidence recovery; unknown never creates proof.

        The record retains the original invocation's terminal evidence only. It
        grants no current eligibility, physical absence, cleanup or retry right.
        """
        try:
            self._worker_begin(now)
            transaction = self._session.get_transaction()
            # api/uv.lock pins SQLAlchemy 2.0.49: reject any previously enlisted
            # connection before the first SQL, including earlier plain SELECTs.
            connections = getattr(transaction, "_connections", None)
            if type(intent_id) is not UUID or type(connections) is not dict or connections:
                return None
            with self._session.no_autoflush:
                root = self._worker_root(intent_id, lock=False)
                row = root["intent"]
                data = _worker_state(row)
                if row["termination_proof_kind"] != "avatar_pre_storage":
                    return None
                _pre_storage_state(row, data)
                # Correlation is indexed. Enumerate IDs only, before loading any
                # summary; duplicate candidates fail even if one has bad scalars.
                candidate_query = (
                    sa.select(Audit.id)
                    .where(Audit.correlation_id == data["correlation_id"], Audit.action == "avatar_pre_storage")
                    .order_by(Audit.id)
                    .limit(2)
                )
                candidates = self._session.execute(candidate_query).scalars().all()
                if len(candidates) != 1:
                    return None
                audit = self._worker_read(Audit, (Audit.id == candidates[0],), lock=False)
                if audit is None or not _uuid(audit["id"]):
                    return None
                _worker_time(audit["created_at"])
                summary = _pre_storage_summary_v2(audit["summary_json"])
                expected_refs = {key: row[key] for key in ("namespace_id", "revision_id", "identity_id", "account_id")}
                expected_refs.update(
                    intent_id=row["id"], attempt_id=row["attempt_id"], file_id=data["reservations"][0]["file_id"]
                )
                if (
                    any(audit[key] != row[key] for key in ("namespace_id", "revision_id", "identity_id", "account_id"))
                    or audit["actor_account_id"] is not None
                    or audit["action"] != "avatar_pre_storage"
                    or audit["result_code"] != "failed"
                    or audit["correlation_id"] != data["correlation_id"]
                    or summary["references"] != expected_refs
                    or summary["proof_ref"] != row["proof_ref"]
                    or summary["reason"] != row["error_code"]
                    or summary["count"] != row["attempt_count"]
                    or summary["generation"] != row["generation"]
                    or summary["fence_epoch"] != row["fence_epoch"]
                    or summary["terminal_intent_sha256"] != _pre_storage_intent_digest(row)
                ):
                    return None
                record = _AvatarPreStorageRecord(intent_id, tuple(row.items()), tuple(audit.items()))
                if not self.reconcile_pre_storage_failure(record, now=now):
                    return None
                # Recheck uniqueness after the exact retained-row reconciler;
                # it deliberately matches one audit ID, not the candidate set.
                if self._session.execute(candidate_query).scalars().all() != candidates:
                    return None
                return record
        except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            return None
