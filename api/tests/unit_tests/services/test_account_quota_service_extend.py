"""Real account creation paths must commit their quota atomically."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from repositories.account_oauth_repository import AccountServiceOAuthAccountRegistrationGateway
from services import account_service
from services.account_email_registration_adapters import AccountServiceRegistrationGateway
from services.account_quota_service_extend import ensure_account_quota_extend
from services.account_service import AccountService, RegisterService, TenantService
from services.entities.account_oauth_entities import OAuthAccountRegistration
from services.errors.account import AccountNormalizedEmailAlreadyInUseError, SeatsLimitExceededError
from tests.unit_tests.services.test_invitation_publication_transport_extend import Wire, client_for


@pytest.fixture(autouse=True)
def dependencies(monkeypatch, config_overrides):
    config_overrides(
        DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, RBAC_ENABLED=False, ACCOUNT_TOTAL_QUOTA=Decimal(15)
    )
    features = MagicMock()
    features.is_registration_allowed.return_value = True
    features.is_workspace_creation_allowed.return_value = False
    features.get_license.return_value.seats.is_available.return_value = True
    monkeypatch.setattr(account_service, "SystemFeatureService", features)
    monkeypatch.setattr(TenantService, "create_owner_tenant_if_not_exist", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(account_service.CommunityTelemetryService, "report_install", lambda **_kwargs: None)
    monkeypatch.setattr("libs.workspace_permission.check_workspace_member_invite_permission", lambda *_args: None)
    monkeypatch.setattr(TenantService, "check_member_permission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(account_service.send_invite_member_mail_task, "delay", lambda **_kwargs: None)
    # Keep the original issuer/publisher and redis-py transport; only socket I/O is offline.
    invitation_redis, _, _ = client_for(Wire(), monkeypatch)
    monkeypatch.setattr(account_service, "redis_client", invitation_redis)
    return features


def _create(path: str, factory: sessionmaker[Session]) -> str:
    if path == "email":
        return AccountServiceRegistrationGateway(session_factory=factory).create(
            email="new@example.com",
            password="password123",
            interface_language="en-US",
            timezone=None,
            ip_address="127.0.0.1",
        )
    if path == "oauth":
        return AccountServiceOAuthAccountRegistrationGateway(session_factory=factory).register(
            OAuthAccountRegistration(
                email="new@example.com", name="New", language="en-US", timezone=None, ip_address="127.0.0.1"
            )
        )
    with factory() as session:
        if path == "setup":
            RegisterService.setup("new@example.com", "New", "password123", "127.0.0.1", "en-US", session=session)
        else:
            inviter = Account(name="Inviter", email="inviter@example.com", status=AccountStatus.ACTIVE)
            tenant = Tenant(name="Invitation workspace")
            session.add_all([inviter, tenant])
            session.commit()
            session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=inviter.id, role="owner", current=True))
            session.commit()
            RegisterService.invite_new_member(tenant, "new@example.com", "en-US", inviter=inviter, session=session)
        return session.scalar(select(Account.id).where(Account.email == "new@example.com"))


@pytest.mark.parametrize("path", ["setup", "email", "oauth", "invitation"])
def test_each_creation_path_creates_one_quota(path, sqlite_session_factory):
    account_id = _create(path, sqlite_session_factory)
    with sqlite_session_factory() as session:
        quota = session.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account_id))
        assert quota is not None
        assert quota.total_quota == Decimal(15)
        assert quota.used_quota == 0
        assert session.scalar(select(func.count()).select_from(AccountMoneyExtend)) == 1
        quota.total_quota, quota.used_quota = Decimal(77), Decimal(11)
        session.commit()
        ensure_account_quota_extend(account_id, session=session)
        ensure_account_quota_extend(account_id, session=session)
        session.commit()
        assert quota.total_quota == Decimal(77)
        assert quota.used_quota == Decimal(11)


@pytest.mark.parametrize("path", ["setup", "email", "oauth", "invitation"])
def test_quota_insert_failure_never_persists_new_account(path, sqlite_session_factory):
    def fail(*_args):
        raise RuntimeError("quota insert failed")

    event.listen(AccountMoneyExtend, "before_insert", fail)
    try:
        with pytest.raises(Exception, match="quota insert failed"):
            _create(path, sqlite_session_factory)
    finally:
        event.remove(AccountMoneyExtend, "before_insert", fail)
    with sqlite_session_factory() as session:
        assert session.scalar(select(Account).where(Account.email == "new@example.com")) is None
        assert session.scalar(select(func.count()).select_from(AccountMoneyExtend)) == 0


def test_existing_login_and_repeated_invitation_do_not_reset_quota(sqlite_session_factory, monkeypatch):
    account_id = _create("invitation", sqlite_session_factory)
    monkeypatch.setattr(AccountService, "issue_token_pair", lambda _account_id: "session-tokens")
    with sqlite_session_factory() as session:
        quota = session.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account_id))
        quota.total_quota, quota.used_quota = Decimal(99), Decimal(7)
        session.commit()
        account = session.get(Account, account_id)
        tenant = session.scalar(select(Tenant))
        inviter = session.scalar(select(Account).where(Account.email == "inviter@example.com"))
        RegisterService.invite_new_member(tenant, account.email, "en-US", inviter=inviter, session=session)
        AccountService.login(account, session=session)
        assert quota.total_quota == Decimal(99)
        assert quota.used_quota == Decimal(7)
        assert session.scalar(select(func.count()).select_from(AccountMoneyExtend)) == 1


def test_alias_and_seat_rejections_leave_no_additional_quota(sqlite_session_factory, dependencies):
    with sqlite_session_factory() as session:
        AccountService.create_account("n.e.w@gmail.com", "Original", "en-US", session=session)
        with pytest.raises(AccountNormalizedEmailAlreadyInUseError):
            AccountService.create_account(
                "new+tag@googlemail.com", "Alias", "en-US", check_normalized_email=True, session=session
            )
        dependencies.get_license.return_value.seats.is_available.return_value = False
        with pytest.raises(SeatsLimitExceededError):
            AccountService.create_account("another@example.com", "No seat", "en-US", session=session)
        assert session.scalar(select(func.count()).select_from(AccountMoneyExtend)) == 1
        assert session.scalar(select(func.count()).select_from(Account)) == 1


def test_dingtalk_new_account_supplies_session_and_gets_quota(sqlite_session_factory, monkeypatch, app):
    from types import SimpleNamespace

    from sqlalchemy.orm import scoped_session

    from services import ding_talk_extend

    sessions = scoped_session(sqlite_session_factory)
    monkeypatch.setattr(ding_talk_extend, "db", SimpleNamespace(session=sessions))
    monkeypatch.setattr(ding_talk_extend.DingTalkService, "get_access_token", lambda: ("dummy-token", ""))
    monkeypatch.setattr(
        ding_talk_extend.requests,
        "post",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200, json=lambda: {"errcode": 0, "result": {"name": "New", "email": "ding@example.com"}}
        ),
    )
    monkeypatch.setattr(ding_talk_extend.TenantExtendService, "get_super_admin_id", lambda: SimpleNamespace(id=None))
    monkeypatch.setattr(
        ding_talk_extend.TenantExtendService, "get_super_admin_tenant_id", lambda: SimpleNamespace(id=None)
    )
    observed = []

    def login(account, *, session, ip_address):
        assert ip_address
        assert session is sessions()
        quota = session.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account.id))
        assert quota is not None
        assert quota.used_quota == 0
        assert quota.total_quota == 15
        observed.append(account.id)
        return "session-tokens"

    monkeypatch.setattr(AccountService, "login", login)
    try:
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            assert ding_talk_extend.DingTalkService.auto_create_user("ding-id") == ("session-tokens", "")
        assert len(observed) == 1
    finally:
        sessions.remove()


def test_disabled_registration_creates_neither_account_nor_quota(sqlite_session_factory, dependencies):
    from services.errors.account import AccountNotFoundError

    dependencies.is_registration_allowed.return_value = False
    with sqlite_session_factory() as session:
        with pytest.raises(AccountNotFoundError):
            AccountService.create_account("disabled@example.com", "Disabled", "en-US", session=session)
        assert session.scalar(select(func.count()).select_from(Account)) == 0
        assert session.scalar(select(func.count()).select_from(AccountMoneyExtend)) == 0


def test_setup_late_failure_cleans_only_its_new_quota(sqlite_session_factory, monkeypatch):
    from uuid import uuid4

    unrelated_id = str(uuid4())
    with sqlite_session_factory.begin() as session:
        session.add(AccountMoneyExtend(id=str(uuid4()), account_id=unrelated_id, total_quota=99, used_quota=7))

    def fail_workspace(*_args, **_kwargs):
        raise RuntimeError("workspace failed")

    monkeypatch.setattr(TenantService, "create_owner_tenant_if_not_exist", fail_workspace)
    with sqlite_session_factory() as session:
        with pytest.raises(ValueError, match="workspace failed"):
            RegisterService.setup("new@example.com", "New", "password123", "127.0.0.1", "en-US", session=session)
    with sqlite_session_factory() as session:
        rows = session.scalars(select(AccountMoneyExtend)).all()
        assert len(rows) == 1
        assert (rows[0].account_id, rows[0].total_quota, rows[0].used_quota) == (unrelated_id, 99, 7)
        assert session.scalar(select(Account).where(Account.email == "new@example.com")) is None
