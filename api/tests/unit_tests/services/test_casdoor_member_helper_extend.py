"""Caller-owned membership writes and legacy post-commit effects, without Casdoor policy."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, func, select

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorIntentKind,
    CasdoorNamespaceExtend,
    CasdoorSyncIntentExtend,
)
from services import account_service
from services.account_service import AccountService, TenantService


@pytest.fixture(autouse=True)
def dependencies(monkeypatch, config_overrides):
    import tasks.initialize_created_app_rbac_access_task as rbac_tasks

    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, RBAC_ENABLED=True, ACCOUNT_TOTAL_QUOTA=Decimal(15))
    billing = MagicMock()
    features = MagicMock()
    features.get_license.return_value.seats.is_available.return_value = True
    delay = MagicMock()
    monkeypatch.setattr(account_service, "BillingService", billing)
    monkeypatch.setattr(account_service, "SystemFeatureService", features)
    monkeypatch.setattr(rbac_tasks.sync_joined_workspace_member_rbac_access_task, "delay", delay)
    return billing, features, delay


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def _seed(session, *, status=AccountStatus.ACTIVE):
    tenant = Tenant(name="Existing workspace")
    account = Account(name="Member", email="member@example.test", status=status)
    session.add_all([tenant, account])
    session.commit()
    return tenant, account


@pytest.mark.parametrize("edition", [DeploymentEdition.COMMUNITY, DeploymentEdition.CLOUD])
def test_helper_flushes_default_join_without_committing_or_any_postcommit_effect(
    edition, sqlite_session, dependencies, monkeypatch, config_overrides
):
    config_overrides(DEPLOYMENT_EDITION=edition)
    tenant, account = _seed(sqlite_session)
    billing, features, delay = dependencies
    commit = MagicMock(wraps=sqlite_session.commit)
    rollback = MagicMock(wraps=sqlite_session.rollback)
    create_workspace = MagicMock()
    monkeypatch.setattr(sqlite_session, "commit", commit)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    monkeypatch.setattr(TenantService, "create_tenant", create_workspace)
    monkeypatch.setattr(TenantService, "create_owner_tenant", create_workspace)
    result = TenantService.persist_tenant_member(tenant, account, sqlite_session)
    assert result.membership_created is True
    assert result.join.role == TenantAccountRole.NORMAL
    assert result.join.current is False
    assert result.join.invited_by is None
    assert result.join.created_at is not None  # Explicit flush materializes the database default.
    assert sqlite_session.get(TenantAccountJoin, result.join.id) is result.join
    assert _count(sqlite_session, Tenant) == 1
    commit.assert_not_called()
    rollback.assert_not_called()
    billing.assert_not_called()
    assert billing.mock_calls == features.mock_calls == []
    delay.assert_not_called()
    create_workspace.assert_not_called()
    sqlite_session.rollback()
    assert _count(sqlite_session, TenantAccountJoin) == 0
    delay.assert_not_called()


def test_helper_reuses_existing_join_and_role_change_is_rolled_back(sqlite_session, dependencies):
    tenant, account = _seed(sqlite_session)
    existing = TenantAccountJoin(
        tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.ADMIN, current=True, invited_by=account.id
    )
    sqlite_session.add(existing)
    sqlite_session.commit()
    result = TenantService.persist_tenant_member(tenant, account, sqlite_session, "editor")
    assert result.join is existing
    assert result.membership_created is False
    assert existing.role == TenantAccountRole.EDITOR
    assert existing.current is True
    assert existing.invited_by == account.id
    assert _count(sqlite_session, TenantAccountJoin) == 1
    dependencies[2].assert_not_called()
    sqlite_session.rollback()
    assert existing.role == TenantAccountRole.ADMIN
    dependencies[2].assert_not_called()


@pytest.mark.parametrize("method", ["persist_tenant_member", "create_tenant_member"])
def test_original_owner_uniqueness_check_rejects_even_existing_owner_reentry(method, sqlite_session, dependencies):
    tenant, account = _seed(sqlite_session)
    owner = TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.OWNER)
    sqlite_session.add(owner)
    sqlite_session.commit()
    with pytest.raises(Exception, match="Tenant already has an owner"):
        getattr(TenantService, method)(tenant, account, sqlite_session, "owner")
    assert owner.role == TenantAccountRole.OWNER
    assert _count(sqlite_session, TenantAccountJoin) == 1
    dependencies[2].assert_not_called()


def test_helper_failure_leaves_rollback_to_caller(sqlite_session, dependencies, monkeypatch):
    tenant, account = _seed(sqlite_session)
    commit = MagicMock(wraps=sqlite_session.commit)
    rollback = MagicMock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "commit", commit)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)

    def fail_join(*_args):
        raise RuntimeError("member insert failed")

    event.listen(TenantAccountJoin, "before_insert", fail_join)
    try:
        with pytest.raises(RuntimeError, match="member insert failed"):
            TenantService.persist_tenant_member(tenant, account, sqlite_session)
        commit.assert_not_called()
        rollback.assert_not_called()
        dependencies[2].assert_not_called()
        sqlite_session.rollback()
        rollback.assert_called_once()
    finally:
        event.remove(TenantAccountJoin, "before_insert", fail_join)
    assert _count(sqlite_session, TenantAccountJoin) == 0


@pytest.mark.parametrize("role", ["normal", "editor", "admin", "owner"])
@pytest.mark.parametrize("existing", [False, True])
def test_legacy_public_commits_before_cloud_cache_then_rbac_task(
    role, existing, sqlite_session_factory, dependencies, config_overrides
):
    billing, _, delay = dependencies
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD)
    order = []
    with sqlite_session_factory() as session:
        tenant, account = _seed(session)
        previous = None
        if existing:
            previous = TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.NORMAL)
            session.add(previous)
            session.commit()

        def committed(_session):
            order.append("commit")

        def verify_cache(tenant_id):
            order.append("cache")
            assert tenant_id == tenant.id
            with sqlite_session_factory() as reader:
                row = reader.scalar(select(TenantAccountJoin).where(TenantAccountJoin.account_id == account.id))
                assert row is not None
                assert row.role == TenantAccountRole(role)

        def verify_task(*args, **kwargs):
            order.append("task")
            assert args == (str(tenant.id), str(account.id))
            assert kwargs == {"operator_account_id": "operator-1"}
            assert order == ["commit", "cache", "task"]

        event.listen(session, "after_commit", committed)
        billing.clean_billing_info_cache.side_effect = verify_cache
        delay.side_effect = verify_task
        result = TenantService.create_tenant_member(tenant, account, session, role, operator_account_id="operator-1")
        assert isinstance(result, TenantAccountJoin)
        if existing:
            assert result is previous
        assert result.role == TenantAccountRole(role)
        assert order == (["commit", "cache"] if existing or role == "owner" else ["commit", "cache", "task"])
        if existing or role == "owner":
            delay.assert_not_called()
        assert _count(session, Tenant) == 1


@pytest.mark.parametrize(
    ("rbac", "status", "role", "existing", "expected_task"),
    [
        (True, AccountStatus.ACTIVE, "normal", False, True),
        (False, AccountStatus.ACTIVE, "normal", False, False),
        (True, AccountStatus.PENDING, "normal", False, False),
        (True, AccountStatus.ACTIVE, "owner", False, False),
        (True, AccountStatus.ACTIVE, "editor", True, False),
        (True, AccountStatus.PENDING, "admin", True, False),
    ],
)
def test_legacy_task_conditions_default_operator_and_existing_update(
    rbac, status, role, existing, expected_task, sqlite_session_factory, dependencies, config_overrides
):
    billing, _, delay = dependencies
    config_overrides(RBAC_ENABLED=rbac)
    with sqlite_session_factory() as session:
        tenant, account = _seed(session, status=status)
        previous = None
        if existing:
            previous = TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.NORMAL)
            session.add(previous)
            session.commit()
        result = TenantService.create_tenant_member(tenant, account, session, role)
        if existing:
            assert result is previous
        assert result.role == TenantAccountRole(role)
        with sqlite_session_factory() as reader:
            assert reader.get(TenantAccountJoin, result.id).role == TenantAccountRole(role)
        assert _count(session, TenantAccountJoin) == 1
        if expected_task:
            delay.assert_called_once_with(str(tenant.id), str(account.id), operator_account_id=None)
        else:
            delay.assert_not_called()
        billing.clean_billing_info_cache.assert_not_called()


def test_public_commit_failure_does_not_dispatch_or_clean_cache(
    sqlite_session, dependencies, config_overrides, monkeypatch
):
    tenant, account = _seed(sqlite_session)
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD)
    commit = MagicMock(side_effect=RuntimeError("caller commit failed"))
    rollback = MagicMock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "commit", commit)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    with pytest.raises(RuntimeError, match="caller commit failed"):
        TenantService.create_tenant_member(tenant, account, sqlite_session)
    commit.assert_called_once()
    rollback.assert_not_called()  # Legacy public member owner has no automatic rollback.
    dependencies[0].clean_billing_info_cache.assert_not_called()
    dependencies[2].assert_not_called()
    sqlite_session.rollback()
    assert _count(sqlite_session, TenantAccountJoin) == 0


@pytest.mark.parametrize("failed_effect", ["cache", "task"])
def test_public_postcommit_failure_keeps_member_durable_and_preserves_effect_order(
    failed_effect, sqlite_session_factory, dependencies, config_overrides
):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD)
    billing, _, delay = dependencies
    effect = billing.clean_billing_info_cache if failed_effect == "cache" else delay
    effect.side_effect = RuntimeError("postcommit failure")
    with sqlite_session_factory() as session:
        tenant, account = _seed(session)
        with pytest.raises(RuntimeError, match="postcommit failure"):
            TenantService.create_tenant_member(tenant, account, session)
    with sqlite_session_factory() as reader:
        row = reader.scalar(select(TenantAccountJoin).where(TenantAccountJoin.account_id == account.id))
        assert row is not None
        assert row.role == TenantAccountRole.NORMAL
    billing.clean_billing_info_cache.assert_called_once_with(tenant.id)
    if failed_effect == "cache":
        delay.assert_not_called()
    else:
        delay.assert_called_once_with(str(tenant.id), str(account.id), operator_account_id=None)


def test_account_join_quota_identity_and_intent_roll_back_in_one_outer_transaction(sqlite_session, dependencies):
    tenant = Tenant(name="Pre-existing workspace")
    integration = CasdoorIntegrationExtend()
    sqlite_session.add_all([tenant, integration])
    sqlite_session.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer="https://synthetic.example",
        organization="Org",
        application="App",
        client_id="Client",
        core_fingerprint="a" * 64,
    )
    sqlite_session.add(namespace)
    sqlite_session.flush()
    revision = CasdoorConfigRevisionExtend(
        integration_id=integration.id,
        namespace_id=namespace.id,
        revision_number=1,
        config_digest="b" * 64,
        browser_frontend_url="https://synthetic.example",
        backend_api_url="https://synthetic.example",
        expected_issuer=namespace.expected_issuer,
        organization="Org",
        application="App",
        client_id="Client",
        button_text="SSO",
        default_workspace_id=tenant.id,
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    sqlite_session.add(revision)
    sqlite_session.commit()
    prepared = AccountService.prepare_account_creation("new@example.test", "New", "en-US")
    models = (Account, TenantAccountJoin, AccountMoneyExtend, CasdoorIdentityExtend, CasdoorSyncIntentExtend)

    def provision_then_fail():
        with sqlite_session.begin():
            account = AccountService.persist_account_creation(prepared, session=sqlite_session)
            member = TenantService.persist_tenant_member(tenant, account, sqlite_session)
            assert member.membership_created
            identity = CasdoorIdentityExtend(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization="Org",
                subject="synthetic-subject",
                last_applied_json="{}",
                profile_sync_json="{}",
            )
            sqlite_session.add(identity)
            sqlite_session.flush()
            sqlite_session.add(
                CasdoorSyncIntentExtend(
                    namespace_id=namespace.id,
                    identity_id=identity.id,
                    account_id=account.id,
                    workspace_id=tenant.id,
                    revision_id=revision.id,
                    generation=1,
                    ownership_epoch=0,
                    fence_epoch=0,
                    kind=CasdoorIntentKind.ROLE_REPLACE,
                    scope_digest="c" * 64,
                    idempotency_key="d" * 64,
                    desired_json="{}",
                )
            )
            sqlite_session.flush()
            for model in models:
                assert _count(sqlite_session, model) == 1
            dependencies[2].assert_not_called()
            raise RuntimeError("later intent failure")

    with pytest.raises(RuntimeError, match="later intent failure"):
        provision_then_fail()
    for model in models:
        assert _count(sqlite_session, model) == 0
    assert _count(sqlite_session, Tenant) == 1
    dependencies[2].assert_not_called()
