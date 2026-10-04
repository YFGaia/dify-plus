"""Offline removal integration: caller-owned SQLite transactions, not race proof."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from enums import DeploymentEdition
from models.account import Account, AccountStatus, TenantAccountJoin, TenantAccountRole
from models.dataset import Dataset
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from models.model import App
from repositories import invitation_authority_repository_extend as authority_module
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services import account_service
from services.account_service import AccountService, TenantService
from services.errors.account import CannotOperateSelfError, MemberNotInTenantError, NoPermissionError
from tests.unit_tests.services.test_casdoor_local_removal_effect_extend import seed


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


@pytest.fixture
def effects(monkeypatch):
    result = SimpleNamespace(billing=Mock(), enterprise=Mock(return_value=True), rbac=Mock())
    monkeypatch.setattr(account_service.BillingService, "clean_billing_info_cache", result.billing)
    monkeypatch.setattr("services.enterprise.account_deletion_sync.sync_workspace_member_removal", result.enterprise)
    monkeypatch.setattr(account_service.RBACService.MemberRoles, "delete_rbac_bindings", result.rbac)
    return result


def lifecycle(session, state):
    return InvitationAuthorityRepository().get_lifecycle(
        session, account_id=state.member.id, workspace_id=state.tenant.id
    )


def set_state(session, state, value):
    return InvitationAuthorityRepository().set_lifecycle_state(
        session, account_id=state.member.id, workspace_id=state.tenant.id, state=value
    )


def remove(session, state):
    TenantService.remove_member_from_tenant(state.tenant, state.member, state.owner, session=session)


def snapshot(session):
    return {
        model: tuple(session.execute(sa.select(model.__table__).order_by(*model.__table__.primary_key.columns)).all())
        for model in (Account, App, Dataset, TenantAccountJoin, Lifecycle, Issuance)
    }


def assert_no_effects(effects):
    for effect in vars(effects).values():
        effect.assert_not_called()


@pytest.mark.parametrize("status", [AccountStatus.ACTIVE, AccountStatus.PENDING])
@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_public_commits_exact_tombstone_and_preserves_other_scopes(
    sqlite_session_factory, monkeypatch, effects, status, prior
):
    repository = InvitationAuthorityRepository()
    with sqlite_session_factory() as session:
        state = seed(session, status=status, other_membership=False)
        before = set_state(session, state, prior) if prior else None
        protected = [
            repository.set_lifecycle_state(session, account_id=account, workspace_id=workspace, state="active")
            for account, workspace in ((state.member.id, state.other.id), (state.owner.id, state.tenant.id))
        ]
        session.commit()
        calls = []
        original = InvitationAuthorityRepository.set_lifecycle_state

        def observed(repo, supplied_session, **kwargs):
            assert supplied_session is session
            assert kwargs == dict(account_id=state.member.id, workspace_id=state.tenant.id, state="withdrawn")
            assert session.get(TenantAccountJoin, state.join.id) is state.join
            assert session.get(App, state.resources[0].id).maintainer == state.member.id
            calls.append("withdrawal")
            return original(repo, supplied_session, **kwargs)

        monkeypatch.setattr(InvitationAuthorityRepository, "set_lifecycle_state", observed)
        sa.event.listen(session, "after_commit", lambda _: calls.append("commit"))
        remove(session, state)
        assert calls == ["withdrawal", "commit"]
        with sqlite_session_factory() as observer:
            after = lifecycle(observer, state)
            assert after.state == "withdrawn"
            assert (after.account_id, after.workspace_id) == (state.member.id, state.tenant.id)
            assert after.epoch == (2 if prior == "active" else 1)
            if before:
                assert after.lifecycle_id == before.lifecycle_id
                assert after.created_at == before.created_at
                if prior == "withdrawn":
                    assert after == before
            assert observer.get(TenantAccountJoin, state.join.id) is None
            assert (observer.get(Account, state.member.id) is None) == (status == AccountStatus.PENDING)
            for row in protected:
                assert (
                    repository.get_lifecycle(observer, account_id=row.account_id, workspace_id=row.workspace_id) == row
                )
            for index, resource in enumerate(state.resources):
                actual = observer.get(type(resource), resource.id)
                assert actual.maintainer == (state.member.id if index % 3 == 1 else state.owner.id)
                assert actual.created_by == state.member.id
        effects.enterprise.assert_called_once()


def test_helper_flush_is_caller_owned_and_withdrawn_repeat_is_idempotent(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = seed(session)
        session.begin()
        transaction = session.get_transaction()
        with monkeypatch.context() as guard:
            for method in ("commit", "rollback", "begin", "begin_nested", "close"):
                guard.setattr(session, method, Mock(side_effect=AssertionError(method)))
            TenantService._persist_member_removal_effect(
                state.tenant, state.member.id, state.join, state.owner.id, session=session
            )
            first = lifecycle(session, state)
            assert set_state(session, state, "withdrawn") == first
            assert session.get_transaction() is transaction
            with sqlite_session_factory() as observer:
                assert lifecycle(observer, state) is None
                assert observer.get(TenantAccountJoin, state.join.id) is not None
        session.rollback()
        assert lifecycle(session, state) is None
        assert session.get(TenantAccountJoin, state.join.id) is not None


@pytest.mark.parametrize("case", ["self", "denied", "admin_removes_owner", "absent_join", "missing_owner"])
def test_rejected_public_removal_creates_no_fact(sqlite_session_factory, effects, case):
    with sqlite_session_factory() as session:
        state = seed(session)
        target, operator = state.member, state.owner
        expected = NoPermissionError
        if case == "self":
            operator = target
            expected = CannotOperateSelfError
        elif case == "denied":
            state.owner_join.role = TenantAccountRole.NORMAL
        elif case == "admin_removes_owner":
            state.join.role = TenantAccountRole.ADMIN
            target, operator = state.owner, state.member
        elif case == "absent_join":
            session.delete(state.join)
            expected = MemberNotInTenantError
        else:
            state.owner_join.role = TenantAccountRole.ADMIN
            expected = ValueError
        session.commit()
        before = snapshot(session)
        with pytest.raises(expected):
            TenantService.remove_member_from_tenant(state.tenant, target, operator, session=session)
        assert snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted
        assert_no_effects(effects)


@pytest.mark.parametrize("prior", [None, "active"])
@pytest.mark.parametrize("failure_table", ["apps", "datasets", "tenant_account_joins", "accounts"])
def test_later_sql_failure_rolls_back_withdrawal_resources_join_and_pending_cleanup(
    sqlite_session_factory, effects, prior, failure_table
):
    with sqlite_session_factory() as session:
        state = seed(session, status=AccountStatus.PENDING, other_membership=False)
        if prior:
            set_state(session, state, prior)
            session.commit()
        before = snapshot(session)
        session.rollback()
        writes = []
        commits = []
        sa.event.listen(session, "after_commit", lambda _: commits.append(1))

        def fail_after_write(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(
                (
                    "INSERT INTO invitation_authority",
                    "UPDATE invitation_authority",
                    "UPDATE apps",
                    "UPDATE datasets",
                    "DELETE FROM tenant_account_joins",
                    "DELETE FROM accounts",
                )
            ):
                writes.append(statement)
            verb = "UPDATE" if failure_table in ("apps", "datasets") else "DELETE FROM"
            if statement.startswith(f"{verb} {failure_table} "):
                raise RuntimeError("injected_after_persistence")

        engine = session.get_bind()
        sa.event.listen(engine, "after_cursor_execute", fail_after_write)
        try:
            with pytest.raises(RuntimeError, match="injected_after_persistence"):
                remove(session, state)
        finally:
            sa.event.remove(engine, "after_cursor_execute", fail_after_write)
            session.rollback()
        assert "invitation_authority_lifecycle_extend" in writes[0]
        if failure_table == "accounts":
            assert any(statement.startswith("DELETE FROM tenant_account_joins") for statement in writes)
        assert commits == []
        assert_no_effects(effects)
        with sqlite_session_factory() as observer:
            assert snapshot(observer) == before


@pytest.mark.parametrize("failure", ["uniqueness", "epoch", "missing_schema"])
def test_repository_errors_abort_before_resources_commit_or_remote_effects(
    sqlite_session_factory, monkeypatch, config_overrides, effects, failure
):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD, RBAC_ENABLED=True)
    monkeypatch.setattr(AccountService, "get_workspace_permission_keys", Mock(return_value={"workspace.member.manage"}))
    monkeypatch.setattr(AccountService, "is_rbac_workspace_owner", Mock(return_value=False))
    with sqlite_session_factory() as session:
        state = seed(session)
        monkeypatch.setattr(AccountService, "get_rbac_workspace_owner_account_id", Mock(return_value=state.owner.id))
        expected = IntegrityError
        if failure == "uniqueness":
            row = InvitationAuthorityRepository().set_lifecycle_state(
                session, account_id=state.owner.id, workspace_id=state.other.id, state="active"
            )
            session.commit()
            monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(row.lifecycle_id))
        elif failure == "epoch":
            row = set_state(session, state, "active")
            session.get(Lifecycle, row.lifecycle_id).epoch = MAX_LIFECYCLE_EPOCH
            session.commit()
            expected = InvitationAuthorityConflict
        else:
            Lifecycle.__table__.drop(session.get_bind())
            expected = OperationalError
        commits, writes = [], []
        sa.event.listen(session, "after_commit", lambda _: commits.append(1))

        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(("UPDATE apps", "UPDATE datasets", "DELETE FROM")):
                writes.append(statement)

        engine = session.get_bind()
        sa.event.listen(engine, "before_cursor_execute", observe)
        try:
            with pytest.raises(expected):
                remove(session, state)
        finally:
            sa.event.remove(engine, "before_cursor_execute", observe)
            session.rollback()
        assert commits == writes == []
        assert_no_effects(effects)
        with sqlite_session_factory() as observer:
            assert observer.get(TenantAccountJoin, state.join.id) is not None
            assert observer.get(App, state.resources[0].id).maintainer == state.member.id
            assert observer.get(Dataset, state.resources[3].id).maintainer == state.member.id


@pytest.mark.parametrize("status", [AccountStatus.ACTIVE, AccountStatus.PENDING])
def test_prior_issuance_receipt_is_rejected_without_rewriting_history(sqlite_session_factory, effects, status):
    repository = InvitationAuthorityRepository()
    with sqlite_session_factory() as session:
        state = seed(session, status=status, other_membership=False)
        row = set_state(session, state, "active")
        authority = dict(
            schema_version=1,
            issuance_id=str(uuid4()),
            lifecycle_id=row.lifecycle_id,
            lifecycle_epoch=row.epoch,
            token_digest="a" * 64,
            join_id_at_issue=state.join.id,
        )
        payload = dict(
            account_id=state.member.id,
            workspace_id=state.tenant.id,
            email=state.member.email,
            role="normal",
            requires_setup=status == AccountStatus.PENDING,
            invitation_authority=authority,
        )
        issued = repository.record_issuance(session, payload_json=json.dumps(payload), actor_id=state.owner.id)
        receipt = dict(
            **authority,
            status="consumed",
            operation_id=str(uuid4()),
            account_id=state.member.id,
            workspace_id=state.tenant.id,
            payload_digest=issued.payload_digest,
            key_digest="b" * 64,
        )
        session.commit()
        remove(session, state)
        with pytest.raises(InvitationAuthorityConflict, match="invitation_lifecycle_conflict"):
            repository.record_consumption(
                session, receipt_json=json.dumps(receipt, sort_keys=True, separators=(",", ":"))
            )
        session.rollback()
        with sqlite_session_factory() as observer:
            assert repository.get_issuance(observer, issuance_id=issued.issuance_id) == issued
            assert issued.state == "issued" and issued.consumption_receipt_json is None
            assert lifecycle(observer, state).state == "withdrawn"


def test_public_commits_withdrawal_before_billing_enterprise_and_rbac(
    sqlite_session_factory, monkeypatch, config_overrides
):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD, RBAC_ENABLED=True)
    monkeypatch.setattr(AccountService, "get_workspace_permission_keys", Mock(return_value={"workspace.member.manage"}))
    monkeypatch.setattr(AccountService, "is_rbac_workspace_owner", Mock(return_value=False))
    with sqlite_session_factory() as session:
        state = seed(session)
        monkeypatch.setattr(AccountService, "get_rbac_workspace_owner_account_id", Mock(return_value=state.owner.id))
        calls = []
        sa.event.listen(session, "after_commit", lambda _: calls.append("commit"))

        def observed(label):
            def effect(*_args, **_kwargs):
                with sqlite_session_factory() as observer:
                    assert lifecycle(observer, state).state == "withdrawn"
                    assert observer.get(TenantAccountJoin, state.join.id) is None
                    assert observer.get(App, state.resources[0].id).maintainer == state.owner.id
                assert calls[0] == "commit"
                calls.append(label)
                return True

            return effect

        monkeypatch.setattr(account_service.BillingService, "clean_billing_info_cache", observed("billing"))
        monkeypatch.setattr(
            "services.enterprise.account_deletion_sync.sync_workspace_member_removal", observed("enterprise")
        )
        monkeypatch.setattr(account_service.RBACService.MemberRoles, "delete_rbac_bindings", observed("rbac"))
        remove(session, state)
        assert calls == ["commit", "billing", "enterprise", "rbac"]
