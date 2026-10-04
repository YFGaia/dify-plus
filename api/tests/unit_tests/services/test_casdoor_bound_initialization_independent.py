"""Independent counterexamples for the bound account initialization seam."""

from datetime import datetime
from unittest.mock import Mock

import pytest
from models.account import Account, AccountStatus
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from services.account_activation_service import (
    AccountActivationService,
    InvalidAccountInitializationError,
    _PreparedBoundInitialization,
)
from services.entities.account_activation_entities import AccountSetup
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError


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
    return service


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


def _prepare(service, account):
    return service.prepare_bound_initialization(
        account_id=account.id,
        account_email=account.email,
        account_status=account.status,
        initialized_at=account.initialized_at,
        setup=AccountSetup("Ready", "en-US", "UTC") if account.initialized_at is None else None,
    )


def _no_sql(engine):
    calls = []

    def capture(*args):
        calls.append(args[2])

    event.listen(engine, "before_cursor_execute", capture)
    return calls, capture


def test_equal_field_forgery_does_not_consume_live_owner_receipt(sqlite_session, sqlite_session_factory):
    account = _seed(sqlite_session)
    service = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    forged = _PreparedBoundInitialization(
        prepared.account_id,
        prepared.account_email,
        prepared.account_status,
        prepared.initialized_at,
        prepared.setup,
        service,
    )

    with sqlite_session.begin():
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_bound_initialization(forged, account=account, session=sqlite_session)
        service.persist_bound_initialization(prepared, account=account, session=sqlite_session)

    assert account.status == AccountStatus.ACTIVE


def test_status_only_rollback_keeps_historical_metadata_and_requires_new_receipt(
    sqlite_session, sqlite_session_factory
):
    initialized_at = datetime(2020, 1, 2, 3, 4, 5)
    account = _seed(sqlite_session, AccountStatus.UNINITIALIZED, initialized_at)
    service = _service(sqlite_session_factory)
    prepared = _prepare(service, account)

    with sqlite_session.begin():
        service.persist_bound_initialization(prepared, account=account, session=sqlite_session)
        sqlite_session.rollback()

    with sqlite_session_factory() as reader:
        restored = reader.get(Account, account.id)
        assert restored.status == AccountStatus.UNINITIALIZED
        assert restored.initialized_at == initialized_at
        assert (restored.name, restored.interface_language, restored.timezone, restored.interface_theme) == (
            "Local name",
            "ja-JP",
            "Asia/Tokyo",
            "dark",
        )

    sqlite_session.rollback()
    with pytest.raises(InvalidAccountInitializationError):
        with sqlite_session.begin():
            service.persist_bound_initialization(prepared, account=account, session=sqlite_session)

    retry = _prepare(service, account)
    service.persist_bound_initialization(retry, account=account, session=sqlite_session)
    sqlite_session.commit()

    assert account.status == AccountStatus.ACTIVE
    assert account.initialized_at == initialized_at


@pytest.mark.parametrize("bad_state", ["dirty", "expired", "failed"])
def test_status_only_rejects_invalid_uow_without_sql_and_consumes_receipt(
    bad_state, sqlite_session, sqlite_session_factory, sqlite_engine
):
    account = _seed(sqlite_session, AccountStatus.PENDING, datetime(2021, 3, 4, 5, 6, 7))
    service = _service(sqlite_session_factory)
    prepared = _prepare(service, account)
    sqlite_session.begin()
    if bad_state == "dirty":
        account.name = "unrelated pending edit"
    elif bad_state == "expired":
        sqlite_session.expire(account, ["interface_theme"])
    else:
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
    sqlite_session.refresh(account)
    retry = _prepare(service, account)
    service.persist_bound_initialization(retry, account=account, session=sqlite_session)
    sqlite_session.commit()

    assert account.status == AccountStatus.ACTIVE
    assert account.initialized_at == datetime(2021, 3, 4, 5, 6, 7)
