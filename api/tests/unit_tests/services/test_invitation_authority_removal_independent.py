"""Independent public-owner verification for invitation withdrawal on removal."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.dataset import Dataset
from models.model import App, AppMode
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services import account_service
from services.account_service import TenantService
from services.errors.account import CannotOperateSelfError


@pytest.fixture(autouse=True)
def deny_network(monkeypatch, config_overrides):
    attempts = []

    def deny(*_args, **_kwargs):
        attempts.append("socket")
        raise AssertionError("network disabled in independent removal verification")

    for name in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.CLOUD)
    yield
    assert attempts == []


@pytest.fixture
def effects(monkeypatch):
    result = SimpleNamespace(billing=Mock(), enterprise=Mock(return_value=True), rbac=Mock())
    monkeypatch.setattr(account_service.BillingService, "clean_billing_info_cache", result.billing)
    monkeypatch.setattr("services.enterprise.account_deletion_sync.sync_workspace_member_removal", result.enterprise)
    monkeypatch.setattr(account_service.RBACService.MemberRoles, "delete_rbac_bindings", result.rbac)
    return result


def make_member(session, status=AccountStatus.ACTIVE):
    tenant = Tenant(name="Independent removal workspace")
    owner = Account(name="Owner", email="owner-independent@example.test", status=AccountStatus.ACTIVE)
    member = Account(name="Member", email="member-independent@example.test", status=status)
    session.add_all((tenant, owner, member))
    session.flush()
    join = TenantAccountJoin(tenant_id=tenant.id, account_id=member.id, role=TenantAccountRole.NORMAL)
    owner_join = TenantAccountJoin(tenant_id=tenant.id, account_id=owner.id, role=TenantAccountRole.OWNER)
    app = App(
        tenant_id=tenant.id,
        name="Independent app",
        mode=AppMode.CHAT,
        enable_site=False,
        enable_api=False,
        created_by=member.id,
        maintainer=member.id,
    )
    dataset = Dataset(tenant_id=tenant.id, name="Independent dataset", created_by=member.id, maintainer=member.id)
    session.add_all((join, owner_join, app, dataset))
    session.commit()
    return SimpleNamespace(tenant=tenant, owner=owner, member=member, join=join, app=app, dataset=dataset)


def get_lifecycle(session, state):
    return InvitationAuthorityRepository().get_lifecycle(
        session, account_id=state.member.id, workspace_id=state.tenant.id
    )


@pytest.mark.parametrize("status", [AccountStatus.ACTIVE, AccountStatus.PENDING])
@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_public_removal_commits_lifecycle_and_resources_before_external_effects(
    sqlite_session_factory, monkeypatch, effects, status, prior
):
    with sqlite_session_factory() as session:
        state = make_member(session, status)
        repository = InvitationAuthorityRepository()
        previous = None
        if prior:
            previous = repository.set_lifecycle_state(
                session, account_id=state.member.id, workspace_id=state.tenant.id, state=prior
            )
            session.commit()

        timeline = []
        sql_writes = []
        original = InvitationAuthorityRepository.set_lifecycle_state

        def observed(repo, supplied_session, **kwargs):
            assert supplied_session is session
            assert kwargs == {
                "account_id": state.member.id,
                "workspace_id": state.tenant.id,
                "state": "withdrawn",
            }
            assert session.get(TenantAccountJoin, state.join.id) is not None
            assert session.get(App, state.app.id).maintainer == state.member.id
            timeline.append("withdrawal")
            return original(repo, supplied_session, **kwargs)

        monkeypatch.setattr(InvitationAuthorityRepository, "set_lifecycle_state", observed)
        sa.event.listen(session, "after_commit", lambda _: timeline.append("commit"))

        def external_effect(label, predecessor):
            def run(*_args, **_kwargs):
                assert timeline[-1] == predecessor
                timeline.append(label)
                return True

            return run

        effects.billing.side_effect = external_effect("billing", "commit")
        effects.enterprise.side_effect = external_effect("enterprise", "billing")

        def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(
                (
                    "INSERT INTO invitation_authority_lifecycle_extend",
                    "UPDATE invitation_authority_lifecycle_extend",
                    "UPDATE apps",
                    "UPDATE datasets",
                    "DELETE FROM tenant_account_joins",
                    "DELETE FROM accounts",
                )
            ):
                words = statement.split()
                table = words[1] if words[0] == "UPDATE" else words[2]
                sql_writes.append(table)

        sa.event.listen(session.get_bind(), "after_cursor_execute", record_sql)
        TenantService.remove_member_from_tenant(state.tenant, state.member, state.owner, session=session)

        assert timeline == ["withdrawal", "commit", "billing", "enterprise"]
        expected_writes = ["apps", "datasets", "tenant_account_joins"]
        if prior != "withdrawn":
            expected_writes.insert(0, "invitation_authority_lifecycle_extend")
        if status == AccountStatus.PENDING:
            expected_writes.append("accounts")
        assert sql_writes == expected_writes
        effects.billing.assert_called_once_with(state.tenant.id)
        effects.enterprise.assert_called_once_with(
            workspace_id=state.tenant.id, member_id=state.member.id, source="workspace_member_removed"
        )
        effects.rbac.assert_not_called()

        with sqlite_session_factory() as observer:
            current = get_lifecycle(observer, state)
            assert current.state == "withdrawn"
            assert (current.account_id, current.workspace_id) == (state.member.id, state.tenant.id)
            assert current.epoch == (2 if prior == "active" else 1)
            if previous:
                assert current.lifecycle_id == previous.lifecycle_id
                assert current.created_at == previous.created_at
                if prior == "withdrawn":
                    assert current == previous
            assert observer.get(TenantAccountJoin, state.join.id) is None
            assert (observer.get(Account, state.member.id) is None) == (status == AccountStatus.PENDING)
            assert observer.get(App, state.app.id).maintainer == state.owner.id
            assert observer.get(Dataset, state.dataset.id).maintainer == state.owner.id


def test_repository_failure_precedes_any_removal_write_or_postcommit_effect(
    sqlite_session_factory, monkeypatch, effects
):
    with sqlite_session_factory() as session:
        state = make_member(session, AccountStatus.PENDING)
        sql_writes = []
        commits = []
        sa.event.listen(session, "after_commit", lambda _: commits.append(True))

        def fail(*_args, **_kwargs):
            raise RuntimeError("lifecycle storage unavailable")

        monkeypatch.setattr(InvitationAuthorityRepository, "set_lifecycle_state", fail)

        def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(("UPDATE apps", "UPDATE datasets", "DELETE FROM")):
                sql_writes.append(statement)

        sa.event.listen(session.get_bind(), "after_cursor_execute", record_sql)
        with pytest.raises(RuntimeError, match="lifecycle storage unavailable"):
            TenantService.remove_member_from_tenant(state.tenant, state.member, state.owner, session=session)
        assert sql_writes == []
        assert commits == []
        effects.billing.assert_not_called()
        effects.enterprise.assert_not_called()
        effects.rbac.assert_not_called()
        session.rollback()

        with sqlite_session_factory() as observer:
            assert get_lifecycle(observer, state) is None
            assert observer.get(TenantAccountJoin, state.join.id) is not None
            assert observer.get(Account, state.member.id) is not None
            assert observer.get(App, state.app.id).maintainer == state.member.id
            assert observer.get(Dataset, state.dataset.id).maintainer == state.member.id


def test_self_rejection_occurs_before_lifecycle_creation(sqlite_session_factory, effects):
    with sqlite_session_factory() as session:
        state = make_member(session)
        with pytest.raises(CannotOperateSelfError):
            TenantService.remove_member_from_tenant(state.tenant, state.member, state.member, session=session)
        assert get_lifecycle(session, state) is None
        effects.billing.assert_not_called()
        effects.enterprise.assert_not_called()


def test_prior_p2_receipt_is_rejected_after_public_withdrawal_without_rewriting_issuance(
    sqlite_session_factory, effects
):
    with sqlite_session_factory() as session:
        state = make_member(session)
        repository = InvitationAuthorityRepository()
        lifecycle = repository.set_lifecycle_state(
            session, account_id=state.member.id, workspace_id=state.tenant.id, state="active"
        )
        authority = {
            "schema_version": 1,
            "issuance_id": str(uuid4()),
            "lifecycle_id": lifecycle.lifecycle_id,
            "lifecycle_epoch": lifecycle.epoch,
            "token_digest": "a" * 64,
            "join_id_at_issue": state.join.id,
        }
        payload = {
            "account_id": state.member.id,
            "email": state.member.email,
            "workspace_id": state.tenant.id,
            "role": "normal",
            "requires_setup": False,
            "invitation_authority": authority,
        }
        issuance = repository.record_issuance(
            session,
            payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            actor_id=state.owner.id,
        )
        receipt = {
            **authority,
            "status": "consumed",
            "operation_id": str(uuid4()),
            "account_id": state.member.id,
            "workspace_id": state.tenant.id,
            "payload_digest": issuance.payload_digest,
            "key_digest": "b" * 64,
        }
        receipt_json = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
        session.commit()

        TenantService.remove_member_from_tenant(state.tenant, state.member, state.owner, session=session)
        with pytest.raises(InvitationAuthorityConflict):
            repository.record_consumption(session, receipt_json=receipt_json)
        session.rollback()

        with sqlite_session_factory() as observer:
            unchanged = repository.get_issuance(observer, issuance_id=issuance.issuance_id)
            assert unchanged == issuance
            assert unchanged.state == "issued"
            assert unchanged.consumption_receipt_json is None
            assert get_lifecycle(observer, state).state == "withdrawn"
        effects.billing.assert_called_once()
        effects.enterprise.assert_called_once()
