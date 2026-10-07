"""Offline default-member lifecycle commits; earlier registration is outside this write."""

import socket
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from flask import Flask
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import scoped_session

from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories import invitation_authority_repository_extend as authority_module
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services import account_service_extend, ding_talk_extend
from services.account_service_extend import TenantExtendService


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


@pytest.fixture
def sessions(sqlite_session_factory, monkeypatch):
    scoped = scoped_session(sqlite_session_factory)
    database = SimpleNamespace(session=scoped)
    monkeypatch.setattr(account_service_extend, "db", database)
    monkeypatch.setattr(ding_talk_extend, "db", database)
    try:
        yield scoped
    finally:
        scoped.remove()


def seed(session, prior=None, *, existing=False, epoch=6):
    workspace, protected = Tenant(name="Default"), Tenant(name="Protected")
    account = Account(name="Member", email="default@example.test")
    session.add_all([workspace, protected, account])
    session.flush()
    state = SimpleNamespace(workspace_id=workspace.id, protected_id=protected.id, account_id=account.id)
    if existing:
        session.add(
            TenantAccountJoin(
                tenant_id=workspace.id, account_id=account.id, role=TenantAccountRole.NORMAL, current=False
            )
        )
    if prior:
        row = InvitationAuthorityRepository().set_lifecycle_state(
            session, account_id=account.id, workspace_id=workspace.id, state=prior
        )
        lifecycle = session.get(Lifecycle, row.lifecycle_id)
        lifecycle.epoch = epoch
        lifecycle.updated_at = datetime(2020, 1, 1)
    session.commit()
    return state


def lifecycle(session, state):
    return InvitationAuthorityRepository().get_lifecycle(
        session, account_id=state.account_id, workspace_id=state.workspace_id
    )


def joins(session, state):
    return session.scalars(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.account_id == state.account_id,
            TenantAccountJoin.tenant_id == state.workspace_id,
        )
    ).all()


def create(state, role=None):
    if role is None:
        return TenantExtendService.create_default_tenant_member_if_not_exist(state.workspace_id, state.account_id)
    return TenantExtendService.create_default_tenant_member_if_not_exist(state.workspace_id, state.account_id, role)


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("role", [None, "editor", "admin"])
def test_new_default_join_flush_event_single_commit_and_original_flags(
    sessions, sqlite_session_factory, monkeypatch, prior, role
):
    session = sessions()
    state = seed(session, prior)
    repository = InvitationAuthorityRepository()
    protected = repository.set_lifecycle_state(
        session, account_id=state.account_id, workspace_id=state.protected_id, state="active"
    )
    before = lifecycle(session, state)
    session.commit()
    events = []
    original = InvitationAuthorityRepository.record_membership_creation

    def observed(repo, supplied, **scope):
        assert supplied is session and supplied is sessions()
        assert scope == dict(account_id=state.account_id, workspace_id=state.workspace_id)
        assert not session.new  # Check before a query could autoflush the Join.
        join = joins(session, state)[0]
        assert join.id and join not in session.new
        assert join.current and join.role == TenantAccountRole(role or "normal")
        assert join.invited_by is None
        with sqlite_session_factory() as reader:
            assert joins(reader, state) == [] and lifecycle(reader, state) == before
        events.append("event")
        return original(repo, supplied, **scope)

    monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", observed)
    sa.event.listen(session, "after_commit", lambda _: events.append("commit"))
    assert create(state, role) is True
    assert events == ["event", "commit"]
    with sqlite_session_factory() as reader:
        after = lifecycle(reader, state)
        assert after.state == "active" and after.epoch == (7 if before else 1)
        assert UUID(after.lifecycle_id).version == 4
        if before:
            assert (after.lifecycle_id, after.created_at) == (before.lifecycle_id, before.created_at)
            assert after.updated_at > before.updated_at
        join = joins(reader, state)[0]
        assert join.current and join.role == TenantAccountRole(role or "normal")
        assert (
            repository.get_lifecycle(reader, account_id=state.account_id, workspace_id=state.protected_id) == protected
        )


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("role", [None, "admin"])
def test_existing_default_join_returns_false_without_event_update_or_commit(sessions, monkeypatch, prior, role):
    session = sessions()
    state = seed(session, prior, existing=True, epoch=MAX_LIFECYCLE_EPOCH)
    before = lifecycle(session, state)
    old = joins(session, state)[0]
    event = Mock(side_effect=AssertionError("existing Join emitted event"))
    commit = Mock(side_effect=AssertionError("existing Join committed"))
    monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)
    monkeypatch.setattr(session, "commit", commit)
    assert create(state, role) is False
    assert lifecycle(session, state) == before
    assert joins(session, state)[0] is old and old.role == TenantAccountRole.NORMAL and not old.current
    event.assert_not_called()
    commit.assert_not_called()


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_event_failure_propagates_and_caller_rolls_back_join_and_lifecycle(
    sessions, sqlite_session_factory, monkeypatch, prior, phase
):
    session = sessions()
    state = seed(session, prior)
    before = lifecycle(session, state)
    session.rollback()
    original = InvitationAuthorityRepository.record_membership_creation
    events = []

    def failure(repo, supplied, **scope):
        assert supplied is session
        assert not session.new
        assert len(joins(session, state)) == 1 and not session.new
        events.append("event")
        if phase == "after":
            original(repo, supplied, **scope)
        raise RuntimeError("default_lifecycle_failure")

    monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", failure)
    sa.event.listen(session, "after_commit", lambda _: events.append("commit"))
    with monkeypatch.context() as guard:
        guard.setattr(session, "rollback", Mock(side_effect=AssertionError("helper rolled back")))
        with pytest.raises(RuntimeError, match="default_lifecycle_failure"):
            create(state)
    assert events == ["event"]
    session.rollback()
    with sqlite_session_factory() as reader:
        assert joins(reader, state) == [] and lifecycle(reader, state) == before
        assert reader.get(Account, state.account_id) is not None
        assert reader.get(Tenant, state.workspace_id) is not None


@pytest.mark.parametrize("failure", ["unique", "epoch_active", "epoch_withdrawn", "missing_schema"])
def test_repository_conflicts_fail_closed_after_join_flush(sessions, sqlite_session_factory, monkeypatch, failure):
    session = sessions()
    prior = failure.removeprefix("epoch_") if failure.startswith("epoch") else None
    state = seed(session, prior, epoch=MAX_LIFECYCLE_EPOCH)
    repository = InvitationAuthorityRepository()
    expected = InvitationAuthorityConflict if prior else IntegrityError
    before = lifecycle(session, state)
    session.rollback()
    if failure == "unique":
        protected = repository.set_lifecycle_state(
            session, account_id=state.account_id, workspace_id=state.protected_id, state="active"
        )
        session.commit()
        monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(protected.lifecycle_id))
    elif failure == "missing_schema":
        Lifecycle.__table__.drop(session.get_bind())
        expected = OperationalError
    writes, commits = [], []
    sa.event.listen(session, "after_commit", lambda _: commits.append(1))

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO tenant_account_joins"):
            writes.append(statement)

    engine = session.get_bind()
    sa.event.listen(engine, "after_cursor_execute", observe)
    try:
        with pytest.raises(expected) as error:
            create(state)
        if prior:
            assert str(error.value) == "invitation_lifecycle_epoch_exhausted"
        assert len(writes) == 1 and commits == []
    finally:
        sa.event.remove(engine, "after_cursor_execute", observe)
        session.rollback()
    with sqlite_session_factory() as reader:
        assert joins(reader, state) == []
        if failure != "missing_schema":
            assert lifecycle(reader, state) == before
        if failure == "unique":
            assert (
                repository.get_lifecycle(reader, account_id=state.account_id, workspace_id=state.protected_id)
                == protected
            )


def dingtalk_stubs(monkeypatch):
    monkeypatch.setattr(ding_talk_extend.DingTalkService, "get_access_token", lambda: ("synthetic-token", ""))
    monkeypatch.setattr(
        ding_talk_extend.requests,
        "post",
        Mock(
            return_value=SimpleNamespace(
                status_code=200,
                json=lambda: {"errcode": 0, "result": {"name": "Member", "email": "ding-default@example.test"}},
            )
        ),
    )
    switch = Mock()
    login = Mock(return_value="synthetic-session")
    monkeypatch.setattr(ding_talk_extend.TenantService, "switch_tenant", switch)
    monkeypatch.setattr(ding_talk_extend.AccountService, "login", login)
    return switch, login


@pytest.mark.parametrize("fail", [False, True])
def test_dingtalk_new_account_follow_on_boundary_and_earlier_registration_commit(
    sessions, sqlite_session_factory, monkeypatch, fail
):
    session = sessions()
    state = seed(session)
    switch, login = dingtalk_stubs(monkeypatch)
    events, registered = [], []
    sa.event.listen(session, "after_commit", lambda _: events.append("commit"))

    def register(**kwargs):
        assert kwargs["session"] is session
        account = Account(name=kwargs["name"], email=kwargs["email"])
        owner_workspace = Tenant(name="Earlier owner workspace")
        session.add_all([account, owner_workspace])
        session.flush()
        session.add(
            TenantAccountJoin(
                tenant_id=owner_workspace.id, account_id=account.id, role=TenantAccountRole.OWNER, current=True
            )
        )
        session.commit()
        registered.append(SimpleNamespace(workspace_id=owner_workspace.id, account_id=account.id))
        return account

    monkeypatch.setattr(ding_talk_extend.RegisterService, "register", register)
    monkeypatch.setattr(TenantExtendService, "get_super_admin_id", lambda: SimpleNamespace(id=state.account_id))
    monkeypatch.setattr(
        TenantExtendService, "get_super_admin_tenant_id", lambda: SimpleNamespace(id=state.workspace_id)
    )
    original = InvitationAuthorityRepository.record_membership_creation

    def event(repo, supplied, **scope):
        assert supplied is session and events == ["commit"]
        events.append("event")
        result = original(repo, supplied, **scope)
        if fail:
            raise RuntimeError("default_lifecycle_failure")
        return result

    monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)
    with Flask(__name__).test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        if fail:
            with pytest.raises(RuntimeError, match="default_lifecycle_failure"):
                ding_talk_extend.DingTalkService.auto_create_user("synthetic-user")
            assert events == ["commit", "event"]
            switch.assert_not_called()
            login.assert_not_called()
            session.rollback()
        else:
            assert ding_talk_extend.DingTalkService.auto_create_user("synthetic-user") == ("synthetic-session", "")
            assert events == ["commit", "event", "commit"]
            switch.assert_called_once()
            login.assert_called_once()
            assert switch.call_args.kwargs["session"] is session and login.call_args.kwargs["session"] is session
    created = registered[0]
    with sqlite_session_factory() as reader:
        assert reader.get(Account, created.account_id) is not None
        assert reader.get(Tenant, created.workspace_id) is not None
        assert len(joins(reader, created)) == 1  # Earlier owner membership remains committed.
        target = SimpleNamespace(account_id=created.account_id, workspace_id=state.workspace_id)
        assert len(joins(reader, target)) == (0 if fail else 1)
        row = lifecycle(reader, target)
        assert row is None if fail else row.state == "active" and row.epoch == 1


def test_dingtalk_existing_account_login_does_not_enter_default_creation(sessions, monkeypatch):
    session = sessions()
    account = Account(name="Existing", email="ding-default@example.test")
    session.add(account)
    session.commit()
    switch, login = dingtalk_stubs(monkeypatch)
    register = Mock(side_effect=AssertionError("existing account registered"))
    create_member = Mock(side_effect=AssertionError("existing account created default membership"))
    monkeypatch.setattr(ding_talk_extend.RegisterService, "register", register)
    monkeypatch.setattr(TenantExtendService, "create_default_tenant_member_if_not_exist", create_member)
    with Flask(__name__).test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert ding_talk_extend.DingTalkService.auto_create_user("synthetic-user") == ("synthetic-session", "")
    register.assert_not_called()
    create_member.assert_not_called()
    switch.assert_not_called()
    login.assert_called_once()
    assert session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(Lifecycle)) == 0
