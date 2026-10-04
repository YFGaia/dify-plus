"""Whole-scope tests use the same actual private commit integration."""

from datetime import datetime
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction as Action
from core.casdoor.leases import CasdoorLeases, CasdoorLeaseScope
from models.account import AccountStatus, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from test_casdoor_local_login_service_extend import local as local_fixture
from test_casdoor_local_login_service_extend import login_env as login_env_fixture
from test_casdoor_local_login_service_extend import ready, run
from test_casdoor_login_account_service_extend import counts, seed
from test_leases import FakeRedisLua

local = local_fixture
login_env = login_env_fixture


def test_archived_namespace_all_workspace_parents_precede_first_business_dml(local):
    with local.session.begin():
        base = dict(local.session.execute(sa.select(*Namespace.__table__.columns)).one()._mapping)
        local.session.execute(sa.insert(Namespace).values(**dict(base, id=str(UUID(int=1)), lifecycle="archived")))
    bundle = ready(local)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        run(local, bundle)
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)
        bundle[3].release()
    first = next(i for i, (sql, _) in enumerate(statements) if sql.startswith("INSERT") or sql.startswith("UPDATE"))
    prefix = statements[:first]
    ns = next(i for i, (sql, _) in enumerate(prefix) if "FROM casdoor_namespace_extend" in sql)
    account = next(i for i, (sql, _) in enumerate(prefix) if "FROM accounts" in sql)
    space = next(i for i, (sql, _) in enumerate(prefix) if "FROM tenants" in sql)
    join = next(i for i, (sql, _) in enumerate(prefix) if "FROM tenant_account_joins" in sql)
    assert ns < account < space < join
    assert "ORDER BY casdoor_namespace_extend.id" in prefix[ns][0]
    assert str(UUID(int=100)) in prefix[space][1] and str(UUID(int=200)) in prefix[space][1]


def test_scope_expansion_after_discovery_aborts_before_account_mutation(local):
    account, _ = seed(local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    bundle = ready(local, Action.USE_BOUND)
    with local.session.begin():
        space = Tenant(name="Old joined space")
        space.id = str(UUID(int=50))
        local.session.add(space)
        local.session.flush()
        local.session.add(Join(account_id=account.id, tenant_id=space.id, role="normal"))
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert not bundle[0]._consumed
    finally:
        bundle[3].release()


def test_missing_actual_lease_key_fails_before_dml(local):
    bundle = list(ready(local))
    bundle[3].release()
    incomplete = CasdoorLeaseScope(bundle[0].key.namespace_id, bundle[0].key.subject)
    bundle[3] = CasdoorLeases(FakeRedisLua(monotonic), incomplete, deadline=bundle[0].deadline)
    bundle[3].acquire()
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
    finally:
        bundle[3].release()


def add_intent(local, *, account_id, workspace_id=None, membership_id=None, kind="member_remove"):
    from uuid import uuid4

    from models.casdoor_extend import CasdoorSyncIntentExtend as Intent

    c = local.env[1]
    row = Intent(
        namespace_id=str(c.namespace_id),
        identity_id=str(uuid4()),
        account_id=account_id,
        workspace_id=workspace_id,
        membership_id=membership_id,
        revision_id=str(c.revision_id),
        generation=1,
        ownership_epoch=0,
        fence_epoch=0,
        kind=kind,
        scope_digest="a" * 64,
        idempotency_key=uuid4().hex * 2,
        desired_json="{}",
        operation_state="applied",
        termination_state="confirmed",
    )
    local.session.add(row)
    return row


@pytest.mark.parametrize("association", ["null", "workspace", "third_history"])
def test_all_history_required_intents_applied_confirmed_block_before_dml(local, association):
    from uuid import uuid4

    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from test_casdoor_local_login_service_extend import committed

    result = committed(local)
    with local.session.begin():
        if association == "third_history":
            base = dict(
                local.session.execute(sa.select(*History.__table__.columns).order_by(History.workspace_id))
                .first()
                ._mapping
            )
            # Two earlier history references are unrelated to the linked third;
            # the complete scalar scan must still find it.
            for n in (40, 50):
                space = Tenant(name=f"History {n}")
                space.id = str(UUID(int=n))
                local.session.add(space)
                local.session.flush()
                local.session.execute(
                    sa.insert(History).values(
                        **dict(
                            base,
                            id=str(uuid4()),
                            workspace_id=space.id,
                            join_id=None,
                            ownership="released",
                            tombstone=True,
                        )
                    )
                )
            intent = add_intent(local, account_id=str(result.account_id), membership_id=base["id"])
            intent.identity_id = base["identity_id"]
        else:
            add_intent(
                local,
                account_id=str(result.account_id),
                workspace_id=str(UUID(int=200)) if association == "workspace" else None,
            )
    bundle = ready(local, Action.USE_BOUND)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert not bundle[0]._consumed
        with local.session.begin():
            from models.casdoor_extend import CasdoorIdentityExtend as Identity

            assert local.session.scalar(sa.select(Identity.sync_generation)) == 1
    finally:
        bundle[3].release()


@pytest.mark.parametrize("count", [2000, 2001])
def test_namespace_total_budget_with_sentinel(local, count):
    from test_casdoor_local_login_service_extend import discover
    from test_casdoor_login_account_service_extend import prepare

    with local.session.begin():
        base = dict(local.session.execute(sa.select(*Namespace.__table__.columns)).one()._mapping)
        local.session.execute(
            sa.insert(Namespace), [dict(base, id=str(UUID(int=i + 1)), lifecycle="archived") for i in range(count - 1)]
        )
    prepared = prepare(local.env)
    if count == 2001:
        with pytest.raises(CasdoorLoginScopeConflict):
            discover(local, prepared)
    else:
        scope = discover(local, prepared)
        assert len(scope.namespaces) == 2000
    assert not prepared._consumed
    assert counts(local.engine) == (0, 0, 0)


@pytest.mark.parametrize("count", [2048, 2049])
def test_history_total_budget_no_text_materialization(local, count):
    from uuid import uuid4

    from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from test_casdoor_local_login_service_extend import committed, discover
    from test_casdoor_login_account_service_extend import prepare

    committed(local)
    with local.session.begin():
        base = dict(local.session.execute(sa.select(*History.__table__.columns).order_by(History.id)).first()._mapping)
        # All history rows can reference the same workspace, but require distinct
        # namespaces because the real history model has a scope unique key.
        ns = dict(local.session.execute(sa.select(*Namespace.__table__.columns)).one()._mapping)
        # Two scopes per namespace keeps namespace count below its own budget.
        revision = dict(local.session.execute(sa.select(*Revision.__table__.columns)).one()._mapping)
        additions = []
        for i in range(count - 2):
            namespace_id = str(UUID(int=10000 + i // 2))
            if i % 2 == 0:
                local.session.execute(sa.insert(Namespace).values(**dict(ns, id=namespace_id, lifecycle="archived")))
                revision_id = str(uuid4())
                local.session.execute(
                    sa.insert(Revision).values(
                        **dict(revision, id=revision_id, namespace_id=namespace_id, revision_number=2 + i // 2)
                    )
                )
            additions.append(
                dict(
                    base,
                    id=str(uuid4()),
                    namespace_id=namespace_id,
                    revision_id=revision_id,
                    identity_id=str(uuid4()),
                    workspace_id=str(UUID(int=100 if i % 2 == 0 else 200)),
                    join_id=None,
                    ownership="released",
                    tombstone=True,
                    baseline_json="x" * 70000,
                )
            )
        local.session.execute(sa.insert(History), additions)
    prepared = prepare(local.env, Action.USE_BOUND)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        if count == 2049:
            with pytest.raises(CasdoorLoginScopeConflict):
                discover(local, prepared)
        else:
            assert len(discover(local, prepared).histories) == 2048
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)
    history_sql = [sql for sql in statements if "FROM casdoor_managed_membership_extend" in sql]
    assert history_sql
    assert all("baseline_json" not in sql and "last_applied_roles_json" not in sql for sql in history_sql)
    assert not prepared._consumed


@pytest.mark.parametrize("kind", ["prior_unmanaged", "prior_released", "configured"])
def test_archived_prior_only_scope_is_protected_but_configured_space_blocks(local, kind):
    from uuid import uuid4

    from models.account import TenantStatus
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from test_casdoor_local_login_service_extend import committed

    result = committed(local)
    with local.session.begin():
        if kind == "configured":
            local.session.execute(
                sa.update(Tenant).where(Tenant.id == str(UUID(int=200))).values(status=TenantStatus.ARCHIVE)
            )
        else:
            space = Tenant(name="Archived prior scope", status=TenantStatus.ARCHIVE)
            space.id = str(UUID(int=50))
            local.session.add(space)
            local.session.flush()
            join = Join(account_id=str(result.account_id), tenant_id=space.id, role="normal")
            local.session.add(join)
            local.session.flush()
            if kind == "prior_released":
                base = dict(
                    local.session.execute(sa.select(*History.__table__.columns).order_by(History.id)).first()._mapping
                )
                local.session.execute(
                    sa.insert(History).values(
                        **dict(base, id=str(uuid4()), workspace_id=space.id, join_id=join.id, ownership="released")
                    )
                )
    if kind == "configured":
        with pytest.raises(CasdoorLoginScopeConflict):
            ready(local, Action.USE_BOUND)
    else:
        bundle = ready(local, Action.USE_BOUND)
        try:
            assert UUID(int=50) in {member.workspace_id for member in bundle[1].lease_scope.members}
            assert run(local, bundle).generation == 2
            with local.session.begin():
                assert local.session.scalar(sa.select(Join.role).where(Join.tenant_id == str(UUID(int=50)))) == "normal"
        finally:
            bundle[3].release()


def test_actual_prelock_sql_compiles_for_update_with_prior_and_unmatched_parents(local):
    from sqlalchemy.dialects import mysql, postgresql
    from sqlalchemy.orm import Session

    account, _ = seed(local.env, AccountStatus.PENDING, datetime(2025, 1, 1))
    with local.session.begin():
        space = Tenant(name="Prior")
        space.id = str(UUID(int=50))
        local.session.add(space)
        local.session.flush()
        local.session.add(Join(account_id=account.id, tenant_id=space.id, role="normal"))
    # Unmatched configured space 200 must still be locked before activation DML.
    from dataclasses import replace

    local.roles = replace(local.roles, effective_roles=())
    bundle = ready(local, Action.ACTIVATE_BOUND)
    statements = []

    def capture(state):
        statements.append(state.statement)

    sa.event.listen(Session, "do_orm_execute", capture)
    try:
        run(local, bundle)
    finally:
        sa.event.remove(Session, "do_orm_execute", capture)
        bundle[3].release()
    parents = next(stmt for stmt in statements if stmt.is_select and "FROM tenants" in str(stmt))
    for dialect in (mysql.dialect(), postgresql.dialect()):
        compiled = str(parents.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        assert "FOR UPDATE" in compiled and "ORDER BY tenants.id" in compiled
        assert all(str(UUID(int=n)) in compiled for n in (50, 100, 200))
    namespace = next(stmt for stmt in statements if stmt.is_select and "FROM casdoor_namespace_extend" in str(stmt))
    for dialect in (mysql.dialect(), postgresql.dialect()):
        compiled = str(namespace.compile(dialect=dialect))
        assert "FOR UPDATE" in compiled and "ORDER BY casdoor_namespace_extend.id" in compiled


def test_foreign_join_reference_is_corrupt_not_historical_absence(local):
    from models.account import Account
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from test_casdoor_local_login_service_extend import committed

    committed(local)
    with local.session.begin():
        account = Account(name="Other", email="other@example.test", status=AccountStatus.ACTIVE)
        local.session.add(account)
        local.session.flush()
        join = Join(account_id=account.id, tenant_id=str(UUID(int=100)), role="normal")
        local.session.add(join)
        local.session.flush()
        local.session.execute(
            sa.update(History)
            .where(History.workspace_id == str(UUID(int=100)))
            .values(join_id=join.id, ownership="released")
        )
    with pytest.raises(CasdoorLoginScopeConflict):
        ready(local, Action.USE_BOUND)
