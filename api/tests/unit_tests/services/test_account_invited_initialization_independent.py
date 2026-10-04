"""Independent transaction and authority checks for invited account setup."""

import json
from decimal import Decimal
from unittest.mock import Mock

import pytest
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from services.account_activation_service import AccountActivationService, InvalidAccountInitializationError
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import AccountSetup, InvitationLookup
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

SETUP = AccountSetup("Independent", "en-US", "UTC")


def seed(session, marker=None):
    account = Account(
        name="Before",
        email="independent@example.test",
        status=AccountStatus.PENDING,
        initialized_at=marker,
        interface_language="ja-JP",
        timezone="Asia/Tokyo",
        interface_theme="dark",
        password="kept",
        password_salt="kept-salt",
    )
    workspace = Tenant(name="Workspace")
    session.add_all([account, workspace])
    session.flush()
    quota = AccountMoneyExtend(
        account_id=account.id, total_quota=Decimal("51.1234567"), used_quota=Decimal("3.0000001")
    )
    membership = TenantAccountJoin(account_id=account.id, tenant_id=workspace.id, role="editor", current=True)
    session.add_all([quota, membership])
    session.commit()
    return account, workspace, quota, membership


def service_for(factory, account, workspace, payload_updates=None):
    payload = {
        "account_id": account.id,
        "email": account.email,
        "workspace_id": workspace.id,
        "role": "editor",
        "requires_setup": False,
    }
    payload.update(payload_updates or {})
    redis, policy, eligibility, cache, sync = (Mock() for _ in range(5))
    redis.get.return_value = json.dumps(payload).encode()
    eligibility.get_freeze_type.return_value = None
    service = AccountActivationService(
        tokens=RedisInvitationTokenStore(redis=redis),
        accounts=SQLAlchemyAccountActivationRepository(factory),
        workspace_policy=policy,
        eligibility=eligibility,
        membership_cache=cache,
        member_access_sync=sync,
    )
    return service, (redis, policy, eligibility, cache, sync)


def prepare(service):
    return service.prepare_invited_initialization(
        InvitationLookup(None, None, "actual-token"), setup=SETUP, authenticated_account_id=None
    )


def capture_sql(engine):
    statements = []

    def listener(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    return statements, listener


def test_equal_field_forgery_cannot_consume_authentic_receipt(sqlite_session, sqlite_session_factory, sqlite_engine):
    account, workspace, _, _ = seed(sqlite_session)
    service, _ = service_for(sqlite_session_factory, account, workspace)
    authentic = prepare(service)
    forged = type(authentic)(
        authentic.observation,
        authentic.account_id,
        authentic.account_email,
        authentic.account_status,
        authentic.initialized_at,
        authentic.setup,
        authentic._owner,
    )
    assert forged is not authentic
    assert (
        forged.observation,
        forged.account_id,
        forged.account_email,
        forged.account_status,
        forged.initialized_at,
        forged.setup,
        forged._owner,
    ) == (
        authentic.observation,
        authentic.account_id,
        authentic.account_email,
        authentic.account_status,
        authentic.initialized_at,
        authentic.setup,
        authentic._owner,
    )
    statements, listener = capture_sql(sqlite_engine)
    try:
        with sqlite_session.begin():
            with pytest.raises(InvalidAccountInitializationError):
                service.persist_invited_initialization(forged, account=account, session=sqlite_session)
            assert statements == []
            service.persist_invited_initialization(authentic, account=account, session=sqlite_session)
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", listener)
    with sqlite_session_factory() as reader:
        saved = reader.get(Account, account.id)
        assert (saved.name, saved.status, saved.initialized_at is not None) == (
            "Independent",
            AccountStatus.ACTIVE,
            True,
        )


def test_real_token_metadata_cannot_override_marker_or_bypass_original_eligibility(
    sqlite_session, sqlite_session_factory
):
    account, workspace, _, _ = seed(sqlite_session, marker=None)
    service, effects = service_for(sqlite_session_factory, account, workspace, {"requires_setup": False})
    prepared = prepare(service)
    assert prepared.observation.invitation.requires_setup is False
    effects[2].get_freeze_type.assert_called_once_with(account.email)
    with sqlite_session.begin():
        service.persist_invited_initialization(prepared, account=account, session=sqlite_session)
    with sqlite_session_factory() as reader:
        saved = reader.get(Account, account.id)
        assert (saved.name, saved.interface_language, saved.timezone, saved.interface_theme) == (
            "Independent",
            "en-US",
            "UTC",
            "light",
        )
        assert saved.initialized_at is not None
    effects[0].get.assert_called_once_with("member_invite:token:actual-token")
    effects[0].delete.assert_not_called()
    assert effects[3].mock_calls == effects[4].mock_calls == []


def test_marker_and_preferences_drift_after_prepare_rejects_before_assignment(
    sqlite_session, sqlite_session_factory, sqlite_engine
):
    account, workspace, _, _ = seed(sqlite_session)
    service, _ = service_for(sqlite_session_factory, account, workspace)
    prepared = prepare(service)
    with sqlite_session_factory.begin() as writer:
        writer.execute(
            text("UPDATE accounts SET initialized_at=CURRENT_TIMESTAMP, interface_theme='light' WHERE id=:id"),
            {"id": account.id},
        )
    with sqlite_session.begin():
        fresh = sqlite_session.scalar(
            select(Account).where(Account.id == account.id).execution_options(populate_existing=True).with_for_update()
        )
        assignments = []
        original = Account.__setattr__

        def record(instance, name, value):
            if instance is fresh:
                assignments.append(name)
            return original(instance, name, value)

        statements, listener = capture_sql(sqlite_engine)
        try:
            with pytest.MonkeyPatch.context() as scoped:
                scoped.setattr(Account, "__setattr__", record)
                with pytest.raises(InvalidAccountInitializationError):
                    service.persist_invited_initialization(prepared, account=fresh, session=sqlite_session)
            assert assignments == []
            assert statements == []
        finally:
            event.remove(sqlite_engine, "before_cursor_execute", listener)
    with sqlite_session_factory() as reader:
        saved = reader.get(Account, account.id)
        assert saved.name == "Before"
        assert saved.initialized_at is not None
        assert saved.interface_theme == "light"
        assert saved.password == "kept" and saved.password_salt == "kept-salt"


def test_later_sql_failure_rolls_back_caller_and_setup_but_receipt_stays_consumed(
    sqlite_session, sqlite_session_factory
):
    account, workspace, quota, membership = seed(sqlite_session)
    service, _ = service_for(sqlite_session_factory, account, workspace)
    prepared = prepare(service)
    with pytest.raises(IntegrityError, match="NOT NULL constraint failed: accounts.email"):
        with sqlite_session.begin():
            workspace.name = "caller earlier write"
            sqlite_session.flush()
            service.persist_invited_initialization(prepared, account=account, session=sqlite_session)
            sqlite_session.flush()
            sqlite_session.add(Account(name="invalid later insert", email=None))
            sqlite_session.flush()
    with sqlite_session_factory() as reader:
        saved = reader.get(Account, account.id)
        assert (saved.name, saved.status, saved.initialized_at) == ("Before", AccountStatus.PENDING, None)
        assert reader.get(Tenant, workspace.id).name == "Workspace"
        assert reader.get(AccountMoneyExtend, quota.id).total_quota == Decimal("51.1234567")
        persisted_membership = reader.get(TenantAccountJoin, membership.id)
        assert persisted_membership.current is True and persisted_membership.role == "editor"
    sqlite_session.rollback()
    retry = prepare(service)
    with sqlite_session.begin():
        fresh = sqlite_session.scalar(
            select(Account).where(Account.id == account.id).execution_options(populate_existing=True).with_for_update()
        )
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_invited_initialization(prepared, account=fresh, session=sqlite_session)
        service.persist_invited_initialization(retry, account=fresh, session=sqlite_session)
