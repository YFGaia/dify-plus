"""Shared creation rules and caller-owned writes, without Casdoor admission/session policy."""

from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, inspect, select

from enums import DeploymentEdition
from libs.password import compare_password
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorIntentKind,
    CasdoorNamespaceExtend,
    CasdoorSyncIntentExtend,
)
from services import account_service
from services.account_quota_service_extend import ensure_account_quota_extend
from services.account_service import AccountService
from services.errors.account import (
    AccountNormalizedEmailAlreadyInUseError,
    AccountNotFoundError,
    AccountRegisterError,
    EmailDomainSuspendedError,
    SeatsLimitExceededError,
)


@pytest.fixture(autouse=True)
def dependencies(monkeypatch, config_overrides):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, ACCOUNT_TOTAL_QUOTA=Decimal(15))
    features = MagicMock()
    features.is_registration_allowed.return_value = True
    features.get_license.return_value.seats.is_available.return_value = True
    billing = MagicMock()
    billing.is_email_in_freeze.return_value = False
    monkeypatch.setattr(account_service, "SystemFeatureService", features)
    monkeypatch.setattr(account_service, "BillingService", billing)
    return features, billing


def _prepare(**kwargs):
    return AccountService.prepare_account_creation("new@example.test", "New", "en-US", **kwargs)


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def _create_using_policy(path, session):
    if path == "prepared":
        return _prepare()
    return AccountService.create_account("new@example.test", "New", "en-US", is_setup=path == "setup", session=session)


def test_closed_public_registration_rejects_before_shared_checks_or_rollback(dependencies, sqlite_session, monkeypatch):
    features, billing = dependencies
    features.is_registration_allowed.return_value = False
    rollback = MagicMock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    with pytest.raises(AccountNotFoundError):
        AccountService.create_account("new@example.test", "New", "en-US", session=sqlite_session)
    features.get_license.assert_not_called()
    billing.is_email_in_freeze.assert_not_called()
    rollback.assert_not_called()
    assert _count(sqlite_session, Account) == _count(sqlite_session, AccountMoneyExtend) == 0


def test_setup_retains_legacy_closed_registration_exception(dependencies, sqlite_session_factory):
    features, _ = dependencies
    features.is_registration_allowed.return_value = False
    with sqlite_session_factory() as session:
        account = AccountService.create_account("setup@example.test", "Setup", "en-US", is_setup=True, session=session)
        account_id = account.id
    with sqlite_session_factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.status == AccountStatus.ACTIVE  # Existing model default; no provider activation claim.
        assert account.initialized_at is None
        assert _count(session, AccountMoneyExtend) == 1


def test_independently_authorized_preparation_reuses_checks_when_public_registration_closed(dependencies):
    features, _ = dependencies
    features.is_registration_allowed.return_value = False
    prepared = _prepare()
    assert inspect(prepared.account).transient
    features.is_registration_allowed.assert_not_called()
    features.get_license.return_value.seats.is_available.assert_called_once()


@pytest.mark.parametrize("path", ["legacy", "setup", "prepared"])
def test_shared_seat_limit_blocks_every_creation_policy(path, dependencies, sqlite_session):
    features, billing = dependencies
    features.get_license.return_value.seats.is_available.return_value = False
    with pytest.raises(SeatsLimitExceededError):
        _create_using_policy(path, sqlite_session)
    billing.is_email_in_freeze.assert_not_called()
    assert _count(sqlite_session, Account) == _count(sqlite_session, AccountMoneyExtend) == 0


@pytest.mark.parametrize("path", ["legacy", "setup", "prepared"])
@pytest.mark.parametrize(
    ("freeze_type", "error"), [(None, AccountRegisterError), ("email_domain_suspended", EmailDomainSuspendedError)]
)
def test_shared_cloud_freeze_blocks_every_creation_policy(
    path, freeze_type, error, dependencies, config_overrides, sqlite_session
):
    _, billing = dependencies
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD)
    billing.is_email_in_freeze.return_value = True
    billing.get_email_freeze_type.return_value = freeze_type
    with pytest.raises(error):
        _create_using_policy(path, sqlite_session)
    billing.is_email_in_freeze.assert_called_once_with("new@example.test")
    assert _count(sqlite_session, Account) == _count(sqlite_session, AccountMoneyExtend) == 0


def test_legacy_normalized_email_guard_stays_before_seat_check(dependencies, sqlite_session):
    features, _ = dependencies
    AccountService.create_account("n.e.w@gmail.com", "Original", "en-US", session=sqlite_session)
    features.get_license.reset_mock()
    features.get_license.return_value.seats.is_available.return_value = False
    with pytest.raises(AccountNormalizedEmailAlreadyInUseError):
        AccountService.create_account(
            "new+alias@googlemail.com", "Duplicate", "en-US", check_normalized_email=True, session=sqlite_session
        )
    features.get_license.assert_not_called()
    assert _count(sqlite_session, Account) == _count(sqlite_session, AccountMoneyExtend) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"password": "weak"}, "Password must contain"), ({"timezone": "Invalid/Timezone"}, "not a valid timezone")],
)
def test_field_validation_rejects_before_legacy_rollback(kwargs, message, sqlite_session, monkeypatch):
    rollback = MagicMock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    with pytest.raises(ValueError, match=message):
        AccountService.create_account("new@example.test", "New", "en-US", session=sqlite_session, **kwargs)
    rollback.assert_not_called()
    assert _count(sqlite_session, Account) == 0


def test_prepared_fields_use_real_password_timezone_and_email_owners():
    prepared = AccountService.prepare_account_creation(
        "n.e.w+alias@googlemail.com",
        "New",
        "en-US",
        password="password123",
        timezone="Asia/Shanghai",
        interface_theme="dark",
        ip_address="127.0.0.1",
    )
    account = prepared.account
    assert account.email == "n.e.w+alias@googlemail.com"
    assert account.normalized_email == "new@gmail.com"
    assert account.timezone == "Asia/Shanghai"
    assert account.interface_theme == "dark"
    assert account.last_login_ip == "127.0.0.1"
    assert compare_password("password123", account.password, account.password_salt)
    assert _prepare().account.timezone == "America/New_York"
    assert _prepare().account.password is None


def test_persist_leaves_commit_and_quota_balance_to_caller(sqlite_session_factory, monkeypatch, dependencies):
    prepared = _prepare()
    features, billing = dependencies
    features.reset_mock()
    billing.reset_mock()
    with sqlite_session_factory() as session:
        commit = MagicMock(wraps=session.commit)
        rollback = MagicMock(wraps=session.rollback)
        monkeypatch.setattr(session, "commit", commit)
        monkeypatch.setattr(session, "rollback", rollback)
        account = AccountService.persist_account_creation(prepared, session=session)
        ensure_account_quota_extend(account.id, session=session)
        quota = session.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account.id))
        quota.total_quota, quota.used_quota = Decimal(81), Decimal(7)
        ensure_account_quota_extend(account.id, session=session)
        ensure_account_quota_extend(account.id, session=session)
        assert quota.total_quota == Decimal(81)
        assert quota.used_quota == Decimal(7)
        assert _count(session, AccountMoneyExtend) == 1
        assert _count(session, Tenant) == _count(session, TenantAccountJoin) == 0
        assert account.initialized_at is None
        commit.assert_not_called()
        rollback.assert_not_called()
        features.get_license.assert_not_called()
        billing.is_email_in_freeze.assert_not_called()
        session.commit()
    with sqlite_session_factory() as session:
        assert session.get(Account, account.id) is not None
        assert _count(session, AccountMoneyExtend) == 1


@pytest.mark.parametrize("field", ["id", "email", "normalized_email"])
def test_persist_rejects_changed_preparation_identity(field, sqlite_session):
    prepared = _prepare()
    setattr(prepared.account, field, str(uuid4()) if field == "id" else "changed@example.test")
    with pytest.raises(ValueError, match="unchanged fresh preparation"):
        AccountService.persist_account_creation(prepared, session=sqlite_session)
    assert _count(sqlite_session, Account) == _count(sqlite_session, AccountMoneyExtend) == 0


def test_persist_rejects_raw_account_pending_object_and_receipt_replay(sqlite_session):
    prepared = _prepare()
    with pytest.raises(TypeError, match="internal prepared receipt"):
        AccountService.persist_account_creation(prepared.account, session=sqlite_session)
    sqlite_session.add(prepared.account)
    with pytest.raises(ValueError, match="unchanged fresh preparation"):
        AccountService.persist_account_creation(prepared, session=sqlite_session)
    sqlite_session.expunge(prepared.account)
    AccountService.persist_account_creation(prepared, session=sqlite_session)
    sqlite_session.rollback()
    assert inspect(prepared.account).transient
    with pytest.raises(ValueError, match="unchanged fresh preparation"):
        AccountService.persist_account_creation(prepared, session=sqlite_session)
    assert _count(sqlite_session, Account) == _count(sqlite_session, AccountMoneyExtend) == 0


@pytest.mark.parametrize("path", ["legacy", "prepared"])
def test_quota_flush_failure_rollback_is_owned_by_public_method_or_caller(path, sqlite_session, monkeypatch):
    prepared = _prepare() if path == "prepared" else None
    rollback = MagicMock(wraps=sqlite_session.rollback)
    commit = MagicMock(wraps=sqlite_session.commit)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    monkeypatch.setattr(sqlite_session, "commit", commit)

    def fail_quota(*_args):
        raise RuntimeError("quota insert failed")

    def create_and_flush():
        if path == "legacy":
            AccountService.create_account("new@example.test", "New", "en-US", session=sqlite_session)
        else:
            AccountService.persist_account_creation(prepared, session=sqlite_session)
            sqlite_session.flush()

    event.listen(AccountMoneyExtend, "before_insert", fail_quota)
    try:
        with pytest.raises(RuntimeError, match="quota insert failed"):
            create_and_flush()
        if path == "prepared":
            rollback.assert_not_called()
            commit.assert_not_called()
            sqlite_session.rollback()
        else:
            commit.assert_called_once()
        rollback.assert_called_once()
    finally:
        event.remove(AccountMoneyExtend, "before_insert", fail_quota)
    assert _count(sqlite_session, Account) == _count(sqlite_session, AccountMoneyExtend) == 0


def test_account_quota_identity_and_intent_roll_back_together(sqlite_session):
    integration = CasdoorIntegrationExtend()
    sqlite_session.add(integration)
    sqlite_session.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer="https://synthetic.example",
        organization="Org",
        application="App",
        client_id="Client",
        core_fingerprint="a" * 64,
    )
    sqlite_session.add(namespace)
    sqlite_session.flush()
    revision = CasdoorConfigRevisionExtend(
        integration_id=integration.id,
        namespace_id=namespace.id,
        revision_number=1,
        config_digest="b" * 64,
        browser_frontend_url="https://synthetic.example",
        backend_api_url="https://synthetic.example",
        expected_issuer=namespace.expected_issuer,
        organization="Org",
        application="App",
        client_id="Client",
        button_text="SSO",
        default_workspace_id=str(uuid4()),
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    sqlite_session.add(revision)
    sqlite_session.commit()
    prepared = _prepare()

    def provision_then_fail():
        with sqlite_session.begin():
            account = AccountService.persist_account_creation(prepared, session=sqlite_session)
            identity = CasdoorIdentityExtend(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization="Org",
                subject="synthetic-subject",
                last_applied_json="{}",
                profile_sync_json="{}",
            )
            sqlite_session.add(identity)
            sqlite_session.flush()
            sqlite_session.add(
                CasdoorSyncIntentExtend(
                    namespace_id=namespace.id,
                    identity_id=identity.id,
                    account_id=account.id,
                    revision_id=revision.id,
                    generation=1,
                    ownership_epoch=0,
                    fence_epoch=0,
                    kind=CasdoorIntentKind.ROLE_REPLACE,
                    scope_digest="c" * 64,
                    idempotency_key="d" * 64,
                    desired_json="{}",
                )
            )
            sqlite_session.flush()
            for model in (Account, AccountMoneyExtend, CasdoorIdentityExtend, CasdoorSyncIntentExtend):
                assert _count(sqlite_session, model) == 1
            raise RuntimeError("later intent failure")

    with pytest.raises(RuntimeError, match="later intent failure"):
        provision_then_fail()
    for model in (Account, AccountMoneyExtend, CasdoorIdentityExtend, CasdoorSyncIntentExtend):
        assert _count(sqlite_session, model) == 0
