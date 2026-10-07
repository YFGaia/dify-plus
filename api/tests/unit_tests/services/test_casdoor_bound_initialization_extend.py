"""Existing-account eligibility and caller-owned setup/status persistence."""

from copy import copy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.account_money_extend import AccountMoneyExtend
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from services.account_activation_service import (
    AccountActivationService,
    EmailDomainSuspendedError,
    FrozenAccountError,
    InvalidAccountInitializationError,
)
from services.entities.account_activation_entities import AccountSetup


def _service(factory):
    effects = [Mock() for _ in range(5)]
    tokens, policy, eligibility, cache, sync = effects
    eligibility.get_freeze_type.return_value = None
    service = AccountActivationService(
        tokens=tokens,
        accounts=SQLAlchemyAccountActivationRepository(factory),
        workspace_policy=policy,
        eligibility=eligibility,
        membership_cache=cache,
        member_access_sync=sync,
    )
    return service, effects


def _seed(session, status=AccountStatus.PENDING, initialized_at=None):
    account = Account(
        name="Local name",
        email="Exact.Local@example.test",
        status=status,
        initialized_at=initialized_at,
        interface_language="ja-JP",
        timezone="Asia/Tokyo",
        interface_theme="dark",
        password="unchanged",
        password_salt="unchanged-salt",
    )
    session.add(account)
    session.commit()
    return account


def _prepare(service, account, **overrides):
    values = {
        "account_id": account.id,
        "account_email": account.email,
        "account_status": account.status,
        "initialized_at": account.initialized_at,
        "setup": AccountSetup("Ready", "en-US", "UTC") if account.initialized_at is None else None,
    }
    values.update(overrides)
    return service.prepare_bound_initialization(**values)


def _no_sql(engine):
    calls = []

    def capture(*args):
        calls.append(args[2])

    event.listen(engine, "before_cursor_execute", capture)
    return calls, capture


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED, AccountStatus.ACTIVE])
def test_setup_preparation_before_uow_then_original_fields_only(
    status, sqlite_session, sqlite_session_factory, sqlite_engine, monkeypatch
):
    account = _seed(sqlite_session, status)
    service, effects = _service(sqlite_session_factory)
    effects[2].get_freeze_type.side_effect = lambda email: (
        pytest.fail("freeze called inside transaction") if sqlite_session.in_transaction() else None
    )
    prepared = _prepare(service, account)
    assert not sqlite_session.in_transaction()
    for effect in effects[:2] + effects[3:]:
        assert effect.mock_calls == []
    with pytest.raises(FrozenInstanceError):
        prepared.account_email = "other@example.test"
    commit, flush, rollback = Mock(), Mock(), Mock()
    with sqlite_session.begin():
        monkeypatch.setattr(sqlite_session, "commit", commit)
        monkeypatch.setattr(sqlite_session, "flush", flush)
        monkeypatch.setattr(sqlite_session, "rollback", rollback)
        calls, capture = _no_sql(sqlite_engine)
        try:
            service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
            assert calls == []
            assert account.name == "Ready"
            assert account.interface_language == "en-US"
            assert account.timezone == "UTC"
            assert account.interface_theme == "light"
            assert account.status == AccountStatus.ACTIVE
            assert type(account.initialized_at) is datetime
            assert account.password == "unchanged"
            assert account.password_salt == "unchanged-salt"
            assert not account._current_tenant
            for effect in (commit, flush, rollback):
                effect.assert_not_called()
        finally:
            event.remove(sqlite_engine, "before_cursor_execute", capture)
        monkeypatch.undo()
    with sqlite_session_factory() as reader:
        assert reader.get(Account, account.id).name == "Ready"
        assert reader.scalar(select(func.count()).select_from(TenantAccountJoin)) == 0
        assert reader.scalar(select(func.count()).select_from(AccountMoneyExtend)) == 0


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED])
def test_already_initialized_activation_preserves_all_original_metadata(
    status, sqlite_session, sqlite_session_factory, sqlite_engine
):
    timestamp = datetime(2020, 1, 2, 3, 4, 5)
    account = _seed(sqlite_session, status, timestamp)
    before = {key: value for key, value in account.__dict__.items() if key != "_sa_instance_state"}
    service, effects = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    effects[2].get_freeze_type.assert_called_once_with(account.email)
    with sqlite_session.begin():
        calls, capture = _no_sql(sqlite_engine)
        try:
            service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
            assert calls == []
        finally:
            event.remove(sqlite_engine, "before_cursor_execute", capture)
        for key, value in before.items():
            assert getattr(account, key) == (AccountStatus.ACTIVE if key == "status" else value)
    with sqlite_session_factory() as reader:
        fresh = reader.get(Account, account.id)
        assert fresh.status == AccountStatus.ACTIVE
        assert fresh.initialized_at == timestamp
        assert (fresh.name, fresh.interface_language, fresh.timezone, fresh.interface_theme) == (
            "Local name",
            "ja-JP",
            "Asia/Tokyo",
            "dark",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id": "invalid"},
        {"account_email": "invalid"},
        {"account_email": "x@example.test\n"},
        {"account_status": "banned"},
        {"account_status": "closed"},
        {"account_status": "unknown"},
        {"account_status": []},
        {"setup": None},
        {"setup": object()},
        {"setup": AccountSetup("", "en-US", "UTC")},
        {"setup": AccountSetup("x" * 256, "en-US", "UTC")},
        {"setup": AccountSetup("x\x00", "en-US", "UTC")},
        {"setup": AccountSetup("x", "invalid", "UTC")},
        {"setup": AccountSetup("x", "en-US", "invalid")},
        {"initialized_at": "2020-01-01"},
        {"initialized_at": datetime(2020, 1, 1, tzinfo=UTC)},
        {"initialized_at": datetime(2020, 1, 1)},
        {"account_status": "active", "initialized_at": datetime(2020, 1, 1), "setup": None},
    ],
)
def test_malformed_observations_reject_before_freeze(overrides, sqlite_session, sqlite_session_factory):
    account = _seed(sqlite_session)
    service, effects = _service(sqlite_session_factory)
    with pytest.raises(InvalidAccountInitializationError):
        _prepare(service, account, **overrides)
    for effect in effects:
        assert effect.mock_calls == []


@pytest.mark.parametrize(
    ("freeze", "error"), [("email_domain_suspended", EmailDomainSuspendedError), ("other-freeze", FrozenAccountError)]
)
@pytest.mark.parametrize("initialized", [False, True])
def test_both_paths_use_original_freeze_classification(
    freeze, error, initialized, sqlite_session, sqlite_session_factory
):
    account = _seed(sqlite_session, initialized_at=datetime(2020, 1, 1) if initialized else None)
    service, effects = _service(sqlite_session_factory)
    effects[2].get_freeze_type.return_value = freeze
    with pytest.raises(error):
        _prepare(service, account)
    assert account.status == AccountStatus.PENDING
    for effect in effects[:2] + effects[3:]:
        assert effect.mock_calls == []


@pytest.mark.parametrize("bad_state", ["no-root", "nested", "dirty", "new", "deleted", "expired", "detached"])
def test_invalid_uow_or_orm_rejects_without_sql_and_consumes_receipt(
    bad_state, sqlite_session, sqlite_session_factory, sqlite_engine
):
    account = _seed(sqlite_session)
    service, _ = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    if bad_state != "no-root":
        sqlite_session.begin()
    if bad_state == "nested":
        sqlite_session.begin_nested()
    elif bad_state == "dirty":
        account.name = "changed locally"
    elif bad_state == "new":
        sqlite_session.add(Account(name="Other", email="other@example.test"))
    elif bad_state == "deleted":
        sqlite_session.delete(account)
    elif bad_state == "expired":
        sqlite_session.expire(account, ["email"])
    elif bad_state == "detached":
        sqlite_session.expunge(account)
    calls, capture = _no_sql(sqlite_engine)
    try:
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
        assert calls == []
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", capture)
    sqlite_session.rollback()
    with sqlite_session.begin(), pytest.raises(InvalidAccountInitializationError):
        service.persist_bound_initialization(prepared, account=account, session=sqlite_session)


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "different@example.test"},
        {"status": AccountStatus.BANNED},
        {"status": AccountStatus.CLOSED},
        {"status": AccountStatus.UNINITIALIZED},
        {"initialized_at": datetime(2020, 1, 1)},
    ],
)
def test_current_db_reconstruction_must_match_preparation(
    changes, sqlite_session, sqlite_session_factory, sqlite_engine
):
    account = _seed(sqlite_session)
    service, _ = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    for field, value in changes.items():
        setattr(account, field, value)
    sqlite_session.commit()
    with sqlite_session.begin():
        calls, capture = _no_sql(sqlite_engine)
        try:
            with pytest.raises(InvalidAccountInitializationError):
                service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
            assert calls == []
        finally:
            event.remove(sqlite_engine, "before_cursor_execute", capture)


def test_exact_account_owner_copy_replace_and_rollback_attempt(sqlite_session, sqlite_session_factory):
    account = _seed(sqlite_session)
    other = _seed(sqlite_session)
    service, _ = _service(sqlite_session_factory)
    foreign, _ = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    with sqlite_session.begin():
        for fake in (copy(prepared), replace(prepared)):
            with pytest.raises(InvalidAccountInitializationError):
                service.persist_bound_initialization(fake, account=account, session=sqlite_session)
        with pytest.raises(InvalidAccountInitializationError):
            foreign.persist_bound_initialization(prepared, account=account, session=sqlite_session)
        service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
        sqlite_session.rollback()
    sqlite_session.refresh(account)
    assert account.status == AccountStatus.PENDING
    assert account.initialized_at is None
    with pytest.raises(InvalidAccountInitializationError):
        service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
    sqlite_session.rollback()
    sqlite_session.refresh(account)
    sqlite_session.refresh(other)
    sqlite_session.commit()
    prepared = _prepare(service, account)
    with sqlite_session.begin(), pytest.raises(InvalidAccountInitializationError):
        service.persist_bound_initialization(prepared, account=other, session=sqlite_session)


def test_wrong_session_and_failed_transaction_reject_zero_sql(sqlite_session, sqlite_session_factory, sqlite_engine):
    account = _seed(sqlite_session)
    service, _ = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    with sqlite_session_factory.begin() as another:
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_bound_initialization(prepared, account=account, session=another)
    prepared = _prepare(service, account)
    sqlite_session.add(Account(name="Invalid", email=None))
    with pytest.raises(IntegrityError):
        sqlite_session.flush()
    calls, capture = _no_sql(sqlite_engine)
    try:
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
        assert calls == []
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", capture)
    sqlite_session.rollback()


@pytest.mark.parametrize("initialized", [False, True])
def test_quota_existing_membership_and_current_workspace_are_preserved(
    initialized, sqlite_session, sqlite_session_factory
):
    account = _seed(sqlite_session, initialized_at=datetime(2020, 1, 1) if initialized else None)
    tenant = Tenant(name="Unrelated current workspace")
    quota = AccountMoneyExtend(
        account_id=account.id, total_quota=Decimal("101.2345678"), used_quota=Decimal("7.0000001")
    )
    sqlite_session.add_all([tenant, quota])
    sqlite_session.flush()
    join = TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.EDITOR, current=True)
    sqlite_session.add(join)
    sqlite_session.commit()
    service, effects = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    with sqlite_session.begin():
        service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
    with sqlite_session_factory() as reader:
        fresh_quota = reader.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account.id))
        assert fresh_quota.total_quota == Decimal("101.2345678")
        assert fresh_quota.used_quota == Decimal("7.0000001")
        fresh_join = reader.get(TenantAccountJoin, join.id)
        assert (fresh_join.tenant_id, fresh_join.role, fresh_join.current) == (
            tenant.id,
            TenantAccountRole.EDITOR,
            True,
        )
    for effect in effects[:2] + effects[3:]:
        assert effect.mock_calls == []


def test_status_only_rechecks_original_timestamp(sqlite_session, sqlite_session_factory):
    account = _seed(sqlite_session, initialized_at=datetime(2020, 1, 1))
    service, _ = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    account.initialized_at = datetime(2021, 1, 1)
    sqlite_session.commit()
    with sqlite_session.begin(), pytest.raises(InvalidAccountInitializationError):
        service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
    assert account.status == AccountStatus.PENDING


def test_field_owner_failure_consumes_attempt_without_rollback(sqlite_session, sqlite_session_factory, monkeypatch):
    account = _seed(sqlite_session)
    service, _ = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    owner = Mock(side_effect=RuntimeError("simulated setup owner failure"))
    rollback = Mock()
    monkeypatch.setattr(SQLAlchemyAccountActivationRepository, "persist_account_setup", owner)
    with sqlite_session.begin():
        with monkeypatch.context() as scoped:
            scoped.setattr(sqlite_session, "rollback", rollback)
            with pytest.raises(RuntimeError, match="simulated setup owner failure"):
                service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
            rollback.assert_not_called()
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
    owner.assert_called_once_with(account, prepared.setup)
