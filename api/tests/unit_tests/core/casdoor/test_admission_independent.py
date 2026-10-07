"""Independent policy counterexamples; projections are synthetic, not auth proof."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

import pytest
from core.casdoor.admission import (
    AccountObservation,
    AdmissionAction,
    AdmissionContext,
    AdmissionError,
    ExactBindingObservation,
    SharedOwnerRequirement,
    decide_admission,
    resolve_initial_setup,
)
from core.casdoor.auth_transactions import AuthMode, SourceSessionContext
from core.casdoor.claims import (
    StructuredUserRef,
    VerifiedIDToken,
    VerifiedNativeAccessToken,
    VerifiedOnlineUser,
    VerifiedProfile,
    VerifiedTokenBundle,
)
from models.account import AccountStatus
from models.casdoor_extend import CasdoorNamespaceLifecycle

_ACCOUNT = UUID(int=812)
_OTHER = UUID(int=813)
_NAMESPACE = UUID(int=814)
_MARKER = datetime(2022, 4, 3, 2, 1)
_CONTEXT = AdmissionContext(
    UUID(int=815),
    UUID(int=816),
    UUID(int=816),
    _NAMESPACE,
    CasdoorNamespaceLifecycle.ACTIVE,
    9,
    "a" * 64,
    "https://id.example.invalid",
    "north",
    "console",
    "client",
    "subject-7",
)
_TOKENS = VerifiedTokenBundle(
    VerifiedIDToken(_CONTEXT.issuer, _CONTEXT.subject, _CONTEXT.client_id, 1, 10),
    VerifiedNativeAccessToken(_CONTEXT.subject, _CONTEXT.organization, _CONTEXT.application, 1, 10),
)
_ONLINE = VerifiedOnlineUser(_CONTEXT.subject, StructuredUserRef(_CONTEXT.organization, "directory-name"))
_PROFILE = VerifiedProfile(_CONTEXT.subject, "person@example.test", True, "New Remote Name", "en-US", "UTC")


def _account(status=AccountStatus.ACTIVE, initialized_at=_MARKER, *, reverse_subject=None, email="person@example.test"):
    return AccountObservation(_ACCOUNT, status, initialized_at, email, reverse_subject)


def _binding():
    return ExactBindingObservation(_NAMESPACE, _CONTEXT.issuer, _CONTEXT.organization, _CONTEXT.subject, _ACCOUNT)


def _source():
    return SourceSessionContext(_ACCOUNT, _ACCOUNT, _ACCOUNT, "b" * 64, False)


def _decide(**overrides):
    values = dict(context=_CONTEXT, mode=AuthMode.LOGIN, tokens=_TOKENS, online=_ONLINE, profile=_PROFILE)
    values.update(overrides)
    return decide_admission(**values)


@pytest.mark.parametrize(
    "status,initialized_at,action,requirements",
    [
        (
            AccountStatus.ACTIVE,
            None,
            AdmissionAction.INITIALIZE_BOUND,
            (SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE, SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST),
        ),
        (
            AccountStatus.PENDING,
            _MARKER,
            AdmissionAction.ACTIVATE_BOUND,
            (
                SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE,
                SharedOwnerRequirement.ACCOUNT_STATUS_ACTIVATION_PERSIST,
            ),
        ),
        (
            AccountStatus.UNINITIALIZED,
            _MARKER,
            AdmissionAction.ACTIVATE_BOUND,
            (
                SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE,
                SharedOwnerRequirement.ACCOUNT_STATUS_ACTIVATION_PERSIST,
            ),
        ),
    ],
)
def test_status_activation_does_not_regenerate_historical_account_setup(
    status, initialized_at, action, requirements, monkeypatch
):
    import core.casdoor.admission as admission

    def setup_must_not_run(*args, **kwargs):
        pytest.fail("A historical initialization marker must route to status-only activation")

    if initialized_at is not None:
        monkeypatch.setattr(admission, "resolve_initial_setup", setup_must_not_run)
    local = _account(status, initialized_at, reverse_subject=_CONTEXT.subject)
    plan = _decide(
        account=local,
        binding=_binding(),
        profile=replace(_PROFILE, name="Changed", locale="ja-JP", zoneinfo="Asia/Tokyo"),
        request_language="zh-Hans",
        request_timezone="Asia/Shanghai",
    )
    assert plan.action is action
    assert plan.account_id == _ACCOUNT and plan.creation_email is None
    if initialized_at is None:
        assert plan.setup is not None
    else:
        assert plan.setup is None
    assert plan.required_shared_owners == requirements
    assert (local.initialized_at, local.email, local.status) == (initialized_at, "person@example.test", status)


@pytest.mark.parametrize("status", [AccountStatus.ACTIVE, AccountStatus.PENDING])
def test_email_coincidence_never_selects_a_local_account(status):
    with pytest.raises(AdmissionError):
        _decide(account=_account(status), email_collision_account_ids=(_ACCOUNT,))


def test_new_organization_admission_does_not_consult_global_registration_or_license(monkeypatch):
    from services.system_feature_service import SystemFeatureService

    def forbidden(*args, **kwargs):
        pytest.fail("Organization admission must leave the legacy registration and license owners unread")

    monkeypatch.setattr(SystemFeatureService, "is_registration_allowed", forbidden)
    monkeypatch.setattr(SystemFeatureService, "get_license", forbidden)
    plan = _decide()
    assert plan.action is AdmissionAction.CREATE_INITIALIZED
    assert plan.creation_email == _PROFILE.email and plan.setup is not None
    assert plan.required_shared_owners == (
        SharedOwnerRequirement.ACCOUNT_CREATION_PREPARE,
        SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,
    )


@pytest.mark.parametrize("remote_email,verified", [(None, None), ("elsewhere@example.test", False)])
def test_exact_binding_survives_remote_email_drift_without_profile_synchronization(remote_email, verified):
    local = _account(reverse_subject=_CONTEXT.subject)
    before = local
    plan = _decide(
        account=local,
        binding=_binding(),
        profile=replace(_PROFILE, email=remote_email, email_verified=verified, name="Renamed Remotely"),
        email_collision_account_ids=(_OTHER,),
    )
    assert plan.action is AdmissionAction.USE_BOUND and plan.account_id == _ACCOUNT
    assert plan.setup is None and plan.creation_email is None and not plan.required_shared_owners
    assert local == before


def test_initial_preferences_keep_original_request_then_idp_owner_precedence():
    setup = resolve_initial_setup(
        _PROFILE,
        local_email="person@example.test",
        request_language="zh-Hans",
        request_timezone="Asia/Shanghai",
    )
    assert (setup.interface_language, setup.timezone) == ("zh-Hans", "Asia/Shanghai")

    fallback = resolve_initial_setup(
        replace(_PROFILE, locale="ja-JP", zoneinfo="Asia/Tokyo"),
        local_email="person@example.test",
        request_language="unsupported-language",
        request_timezone="not/a-zone",
    )
    assert (fallback.interface_language, fallback.timezone) == ("ja-JP", "Asia/Tokyo")


def test_link_cannot_initialize_pending_account_and_diagnostic_is_no_admission():
    with pytest.raises(AdmissionError):
        _decide(mode=AuthMode.LINK, source=_source(), account=_account(AccountStatus.PENDING, None))
    management = replace(_source(), management_authorized=True)
    diagnostic_context = replace(_CONTEXT, revision_id=UUID(int=817))
    plan = _decide(mode=AuthMode.DIAGNOSTIC, source=management, context=diagnostic_context)
    assert plan.action is AdmissionAction.NO_ACCOUNT_ADMISSION
    assert plan.account_id is None and plan.setup is None and not plan.required_shared_owners


def test_partial_typed_binding_and_unscoped_invitation_cannot_authorize_account():
    with pytest.raises(AdmissionError):
        _decide(account=_account(reverse_subject=_CONTEXT.subject), binding=object.__new__(ExactBindingObservation))
    # An invitation from another namespace is not an alternate route to the same email.
    from core.casdoor.admission import InvitationObservation

    invitation = InvitationObservation(UUID(int=818), _CONTEXT.subject, _ACCOUNT, "person@example.test", UUID(int=819))
    with pytest.raises(AdmissionError):
        _decide(account=_account(), invitation=invitation)
