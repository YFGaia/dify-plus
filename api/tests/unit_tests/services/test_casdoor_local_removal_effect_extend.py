"""Original removal SQL and public policy, using caller-owned SQLite transactions."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from enums import DeploymentEdition
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
)
from models.account_money_extend import AccountMoneyExtend
from models.agent import Agent, AgentScope, AgentSource
from models.casdoor_extend import (
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
)
from models.dataset import Dataset
from models.model import App
from services import account_service
from services.account_service import AccountService, TenantService
from services.errors.account import (
    CannotOperateSelfError,
    MemberNotInTenantError,
    NoPermissionError,
)
from sqlalchemy import event, select


@pytest.fixture(autouse=True)
def local_config(config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)


def seed(session, *, status=AccountStatus.ACTIVE, other_membership=True):
    tenant, other = Tenant(name="Removal workspace"), Tenant(name="Other workspace")
    member = Account(name="Member", email="removal@example.test", status=status)
    owner = Account(name="Owner", email="owner@example.test")
    session.add_all([tenant, other, member, owner])
    session.flush()
    join = TenantAccountJoin(tenant_id=tenant.id, account_id=member.id, role=TenantAccountRole.NORMAL, current=True)
    owner_join = TenantAccountJoin(tenant_id=tenant.id, account_id=owner.id, role=TenantAccountRole.OWNER)
    session.add_all([join, owner_join])
    if other_membership:
        session.add(TenantAccountJoin(tenant_id=other.id, account_id=member.id, role=TenantAccountRole.EDITOR))
    resources = []
    for model in (App, Dataset):
        for workspace, maintainer in ((tenant.id, member.id), (other.id, member.id), (tenant.id, owner.id)):
            kwargs = dict(tenant_id=workspace, name="Resource", created_by=member.id, maintainer=maintainer)
            if model is App:
                kwargs.update(mode="chat", enable_site=False, enable_api=False)
            resource = model(**kwargs)
            session.add(resource)
            resources.append(resource)
    session.flush()
    agent = Agent(
        tenant_id=tenant.id,
        name="Original lineage",
        scope=AgentScope.WORKFLOW_ONLY,
        source=AgentSource.WORKFLOW,
        app_id=resources[0].id,
        backing_app_id=resources[0].id,
        workflow_id=str(uuid4()),
        workflow_node_id="original-node",
        active_config_snapshot_id=str(uuid4()),
        created_by=member.id,
        updated_by=member.id,
    )
    quota = AccountMoneyExtend(account_id=member.id, total_quota=Decimal("71.25"), used_quota=Decimal("4.50"))
    integration = CasdoorIntegrationExtend()
    session.add_all([agent, quota, integration])
    session.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer="https://issuer.example.test",
        organization="Org",
        application="App",
        client_id="synthetic-client",
        core_fingerprint="a" * 64,
    )
    session.add(namespace)
    session.flush()
    identity = CasdoorIdentityExtend(
        namespace_id=namespace.id,
        account_id=member.id,
        issuer=namespace.expected_issuer,
        organization="Org",
        subject="synthetic-subject",
        last_applied_json="{}",
        profile_sync_json="{}",
    )
    session.add(identity)
    session.commit()
    return SimpleNamespace(
        tenant=tenant,
        other=other,
        member=member,
        owner=owner,
        join=join,
        owner_join=owner_join,
        resources=resources,
        agent=agent,
        quota=quota,
        identity=identity,
    )


def snapshot(session, model):
    return tuple(session.execute(select(model.__table__).order_by(model.__table__.c.id)).all())


def invoke_helper(session, state):
    TenantService._persist_member_removal_effect(
        state.tenant, state.member.id, state.join, state.owner.id, session=session
    )


@pytest.mark.parametrize("status", [AccountStatus.ACTIVE, AccountStatus.PENDING])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_helper_exact_effect_and_caller_transaction(sqlite_session_factory, monkeypatch, status, finish):
    with sqlite_session_factory() as session:
        state = seed(session, status=status)
        protected = (Account, AccountMoneyExtend, CasdoorIdentityExtend, Agent)
        before = {model: snapshot(session, model) for model in protected}
        original_joins = snapshot(session, TenantAccountJoin)
        originals = {model: snapshot(session, model) for model in (App, Dataset)}
        transaction = session.get_transaction()
        with monkeypatch.context() as guard:
            for name in ("commit", "rollback", "begin_nested", "close"):
                guard.setattr(session, name, Mock(side_effect=AssertionError(name)))
            invoke_helper(session, state)
            assert session.get_transaction() is transaction
            assert state.join in session.deleted
            session.flush()  # Only the caller flushes the scheduled exact Join deletion.
            assert session.get(TenantAccountJoin, state.join.id) is None
            for index, resource in enumerate(state.resources):
                session.refresh(resource)
                expected = state.owner.id if index % 3 in (0, 2) else state.member.id
                assert resource.maintainer == expected
                assert resource.created_by == state.member.id
            for model in protected:
                assert snapshot(session, model) == before[model]
            remaining = snapshot(session, TenantAccountJoin)
            assert remaining == tuple(row for row in original_joins if row.id != state.join.id)
            with sqlite_session_factory() as observer:
                assert observer.get(TenantAccountJoin, state.join.id) is not None
                assert observer.get(App, state.resources[0].id).maintainer == state.member.id
        getattr(session, finish)()
        with sqlite_session_factory() as observer:
            for model in protected:
                assert snapshot(observer, model) == before[model]
            if finish == "rollback":
                assert snapshot(observer, TenantAccountJoin) == original_joins
                for model in (App, Dataset):
                    assert snapshot(observer, model) == originals[model]
            else:
                assert observer.get(TenantAccountJoin, state.join.id) is None
                assert observer.get(App, state.resources[0].id).maintainer == state.owner.id
                assert observer.get(Dataset, state.resources[3].id).maintainer == state.owner.id


@pytest.mark.parametrize("failure", ["second_update", "after_delete_flush"])
def test_failure_rolls_back_whole_original_effect(sqlite_session_factory, failure):
    with sqlite_session_factory() as session:
        state = seed(session)
        models = (Account, AccountMoneyExtend, CasdoorIdentityExtend, Agent, App, Dataset, TenantAccountJoin)
        before = {model: snapshot(session, model) for model in models}
        session.rollback()
        writes = []

        def fail_second(connection, cursor, statement, parameters, context, many):
            if statement.startswith("UPDATE apps"):
                writes.append("app")
            if failure == "second_update" and statement.startswith("UPDATE datasets"):
                assert writes == ["app"]
                raise RuntimeError("injected second UPDATE failure")

        event.listen(session.bind, "before_cursor_execute", fail_second)
        try:
            with pytest.raises(RuntimeError, match="injected"):
                with session.begin():
                    invoke_helper(session, state)
                    session.flush()
                    assert session.get(TenantAccountJoin, state.join.id) is None
                    raise RuntimeError("injected later caller failure")
        finally:
            event.remove(session.bind, "before_cursor_execute", fail_second)
        with sqlite_session_factory() as observer:
            for model in models:
                assert snapshot(observer, model) == before[model]


@pytest.mark.parametrize(
    "edition", [DeploymentEdition.COMMUNITY, DeploymentEdition.CLOUD, DeploymentEdition.ENTERPRISE]
)
@pytest.mark.parametrize("remote", [False, True])
def test_public_preserves_policy_owner_resolution_and_postcommit_order(
    sqlite_session_factory, monkeypatch, config_overrides, edition, remote
):
    config_overrides(DEPLOYMENT_EDITION=edition, RBAC_ENABLED=remote)
    with sqlite_session_factory() as session:
        state = seed(session)
        calls = []
        event.listen(session, "after_commit", lambda _: calls.append("commit"))
        if remote:
            monkeypatch.setattr(
                AccountService, "get_workspace_permission_keys", Mock(return_value={"workspace.member.manage"})
            )
            monkeypatch.setattr(AccountService, "is_rbac_workspace_owner", Mock(return_value=False))
            # Deliberately distinct from the LOCAL owner; prove the remote owner result is used.
            owner_id = str(uuid4())
            monkeypatch.setattr(AccountService, "get_rbac_workspace_owner_account_id", Mock(return_value=owner_id))
        else:
            owner_id = state.owner.id

        def observed(label):
            def effect(*args, **kwargs):
                assert calls and calls[0] == "commit"
                with sqlite_session_factory() as observer:
                    assert observer.get(TenantAccountJoin, state.join.id) is None
                    assert observer.get(App, state.resources[0].id).maintainer == owner_id
                calls.append(label)
                return True

            return effect

        monkeypatch.setattr(account_service.BillingService, "clean_billing_info_cache", observed("billing"))
        monkeypatch.setattr(
            "services.enterprise.account_deletion_sync.sync_workspace_member_removal", observed("enterprise")
        )
        monkeypatch.setattr(account_service.RBACService.MemberRoles, "delete_rbac_bindings", observed("rbac"))
        TenantService.remove_member_from_tenant(state.tenant, state.member, state.owner, session=session)
        expected = ["commit"] + (["billing"] if edition == DeploymentEdition.CLOUD else []) + ["enterprise"]
        assert calls == expected + (["rbac"] if remote else [])
        assert session.get(Account, state.member.id) is not None


@pytest.mark.parametrize("case", ["self", "no_permission", "admin_removes_owner", "missing_member", "missing_owner"])
def test_public_original_denials_write_nothing(sqlite_session_factory, case):
    with sqlite_session_factory() as session:
        state = seed(session)
        target, operator = state.member, state.owner
        error = NoPermissionError
        if case == "self":
            operator = target
            error = CannotOperateSelfError
        elif case == "no_permission":
            state.owner_join.role = TenantAccountRole.NORMAL
        elif case == "admin_removes_owner":
            state.join.role = TenantAccountRole.ADMIN
            target, operator = state.owner, state.member
        elif case == "missing_member":
            session.delete(state.join)
            error = MemberNotInTenantError
        elif case == "missing_owner":
            state.owner_join.role = TenantAccountRole.ADMIN
            error = ValueError
        session.commit()
        models = (Account, TenantAccountJoin, App, Dataset)
        before = {model: snapshot(session, model) for model in models}
        with pytest.raises(error):
            TenantService.remove_member_from_tenant(state.tenant, target, operator, session=session)
        assert not session.new and not session.dirty and not session.deleted
        for model in models:
            assert snapshot(session, model) == before[model]


def test_public_accepts_original_detached_account_and_tenant_contract(sqlite_session_factory):
    with sqlite_session_factory() as session:
        state = seed(session)
        session.expunge(state.tenant)
        session.expunge(state.member)
        TenantService.remove_member_from_tenant(state.tenant, state.member, state.owner, session=session)
    with sqlite_session_factory() as observer:
        assert observer.get(TenantAccountJoin, state.join.id) is None
        assert observer.get(Account, state.member.id) is not None
        assert observer.get(App, state.resources[0].id).maintainer == state.owner.id
