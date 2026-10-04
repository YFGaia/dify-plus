"""Independent checks for default-workspace Join lifecycle ownership."""

import socket
from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import scoped_session

from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
)
from services import account_service_extend
from services.account_service_extend import TenantExtendService


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    attempts = []

    def reject(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("network access is outside this check")

    for operation in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, operation, reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)
    yield
    assert attempts == []


@pytest.fixture
def bound_session(sqlite_session_factory, monkeypatch):
    scoped = scoped_session(sqlite_session_factory)
    monkeypatch.setattr(account_service_extend, "db", SimpleNamespace(session=scoped))
    try:
        yield scoped
    finally:
        scoped.remove()


def make_scope(session, *, lifecycle_state=None, existing=False):
    workspace = Tenant(name="Independent default workspace")
    account = Account(name="Independent default member", email="default-independent@example.test")
    session.add_all([workspace, account])
    session.flush()
    scope = SimpleNamespace(workspace_id=workspace.id, account_id=account.id)
    if existing:
        session.add(
            TenantAccountJoin(
                tenant_id=scope.workspace_id,
                account_id=scope.account_id,
                role=TenantAccountRole.NORMAL,
                current=False,
            )
        )
    if lifecycle_state:
        prior = InvitationAuthorityRepository().set_lifecycle_state(
            session,
            account_id=scope.account_id,
            workspace_id=scope.workspace_id,
            state=lifecycle_state,
        )
        row = session.get(InvitationAuthorityLifecycleExtend, prior.lifecycle_id)
        row.epoch = 12
        row.updated_at = datetime(2021, 1, 1)
    session.commit()
    return scope


def read_join(session, scope):
    return session.scalar(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.tenant_id == scope.workspace_id,
            TenantAccountJoin.account_id == scope.account_id,
        )
    )


def read_event(session, scope):
    return InvitationAuthorityRepository().get_lifecycle(
        session, account_id=scope.account_id, workspace_id=scope.workspace_id
    )


def create(scope, role="normal"):
    return TenantExtendService.create_default_tenant_member_if_not_exist(scope.workspace_id, scope.account_id, role)


def test_new_default_join_is_flushed_before_event_and_committed_once(
    bound_session, sqlite_session_factory, monkeypatch
):
    session = bound_session()
    scope = make_scope(session)
    original_record = InvitationAuthorityRepository.record_membership_creation
    trace = []

    def inspect_record(repository, passed_session, **identity):
        assert passed_session is session
        assert identity == {
            "account_id": scope.account_id,
            "workspace_id": scope.workspace_id,
        }
        assert not passed_session.new
        join = read_join(passed_session, scope)
        assert join is not None and join.id
        assert join.role == TenantAccountRole.NORMAL and join.current is True and join.invited_by is None
        with sqlite_session_factory() as independent_reader:
            assert read_join(independent_reader, scope) is None
            assert read_event(independent_reader, scope) is None
        trace.append("lifecycle")
        return original_record(repository, passed_session, **identity)

    monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", inspect_record)
    sa.event.listen(session, "after_commit", lambda _: trace.append("commit"))
    assert create(scope) is True
    assert trace == ["lifecycle", "commit"]
    with sqlite_session_factory() as reader:
        join = read_join(reader, scope)
        event = read_event(reader, scope)
        assert join is not None and join.role == TenantAccountRole.NORMAL and join.current
        assert event is not None and event.state == "active" and event.epoch == 1


@pytest.mark.parametrize("prior_state", ["active", "withdrawn"])
def test_new_default_join_reactivates_existing_lifecycle_without_replacing_identity(bound_session, prior_state):
    session = bound_session()
    scope = make_scope(session, lifecycle_state=prior_state)
    before = read_event(session, scope)
    assert create(scope, "editor") is True
    after = read_event(session, scope)
    assert (after.lifecycle_id, after.created_at) == (
        before.lifecycle_id,
        before.created_at,
    )
    assert after.state == "active" and after.epoch == 13
    join = read_join(session, scope)
    assert join.role == TenantAccountRole.EDITOR and join.current


def test_existing_default_join_returns_false_without_lifecycle_event(bound_session, monkeypatch):
    session = bound_session()
    scope = make_scope(session, lifecycle_state="active", existing=True)
    before = read_event(session, scope)
    existing_join = read_join(session, scope)
    monkeypatch.setattr(
        InvitationAuthorityRepository,
        "record_membership_creation",
        lambda *_args, **_kwargs: pytest.fail("existing Join advanced lifecycle"),
    )
    monkeypatch.setattr(session, "commit", lambda: pytest.fail("existing Join committed"))
    assert create(scope, "admin") is False
    assert read_join(session, scope) is existing_join
    assert existing_join.role == TenantAccountRole.NORMAL and not existing_join.current
    assert read_event(session, scope) == before


def test_event_error_after_lifecycle_write_needs_caller_rollback(bound_session, sqlite_session_factory, monkeypatch):
    session = bound_session()
    scope = make_scope(session, lifecycle_state="withdrawn")
    before = read_event(session, scope)
    original_record = InvitationAuthorityRepository.record_membership_creation
    commits = []
    sa.event.listen(session, "after_commit", lambda _: commits.append(True))

    def write_then_raise(repository, passed_session, **identity):
        assert passed_session is session
        assert read_join(passed_session, scope) is not None
        original_record(repository, passed_session, **identity)
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", write_then_raise)
    with pytest.raises(RuntimeError, match="injected event failure"):
        create(scope)
    assert commits == []
    session.rollback()
    with sqlite_session_factory() as reader:
        assert read_join(reader, scope) is None
        after = read_event(reader, scope)
        assert (after.lifecycle_id, after.created_at, after.state, after.epoch) == (
            before.lifecycle_id,
            before.created_at,
            before.state,
            before.epoch,
        )
