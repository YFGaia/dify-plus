"""Sequential offline invitation activation; caller rollback owns all SQL writes."""

import json
import socket
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories import invitation_authority_repository_extend as authority_module
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services.entities.account_activation_entities import AccountInvitation, AccountSetup


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    attempts = []

    def deny(*_args, **_kwargs):
        attempts.append(1)
        raise AssertionError("offline network denied")

    for name in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    yield
    assert attempts == []


def seed(session, prior=None, *, existing=False, epoch=7):
    account = Account(name="Pending", email="activation@example.test", status=AccountStatus.PENDING)
    tenant, other = Tenant(name="Activation"), Tenant(name="Current")
    session.add_all([account, tenant, other])
    session.flush()
    session.add(
        TenantAccountJoin(account_id=account.id, tenant_id=other.id, role=TenantAccountRole.NORMAL, current=True)
    )
    if existing:
        session.add(TenantAccountJoin(account_id=account.id, tenant_id=tenant.id, role=TenantAccountRole.EDITOR))
    if prior:
        record = InvitationAuthorityRepository().set_lifecycle_state(
            session, account_id=account.id, workspace_id=tenant.id, state=prior
        )
        row = session.get(Lifecycle, record.lifecycle_id)
        row.epoch = epoch
        row.updated_at = datetime(2020, 1, 1)
    protected = InvitationAuthorityRepository().set_lifecycle_state(
        session, account_id=account.id, workspace_id=other.id, state="active"
    )
    session.commit()
    return SimpleNamespace(
        account_id=account.id,
        workspace_id=tenant.id,
        other_id=other.id,
        protected=protected,
        invitation=AccountInvitation(
            account_id=account.id,
            account_email=account.email,
            account_status="pending",
            workspace_id=tenant.id,
            workspace_name=tenant.name,
            role="admin",
            requires_setup=True,
        ),
    )


def lifecycle(session, state):
    return InvitationAuthorityRepository().get_lifecycle(
        session, account_id=state.account_id, workspace_id=state.workspace_id
    )


def join(session, state, workspace_id=None):
    return session.scalar(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.account_id == state.account_id,
            TenantAccountJoin.tenant_id == (workspace_id or state.workspace_id),
        )
    )


def setup():
    return AccountSetup(name="Activated", interface_language="en-US", timezone="UTC")


def assert_original(session, state, before):
    assert join(session, state) is None
    assert join(session, state, state.other_id).current
    account = session.get(Account, state.account_id)
    assert account.name == "Pending" and account.status == AccountStatus.PENDING
    assert account.initialized_at is None
    assert lifecycle(session, state) == before
    assert (
        InvitationAuthorityRepository().get_lifecycle(session, account_id=state.account_id, workspace_id=state.other_id)
        == state.protected
    )


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_new_join_flush_then_event_share_caller_and_preserve_activation(
    sqlite_session_factory, monkeypatch, prior, finish
):
    with sqlite_session_factory() as session:
        state = seed(session, prior)
        before = lifecycle(session, state)
        root = session.get_transaction()
        calls, flushes = [], []
        original = InvitationAuthorityRepository.record_membership_creation
        real_flush = session.flush

        def flush(objects=None):
            if objects is not None:
                flushes.append(list(objects))
            return real_flush(objects)

        def observed(repo, supplied_session, **kwargs):
            assert supplied_session is session
            assert kwargs == dict(account_id=state.account_id, workspace_id=state.workspace_id)
            member = join(session, state)
            assert flushes[-1] == [member]
            assert sa.inspect(member).persistent and member.current and member.last_opened_at is not None
            assert member.role == TenantAccountRole.ADMIN
            assert session.get(Account, state.account_id).name == "Activated"
            calls.append(1)
            return original(repo, supplied_session, **kwargs)

        monkeypatch.setattr(session, "flush", flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", observed)
        with monkeypatch.context() as guard:
            for name in ("begin", "begin_nested", "commit", "rollback", "close"):
                guard.setattr(session, name, Mock(side_effect=AssertionError(name)))
            result = SQLAlchemyAccountActivationRepository(sqlite_session_factory).persist_activation(
                state.invitation, role="admin", setup=setup(), session=session
            )
            assert result.membership_created and calls == [1]
            assert session.get_transaction() is root
            after = lifecycle(session, state)
            assert after.state == "active" and after.epoch == (8 if before else 1)
            assert UUID(after.lifecycle_id).version == 4
            if before:
                assert (after.lifecycle_id, after.created_at) == (before.lifecycle_id, before.created_at)
                assert after.updated_at > before.updated_at
            with sqlite_session_factory() as observer:
                assert_original(observer, state, before)
        getattr(session, finish)()
        with sqlite_session_factory() as observer:
            if finish == "rollback":
                assert_original(observer, state, before)
            else:
                assert lifecycle(observer, state) == after
                assert join(observer, state).role == TenantAccountRole.ADMIN
                assert join(observer, state).current and not join(observer, state, state.other_id).current
                account = observer.get(Account, state.account_id)
                assert (account.name, account.interface_language, account.timezone, account.interface_theme) == (
                    "Activated",
                    "en-US",
                    "UTC",
                    "light",
                )
                assert account.status == AccountStatus.ACTIVE and account.initialized_at is not None


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_existing_join_keeps_role_and_lifecycle_even_at_max_epoch(sqlite_session_factory, monkeypatch, prior):
    with sqlite_session_factory() as session:
        state = seed(session, prior, existing=True, epoch=MAX_LIFECYCLE_EPOCH)
        before = lifecycle(session, state)
        member_id = join(session, state).id
        event = Mock(side_effect=AssertionError("existing Join emits no event"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)
        result = SQLAlchemyAccountActivationRepository(sqlite_session_factory).persist_activation(
            state.invitation, role="admin", setup=None, session=session
        )
        assert not result.membership_created
        session.commit()
        with sqlite_session_factory() as observer:
            member = join(observer, state)
            assert member.id == member_id and member.role == TenantAccountRole.EDITOR
            assert member.current and member.last_opened_at is not None
            assert lifecycle(observer, state) == before
            assert observer.get(Account, state.account_id).status == AccountStatus.PENDING
        event.assert_not_called()


@pytest.mark.parametrize("wrapper", [False, True])
@pytest.mark.parametrize(
    "failure", ["unique", "epoch_active", "epoch_withdrawn", "insert_after_write", "update_after_write", "schema"]
)
def test_lifecycle_failure_rolls_back_join_setup_and_workspace(sqlite_session_factory, monkeypatch, wrapper, failure):
    with sqlite_session_factory() as session:
        prior = (
            "withdrawn"
            if failure == "epoch_withdrawn"
            else "active"
            if failure in ("epoch_active", "update_after_write")
            else None
        )
        state = seed(session, prior, epoch=MAX_LIFECYCLE_EPOCH if failure.startswith("epoch") else 7)
        before = lifecycle(session, state)
        session.rollback()
        if failure == "unique":
            monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(state.protected.lifecycle_id))
        if failure == "schema":
            session.execute(sa.text("DROP TABLE invitation_authority_lifecycle_extend"))
            session.commit()
        writes, commits = [], []
        engine = session.get_bind()

        def observed(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(
                ("INSERT INTO tenant_account_joins", "INSERT INTO invitation_authority", "UPDATE invitation_authority")
            ):
                writes.append(statement)
                if failure.endswith("after_write") and "invitation_authority" in statement:
                    raise RuntimeError("lifecycle_write_failed")

        def committed(_session):
            commits.append(1)

        sa.event.listen(engine, "after_cursor_execute", observed)
        sa.event.listen(sqlite_session_factory.class_, "after_commit", committed)
        expected = (
            InvitationAuthorityConflict
            if failure.startswith("epoch")
            else IntegrityError
            if failure == "unique"
            else OperationalError
            if failure == "schema"
            else RuntimeError
        )
        repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
        try:
            with pytest.raises(expected):
                if wrapper:
                    repository.activate(state.invitation, role="admin", setup=setup())
                else:
                    repository.persist_activation(state.invitation, role="admin", setup=setup(), session=session)
            assert writes[0].startswith("INSERT INTO tenant_account_joins")
            assert commits == []
        finally:
            sa.event.remove(engine, "after_cursor_execute", observed)
            sa.event.remove(sqlite_session_factory.class_, "after_commit", committed)
            session.rollback()
        with sqlite_session_factory() as observer:
            if failure == "schema":
                assert join(observer, state) is None and join(observer, state, state.other_id).current
                account = observer.get(Account, state.account_id)
                assert account.name == "Pending" and account.initialized_at is None
                assert account.status == AccountStatus.PENDING
            else:
                assert_original(observer, state, before)


def test_explicit_join_flush_failure_never_calls_event(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = seed(session)
        event = Mock(side_effect=AssertionError("event before flush"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)
        real_flush = session.flush

        def fail_exact_join(objects=None):
            if objects is not None and isinstance(objects[0], TenantAccountJoin):
                raise RuntimeError("join_flush_failed")
            return real_flush(objects)

        monkeypatch.setattr(session, "flush", fail_exact_join)
        with pytest.raises(RuntimeError, match="join_flush_failed"):
            SQLAlchemyAccountActivationRepository(sqlite_session_factory).persist_activation(
                state.invitation, role="admin", setup=setup(), session=session
            )
        session.rollback()
        event.assert_not_called()
        assert_original(session, state, None)


@pytest.mark.parametrize("consumed", [False, True])
def test_activation_fences_prior_issuance_and_consumed_receipt(sqlite_session_factory, consumed):
    authority = InvitationAuthorityRepository()
    with sqlite_session_factory() as session:
        state = seed(session, "active")
        before = lifecycle(session, state)
        facts = dict(
            schema_version=1,
            issuance_id=str(uuid4()),
            lifecycle_id=before.lifecycle_id,
            lifecycle_epoch=before.epoch,
            token_digest="a" * 64,
            join_id_at_issue=None,
        )
        issued = authority.record_issuance(
            session,
            payload_json=json.dumps(
                dict(
                    account_id=state.account_id,
                    workspace_id=state.workspace_id,
                    email=state.invitation.account_email,
                    role="admin",
                    requires_setup=True,
                    invitation_authority=facts,
                )
            ),
        )
        receipt = json.dumps(
            dict(
                **facts,
                status="consumed",
                operation_id=str(uuid4()),
                account_id=state.account_id,
                workspace_id=state.workspace_id,
                payload_digest=issued.payload_digest,
                key_digest="b" * 64,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        if consumed:
            issued = authority.record_consumption(session, receipt_json=receipt)
        session.commit()
        result = SQLAlchemyAccountActivationRepository(sqlite_session_factory).activate(
            state.invitation, role="admin", setup=None
        )
        assert result.membership_created
        with pytest.raises(InvitationAuthorityConflict, match="invitation_lifecycle_conflict"):
            authority.record_consumption(session, receipt_json=receipt)
        session.rollback()
        assert authority.get_issuance(session, issuance_id=issued.issuance_id) == issued
        assert lifecycle(session, state).epoch == before.epoch + 1
