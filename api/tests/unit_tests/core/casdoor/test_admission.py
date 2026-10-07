"""Admission policy with synthetic observations; no authentication/UoW proof."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from uuid import UUID

import pytest
from core.casdoor.admission import (
    AccountObservation,
    AdmissionAction,
    AdmissionContext,
    AdmissionError,
    ExactBindingObservation,
    InvitationObservation,
    SharedOwnerRequirement,
    decide_admission,
    local_account_email,
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
from libs.helper import email as validate_email
from libs.helper import timezone as validate_timezone
from models.account import AccountStatus
from models.casdoor_extend import CasdoorNamespaceLifecycle
from services.account_email import normalize_email

ACCOUNT = UUID(int=10)
OTHER = UUID(int=11)
NOW = datetime(2026, 10, 1)
CONTEXT = AdmissionContext(
    UUID(int=1),
    UUID(int=2),
    UUID(int=2),
    UUID(int=3),
    CasdoorNamespaceLifecycle.ACTIVE,
    0,
    "a" * 64,
    "https://issuer.example.test",
    "ExactOrg",
    "ExactApp",
    "ExactClient",
    "stable-sub",
)
TOKENS = VerifiedTokenBundle(
    VerifiedIDToken(CONTEXT.issuer, CONTEXT.subject, CONTEXT.client_id, 1, 10),
    VerifiedNativeAccessToken(CONTEXT.subject, CONTEXT.organization, CONTEXT.application, 1, 10),
)
ONLINE = VerifiedOnlineUser(CONTEXT.subject, StructuredUserRef(CONTEXT.organization, "directory-name"))
PROFILE = VerifiedProfile(CONTEXT.subject, "person@example.test", True, "Person", "en-US", "UTC")


def account(status=AccountStatus.ACTIVE, initialized=NOW, *, bound=False):
    return AccountObservation(ACCOUNT, status, initialized, "person@example.test", CONTEXT.subject if bound else None)


def binding():
    return ExactBindingObservation(CONTEXT.namespace_id, CONTEXT.issuer, CONTEXT.organization, CONTEXT.subject, ACCOUNT)


def invite():
    return InvitationObservation(CONTEXT.namespace_id, CONTEXT.subject, ACCOUNT, "person@example.test", UUID(int=20))


def source(*, management=False):
    return SourceSessionContext(ACCOUNT, ACCOUNT, ACCOUNT, "b" * 64, management)


def decide(**changes):
    inputs = dict(context=CONTEXT, mode=AuthMode.LOGIN, tokens=TOKENS, online=ONLINE, profile=PROFILE)
    inputs.update(changes)
    return decide_admission(**inputs)


def test_new_account_desired_initialized_active_without_global_registration_policy(monkeypatch):
    from services.system_feature_service import SystemFeatureService

    def forbidden(*args, **kwargs):
        pytest.fail("Pure organization policy must not read shared registration, seat or provider policy")

    monkeypatch.setattr(SystemFeatureService, "is_registration_allowed", forbidden)
    monkeypatch.setattr(SystemFeatureService, "get_license", forbidden)
    plan = decide()
    assert plan.action is AdmissionAction.CREATE_INITIALIZED
    assert plan.account_id is None
    assert plan.creation_email == PROFILE.email
    assert plan.setup.name == "Person"
    assert plan.required_shared_owners == (
        SharedOwnerRequirement.ACCOUNT_CREATION_PREPARE,
        SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,
    )
    assert not any(hasattr(plan, v) for v in ("authenticated", "checked", "persisted", "session_allowed", "is_setup"))


@pytest.mark.parametrize("email", ["not-mail", "a@localhost", "a..b@example.test", "a@-bad.test", "a@é.test"])
@pytest.mark.parametrize("verified", [True, False, None])
def test_new_rejects_present_malformed_email(email, verified):
    with pytest.raises(AdmissionError):
        decide(profile=replace(PROFILE, email=email, email_verified=verified))


@pytest.mark.parametrize("verified", [True, False, None])
def test_new_uses_legal_provider_email_without_changing_verification(verified):
    profile = replace(PROFILE, email_verified=verified)
    assert decide(profile=profile).creation_email == profile.email
    assert profile.email_verified is verified


@pytest.mark.parametrize("verified", [True, False, None])
@pytest.mark.parametrize("name", ["on_ccc2d78352150d14cf19299d66491b27", "名 字", "___", "A" * 255, "a.+b@x"])
def test_missing_email_creates_bounded_deterministic_local_address(name, verified):
    profile = replace(PROFILE, email=None, email_verified=verified)
    online = replace(ONLINE, user_ref=replace(ONLINE.user_ref, name=name))
    plan = decide(profile=profile, online=online)
    address = plan.creation_email
    assert address == local_account_email(CONTEXT, online, profile)
    assert address.endswith("@casdoor.invalid")
    assert address.isascii()
    assert len(address.split("@", 1)[0]) <= 64
    assert len(address) <= 254
    assert validate_email(address) == address
    assert profile.email is None
    assert profile.email_verified is verified
    other_namespace = replace(CONTEXT, namespace_id=OTHER)
    assert local_account_email(other_namespace, online, profile) != address
    other_subject = replace(CONTEXT, subject="another-sub")
    assert local_account_email(
        other_subject, replace(online, subject=other_subject.subject), replace(profile, subject=other_subject.subject)
    ) != address


def test_generated_email_collision_never_links_existing_account():
    with pytest.raises(AdmissionError):
        decide(profile=replace(PROFILE, email=None), email_collision_account_ids=(ACCOUNT,))


def test_missing_email_cannot_use_generated_address_as_invitation_mailbox():
    with pytest.raises(AdmissionError):
        decide(account=account(), invitation=invite(), profile=replace(PROFILE, email=None))


@pytest.mark.parametrize("status", list(AccountStatus))
def test_email_match_never_selects_or_initializes_local_account(status):
    with pytest.raises(AdmissionError, match="identity_conflict"):
        decide(account=account(status), email_collision_account_ids=(ACCOUNT,))


@pytest.mark.parametrize("matches", [(ACCOUNT,), (ACCOUNT, OTHER)])
def test_collision_without_target_is_not_permission_to_create(matches):
    with pytest.raises(AdmissionError):
        decide(email_collision_account_ids=matches)


@pytest.mark.parametrize("email", [None, "new@example.test", "person@example.test"])
@pytest.mark.parametrize("verified", [True, False, None])
def test_initialized_exact_binding_preserves_account_despite_remote_email(email, verified):
    local = account(bound=True)
    before = local
    plan = decide(
        account=local,
        binding=binding(),
        profile=replace(PROFILE, email=email, email_verified=verified, name="Remote Changed", locale="zh-Hans"),
        email_collision_account_ids=(OTHER,),
        request_language="ja-JP",
        request_timezone="Asia/Tokyo",
    )
    assert plan.action is AdmissionAction.USE_BOUND
    assert plan.account_id == ACCOUNT
    assert plan.creation_email is None and plan.setup is None
    assert plan.required_shared_owners == ()
    assert local == before and local.email == "person@example.test"


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED, AccountStatus.ACTIVE])
def test_bound_uninitialized_uses_shared_owner_gap_and_setup_not_activation_invite(status):
    plan = decide(account=account(status, None, bound=True), binding=binding())
    assert plan.action is AdmissionAction.INITIALIZE_BOUND
    assert plan.account_id == ACCOUNT
    assert plan.required_shared_owners == (
        SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE,
        SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,
    )


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED])
@pytest.mark.parametrize("authorization", ["binding", "invitation"])
def test_historical_initialized_marker_requires_status_only_activation(status, authorization, monkeypatch):
    import core.casdoor.admission as admission

    def forbidden(*args, **kwargs):
        pytest.fail("Status activation must preserve original name/preferences/initialization timestamp")

    monkeypatch.setattr(admission, "resolve_initial_setup", forbidden)
    local = account(status, NOW, bound=authorization == "binding")
    inputs = {"binding": binding()} if authorization == "binding" else {"invitation": invite()}
    plan = decide(
        account=local,
        profile=replace(PROFILE, name="Remote Changed", locale="ja-JP", zoneinfo="Asia/Tokyo"),
        request_language="zh-Hans",
        request_timezone="Asia/Shanghai",
        **inputs,
    )
    assert plan.action is (
        AdmissionAction.ACTIVATE_BOUND if authorization == "binding" else AdmissionAction.ACTIVATE_INVITED
    )
    assert plan.account_id == ACCOUNT and plan.creation_email is None and plan.setup is None
    assert plan.required_shared_owners == (
        SharedOwnerRequirement.BOUND_INITIALIZATION_PREPARE
        if authorization == "binding"
        else SharedOwnerRequirement.INVITATION_ACTIVATION_PREPARE,
        SharedOwnerRequirement.ACCOUNT_STATUS_ACTIVATION_PERSIST,
    )
    assert local.initialized_at is NOW and local.status is status and local.email == "person@example.test"


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED])
def test_source_link_cannot_status_activate_historically_initialized_account(status):
    with pytest.raises(AdmissionError):
        decide(mode=AuthMode.LINK, source=source(), account=account(status, NOW))


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED, AccountStatus.ACTIVE])
def test_exact_invitation_initializes_same_account_without_duplicate_quota_requirement(status):
    plan = decide(account=account(status, None), invitation=invite())
    assert plan.action is AdmissionAction.INITIALIZE_INVITED
    assert plan.account_id == ACCOUNT
    assert plan.required_shared_owners == (
        SharedOwnerRequirement.INVITATION_ACTIVATION_PREPARE,
        SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,
    )


def test_initialized_invitation_preserves_fields_and_requires_real_owner_eligibility():
    plan = decide(account=account(), invitation=invite())
    assert plan.action is AdmissionAction.USE_INVITED and plan.setup is None
    assert plan.required_shared_owners == (SharedOwnerRequirement.INVITATION_ACTIVATION_PREPARE,)


def test_invite_normalization_reuses_original_owner_but_exact_local_invitation_email():
    local = replace(account(), email="p.er.son+tag@googlemail.com")
    authorized = replace(invite(), email=local.email)
    plan = decide(account=local, invitation=authorized, profile=replace(PROFILE, email="person@gmail.com"))
    assert plan.account_id == ACCOUNT
    assert normalize_email(local.email) == normalize_email("person@gmail.com")
    with pytest.raises(AdmissionError, match="invitation_mismatch"):
        decide(account=local, invitation=replace(authorized, email="person@gmail.com"))


@pytest.mark.parametrize("verified", [False, None])
def test_invitation_requires_verified_mail_even_for_existing_initialized_bound_account(verified):
    with pytest.raises(AdmissionError):
        decide(
            account=account(bound=True),
            binding=binding(),
            invitation=invite(),
            profile=replace(PROFILE, email_verified=verified),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", OTHER),
        ("namespace_id", OTHER),
        ("subject", "other"),
        ("email", "other@example.test"),
        ("workspace_id", "payload-id"),
    ],
)
def test_invitation_exact_scope_mismatch(field, value):
    with pytest.raises(AdmissionError, match="invitation_mismatch"):
        decide(account=account(bound=True), binding=binding(), invitation=replace(invite(), **{field: value}))


@pytest.mark.parametrize("status", [AccountStatus.BANNED, AccountStatus.CLOSED, "active", "future", None])
@pytest.mark.parametrize("authorization", ["binding", "invitation", "source"])
def test_local_status_precedence_never_unbans_or_creates_replacement(status, authorization):
    inputs = dict(account=account(status, None, bound=authorization == "binding"))
    if authorization == "binding":
        inputs["binding"] = binding()
    elif authorization == "invitation":
        inputs["invitation"] = invite()
    else:
        inputs.update(mode=AuthMode.LINK, source=source())
    with pytest.raises(AdmissionError):
        decide(**inputs)


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED, AccountStatus.ACTIVE])
def test_source_link_alone_cannot_initialize(status):
    with pytest.raises(AdmissionError):
        decide(mode=AuthMode.LINK, source=source(), account=account(status, None))


def test_link_source_selects_exact_eligible_account_without_login_or_email_claim_authority():
    plan = decide(
        mode=AuthMode.LINK,
        source=source(),
        account=account(),
        profile=replace(PROFILE, email=None, email_verified=None),
        email_collision_account_ids=(OTHER,),
    )
    assert plan.action is AdmissionAction.LINK_EXISTING and plan.mode is AuthMode.LINK
    assert plan.setup is None and plan.creation_email is None and plan.required_shared_owners == ()
    with pytest.raises(AdmissionError):
        decide(
            mode=AuthMode.LINK,
            source=replace(source(), account_id=OTHER, access_account_id=OTHER, refresh_account_id=OTHER),
            account=account(),
        )
    with pytest.raises(AdmissionError):
        decide(source=source(), account=account(bound=True), binding=binding())


@pytest.mark.parametrize("mode", [AuthMode.DIAGNOSTIC, AuthMode.REAUTH_UNLINK])
def test_non_admission_modes_have_no_persistence_plan(mode):
    plan = decide(mode=mode, source=source(management=True), profile=replace(PROFILE, email=None, email_verified=None))
    assert plan.action is AdmissionAction.NO_ACCOUNT_ADMISSION
    assert plan.account_id is None and plan.setup is None and plan.creation_email is None
    assert plan.required_shared_owners == ()
    with pytest.raises(AdmissionError):
        decide(mode=mode)


def test_diagnostic_draft_and_management_source_are_distinct_from_active_login_context():
    draft = replace(CONTEXT, revision_id=OTHER)
    assert (
        decide(context=draft, mode=AuthMode.DIAGNOSTIC, source=source(management=True)).action
        is AdmissionAction.NO_ACCOUNT_ADMISSION
    )
    with pytest.raises(AdmissionError):
        decide(context=draft)
    with pytest.raises(AdmissionError):
        decide(mode=AuthMode.DIAGNOSTIC, source=source())


@pytest.mark.parametrize("mode", ["login", "link", True, None])
def test_mode_must_be_actual_enum(mode):
    with pytest.raises(AdmissionError):
        decide(mode=mode)


@pytest.mark.parametrize(
    "field,value",
    [
        ("integration_id", "1"),
        ("namespace_id", "3"),
        ("fence_epoch", True),
        ("fence_epoch", -1),
        ("config_digest", "A" * 64),
        ("issuer", ""),
        ("organization", "x\n"),
        ("subject", "\ud800"),
        ("application", "x" * 256),
        ("namespace_lifecycle", "active"),
    ],
)
def test_malformed_current_context(field, value):
    with pytest.raises(AdmissionError):
        decide(context=replace(CONTEXT, **{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace_id", OTHER),
        ("issuer", "other"),
        ("organization", "exactorg"),
        ("subject", "other"),
        ("account_id", OTHER),
        ("account_id", str(ACCOUNT)),
    ],
)
def test_binding_exact_chain_and_uuid(field, value):
    with pytest.raises(AdmissionError):
        decide(account=account(bound=True), binding=replace(binding(), **{field: value}))


def test_binding_needs_exact_reverse_observation_and_link_cannot_replace_different_subject():
    with pytest.raises(AdmissionError):
        decide(account=account(), binding=binding())
    with pytest.raises(AdmissionError):
        decide(mode=AuthMode.LINK, source=source(), account=replace(account(), namespace_subject="another-sub"))
    with pytest.raises(AdmissionError):
        decide(mode=AuthMode.LINK, source=source(), account=account(bound=True))


@pytest.mark.parametrize(
    "field,projection",
    [
        ("context", AdmissionContext),
        ("tokens", VerifiedTokenBundle),
        ("online", VerifiedOnlineUser),
        ("profile", VerifiedProfile),
        ("account", AccountObservation),
        ("binding", ExactBindingObservation),
        ("invitation", InvitationObservation),
        ("source", SourceSessionContext),
    ],
)
def test_partial_typed_projection_has_stable_failure(field, projection):
    with pytest.raises(AdmissionError):
        decide(**{field: object.__new__(projection)})


@pytest.mark.parametrize("owner", ["identity", "native_access"])
@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), 10, "1"])
def test_token_date_shape_only_not_signature_or_freshness_verification(owner, value):
    part = getattr(TOKENS, owner)
    with pytest.raises(AdmissionError):
        decide(tokens=replace(TOKENS, **{owner: replace(part, issued_at=value)}))


@pytest.mark.parametrize("projection", [None, {}, True])
def test_public_primitive_or_boolean_cannot_substitute_typed_profile(projection):
    with pytest.raises(AdmissionError):
        decide(profile=projection)


@pytest.mark.parametrize(
    "changes",
    [
        dict(tokens=replace(TOKENS, identity=replace(TOKENS.identity, subject="other"))),
        dict(tokens=replace(TOKENS, native_access=replace(TOKENS.native_access, organization="OtherOrg"))),
        dict(online=replace(ONLINE, subject="other")),
        dict(online=replace(ONLINE, user_ref=StructuredUserRef("OtherOrg", "same-name"))),
        dict(profile=replace(PROFILE, subject="other")),
        dict(profile=replace(PROFILE, email_verified=1)),
        dict(profile=replace(PROFILE, name="bad\nname")),
    ],
)
def test_cross_projection_consistency_and_schema(changes):
    with pytest.raises(AdmissionError):
        decide(**changes)


@pytest.mark.parametrize("matches", [[ACCOUNT], (str(ACCOUNT),), (ACCOUNT, ACCOUNT), (ACCOUNT,) * 2049])
def test_malformed_collision_projection(matches):
    with pytest.raises(AdmissionError):
        decide(email_collision_account_ids=matches)


@pytest.mark.parametrize(
    "request_language,idp_language,expected",
    [
        ("zh-Hans", "ja-JP", "zh-Hans"),
        ("invalid", "ja-JP", "ja-JP"),
        (None, "invalid", "en-US"),
        (["en-US"], "ko-KR", "ko-KR"),
    ],
)
def test_preference_language_request_then_idp_then_original_default(request_language, idp_language, expected):
    plan = decide(request_language=request_language, profile=replace(PROFILE, locale=idp_language, zoneinfo=None))
    assert plan.setup.interface_language == expected
    assert validate_timezone(plan.setup.timezone) == plan.setup.timezone


@pytest.mark.parametrize(
    "request_timezone,idp_timezone,expected",
    [
        ("Asia/Shanghai", "Asia/Tokyo", "Asia/Shanghai"),
        ("invalid", "Asia/Tokyo", "Asia/Tokyo"),
        (None, "invalid", "America/New_York"),
        (["UTC"], "UTC", "UTC"),
    ],
)
def test_timezone_original_validator_and_mapping_fallback(request_timezone, idp_timezone, expected):
    plan = decide(request_timezone=request_timezone, profile=replace(PROFILE, zoneinfo=idp_timezone))
    assert plan.setup.timezone == expected


def test_validators_and_initial_name_fallback_are_original_owners():
    plan = decide(profile=replace(PROFILE, name=None, locale=None, zoneinfo=None))
    assert plan.setup.name == "person"
    assert validate_email(plan.creation_email) == plan.creation_email
    assert plan.setup.interface_language == "en-US" and plan.setup.timezone == "America/New_York"


def test_plan_and_inputs_frozen_and_redacted():
    plan = decide()
    assert "person@example.test" not in repr(plan) and "stable-sub" not in repr(plan)
    with pytest.raises(FrozenInstanceError):
        plan.action = AdmissionAction.USE_BOUND
    assert PROFILE.name == "Person" and TOKENS.identity.subject == CONTEXT.subject
