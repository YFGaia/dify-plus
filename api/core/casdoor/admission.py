"""Pure organization admission decisions, never authentication or persistence.

Every input is a server-owned observation of a bounded attempt. Public frozen
types, matching identifiers and token dates prove only shape/consistency. I18-B/C
must reconstruct current DB/configuration/binding/invitation/source owners after
complete sorted leases, verify actual signed claims and fresh online state/graph,
and recheck fences before commit. I19 owns invitation/permission finalization and
session issuance. This module performs none of those operations.
"""

import math
import re
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from constants.languages import get_valid_language, language_timezone_mapping, supported_language
from libs.helper import email as validate_email
from libs.helper import timezone as validate_timezone
from models.account import AccountStatus
from models.casdoor_extend import CasdoorNamespaceLifecycle
from services.account_email import normalize_email
from services.entities.account_activation_entities import AccountSetup

from core.casdoor.auth_transactions import AuthMode, SourceSessionContext
from core.casdoor.claims import (
    StructuredUserRef,
    VerifiedIDToken,
    VerifiedNativeAccessToken,
    VerifiedOnlineUser,
    VerifiedProfile,
    VerifiedTokenBundle,
)
from core.casdoor.errors import CasdoorErrorCode


class AdmissionError(ValueError):
    """Stable, non-enumerating failure; never include provider/account input."""

    def __init__(self, code: CasdoorErrorCode = CasdoorErrorCode.IDENTITY_CONFLICT) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, repr=False)
class AdmissionContext:
    """Current owner-chain projection, not evidence that config is active."""

    integration_id: UUID
    revision_id: UUID
    active_revision_id: UUID
    namespace_id: UUID
    namespace_lifecycle: CasdoorNamespaceLifecycle
    fence_epoch: int
    config_digest: str
    issuer: str
    organization: str
    application: str
    client_id: str
    subject: str


@dataclass(frozen=True, repr=False)
class AccountObservation:
    """Exact current account and reverse binding in the context namespace.

    namespace_subject=None means the caller actually found no reverse binding,
    not that a request omitted it. No local profile fields are synchronization
    targets. initialized_at is observed separately from the model's ACTIVE default.
    """

    account_id: UUID
    status: AccountStatus
    initialized_at: datetime | None
    email: str
    namespace_subject: str | None = None


@dataclass(frozen=True, repr=False)
class ExactBindingObservation:
    namespace_id: UUID
    issuer: str
    organization: str
    subject: str
    account_id: UUID


@dataclass(frozen=True, repr=False)
class InvitationObservation:
    """Already resolved valid invite for the exact account; not a token proof.

    Caller must validate real token/eligibility/workspace and reconstruct this
    projection for this attempt. No raw invitation token is carried by the policy.
    """

    namespace_id: UUID
    subject: str
    account_id: UUID
    email: str
    workspace_id: UUID


class AdmissionAction(StrEnum):
    CREATE_INITIALIZED = "create_initialized"
    INITIALIZE_BOUND = "initialize_bound"
    INITIALIZE_INVITED = "initialize_invited"
    ACTIVATE_BOUND = "activate_bound"
    ACTIVATE_INVITED = "activate_invited"
    USE_BOUND = "use_bound"
    USE_INVITED = "use_invited"
    LINK_EXISTING = "link_existing"
    NO_ACCOUNT_ADMISSION = "no_account_admission"


class SharedOwnerRequirement(StrEnum):
    """Required future calls, deliberately not a checked/persisted receipt."""

    ACCOUNT_CREATION_PREPARE = "account_creation_prepare_seats_freeze"
    INVITATION_ACTIVATION_PREPARE = "invitation_activation_prepare_eligibility"
    BOUND_INITIALIZATION_PREPARE = "bound_initialization_prepare_owner_gap"
    ACCOUNT_SETUP_PERSIST = "account_setup_persist"
    ACCOUNT_STATUS_ACTIVATION_PERSIST = "account_status_activation_persist"


@dataclass(frozen=True, repr=False)
class AdmissionPlan:
    context: AdmissionContext
    mode: AuthMode
    action: AdmissionAction
    account_id: UUID | None = None
    creation_email: str | None = None
    setup: AccountSetup | None = None
    required_shared_owners: tuple[SharedOwnerRequirement, ...] = ()


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


def _optional_text(value: object, limit: int = 255) -> bool:
    return value is None or _text(value, limit)


def _projection(value: object, expected: type) -> bool:
    """Reject even partial objects of the expected frozen dataclass type."""
    return type(value) is expected and all(hasattr(value, field.name) for field in fields(expected))


def _dates(token: VerifiedIDToken | VerifiedNativeAccessToken) -> bool:
    try:
        return all(type(v) in (int, float) and math.isfinite(v) for v in (token.issued_at, token.expires_at)) and (
            token.expires_at > token.issued_at
        )
    except OverflowError:
        return False


def _validate_identity(context, mode, tokens, online, profile) -> None:
    if (
        not _projection(context, AdmissionContext)
        or type(mode) is not AuthMode
        or not all(
            type(v) is UUID
            for v in (context.integration_id, context.revision_id, context.active_revision_id, context.namespace_id)
        )
        or type(context.namespace_lifecycle) is not CasdoorNamespaceLifecycle
        or context.namespace_lifecycle is not CasdoorNamespaceLifecycle.ACTIVE
        or (mode is not AuthMode.DIAGNOSTIC and context.revision_id != context.active_revision_id)
        or type(context.fence_epoch) is not int
        or context.fence_epoch < 0
        or type(context.config_digest) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", context.config_digest)
        or not all(
            _text(v, limit)
            for v, limit in (
                (context.issuer, 2048),
                (context.organization, 255),
                (context.application, 255),
                (context.client_id, 255),
                (context.subject, 255),
            )
        )
        or not _projection(tokens, VerifiedTokenBundle)
        or not _projection(tokens.identity, VerifiedIDToken)
        or not _projection(tokens.native_access, VerifiedNativeAccessToken)
        or not _projection(online, VerifiedOnlineUser)
        or not _projection(online.user_ref, StructuredUserRef)
        or not _projection(profile, VerifiedProfile)
    ):
        raise AdmissionError(CasdoorErrorCode.INVALID_TRANSACTION)
    identity, native = tokens.identity, tokens.native_access
    if (
        (identity.issuer, identity.client_id, identity.subject) != (context.issuer, context.client_id, context.subject)
        or (native.organization, native.application, native.subject)
        != (context.organization, context.application, context.subject)
        or (online.user_ref.owner, online.subject) != (context.organization, context.subject)
        or not _text(online.user_ref.name)
        or profile.subject != context.subject
        or not _dates(identity)
        or not _dates(native)
        or (profile.email_verified is not None and type(profile.email_verified) is not bool)
        or not all(
            _optional_text(v, limit)
            for v, limit in ((profile.email, 254), (profile.name, 255), (profile.locale, 64), (profile.zoneinfo, 64))
        )
    ):
        raise AdmissionError(CasdoorErrorCode.INVALID_TRANSACTION)


def _validate_account(account: AccountObservation) -> None:
    if (
        not _projection(account, AccountObservation)
        or type(account.account_id) is not UUID
        or type(account.status) is not AccountStatus
        or account.status in (AccountStatus.BANNED, AccountStatus.CLOSED)
        or (account.initialized_at is not None and type(account.initialized_at) is not datetime)
        or not _text(account.email, 254)
        or not _optional_text(account.namespace_subject)
    ):
        raise AdmissionError()


def _legal_verified_email(profile: VerifiedProfile) -> str:
    if not profile.eligible_for_first_admission():
        raise AdmissionError()
    try:
        return validate_email(profile.email)
    except (ValueError, TypeError):
        raise AdmissionError() from None


def resolve_initial_setup(
    profile: VerifiedProfile,
    *,
    local_email: str,
    request_language: str | None = None,
    request_timezone: str | None = None,
) -> AccountSetup:
    """Initial fields only, reusing the original preference validators/defaults.

    Not admission authorization. Caller passes this only for an authorized initial
    setup; accounts with an initialized_at marker never reach this resolver through
    decide_admission, including pending/uninitialized accounts needing activation.
    """
    if not _projection(profile, VerifiedProfile) or not _text(local_email, 254):
        raise AdmissionError()
    try:
        validate_email(local_email)
    except (ValueError, TypeError):
        raise AdmissionError() from None
    language = None
    for candidate in (request_language, profile.locale):
        try:
            language = supported_language(candidate)
        except (ValueError, TypeError):
            continue
        break
    language = get_valid_language(language)
    timezone = None
    for candidate in (request_timezone, profile.zoneinfo):
        try:
            timezone = validate_timezone(candidate)
        except (ValueError, TypeError):
            continue
        break
    timezone = timezone or language_timezone_mapping.get(language, "UTC")
    name = profile.name if _text(profile.name) else local_email.split("@", 1)[0]
    if not _text(name):
        raise AdmissionError()
    return AccountSetup(name=name, interface_language=language, timezone=timezone)


def decide_admission(
    *,
    context: AdmissionContext,
    mode: AuthMode,
    tokens: VerifiedTokenBundle,
    online: VerifiedOnlineUser,
    profile: VerifiedProfile,
    account: AccountObservation | None = None,
    binding: ExactBindingObservation | None = None,
    invitation: InvitationObservation | None = None,
    source: SourceSessionContext | None = None,
    email_collision_account_ids: tuple[UUID, ...] = (),
    request_language: str | None = None,
    request_timezone: str | None = None,
) -> AdmissionPlan:
    """Choose a policy action; no input/output constitutes an authorization proof.

    Email lookup/normalization/legacy fallback and reverse-binding lookup remain
    actual caller reads. Collision IDs never select or authorize an account.
    Old public registration policy is intentionally not read. Every returned
    initialization plan still needs the indicated shared owner calls and current
    config/lease/precommit checks; CREATE_INITIALIZED is a desired outcome only.
    """
    _validate_identity(context, mode, tokens, online, profile)
    if (
        type(email_collision_account_ids) is not tuple
        or len(email_collision_account_ids) > 2048
        or any(type(v) is not UUID for v in email_collision_account_ids)
        or len(set(email_collision_account_ids)) != len(email_collision_account_ids)
    ):
        raise AdmissionError()
    if account is not None:
        _validate_account(account)
        if account.namespace_subject not in (None, context.subject):
            raise AdmissionError()
        if binding is None and account.namespace_subject is not None:
            raise AdmissionError()
    if binding is not None and (
        not _projection(binding, ExactBindingObservation)
        or type(binding.account_id) is not UUID
        or (binding.namespace_id, binding.issuer, binding.organization, binding.subject)
        != (context.namespace_id, context.issuer, context.organization, context.subject)
        or type(binding.namespace_id) is not UUID
        or account is None
        or account.account_id != binding.account_id
        or account.namespace_subject != context.subject
    ):
        raise AdmissionError()
    if invitation is not None:
        if (
            not _projection(invitation, InvitationObservation)
            or type(invitation.namespace_id) is not UUID
            or type(invitation.account_id) is not UUID
            or type(invitation.workspace_id) is not UUID
            or (invitation.namespace_id, invitation.subject) != (context.namespace_id, context.subject)
            or account is None
            or invitation.account_id != account.account_id
            or not _text(invitation.email, 254)
            or invitation.email != account.email
        ):
            raise AdmissionError(CasdoorErrorCode.INVITATION_MISMATCH)
        if normalize_email(_legal_verified_email(profile)) != normalize_email(invitation.email):
            raise AdmissionError(CasdoorErrorCode.INVITATION_MISMATCH)
    if mode is AuthMode.LOGIN:
        if source is not None:
            raise AdmissionError(CasdoorErrorCode.INVALID_TRANSACTION)
    else:
        if (
            not _projection(source, SourceSessionContext)
            or type(source.account_id) is not UUID
            or source.account_id != source.refresh_account_id
            or source.account_id != source.access_account_id
            or type(source.refresh_account_id) is not UUID
            or type(source.access_account_id) is not UUID
            or type(source.refresh_digest) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", source.refresh_digest)
            or type(source.management_authorized) is not bool
            or (mode is AuthMode.DIAGNOSTIC and source.management_authorized is not True)
        ):
            raise AdmissionError(CasdoorErrorCode.INVALID_TRANSACTION)
        if mode in (AuthMode.DIAGNOSTIC, AuthMode.REAUTH_UNLINK):
            return AdmissionPlan(context, mode, AdmissionAction.NO_ACCOUNT_ADMISSION)
        if account is None or account.account_id != source.account_id or invitation is not None:
            raise AdmissionError()
        if account.status is not AccountStatus.ACTIVE or account.initialized_at is None:
            raise AdmissionError()
        return AdmissionPlan(context, mode, AdmissionAction.LINK_EXISTING, account_id=account.account_id)

    if binding is None and invitation is None:
        if account is not None or email_collision_account_ids:
            raise AdmissionError()
        creation_email = _legal_verified_email(profile)
        setup = resolve_initial_setup(
            profile, local_email=creation_email, request_language=request_language, request_timezone=request_timezone
        )
        return AdmissionPlan(
            context,
            mode,
            AdmissionAction.CREATE_INITIALIZED,
            creation_email=creation_email,
            setup=setup,
            required_shared_owners=(
                SharedOwnerRequirement.ACCOUNT_CREATION_PREPARE,
                SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,
            ),
        )
    # Both authorization paths resolve the same exact account above. Extra email
    # matches cannot reassign a bound account, including remote email changes.
    needs_initialization = account.initialized_at is None
    needs_activation = account.status in (AccountStatus.PENDING, AccountStatus.UNINITIALIZED)
    requirements = []
    if invitation is not None:
        requirements.append(SharedOwnerRequirement.INVITATION_ACTIVATION_PREPARE)
    if needs_initialization or needs_activation:
        if invitation is None:
            requirements.append(SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE)
        requirements.append(
            SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST
            if needs_initialization
            else SharedOwnerRequirement.ACCOUNT_STATUS_ACTIVATION_PERSIST
        )
    if needs_initialization:
        action = AdmissionAction.INITIALIZE_BOUND if binding is not None else AdmissionAction.INITIALIZE_INVITED
    elif needs_activation:
        action = AdmissionAction.ACTIVATE_BOUND if binding is not None else AdmissionAction.ACTIVATE_INVITED
    else:
        action = AdmissionAction.USE_BOUND if binding is not None else AdmissionAction.USE_INVITED
    setup = (
        resolve_initial_setup(
            profile, local_email=account.email, request_language=request_language, request_timezone=request_timezone
        )
        if needs_initialization
        else None
    )
    return AdmissionPlan(
        context, mode, action, account_id=account.account_id, setup=setup, required_shared_owners=tuple(requirements)
    )
