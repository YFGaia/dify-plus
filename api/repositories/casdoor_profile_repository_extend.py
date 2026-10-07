"""Bounded local profile writes in an already locked, caller-owned root UoW.

The real caller must have acquired integration -> ALL namespaces -> account /
identity -> ALL workspace parents before B2 writes, flushed B2/B3, and must reuse
B3's allocated generation. This owner only rechecks parents; it cannot establish
prior lock ordering, consumed-auth provenance or admission authorization. In
particular CREATE baseline adoption trusts the actual same-root B2 call stack,
not the publicly constructible AdmissionPlan. Failure requires whole-root caller
rollback. No commit, retries, remote I/O, role graph, avatar or session work.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.orm import Session

from core.casdoor.admission import AdmissionAction, AdmissionContext, AdmissionPlan
from core.casdoor.auth_transactions import AuthMode
from core.casdoor.claims import VerifiedProfile
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.mapping import (
    MappingError,
    MappingIdentityContext,
    validate_mapping_context,
)
from core.casdoor.request_safety import (
    LocalReferences,
    ProfileAuditAction,
    ProfileAuditEvent,
    ProfileAuditResult,
    ProfileAuditSummary,
    ProfileNameReason,
)
from libs.helper import email as validate_email
from models.account import Account, AccountStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
)
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
    CasdoorConfigurationRepository,
)
from repositories.casdoor_generation_repository_extend import MAX_GENERATION
from services.account_email import normalize_email
from services.entities.account_activation_entities import AccountSetup

MAX_SNAPSHOT_BYTES = 4096


class CasdoorProfileConflict(ValueError):  # noqa: N818 - matches existing Casdoor repository conflicts
    code = CasdoorErrorCode.CONFIG_CONFLICT

    def __init__(self) -> None:
        super().__init__(self.code.value)


class RemoteEmailStatus(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, repr=False)
class ProfilePersistenceOutcome:
    identity_id: UUID
    account_id: UUID
    generation: int
    name_status: ProfileAuditResult
    name_reason: ProfileNameReason
    remote_email_status: RemoteEmailStatus
    remote_email_differs: bool
    snapshot_changed: bool
    synced_at: datetime


def _name(value: object) -> str | None:
    if value is None:
        return None
    try:
        if (
            type(value) is not str
            or len(value.encode("utf-8")) > 255
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise CasdoorProfileConflict()
    except UnicodeError:
        raise CasdoorProfileConflict() from None
    return value if value.strip() else None


def _clock(value: object) -> datetime:
    if type(value) is not datetime or value.utcoffset() != timedelta(0) or not math.isfinite(value.timestamp()):
        raise CasdoorProfileConflict()
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise CasdoorProfileConflict()
    try:
        parsed = _clock(datetime.fromisoformat(value))
        if _time(parsed) != value:
            raise CasdoorProfileConflict()
        return parsed
    except ValueError:
        raise CasdoorProfileConflict() from None


def _generation(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_GENERATION


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CasdoorProfileConflict()
        result[key] = value
    return result


def _dump(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _snapshot(raw: object, *, baseline: bool, generation: int) -> dict:
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise CasdoorProfileConflict()
        value = json.loads(raw, object_pairs_hook=_pairs)
        if type(value) is not dict or any(type(v) not in (str, int, bool) for v in value.values()):
            raise CasdoorProfileConflict()
        if value == {}:
            return value
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise CasdoorProfileConflict()
        if baseline:
            if set(value) not in (
                {"schema_version"},
                {"schema_version", "name", "name_generation"},
            ):
                raise CasdoorProfileConflict()
            if "name" in value and (
                _name(value["name"]) is None
                or not _generation(value["name_generation"])
                or value["name_generation"] > generation
            ):
                raise CasdoorProfileConflict()
        else:
            if set(value) != {
                "schema_version",
                "generation",
                "auth_started_at",
                "correlation_id",
                "last_sync_at",
                "name_status",
                "name_reason",
                "remote_email_status",
                "remote_email_differs",
            }:
                raise CasdoorProfileConflict()
            if (
                not _generation(value["generation"])
                or value["generation"] > generation
                or type(value["remote_email_differs"]) is not bool
                or type(value["correlation_id"]) is not str
                or str(UUID(value["correlation_id"])) != value["correlation_id"]
                or _parse_time(value["auth_started_at"]) > _parse_time(value["last_sync_at"])
            ):
                raise CasdoorProfileConflict()
            ProfileAuditResult(value["name_status"])
            ProfileNameReason(value["name_reason"])
            RemoteEmailStatus(value["remote_email_status"])
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CasdoorProfileConflict() from None


class CasdoorProfileRepository:
    def __init__(
        self,
        session: Session,
        *,
        configuration_repository: CasdoorConfigurationRepository,
    ) -> None:
        if (
            type(configuration_repository) is not CasdoorConfigurationRepository
            or configuration_repository.session is not session
        ):
            raise CasdoorProfileConflict()
        self._session = session
        self._configuration_repository = configuration_repository

    def persist(
        self,
        context: MappingIdentityContext,
        profile: VerifiedProfile,
        *,
        expected_generation: int,
        expected_fence_epoch: int,
        auth_started_at: datetime,
        admission: AdmissionPlan,
        correlation_id: UUID,
        now: datetime,
    ) -> ProfilePersistenceOutcome:
        session = self._session
        if not session.is_active or not session.in_transaction() or session.in_nested_transaction():
            raise RuntimeError("Casdoor profile requires an active caller-owned root transaction")
        if session.new or session.dirty or session.deleted:
            raise CasdoorProfileConflict()
        try:
            validate_mapping_context(context)
        except (MappingError, TypeError, AttributeError):
            raise CasdoorProfileConflict() from None
        if (
            type(profile) is not VerifiedProfile
            or type(profile.subject) is not str
            or profile.subject != context.subject
            or (profile.email_verified is not None and type(profile.email_verified) is not bool)
            or not _generation(expected_generation)
            or not _generation(expected_fence_epoch)
            or type(correlation_id) is not UUID
            or type(admission) is not AdmissionPlan
            or type(admission.context) is not AdmissionContext
            or type(admission.mode) is not AuthMode
            or type(admission.action) is not AdmissionAction
            or admission.action is AdmissionAction.NO_ACCOUNT_ADMISSION
            or admission.mode not in (AuthMode.LOGIN, AuthMode.LINK)
        ):
            raise CasdoorProfileConflict()
        started, current = _clock(auth_started_at), _clock(now)
        if started > current:
            raise CasdoorProfileConflict()
        remote_name = _name(profile.name)
        ac = admission.context
        if (
            any(
                getattr(ac, k) != getattr(context, k)
                for k in (
                    "integration_id",
                    "revision_id",
                    "namespace_id",
                    "config_digest",
                    "issuer",
                    "organization",
                    "application",
                    "client_id",
                    "subject",
                )
            )
            or ac.active_revision_id != context.revision_id
            or ac.namespace_lifecycle is not CasdoorNamespaceLifecycle.ACTIVE
            or type(ac.fence_epoch) is not int
            or ac.fence_epoch != expected_fence_epoch
            or (
                admission.action is AdmissionAction.CREATE_INITIALIZED
                and (
                    admission.mode is not AuthMode.LOGIN
                    or admission.account_id is not None
                    or type(admission.setup) is not AccountSetup
                )
            )
            or (
                admission.action is not AdmissionAction.CREATE_INITIALIZED
                and admission.account_id != context.account_id
            )
        ):
            raise CasdoorProfileConflict()
        with session.no_autoflush:
            integration = session.execute(
                sa.select(
                    CasdoorIntegrationExtend.enabled,
                    CasdoorIntegrationExtend.active_revision_id,
                )
                .where(
                    CasdoorIntegrationExtend.id == str(context.integration_id),
                    CasdoorIntegrationExtend.slot == 1,
                )
                .with_for_update()
            ).one_or_none()
            if (
                integration is None
                or integration.enabled is not True
                or integration.active_revision_id != str(context.revision_id)
            ):
                raise CasdoorProfileConflict()
            # Refresh identity-map parents BEFORE the original configuration owner
            # uses session.get. Retain strong refs through its validation chain.
            namespace = session.scalar(
                sa.select(CasdoorNamespaceExtend)
                .where(CasdoorNamespaceExtend.id == str(context.namespace_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            revision = session.scalar(
                sa.select(CasdoorConfigRevisionExtend)
                .where(CasdoorConfigRevisionExtend.id == str(context.revision_id))
                .execution_options(populate_existing=True)
            )
            if (
                namespace is None
                or revision is None
                or namespace.integration_id != str(context.integration_id)
                or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE
                or namespace.fence_epoch != expected_fence_epoch
                or revision.namespace_id != namespace.id
                or revision.config_digest != context.config_digest
            ):
                raise CasdoorProfileConflict()
            try:
                verified_revision = self._configuration_repository._revision(
                    str(context.integration_id), str(context.revision_id)
                )
                configuration = self._configuration_repository._configuration(verified_revision)
            except (
                CasdoorConfigurationError,
                ValidationError,
                ValueError,
                TypeError,
                RecursionError,
            ):
                raise CasdoorProfileConflict() from None
            if any(
                getattr(configuration, k) != getattr(context, c)
                for k, c in (
                    ("expected_issuer", "issuer"),
                    ("organization", "organization"),
                    ("application", "application"),
                    ("client_id", "client_id"),
                )
            ):
                raise CasdoorProfileConflict()
            account = session.execute(
                sa.select(
                    Account.id,
                    Account.name,
                    Account.email,
                    Account.status,
                    Account.initialized_at,
                )
                .where(Account.id == str(context.account_id))
                .with_for_update()
            ).one_or_none()
            if account is None or account.status != AccountStatus.ACTIVE or account.initialized_at is None:
                raise CasdoorProfileConflict()
            if admission.action is AdmissionAction.CREATE_INITIALIZED and (
                admission.creation_email != account.email or admission.setup.name != account.name
            ):
                raise CasdoorProfileConflict()
            digest = hashlib.sha256(context.subject.encode("utf-8")).hexdigest()
            identity_where = (
                CasdoorIdentityExtend.id == str(context.identity_id),
                CasdoorIdentityExtend.namespace_id == str(context.namespace_id),
                CasdoorIdentityExtend.account_id == str(context.account_id),
                CasdoorIdentityExtend.issuer == context.issuer,
                CasdoorIdentityExtend.organization == context.organization,
                CasdoorIdentityExtend.subject == context.subject,
                CasdoorIdentityExtend.subject_digest == digest,
                CasdoorIdentityExtend.sync_generation == expected_generation,
            )
            identity = session.execute(
                sa.select(*CasdoorIdentityExtend.__table__.columns).where(*identity_where).with_for_update()
            ).one_or_none()
            if (
                identity is None
                or type(identity.sync_generation) is not int
                or identity.sync_generation != expected_generation
                or identity.id != str(context.identity_id)
                or identity.namespace_id != str(context.namespace_id)
                or identity.account_id != str(context.account_id)
                or identity.issuer != context.issuer
                or identity.organization != context.organization
                or identity.subject != context.subject
                or identity.subject_digest != digest
            ):
                raise CasdoorProfileConflict()
            baseline = _snapshot(
                identity.last_applied_json,
                baseline=True,
                generation=expected_generation,
            )
            sync = _snapshot(
                identity.profile_sync_json,
                baseline=False,
                generation=expected_generation,
            )
            if identity.remote_profile_version is not None and (
                type(identity.remote_profile_version) is not str
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity.remote_profile_version)
            ):
                raise CasdoorProfileConflict()
            remote_email, verified = identity.remote_email, None
            email_status = RemoteEmailStatus.UNAVAILABLE
            differs = False
            if profile.email is not None:
                try:
                    if type(profile.email) is not str or len(profile.email.encode("utf-8")) > 254:
                        raise ValueError
                    remote_email = validate_email(profile.email)
                    verified = profile.email_verified
                    differs = normalize_email(remote_email) != normalize_email(account.email)
                    email_status = RemoteEmailStatus.DIFFERENT if differs else RemoteEmailStatus.SAME
                except (ValueError, TypeError, UnicodeError):
                    remote_email, verified = identity.remote_email, None
                    email_status = RemoteEmailStatus.INVALID
            content = _dump(
                {
                    "name": remote_name,
                    "email": remote_email
                    if email_status in (RemoteEmailStatus.SAME, RemoteEmailStatus.DIFFERENT)
                    else None,
                    "email_verified": verified,
                }
            )
            version = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            status, reason = (
                ProfileAuditResult.SKIPPED,
                ProfileNameReason.EMPTY_REMOTE_NAME,
            )
            target_name = account.name
            next_baseline = baseline.copy()
            if configuration.name_sync == "off":
                status, reason = ProfileAuditResult.DISABLED, ProfileNameReason.DISABLED
            elif remote_name is None:
                pass
            elif (
                admission.action is AdmissionAction.CREATE_INITIALIZED
                and remote_name == account.name
                and "name" not in baseline
            ):
                status, reason = (
                    ProfileAuditResult.APPLIED,
                    ProfileNameReason.CREATED_BASELINE,
                )
                next_baseline = {
                    "schema_version": 1,
                    "name": remote_name,
                    "name_generation": expected_generation,
                }
            elif "name" in baseline and account.name != baseline["name"]:
                status, reason = (
                    ProfileAuditResult.LOCAL_OVERRIDE,
                    ProfileNameReason.LOCAL_OVERRIDE,
                )
            elif not account.name.strip():
                target_name = remote_name
                status, reason = (
                    ProfileAuditResult.APPLIED,
                    ProfileNameReason.FILLED_EMPTY,
                )
            elif configuration.name_sync == "fill_empty":
                reason = ProfileNameReason.LOCAL_NAME_PRESENT
            elif "name" not in baseline:
                reason = ProfileNameReason.UNOWNED_LOCAL_NAME
            elif remote_name == account.name:
                status, reason = (
                    ProfileAuditResult.UNCHANGED,
                    ProfileNameReason.SAME_NAME,
                )
            else:
                target_name = remote_name
                status, reason = (
                    ProfileAuditResult.APPLIED,
                    ProfileNameReason.MANAGED_UPDATE,
                )
            if target_name != account.name:
                next_baseline = {
                    "schema_version": 1,
                    "name": target_name,
                    "name_generation": expected_generation,
                }
            if sync:
                previous = _parse_time(sync["auth_started_at"])
                if _parse_time(sync["last_sync_at"]) > current:
                    raise CasdoorProfileConflict()
                late_reason = None
                if started < previous:
                    late_reason = ProfileNameReason.STALE_PROFILE_ATTEMPT
                elif started == previous and (
                    sync["correlation_id"] != str(correlation_id) or version != identity.remote_profile_version
                ):
                    late_reason = ProfileNameReason.AMBIGUOUS_PROFILE_ATTEMPT
                if late_reason is not None:
                    return ProfilePersistenceOutcome(
                        context.identity_id,
                        context.account_id,
                        expected_generation,
                        ProfileAuditResult.SKIPPED,
                        late_reason,
                        email_status,
                        differs,
                        False,
                        _parse_time(sync["last_sync_at"]),
                    )
                # Exact reentry may retain the earlier applied diagnostic. A
                # changed local value ALWAYS wins over this replay optimization.
                if (
                    started == previous
                    and sync["generation"] == expected_generation
                    and version == identity.remote_profile_version
                    and target_name == account.name
                    and baseline == next_baseline
                    and (
                        status is ProfileAuditResult.UNCHANGED
                        or (status is ProfileAuditResult.SKIPPED and reason is ProfileNameReason.LOCAL_NAME_PRESENT)
                    )
                    and sync["name_status"] == ProfileAuditResult.APPLIED.value
                    and baseline.get("name") == account.name == remote_name
                ):
                    status, reason = (
                        ProfileAuditResult(sync["name_status"]),
                        ProfileNameReason(sync["name_reason"]),
                    )
            next_sync = {
                "schema_version": 1,
                "generation": expected_generation,
                "auth_started_at": _time(started),
                "correlation_id": str(correlation_id),
                "last_sync_at": _time(current),
                "name_status": status.value,
                "name_reason": reason.value,
                "remote_email_status": email_status.value,
                "remote_email_differs": differs,
            }
            if sync and all(sync[k] == v for k, v in next_sync.items() if k != "last_sync_at"):
                next_sync["last_sync_at"] = sync["last_sync_at"]
            changed = (
                next_sync != sync
                or next_baseline != baseline
                or version != identity.remote_profile_version
                or remote_email != identity.remote_email
                or verified != identity.email_verified
            )
            if changed:
                if target_name != account.name:
                    result = session.execute(
                        sa.update(Account)
                        .where(
                            Account.id == str(context.account_id),
                            Account.name == account.name,
                            Account.status == AccountStatus.ACTIVE,
                            Account.initialized_at == account.initialized_at,
                        )
                        .values(name=target_name)
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount != 1:
                        raise CasdoorProfileConflict()
                result = session.execute(
                    sa.update(CasdoorIdentityExtend)
                    .where(
                        *identity_where,
                        CasdoorIdentityExtend.last_applied_json == identity.last_applied_json,
                        CasdoorIdentityExtend.profile_sync_json == identity.profile_sync_json,
                        CasdoorIdentityExtend.remote_profile_version == identity.remote_profile_version,
                        CasdoorIdentityExtend.remote_email == identity.remote_email,
                        CasdoorIdentityExtend.email_verified == identity.email_verified,
                    )
                    .values(
                        last_applied_json=_dump(next_baseline),
                        profile_sync_json=_dump(next_sync),
                        remote_profile_version=version,
                        remote_email=remote_email,
                        email_verified=verified,
                        last_seen_at=current.replace(tzinfo=None),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise CasdoorProfileConflict()
                session.flush()
                CasdoorAuditRepository(session).append_profile(
                    ProfileAuditEvent(
                        ProfileAuditAction.PROFILE_SYNC,
                        status,
                        correlation_id,
                        LocalReferences(
                            namespace_id=context.namespace_id,
                            revision_id=context.revision_id,
                            identity_id=context.identity_id,
                            account_id=context.account_id,
                        ),
                        ProfileAuditSummary(
                            expected_generation,
                            status,
                            reason,
                            remote_email != identity.remote_email or verified != identity.email_verified,
                            differs,
                        ),
                    )
                )
            return ProfilePersistenceOutcome(
                context.identity_id,
                context.account_id,
                expected_generation,
                status,
                reason,
                email_status,
                differs,
                changed,
                _parse_time(next_sync["last_sync_at"]),
            )
