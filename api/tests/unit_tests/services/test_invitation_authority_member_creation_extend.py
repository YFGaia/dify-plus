"""Offline new-Join lifecycle events in caller transactions; SQLite is not race proof."""

import json
import socket
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories import invitation_authority_repository_extend as authority_module
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services import account_service
from services.account_service import TenantService
from tests.unit_tests.services.test_casdoor_local_withdraw_regrant_extend import caller, invoke, withdrawal
from tests.unit_tests.services.test_casdoor_local_withdraw_regrant_extend import state as casdoor_state_fixture

casdoor_state = casdoor_state_fixture


@pytest.fixture(autouse=True)
def offline(monkeypatch, config_overrides):
    attempts = []

    def deny(*_args, **_kwargs):
        attempts.append(1)
        raise AssertionError("offline network denied")

    for name in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    yield
    assert attempts == []


def seed(session, prior=None, *, existing=False, epoch=7, status=AccountStatus.ACTIVE):
    tenant, other = Tenant(name="Creation"), Tenant(name="Protected")
    account = Account(name="Member", email="creation@example.test", status=status)
    session.add_all([tenant, other, account])
    session.flush()
    state = SimpleNamespace(tenant=tenant, other=other, account=account)
    if existing:
        session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.NORMAL))
    if prior:
        record = InvitationAuthorityRepository().set_lifecycle_state(
            session, account_id=account.id, workspace_id=tenant.id, state=prior
        )
        row = session.get(Lifecycle, record.lifecycle_id)
        row.epoch = epoch
        row.updated_at = datetime(2020, 1, 1)
    session.commit()
    return state


def lifecycle(session, state):
    return InvitationAuthorityRepository().get_lifecycle(
        session, account_id=state.account.id, workspace_id=state.tenant.id
    )


def joins(session, state):
    return session.scalars(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.account_id == state.account.id, TenantAccountJoin.tenant_id == state.tenant.id
        )
    ).all()


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_new_join_advances_exact_scope_after_flush_in_caller_transaction(
    sqlite_session_factory, monkeypatch, prior, finish
):
    repository = InvitationAuthorityRepository()
    with sqlite_session_factory() as session:
        state = seed(session, prior)
        protected = repository.set_lifecycle_state(
            session, account_id=state.account.id, workspace_id=state.other.id, state="active"
        )
        before = lifecycle(session, state)
        session.commit()
        session.begin()
        root = session.get_transaction()
        original = InvitationAuthorityRepository.record_membership_creation
        calls = []

        def observed(repo, supplied_session, **kwargs):
            assert supplied_session is session
            assert kwargs == dict(account_id=state.account.id, workspace_id=state.tenant.id)
            assert len(joins(session, state)) == 1
            assert not session.new
            calls.append(1)
            return original(repo, supplied_session, **kwargs)

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", observed)
        with monkeypatch.context() as guard:
            for name in ("commit", "rollback", "begin", "begin_nested", "close"):
                guard.setattr(session, name, Mock(side_effect=AssertionError(name)))
            result = TenantService.persist_tenant_member(state.tenant, state.account, session)
            after = lifecycle(session, state)
            assert calls == [1] and result.membership_created
            assert result.join.id and result.join.role == TenantAccountRole.NORMAL
            assert after.state == "active" and after.epoch == (8 if before else 1)
            assert UUID(after.lifecycle_id).version == 4
            if before:
                assert (after.lifecycle_id, after.created_at) == (before.lifecycle_id, before.created_at)
                assert after.updated_at > before.updated_at
            assert session.get_transaction() is root
            with sqlite_session_factory() as observer:
                assert lifecycle(observer, state) == before
                assert joins(observer, state) == []
        getattr(session, finish)()
        with sqlite_session_factory() as observer:
            assert lifecycle(observer, state) == (after if finish == "commit" else before)
            assert len(joins(observer, state)) == (1 if finish == "commit" else 0)
            assert (
                repository.get_lifecycle(observer, account_id=state.account.id, workspace_id=state.other.id)
                == protected
            )


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("role", ["normal"])
def test_existing_join_same_role_noop_never_advances(sqlite_session_factory, monkeypatch, prior, role):
    with sqlite_session_factory() as session:
        state = seed(session, prior, existing=True, epoch=MAX_LIFECYCLE_EPOCH)
        before = lifecycle(session, state)
        old_join = joins(session, state)[0]
        event = Mock(side_effect=AssertionError("existing Join must not emit creation"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)
        result = TenantService.persist_tenant_member(state.tenant, state.account, session, role)
        assert result.join is old_join and not result.membership_created
        assert result.join.role == TenantAccountRole(role)
        session.commit()
        with sqlite_session_factory() as observer:
            assert lifecycle(observer, state) == before
            assert joins(observer, state)[0].id == old_join.id
            assert joins(observer, state)[0].role == TenantAccountRole(role)
        event.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["unique", "epoch_active", "epoch_withdrawn", "insert_after_write", "update_after_write"]
)
def test_lifecycle_failure_after_join_flush_rolls_back_entire_creation(sqlite_session_factory, monkeypatch, failure):
    repository = InvitationAuthorityRepository()
    with sqlite_session_factory() as session:
        prior = (
            "withdrawn"
            if failure == "epoch_withdrawn"
            else "active"
            if failure in ("epoch_active", "update_after_write")
            else None
        )
        state = seed(session, prior, epoch=MAX_LIFECYCLE_EPOCH if failure.startswith("epoch") else 7)
        if failure == "unique":
            protected = repository.set_lifecycle_state(
                session, account_id=state.account.id, workspace_id=state.other.id, state="active"
            )
            session.commit()
            monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(protected.lifecycle_id))
        before = lifecycle(session, state)
        session.rollback()
        writes, commits = [], []
        sa.event.listen(session, "after_commit", lambda _: commits.append(1))

        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(
                ("INSERT INTO tenant_account_joins", "INSERT INTO invitation_authority", "UPDATE invitation_authority")
            ):
                writes.append(statement)
                if failure.endswith("after_write") and "invitation_authority" in statement:
                    raise RuntimeError("injected_lifecycle_write_failure")

        engine = session.get_bind()
        sa.event.listen(engine, "after_cursor_execute", observe)
        expected = (
            InvitationAuthorityConflict
            if failure.startswith("epoch")
            else IntegrityError
            if failure == "unique"
            else RuntimeError
        )
        try:
            with pytest.raises(expected) as error:
                TenantService.persist_tenant_member(state.tenant, state.account, session)
            if failure.startswith("epoch"):
                assert str(error.value) == "invitation_lifecycle_epoch_exhausted"
            assert writes[0].startswith("INSERT INTO tenant_account_joins")
            assert commits == []
        finally:
            sa.event.remove(engine, "after_cursor_execute", observe)
            session.rollback()
        with sqlite_session_factory() as observer:
            assert joins(observer, state) == []
            assert lifecycle(observer, state) == before
            if failure == "unique":
                assert (
                    repository.get_lifecycle(observer, account_id=state.account.id, workspace_id=state.other.id)
                    == protected
                )


def test_join_flush_failure_does_not_call_creation_event(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = seed(session)
        event = Mock(side_effect=AssertionError("lifecycle event before Join flush"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)
        with monkeypatch.context() as guard:
            guard.setattr(session, "flush", Mock(side_effect=RuntimeError("join_flush_failed")))
            with pytest.raises(RuntimeError, match="join_flush_failed"):
                TenantService.persist_tenant_member(state.tenant, state.account, session)
        session.rollback()
        event.assert_not_called()
        assert joins(session, state) == [] and lifecycle(session, state) is None


@pytest.mark.parametrize("consumed", [False, True])
def test_new_join_fences_old_issuance_and_consumption_replay(sqlite_session_factory, consumed):
    repository = InvitationAuthorityRepository()
    with sqlite_session_factory() as session:
        state = seed(session, "active")
        row = lifecycle(session, state)
        authority = dict(
            schema_version=1,
            issuance_id=str(uuid4()),
            lifecycle_id=row.lifecycle_id,
            lifecycle_epoch=row.epoch,
            token_digest="a" * 64,
            join_id_at_issue=None,
        )
        payload = dict(
            account_id=state.account.id,
            workspace_id=state.tenant.id,
            email=state.account.email,
            role="normal",
            requires_setup=False,
            invitation_authority=authority,
        )
        issued = repository.record_issuance(session, payload_json=json.dumps(payload))
        receipt = json.dumps(
            dict(
                **authority,
                status="consumed",
                operation_id=str(uuid4()),
                account_id=state.account.id,
                workspace_id=state.tenant.id,
                payload_digest=issued.payload_digest,
                key_digest="b" * 64,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        if consumed:
            issued = repository.record_consumption(session, receipt_json=receipt)
        session.commit()
        TenantService.persist_tenant_member(state.tenant, state.account, session)
        session.commit()
        with pytest.raises(InvitationAuthorityConflict, match="invitation_lifecycle_conflict"):
            repository.record_consumption(session, receipt_json=receipt)
        session.rollback()
        assert repository.get_issuance(session, issuance_id=issued.issuance_id) == issued
        assert session.scalar(sa.select(sa.func.count()).select_from(Issuance)) == 1
        assert lifecycle(session, state).epoch == row.epoch + 1


@pytest.mark.parametrize("status", [AccountStatus.ACTIVE, AccountStatus.PENDING])
def test_public_creation_commits_both_before_existing_effects(
    sqlite_session_factory, monkeypatch, config_overrides, status
):
    import tasks.initialize_created_app_rbac_access_task as tasks

    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD, RBAC_ENABLED=True)
    with sqlite_session_factory() as session:
        state = seed(session, status=status)
        calls = []
        sa.event.listen(session, "after_commit", lambda _: calls.append("commit"))

        def observed(label):
            def effect(*_args, **_kwargs):
                assert calls[0] == "commit"
                with sqlite_session_factory() as observer:
                    assert lifecycle(observer, state).epoch == 1
                    assert len(joins(observer, state)) == 1
                calls.append(label)

            return effect

        billing = Mock(side_effect=observed("billing"))
        delay = Mock(side_effect=observed("task"))
        monkeypatch.setattr(account_service.BillingService, "clean_billing_info_cache", billing)
        monkeypatch.setattr(tasks.sync_joined_workspace_member_rbac_access_task, "delay", delay)
        result = TenantService.create_tenant_member(
            state.tenant, state.account, session, operator_account_id=state.account.id
        )
        assert result.id == joins(session, state)[0].id
        assert calls == (["commit", "billing", "task"] if status == AccountStatus.ACTIVE else ["commit", "billing"])
        if status == AccountStatus.ACTIVE:
            delay.assert_called_once_with(state.tenant.id, state.account.id, operator_account_id=state.account.id)
        else:
            delay.assert_not_called()


def test_real_casdoor_regrant_shares_root_and_rolls_back_lifecycle(casdoor_state):
    s = casdoor_state
    repository = InvitationAuthorityRepository()
    account_id, workspace_id = s.account.id, s.spaces[1].id

    def current(session):
        return repository.get_lifecycle(session, account_id=account_id, workspace_id=workspace_id)

    with s.factory() as observer:
        initial = current(observer)
        assert initial.state == "active" and initial.epoch == 1
    with caller(s):
        withdrawal(s)
    with s.factory() as observer:
        withdrawn = current(observer)
        assert withdrawn.state == "withdrawn" and withdrawn.epoch == 2
    with pytest.raises(RuntimeError, match="caller_abort"):
        with caller(s):
            root = s.session.get_transaction()
            result = invoke(s, generation=2)
            assert any(target.membership_regranted for target in result.workspaces)
            assert s.session.get_transaction() is root
            assert current(s.session).epoch == 3
            with s.factory() as observer:
                assert current(observer) == withdrawn
            raise RuntimeError("caller_abort")
    with s.factory() as observer:
        assert current(observer) == withdrawn
    with caller(s):
        result = invoke(s, generation=2)
        assert any(target.membership_regranted for target in result.workspaces)
    with s.factory() as observer:
        after = current(observer)
        assert after.lifecycle_id == initial.lifecycle_id and after.state == "active" and after.epoch == 3
