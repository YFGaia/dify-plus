"""Signed offline login composition for local creation addresses; no live IdP proof."""

from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction, local_account_email
from core.casdoor.claims import StructuredUserRef, VerifiedOnlineUser, VerifiedProfile
from core.helper import ssrf_proxy
from models.account import Account, AccountStatus
from models.casdoor_extend import CasdoorIdentityExtend
from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from sqlalchemy.orm import Session
from test_casdoor_diagnostic_flow_extend import begin as begin_diagnostic
from test_casdoor_diagnostic_flow_extend import complete as complete_diagnostic
from test_casdoor_local_http_service_extend import begin, complete, counts
from test_gateway import response

pytest_plugins = ("test_casdoor_diagnostic_flow_extend",)


def remote_profile(flow, monkeypatch, email, verified):
    context = flow.local.env[1]
    profile = {"sub": context.subject, "name": "Remote Name"}
    if email is not None:
        profile["email"] = email
    if verified is not None:
        profile["email_verified"] = verified
    original = ssrf_proxy.make_request_with_deadline

    def transport(method, url, **kwargs):
        result = original(method, url, **kwargs)
        return response(profile) if urlsplit(url).path.endswith("userinfo") else result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    online = VerifiedOnlineUser(context.subject, StructuredUserRef(context.organization, "person"))
    snapshot = VerifiedProfile(context.subject, email, verified, "Remote Name", None, None)
    return profile, local_account_email(context, online, snapshot)


@pytest.mark.parametrize("email", [None, "new@example.test"])
@pytest.mark.parametrize("verified", [True, False, None])
def test_creation_address_drives_preflight_leases_persist_and_postwrite(production, monkeypatch, email, verified):
    f = production.flow
    profile, expected = remote_profile(f, monkeypatch, email, verified)
    scanned, scoped, postwrite = [], [], []
    original_reconstruct = CasdoorAccountPreflightRepository.reconstruct
    original_discover = CasdoorLoginScopeRepository.discover
    original_postwrite = CasdoorAccountPreflightRepository._observe_postwrite_new_collisions

    def reconstruct(owner, *args, **kwargs):
        scanned.append(kwargs.get("collision_email"))
        return original_reconstruct(owner, *args, **kwargs)

    def discover(owner, prepared, **kwargs):
        result = original_discover(owner, prepared, **kwargs)
        if prepared.plan.action is AdmissionAction.CREATE_INITIALIZED:
            assert prepared.plan.creation_email == expected
            assert expected in result.lease_scope.emails
            scoped.append(result)
        return result

    def postwrite_scan(owner, *args, **kwargs):
        postwrite.append(kwargs["collision_email"])
        return original_postwrite(owner, *args, **kwargs)

    monkeypatch.setattr(CasdoorAccountPreflightRepository, "reconstruct", reconstruct)
    monkeypatch.setattr(CasdoorLoginScopeRepository, "discover", discover)
    monkeypatch.setattr(CasdoorAccountPreflightRepository, "_observe_postwrite_new_collisions", postwrite_scan)
    scope, _ = begin(f)
    result = complete(f, scope)
    assert result.error is None
    assert result.tokens
    assert result.phases.local_outcome == "committed"
    assert result.phases.token_outcome == "issued"
    assert scanned.count(expected) >= 3
    assert scoped
    assert postwrite == [expected]
    with Session(f.local.engine) as reader:
        account = reader.scalar(sa.select(Account))
        identity = reader.scalar(sa.select(CasdoorIdentityExtend))
        assert account.email == expected
        assert account.status is AccountStatus.ACTIVE
        assert account.initialized_at
        assert identity.remote_email == email
        assert identity.email_verified is (verified if email is not None else None)
    assert profile.get("email") == email
    assert profile.get("email_verified") is verified


def test_repeated_login_keeps_exact_binding_and_generated_local_email(production, monkeypatch):
    f = production.flow
    _, generated = remote_profile(f, monkeypatch, None, True)
    scope, _ = begin(f)
    assert complete(f, scope).tokens
    with Session(f.local.engine) as reader:
        original_id = reader.scalar(sa.select(Account.id))
    remote_profile(f, monkeypatch, "later@example.test", False)
    assert f.service.start(browser_scope=scope, server_ip="192.0.2.7").status == 302
    assert complete(f, scope).tokens
    assert counts(f)[Account] == 1
    assert counts(f)[CasdoorIdentityExtend] == 1
    with Session(f.local.engine) as reader:
        account = reader.scalar(sa.select(Account))
        identity = reader.scalar(sa.select(CasdoorIdentityExtend))
        assert account.id == original_id
        assert account.email == generated
        assert identity.remote_email == "later@example.test"
        assert identity.email_verified is False
        assert identity.sync_generation == 2


@pytest.mark.parametrize("email", [None, "new@example.test"])
def test_creation_email_collision_rejects_without_link_or_business_writes(production, monkeypatch, email):
    f = production.flow
    _, expected = remote_profile(f, monkeypatch, email, False)
    with Session(f.local.engine) as writer, writer.begin():
        writer.add(Account(name="Existing independent account", email=expected.upper(), status=AccountStatus.ACTIVE))
    before = counts(f)
    scope, _ = begin(f)
    result = complete(f, scope)
    assert result.error is not None
    assert result.tokens is None
    assert result.phases.local_outcome == "not_started"
    assert counts(f) == before
    assert not f.lease.data
    with Session(f.local.engine) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0


def test_generated_email_late_collision_rolls_back_every_login_write(production, monkeypatch):
    f = production.flow
    _, expected = remote_profile(f, monkeypatch, None, True)
    original = CasdoorLoginAccountService.persist_login_account

    def insert_collision(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        kwargs["session"].execute(
            sa.insert(Account).values(name="Concurrent account", email=expected.upper(), status=AccountStatus.ACTIVE)
        )
        return result

    monkeypatch.setattr(CasdoorLoginAccountService, "persist_login_account", insert_collision)
    before = counts(f)
    scope, _ = begin(f)
    result = complete(f, scope)
    assert not f.control.tokens
    assert getattr(result, "tokens", None) is None
    assert result.phases.local_outcome == "unknown"
    assert result.phases.cleanup_released
    assert counts(f) == before
    assert not f.lease.data


def test_missing_email_diagnostic_does_not_create_or_mutate_business_accounts(diagnostic, monkeypatch):
    d = diagnostic
    remote_profile(d.f, monkeypatch, None, True)
    before = counts(d.f)
    _, state = begin_diagnostic(d)
    result = complete_diagnostic(d, state)
    assert result.status_code == 302
    assert counts(d.f) == before
    assert not d.f.control.tokens
    assert not d.f.control.billing
    with Session(d.f.local.engine) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0
