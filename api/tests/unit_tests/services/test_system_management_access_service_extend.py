"""Instance configuration authority stays bound to installation data, not names."""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker

from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.base import Base
from models.model import DifySetup
from models.system_management_scope_extend import SystemManagementScopeExtend
from services.system_management_access_service_extend import (
    SystemManagementAccessService as Access,
)
from services.system_management_access_service_extend import (
    SystemManagementForbiddenError,
)

NOW = datetime(2026, 10, 4, 6, 17, 25)


def migration():
    path = Path(__file__).parents[3] / "migrations_extend/versions/2026_10_07_0001-026_system_management_scope.py"
    spec = importlib.util.spec_from_file_location("scope_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")
    tables = [
        Account.__table__,
        Tenant.__table__,
        TenantAccountJoin.__table__,
        DifySetup.__table__,
        SystemManagementScopeExtend.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        owner = Account(name="Setup owner", email="owner@example.test")
        owner.initialized_at = NOW - timedelta(microseconds=364206)
        owner.created_at = NOW
        initial = Tenant(name="Arbitrary initialization name")
        initial.created_at = NOW
        other = Tenant(name="Admin's Workspace")
        other.created_at = NOW + timedelta(days=1)
        session.add_all([owner, initial, other])
        session.flush()
        first_join = TenantAccountJoin(
            account_id=owner.id, tenant_id=initial.id, role=TenantAccountRole.OWNER, current=True
        )
        second_join = TenantAccountJoin(
            account_id=owner.id, tenant_id=other.id, role=TenantAccountRole.ADMIN, current=False
        )
        setup = DifySetup(version="test")
        setup.setup_at = NOW
        session.add_all([first_join, second_join, setup, SystemManagementScopeExtend(tenant_id=initial.id)])
        session.flush()
        ids = (owner.id, initial.id, other.id, first_join.id, second_join.id)
    yield factory, ids
    engine.dispose()


@pytest.mark.parametrize("role", [TenantAccountRole.OWNER, TenantAccountRole.ADMIN])
def test_only_initialization_workspace_admins_can_manage(storage, role):
    factory, (account_id, first_id, other_id, join_id, other_join_id) = storage
    with factory.begin() as session:
        actor = session.get(Account, account_id)
        actor.set_tenant_id_with_session(first_id, session=session)
        first = session.get(TenantAccountJoin, join_id)
        first.role = role
        session.flush()
        assert Access.can_manage(actor, session=session)
        first.current = False
        session.get(TenantAccountJoin, other_join_id).current = True
        actor.set_tenant_id_with_session(other_id, session=session)
        session.flush()
        assert not Access.can_manage(actor, session=session)
        with pytest.raises(SystemManagementForbiddenError):
            Access.require_management(actor, session=session)


@pytest.mark.parametrize(
    "role", [TenantAccountRole.NORMAL, TenantAccountRole.EDITOR, TenantAccountRole.DATASET_OPERATOR]
)
def test_other_roles_denied_even_when_cached_role_is_owner(storage, role):
    factory, (account_id, _, _, join_id, _) = storage
    with factory.begin() as session:
        actor = session.get(Account, account_id)
        actor.role = TenantAccountRole.OWNER
        session.get(TenantAccountJoin, join_id).role = role
        session.flush()
        assert not Access.can_manage(actor, session=session)


def test_request_workspace_disagreement_and_multiple_current_memberships_deny(storage):
    factory, (account_id, _, other_id, _, other_join_id) = storage
    with factory.begin() as session:
        actor = session.get(Account, account_id)
        actor.set_tenant_id_with_session(other_id, session=session)
        assert not Access.can_manage(actor, session=session)
        actor._current_tenant = None
        session.get(TenantAccountJoin, other_join_id).current = True
        session.flush()
        assert not Access.can_manage(actor, session=session)


@pytest.mark.parametrize("change", ["banned", "uninitialized", "archived", "anchor_missing"])
def test_invalid_state_denies_without_electing_another_workspace(storage, change):
    factory, (account_id, first_id, other_id, _, _) = storage
    with factory.begin() as session:
        actor = session.get(Account, account_id)
        if change == "banned":
            actor.status = AccountStatus.BANNED
        elif change == "uninitialized":
            actor.initialized_at = None
        elif change == "archived":
            session.get(Tenant, first_id).status = TenantStatus.ARCHIVE
        else:
            session.delete(session.get(SystemManagementScopeExtend, "initialization"))
        session.flush()
        assert not Access.can_manage(actor, session=session)
        assert not Access.can_manage(SimpleNamespace(id=account_id, current_tenant_id=other_id), session=session)


def test_rename_new_earlier_import_and_role_revocation_do_not_change_authority(storage):
    factory, (account_id, first_id, _, join_id, _) = storage
    with factory.begin() as session:
        actor = session.get(Account, account_id)
        session.get(Tenant, first_id).name = "Renamed"
        imported = Tenant(name="Older imported workspace")
        imported.created_at = NOW - timedelta(days=100)
        session.add(imported)
        session.flush()
        assert Access.can_manage(actor, session=session)
        session.execute(
            sa.update(TenantAccountJoin)
            .where(TenantAccountJoin.id == join_id)
            .values(role=TenantAccountRole.NORMAL)
            .execution_options(synchronize_session=False)
        )
        assert not Access.can_manage(actor, session=session)
        assert session.get(SystemManagementScopeExtend, "initialization").tenant_id == first_id


def test_missing_database_account_and_pending_writes_cannot_grant_access(storage):
    factory, _ = storage
    with factory() as session:
        actor = Account(name="Not persisted", email="not-persisted@example.test")
        actor.id = str(uuid4())
        actor.initialized_at = NOW
        actor.role = TenantAccountRole.OWNER
        session.add(actor)
        with patch.object(session, "flush", wraps=session.flush) as flush:
            assert not Access.can_manage(actor, session=session)
            flush.assert_not_called()


@pytest.mark.parametrize("invalid", [None, "tie", "no_setup", "no_owner", "not_initialized", "late_owner", "archived"])
def test_migration_backfill_validates_installation_evidence(storage, invalid):
    factory, (account_id, first_id, _, join_id, _) = storage
    with factory.begin() as session:
        session.delete(session.get(SystemManagementScopeExtend, "initialization"))
        if invalid == "tie":
            same = Tenant(name="Same timestamp")
            same.created_at = NOW
            session.add(same)
        elif invalid == "no_setup":
            session.execute(sa.delete(DifySetup))
        elif invalid == "no_owner":
            session.get(TenantAccountJoin, join_id).role = TenantAccountRole.ADMIN
        elif invalid == "not_initialized":
            session.get(Account, account_id).initialized_at = None
        elif invalid == "late_owner":
            session.get(Account, account_id).initialized_at = NOW + timedelta(days=1)
        elif invalid == "archived":
            session.get(Tenant, first_id).status = TenantStatus.ARCHIVE
        session.flush()
        assert migration()._initialization_tenant_id(session.connection()) == (first_id if invalid is None else None)


def test_migration_upgrade_downgrade_and_empty_new_install(storage):
    factory, (_, first_id, _, _, _) = storage
    with factory.begin() as session:
        SystemManagementScopeExtend.__table__.drop(session.connection())
        with Operations.context(MigrationContext.configure(session.connection())):
            migration().upgrade()
            assert session.scalar(sa.select(SystemManagementScopeExtend.tenant_id)) == first_id
            migration().downgrade()
            session.execute(sa.delete(DifySetup))
            migration().upgrade()
            assert session.scalar(sa.select(SystemManagementScopeExtend.tenant_id)) is None


def test_setup_record_uses_actual_created_workspace_and_refuses_rebinding(storage):
    factory, (account_id, first_id, _, _, _) = storage
    with factory.begin() as session:
        actor = session.get(Account, account_id)
        actor.set_tenant_id_with_session(first_id, session=session)
        session.delete(session.get(SystemManagementScopeExtend, "initialization"))
        session.flush()
        Access.record_initialization(actor, session=session)
        session.flush()
        assert session.get(SystemManagementScopeExtend, "initialization").tenant_id == first_id
        with pytest.raises(ValueError, match="already recorded"):
            Access.record_initialization(actor, session=session)


def test_register_setup_persists_real_created_workspace_with_setup_record(storage):
    from services.account_service import RegisterService

    factory, _ = storage
    with factory.begin() as session:
        session.execute(sa.delete(SystemManagementScopeExtend))
        session.execute(sa.delete(DifySetup))
        session.execute(sa.delete(TenantAccountJoin))
        session.execute(sa.delete(Account))
        session.execute(sa.delete(Tenant))

    def create_account(*, session, **kwargs):
        account = Account(name=kwargs["name"], email=kwargs["email"])
        session.add(account)
        session.commit()
        return account

    def create_workspace(*, account, session, **_kwargs):
        tenant = Tenant(name="Never inferred from this name")
        session.add(tenant)
        session.flush()
        session.add(
            TenantAccountJoin(account_id=account.id, tenant_id=tenant.id, role=TenantAccountRole.OWNER, current=False)
        )
        session.flush()
        account.set_tenant_id_with_session(tenant.id, session=session)
        session.commit()

    with (
        patch("services.account_service.AccountService.create_account", side_effect=create_account),
        patch("services.account_service.TenantService.create_owner_tenant_if_not_exist", side_effect=create_workspace),
        patch("services.account_service.CommunityTelemetryService.report_install"),
    ):
        with factory() as session:
            RegisterService.setup("new@example.test", "New owner", "synthetic", "127.0.0.1", "en-US", session=session)
    with factory() as session:
        scope = session.get(SystemManagementScopeExtend, "initialization")
        tenant = session.scalar(sa.select(Tenant))
        assert scope.tenant_id == tenant.id
        assert session.scalar(sa.select(DifySetup)) is not None
        assert session.scalar(sa.select(Account.initialized_at)) is not None


def test_anchor_deletion_cannot_dynamically_promote_other_workspace(storage):
    factory, (account_id, first_id, other_id, join_id, other_join_id) = storage
    with factory.begin() as session:
        session.execute(sa.delete(Tenant).where(Tenant.id == first_id))
        session.get(TenantAccountJoin, join_id).current = False
        session.get(TenantAccountJoin, other_join_id).current = True
        session.flush()
        actor = session.get(Account, account_id)
        actor.set_tenant_id_with_session(other_id, session=session)
        assert not Access.can_manage(actor, session=session)
        assert session.get(SystemManagementScopeExtend, "initialization").tenant_id == first_id


def test_setup_rejects_existing_installation_before_writes_and_preserves_anchor(storage):
    from services.account_service import RegisterService

    factory, (_, first_id, _, _, _) = storage
    with factory() as session, patch("services.account_service.AccountService.create_account") as create_account:
        with pytest.raises(ValueError, match="already initialized"):
            RegisterService.setup("new@example.test", "Other", "synthetic", "127.0.0.1", "en-US", session=session)
        create_account.assert_not_called()
        assert session.get(SystemManagementScopeExtend, "initialization").tenant_id == first_id
        assert session.scalar(sa.select(sa.func.count()).select_from(Account)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(Tenant)) == 2


def test_foreign_key_preserves_initialization_workspace_identity(storage):
    factory, (_, first_id, _, _, _) = storage
    with factory() as session:
        session.execute(sa.text("PRAGMA foreign_keys=ON"))
        with pytest.raises(sa.exc.IntegrityError):
            session.execute(sa.delete(Tenant).where(Tenant.id == first_id))
        session.rollback()
        assert session.get(SystemManagementScopeExtend, "initialization").tenant_id == first_id
        assert session.get(Tenant, first_id) is not None


def test_plain_principal_cannot_impersonate_persisted_initialization_owner(storage):
    factory, (account_id, first_id, _, _, _) = storage
    with factory() as session:
        projection = SimpleNamespace(id=account_id, current_tenant_id=first_id, status="active", current_role="owner")
        assert not Access.can_manage(projection, session=session)
        unpersisted = Account(name="No id", email="no-id@example.test")
        unpersisted.id = None
        assert not Access.can_manage(unpersisted, session=session)
