"""Real invitation-store decoding and exact account-only caller transactions."""

import json
from copy import copy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.account_money_extend import AccountMoneyExtend
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from services.account_activation_service import (
    AccountActivationService,
    EmailDomainSuspendedError,
    FrozenAccountError,
    InvalidAccountInitializationError,
    InvalidInvitationError,
    InvitationAccountMismatchError,
)
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import AccountSetup, InvitationLookup

STAMP = datetime(2020, 1, 2, 3, 4, 5)
SETUP = AccountSetup("Ready", "en-US", "UTC")


def seed(session, status=AccountStatus.PENDING, marker=None):
    account = Account(
        name="Original",
        email="invitee@example.test",
        status=status,
        initialized_at=marker,
        interface_language="ja-JP",
        timezone="Asia/Tokyo",
        interface_theme="dark",
        password="original",
        password_salt="salt",
    )
    tenant = Tenant(name="Invitation workspace")
    session.add_all([account, tenant])
    session.flush()
    quota = AccountMoneyExtend(
        account_id=account.id, total_quota=Decimal("101.2345678"), used_quota=Decimal("7.0000001")
    )
    join = TenantAccountJoin(account_id=account.id, tenant_id=tenant.id, role=TenantAccountRole.EDITOR, current=True)
    session.add_all([quota, join])
    session.commit()
    return account, tenant, quota, join


def service_for(factory, account, tenant, requires_setup=None):
    payload = {
        "account_id": account.id,
        "email": account.email,
        "workspace_id": tenant.id,
        "role": "admin",
        "requires_setup": requires_setup,
    }
    redis, policy, eligibility, cache, sync = (Mock() for _ in range(5))
    redis.get.return_value = json.dumps(payload).encode()
    eligibility.get_freeze_type.return_value = None
    tokens = RedisInvitationTokenStore(redis=redis)
    service = AccountActivationService(
        tokens=tokens,
        accounts=SQLAlchemyAccountActivationRepository(factory),
        workspace_policy=policy,
        eligibility=eligibility,
        membership_cache=cache,
        member_access_sync=sync,
    )
    return service, (redis, policy, eligibility, cache, sync), payload


def prepare(service, setup=SETUP, lookup=None, authenticated=None):
    return service.prepare_invited_initialization(
        lookup or InvitationLookup(None, None, "real-token"),
        setup=setup,
        authenticated_account_id=authenticated,
    )


def no_sql(engine):
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    return statements, capture


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED, AccountStatus.ACTIVE])
@pytest.mark.parametrize("marker", [None, STAMP])
@pytest.mark.parametrize("requires_setup", [None, True, False])
def test_marker_first_matrix_and_original_owners(
    status,
    marker,
    requires_setup,
    sqlite_session,
    sqlite_session_factory,
    sqlite_engine,
    monkeypatch,
):
    account, tenant, quota, join = seed(sqlite_session, status, marker)
    before = {key: value for key, value in account.__dict__.items() if key != "_sa_instance_state"}
    service, effects, _ = service_for(sqlite_session_factory, account, tenant, requires_setup)
    prepared = prepare(service, SETUP if marker is None else None)
    assert prepared.observation.invitation.role == "admin"
    assert prepared.observation.invitation.requires_setup is requires_setup
    effects[0].get.assert_called_once_with("member_invite:token:real-token")
    effects[1].ensure_allowed.assert_called_once_with(tenant.id)
    effects[2].get_freeze_type.assert_called_once_with(account.email)
    assignments = []
    original = Account.__setattr__

    def record(instance, name, value):
        if instance is account:
            assignments.append(name)
        return original(instance, name, value)

    with sqlite_session.begin():
        statements, capture = no_sql(sqlite_engine)
        with monkeypatch.context() as scoped:
            scoped.setattr(Account, "__setattr__", record)
            try:
                service.persist_invited_initialization(prepared, account=account, session=sqlite_session)
                assert statements == []
            finally:
                event.remove(sqlite_engine, "before_cursor_execute", capture)
        assert assignments == (
            ["name", "interface_language", "timezone", "interface_theme", "status", "initialized_at"]
            if marker is None
            else ["status"]
            if status != AccountStatus.ACTIVE
            else []
        )
    with sqlite_session_factory() as reader:
        fresh = reader.get(Account, account.id)
        assert fresh.status == AccountStatus.ACTIVE
        if marker is None:
            assert (fresh.name, fresh.interface_language, fresh.timezone, fresh.interface_theme) == (
                "Ready",
                "en-US",
                "UTC",
                "light",
            )
            assert fresh.initialized_at is not None
        else:
            for key in ("name", "interface_language", "timezone", "interface_theme", "initialized_at"):
                assert getattr(fresh, key) == before[key]
        assert fresh.password == "original"
        assert fresh.password_salt == "salt"
        fresh_quota = reader.get(AccountMoneyExtend, quota.id)
        assert (fresh_quota.total_quota, fresh_quota.used_quota) == (Decimal("101.2345678"), Decimal("7.0000001"))
        fresh_join = reader.get(TenantAccountJoin, join.id)
        assert (fresh_join.role, fresh_join.current, fresh_join.last_opened_at) == (
            join.role,
            True,
            join.last_opened_at,
        )
    effects[0].delete.assert_not_called()
    assert effects[3].mock_calls == effects[4].mock_calls == []


def test_reads_closed_before_original_policy_and_freeze(sqlite_session, sqlite_engine):
    account, tenant, _, _ = seed(sqlite_session)
    opened = []

    class ReadSession(Session):
        def __enter__(self):
            opened.append(self)
            return super().__enter__()

        def close(self):
            super().close()
            opened.remove(self)

    service, effects, _ = service_for(sessionmaker(sqlite_engine, class_=ReadSession), account, tenant)
    order = []

    def check_closed(kind):
        assert opened == []
        assert not sqlite_session.in_transaction()
        order.append(kind)

    effects[1].ensure_allowed.side_effect = lambda _: check_closed("policy")
    effects[2].get_freeze_type.side_effect = lambda _: check_closed("freeze")
    prepare(service)
    assert order == ["policy", "freeze"]


def test_original_workspace_key_case_fallback_and_metadata_defaults(sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    lookup = InvitationLookup(tenant.id, account.email.upper(), "case-token")
    lower = replace(lookup, email=account.email)
    key = RedisInvitationTokenStore._workspace_invitation_key(lower)
    effects[0].get.side_effect = lambda current: account.id.encode() if current == key else None
    prepared = prepare(service, lookup=lookup)
    assert effects[0].get.call_count == 2
    assert prepared.observation.invitation.role is None
    assert prepared.observation.invitation.requires_setup is None
    effects[2].get_freeze_type.assert_called_once_with(account.email)


@pytest.mark.parametrize("raw", [None, b"not-json", b"[]", b"{}", b"\xff", b'{"account_id":123}', b'"scalar"'])
def test_real_token_decoding_failures_are_fixed_invitation_error(raw, sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    effects[0].get.return_value = raw
    with pytest.raises(InvalidInvitationError):
        prepare(service)
    effects[1].ensure_allowed.assert_not_called()
    effects[2].get_freeze_type.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "bad"),
        ("account_id", str(uuid4()).upper()),
        ("workspace_id", "bad"),
        ("email", "bad"),
        ("email", "x" * 256 + "@example.test"),
        ("email", "x@example.test\n"),
    ],
)
def test_malformed_resolved_scalars_reject_before_sql(
    field, value, sqlite_session, sqlite_session_factory, sqlite_engine
):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, payload = service_for(sqlite_session_factory, account, tenant)
    payload[field] = value
    effects[0].get.return_value = json.dumps(payload).encode()
    statements, capture = no_sql(sqlite_engine)
    try:
        with pytest.raises(InvalidInvitationError):
            prepare(service)
        assert statements == []
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", capture)


@pytest.mark.parametrize(
    "lookup",
    [
        InvitationLookup(None, None, ""),
        InvitationLookup(None, None, "x" * 513),
        InvitationLookup("bad", None, "x"),
        InvitationLookup(None, "bad", "x"),
        InvitationLookup(None, None, "\ud800"),
    ],
)
def test_bad_lookup_never_reaches_store(lookup, sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    with pytest.raises(InvalidInvitationError):
        prepare(service, lookup=lookup)
    effects[0].get.assert_not_called()


@pytest.mark.parametrize(
    "change", ["missing-account", "missing-workspace", "email", "workspace-status", "banned", "closed", "unknown"]
)
def test_current_resolution_rejects_invalid_rows(change, sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, payload = service_for(sqlite_session_factory, account, tenant)
    if change == "missing-account":
        payload["account_id"] = str(uuid4())
    elif change == "missing-workspace":
        payload["workspace_id"] = str(uuid4())
    elif change == "email":
        account.email = "changed@example.test"
    elif change == "workspace-status":
        tenant.status = "archive"
    elif change == "unknown":
        sqlite_session.execute(text("UPDATE accounts SET status='unknown' WHERE id=:id"), {"id": account.id})
    else:
        account.status = change
    sqlite_session.commit()
    effects[0].get.return_value = json.dumps(payload).encode()
    with pytest.raises(InvalidInvitationError):
        prepare(service)
    effects[2].get_freeze_type.assert_not_called()


@pytest.mark.parametrize(
    ("marker", "setup"),
    [
        (None, None),
        (None, AccountSetup("", "en-US", "UTC")),
        (None, AccountSetup("x", "bad", "UTC")),
        (None, AccountSetup("x", "en-US", "bad")),
        (STAMP, SETUP),
    ],
)
def test_setup_validation_marker_first(marker, setup, sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session, marker=marker)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    with pytest.raises(InvalidInvitationError):
        prepare(service, setup)
    effects[2].get_freeze_type.assert_not_called()


@pytest.mark.parametrize(
    ("freeze", "error"), [("email_domain_suspended", EmailDomainSuspendedError), ("other", FrozenAccountError)]
)
@pytest.mark.parametrize("marker", [None, STAMP])
def test_exact_once_freeze_including_initialized_active(freeze, error, marker, sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session, AccountStatus.ACTIVE, marker)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    effects[2].get_freeze_type.return_value = freeze
    with pytest.raises(error):
        prepare(service, SETUP if marker is None else None)
    effects[2].get_freeze_type.assert_called_once_with(account.email)


def test_explicit_authenticated_account_must_match(sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    with pytest.raises(InvitationAccountMismatchError):
        prepare(service, authenticated=str(uuid4()))
    effects[2].get_freeze_type.assert_not_called()
    prepare(service, authenticated=account.id)


@pytest.mark.parametrize(
    "bad",
    ["no-root", "nested", "new", "dirty", "deleted", "expired", "detached", "wrong-account", "wrong-session", "failed"],
)
def test_bad_root_or_account_consumes_authentic_attempt_zero_sql(
    bad, sqlite_session, sqlite_session_factory, sqlite_engine
):
    account, tenant, _, _ = seed(sqlite_session)
    service, _, _ = service_for(sqlite_session_factory, account, tenant)
    prepared = prepare(service)
    target, session = account, sqlite_session
    if bad != "no-root":
        sqlite_session.begin()
    if bad == "nested":
        sqlite_session.begin_nested()
    elif bad == "new":
        sqlite_session.add(Account(name="New", email="new@example.test"))
    elif bad == "dirty":
        account.name = "dirty"
    elif bad == "deleted":
        sqlite_session.delete(account)
    elif bad == "expired":
        sqlite_session.expire(account, ["interface_theme"])
    elif bad == "detached":
        sqlite_session.expunge(account)
    elif bad == "wrong-account":
        target = Account(name="Detached", email="other@example.test")
    elif bad == "wrong-session":
        session = sqlite_session_factory()
        session.begin()
    elif bad == "failed":
        sqlite_session.add(Account(name="Invalid", email=None))
        with pytest.raises(IntegrityError):
            sqlite_session.flush()
    statements, capture = no_sql(sqlite_engine)
    try:
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_invited_initialization(prepared, account=target, session=session)
        assert statements == []
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", capture)
        if session is not sqlite_session:
            session.close()
    sqlite_session.rollback()
    with sqlite_session.begin(), pytest.raises(InvalidAccountInitializationError):
        service.persist_invited_initialization(prepared, account=account, session=sqlite_session)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Changed"),
        ("email", "new@example.test"),
        ("status", AccountStatus.UNINITIALIZED),
        ("initialized_at", STAMP),
        ("interface_theme", "other"),
    ],
)
def test_fresh_current_snapshot_drift_rejected(field, value, sqlite_session, sqlite_session_factory, sqlite_engine):
    account, tenant, _, _ = seed(sqlite_session)
    service, _, _ = service_for(sqlite_session_factory, account, tenant)
    prepared = prepare(service)
    setattr(account, field, value)
    sqlite_session.commit()
    with sqlite_session.begin():
        statements, capture = no_sql(sqlite_engine)
        try:
            with pytest.raises(InvalidAccountInitializationError):
                service.persist_invited_initialization(prepared, account=account, session=sqlite_session)
            assert statements == []
        finally:
            event.remove(sqlite_engine, "before_cursor_execute", capture)


def test_copies_equal_fields_and_foreign_owner_cannot_consume_live_original(sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, _, _ = service_for(sqlite_session_factory, account, tenant)
    foreign, _, _ = service_for(sqlite_session_factory, account, tenant)
    prepared = prepare(service)
    with sqlite_session.begin():
        for forged in (copy(prepared), replace(prepared), replace(prepared, setup=SETUP)):
            with pytest.raises(InvalidAccountInitializationError):
                service.persist_invited_initialization(forged, account=account, session=sqlite_session)
        with pytest.raises(InvalidAccountInitializationError):
            foreign.persist_invited_initialization(prepared, account=account, session=sqlite_session)
        service.persist_invited_initialization(prepared, account=account, session=sqlite_session)
    with sqlite_session.begin(), pytest.raises(InvalidAccountInitializationError):
        service.persist_invited_initialization(prepared, account=account, session=sqlite_session)


def test_later_real_write_failure_whole_root_rollback_then_fresh_retry(sqlite_session, sqlite_session_factory):
    account, tenant, quota, join = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    prepared = prepare(service)

    def failing_caller():
        with sqlite_session.begin():
            sqlite_session.execute(text("BEGIN"))
            tenant.name = "Earlier write"
            sqlite_session.flush()
            service.persist_invited_initialization(prepared, account=account, session=sqlite_session)
            sqlite_session.flush()
            with sqlite_session_factory() as reader:
                assert reader.get(Account, account.id).name == "Original"
                assert reader.get(Tenant, tenant.id).name == "Invitation workspace"
            sqlite_session.add(Account(name="Real invalid later insert", email=None))
            sqlite_session.flush()

    with pytest.raises(IntegrityError):
        failing_caller()

    with sqlite_session_factory() as reader:
        fresh = reader.get(Account, account.id)
        assert (fresh.name, fresh.status, fresh.initialized_at) == ("Original", AccountStatus.PENDING, None)
        assert reader.get(Tenant, tenant.id).name == "Invitation workspace"
        assert reader.get(AccountMoneyExtend, quota.id).total_quota == Decimal("101.2345678")
        assert reader.get(TenantAccountJoin, join.id).current is True
    sqlite_session.rollback()
    fresh_receipt = prepare(service)
    with sqlite_session.begin():
        fresh = sqlite_session.scalar(
            select(Account).where(Account.id == account.id).execution_options(populate_existing=True).with_for_update()
        )
        with pytest.raises(InvalidAccountInitializationError):
            service.persist_invited_initialization(prepared, account=fresh, session=sqlite_session)
        service.persist_invited_initialization(fresh_receipt, account=fresh, session=sqlite_session)
    effects[0].delete.assert_not_called()


@pytest.mark.parametrize(
    "lookup",
    [InvitationLookup(str(uuid4()), None, "real-token"), InvitationLookup(None, "other@example.test", "real-token")],
)
def test_partial_lookup_cannot_substitute_workspace_or_email(lookup, sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    with pytest.raises(InvalidInvitationError):
        prepare(service, lookup=lookup)
    effects[2].get_freeze_type.assert_not_called()


def test_observation_rejects_account_changed_between_closed_reads(sqlite_session, sqlite_session_factory, monkeypatch):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    original = service._accounts.resolve

    def resolve_then_change(token):
        resolved = original(token)
        with sqlite_session_factory.begin() as writer:
            writer.get(Account, account.id).status = AccountStatus.BANNED
        return resolved

    monkeypatch.setattr(service._accounts, "resolve", resolve_then_change)
    with pytest.raises(InvalidInvitationError):
        prepare(service)
    effects[2].get_freeze_type.assert_not_called()


def test_original_workspace_bytes_decode_failure_is_fixed(sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    effects[0].get.return_value = b"\xff"
    with pytest.raises(InvalidInvitationError):
        prepare(service, lookup=InvitationLookup(tenant.id, account.email, "real-token"))
    effects[2].get_freeze_type.assert_not_called()


def test_workspace_policy_rejection_precedes_freeze(sqlite_session, sqlite_session_factory):
    account, tenant, _, _ = seed(sqlite_session)
    service, effects, _ = service_for(sqlite_session_factory, account, tenant)
    effects[1].ensure_allowed.side_effect = RuntimeError("workspace policy rejected")
    with pytest.raises(RuntimeError, match="workspace policy rejected"):
        prepare(service)
    effects[2].get_freeze_type.assert_not_called()
