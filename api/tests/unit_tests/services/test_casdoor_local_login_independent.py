"""Independent SQLite counterexamples for the private LOCAL root commit."""

from datetime import datetime
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction as Action
from models.account import Account, AccountStatus, Tenant, TenantStatus
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict, CasdoorLoginScopeRepository
from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService
from test_casdoor_local_login_service_extend import local as local_fixture
from test_casdoor_local_login_service_extend import login_env as login_env_fixture
from test_casdoor_local_login_service_extend import ready, run
from test_casdoor_login_account_service_extend import counts, seed

local = local_fixture
login_env = login_env_fixture


def test_scope_projection_uses_null_flags_for_password_presence(local):
    bundle = ready(local)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        assert run(local, bundle).generation == 1
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)
        bundle[3].release()

    scope_reads = [sql for sql in statements if "accounts.password is null" in sql]
    assert scope_reads
    assert all("accounts.password_salt is null" in sql for sql in scope_reads)
    bad = [sql for sql in scope_reads if "accounts.password," in sql or "accounts.password_salt," in sql]
    assert not bad, bad
    assert counts(local.engine) == (1, 1, 1)


def test_linked_history_intent_with_misleading_workspace_fails_before_dml(local):
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from test_casdoor_local_login_service_extend import committed

    result = committed(local)
    bundle = ready(local, Action.USE_BOUND)
    with local.session.begin():
        history = local.session.scalar(sa.select(History).where(History.workspace_id == str(UUID(int=100))))
        assert history is not None
        local.session.add(
            Intent(
                namespace_id=history.namespace_id,
                identity_id=history.identity_id,
                account_id=history.account_id,
                workspace_id=str(UUID(int=200)),
                membership_id=history.id,
                revision_id=history.revision_id,
                generation=history.desired_generation,
                ownership_epoch=history.ownership_epoch,
                fence_epoch=0,
                kind="member_remove",
                scope_digest="b" * 64,
                idempotency_key="c" * 64,
                desired_json="{}",
                operation_state="applied",
                termination_state="confirmed",
            )
        )

    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.upper().lstrip())

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert not bundle[0]._consumed
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
        with local.session.begin():
            assert local.session.scalar(sa.select(Identity.sync_generation)) == result.generation
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)
        bundle[3].release()


def test_prior_workspace_status_drift_after_membership_write_rolls_back_root(local, monkeypatch):
    account, _ = seed(local.env, AccountStatus.PENDING, datetime(2025, 1, 1))
    with local.session.begin():
        prior = Tenant(name="Prior workspace", status=TenantStatus.NORMAL)
        prior.id = str(UUID(int=50))
        local.session.add(prior)
        local.session.flush()
        local.session.add(Join(account_id=account.id, tenant_id=prior.id, role="normal"))
    bundle = ready(local, Action.ACTIVATE_BOUND)
    original = CasdoorLocalMembershipService.persist_local_memberships
    original_recheck = CasdoorLoginScopeRepository.recheck_before_commit

    def assert_prior_scope_drift_is_visible(repository, prepared, before, plan, result):
        current = repository.discover(prepared, _lock=True, _parents=tuple(UUID(row.id) for row in before.workspaces))
        assert current.workspaces != before.workspaces, (before.workspaces, current.workspaces)
        assert next(row.status for row in before.workspaces if row.id == str(UUID(int=50))) is TenantStatus.NORMAL
        assert next(row.status for row in current.workspaces if row.id == str(UUID(int=50))) is TenantStatus.ARCHIVE
        return original_recheck(repository, prepared, before, plan, result)

    def drift_after_membership_write(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        changed = owner._session.execute(
            sa.text("UPDATE tenants SET status = 'archive' WHERE id = :tenant_id"),
            {"tenant_id": str(UUID(int=50))},
        )
        assert changed.rowcount == 1
        assert (
            owner._session.scalar(sa.select(Tenant.status).where(Tenant.id == str(UUID(int=50))))
            is TenantStatus.ARCHIVE
        )
        return result

    monkeypatch.setattr(CasdoorLocalMembershipService, "persist_local_memberships", drift_after_membership_write)
    monkeypatch.setattr(CasdoorLoginScopeRepository, "recheck_before_commit", assert_prior_scope_drift_is_visible)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert counts(local.engine) == (1, 1, 1)
        with local.session.begin():
            assert (
                local.session.scalar(sa.select(Account.status).where(Account.id == account.id)) is AccountStatus.PENDING
            )
            archived = local.session.scalar(sa.select(Tenant.status).where(Tenant.id == str(UUID(int=50))))
            assert archived is TenantStatus.NORMAL
            assert local.session.scalar(sa.select(sa.func.count()).select_from(Join)) == 1
            assert local.session.scalar(sa.select(Identity.sync_generation)) == 0
        assert bundle[0]._consumed
    finally:
        bundle[3].release()
