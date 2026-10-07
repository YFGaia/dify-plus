"""Independent transaction checks for the Casdoor member persistence seam."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from services import account_service
from services.account_service import TenantService


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def _seed(session):
    tenant = Tenant(name="Independent rollback workspace")
    account = Account(name="Independent member", email="independent-member@example.test", status=AccountStatus.ACTIVE)
    session.add_all([tenant, account])
    session.commit()
    return tenant, account


def test_persist_member_flush_is_invisible_to_reader_and_outer_rollback_removes_it(
    sqlite_session_factory,
):
    with sqlite_session_factory() as writer:
        tenant, account = _seed(writer)

        def flush_then_abort():
            with writer.begin():
                persisted = TenantService.persist_tenant_member(tenant, account, writer)
                assert persisted.membership_created is True
                assert persisted.join.role == TenantAccountRole.NORMAL
                with sqlite_session_factory() as reader:
                    assert (
                        reader.scalar(select(TenantAccountJoin).where(TenantAccountJoin.id == persisted.join.id))
                        is None
                    )
                raise RuntimeError("abort membership transaction")

        with pytest.raises(RuntimeError, match="abort membership transaction"):
            flush_then_abort()

        with sqlite_session_factory() as reader:
            assert _count(reader, TenantAccountJoin) == 0


def test_legacy_wrapper_commits_before_cache_and_dispatches_original_task(
    sqlite_session_factory, config_overrides, monkeypatch
):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD, RBAC_ENABLED=True)
    billing = MagicMock()
    monkeypatch.setattr(account_service, "BillingService", billing)
    import tasks.initialize_created_app_rbac_access_task as rbac_tasks

    delay = MagicMock()
    monkeypatch.setattr(rbac_tasks.sync_joined_workspace_member_rbac_access_task, "delay", delay)
    sequence = []

    with sqlite_session_factory() as writer:
        tenant, account = _seed(writer)

        def inspect_committed_cache_key(tenant_id):
            sequence.append("cache")
            assert tenant_id == tenant.id
            with sqlite_session_factory() as reader:
                joined = reader.scalar(
                    select(TenantAccountJoin).where(
                        TenantAccountJoin.tenant_id == tenant.id,
                        TenantAccountJoin.account_id == account.id,
                    )
                )
                assert joined is not None
                assert joined.role == TenantAccountRole.NORMAL

        def inspect_task(*args, **kwargs):
            sequence.append("task")
            assert args == (str(tenant.id), str(account.id))
            assert kwargs == {"operator_account_id": "independent-operator"}
            assert sequence == ["cache", "task"]

        billing.clean_billing_info_cache.side_effect = inspect_committed_cache_key
        delay.side_effect = inspect_task
        result = TenantService.create_tenant_member(
            tenant,
            account,
            writer,
            operator_account_id="independent-operator",
        )

        assert isinstance(result, TenantAccountJoin)
        assert result.role == TenantAccountRole.NORMAL
        billing.clean_billing_info_cache.assert_called_once_with(tenant.id)
        delay.assert_called_once_with(str(tenant.id), str(account.id), operator_account_id="independent-operator")
        assert sequence == ["cache", "task"]
