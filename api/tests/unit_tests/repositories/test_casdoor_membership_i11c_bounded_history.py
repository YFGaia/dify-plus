"""I11-C actual SQLite bounds; accepted fixtures remain unchanged."""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.ownership import MAX_SNAPSHOT_BYTES, MembershipBackend, OwnershipDecision, decide_ownership
from models.account import Tenant, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from repositories.casdoor_membership_repository_extend import CasdoorMembershipConflict
from services.account_service import TenantService
from sqlalchemy.dialects import mysql, postgresql
from test_casdoor_membership_repository_extend import count, inspect, register
from test_casdoor_membership_repository_extend import storage as storage_fixture

storage = storage_fixture


def old_history(s, number=1, *, oversized=False):
    namespace = Namespace(
        id=str(UUID(int=number)),
        integration_id=s.integration.id,
        expected_issuer=s.namespace.expected_issuer,
        organization=f"Old{number}",
        application="App",
        client_id="Client",
        core_fingerprint=f"{number:064x}",
        lifecycle="archived",
    )
    s.session.add(namespace)
    s.session.flush()
    row = History(
        namespace_id=namespace.id,
        identity_id=str(uuid4()),
        account_id=s.account.id,
        workspace_id=s.workspace.id,
        join_id=None,
        ownership=CasdoorMembershipOwnership.RELEASED,
        source=CasdoorMembershipSource.MAPPING,
        revision_id=s.revision.id,
        desired_generation=0,
        last_applied_roles_json="中" * MAX_SNAPSHOT_BYTES if oversized else "{}",
        desired_roles_json="{}",
        baseline_json="{}",
        tombstone=True,
    )
    s.session.add(row)
    s.session.flush()
    return row


def capture(s):
    statements = []
    sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
    return statements


def history_selects(statements):
    return [
        statement
        for statement in statements
        if statement.is_select and str(statement.get_final_froms()[0]) == "casdoor_managed_membership_extend"
    ]


def materializations(statements):
    return [
        statement for statement in history_selects(statements) if "baseline_json" in statement.selected_columns.keys()
    ]


@pytest.mark.parametrize("current,old_count", [(False, 0), (True, 0), (False, 1), (True, 1), (False, 2), (True, 2)])
@pytest.mark.parametrize("precedence", ["managed", "owner", "released", "override", "tombstone"])
def test_original_valid_decision_matrix_and_exact_current(storage, current, old_count, precedence):
    s = storage
    with s.session.begin():
        current_row = None
        if current:
            snapshot = register(s)
            current_row = s.session.get(History, str(snapshot.membership_id))
            if precedence == "released":
                current_row.ownership = CasdoorMembershipOwnership.RELEASED
            elif precedence == "override":
                current_row.ownership = CasdoorMembershipOwnership.LOCAL_OVERRIDE
            elif precedence == "tombstone":
                current_row.tombstone = True
        if precedence == "owner":
            TenantService.persist_tenant_member(s.workspace, s.account, s.session, "owner")
        s.session.flush()
        for number in range(1, old_count + 1):
            old_history(s, number)
        statements = capture(s)
        result = inspect(s)
        # Original algorithm: current snapshot + pure owner, then history
        # existence/cardinality adjustments. No alternative baseline parser.
        expected_snapshot = s.repo._snapshot(current_row) if current_row is not None else None
        expected = decide_ownership(result.observation, expected_snapshot)
        if old_count and not current and expected is OwnershipDecision.NEW_JOIN_REQUIRED:
            expected = OwnershipDecision.PRESERVE_UNMANAGED
        if current and old_count and expected is OwnershipDecision.MANAGED_CURRENT:
            expected = OwnershipDecision.AUTHORIZATION_PENDING
        assert result.decision is expected
        assert result.managed == expected_snapshot
        reads = history_selects(statements)
        assert all(
            statement._limit_clause is not None
            for statement in reads
            if not any("length" in str(column) for column in statement.selected_columns)
        )
        assert len(materializations(statements)) == int(current)
        if current and old_count == 2:
            refs = s.repo._history_refs(UUID(s.account.id), UUID(s.workspace.id))
            assert all(row.namespace_id != s.namespace.id for row in refs)
            assert result.managed.namespace_id == UUID(s.namespace.id)


@pytest.mark.parametrize("field", ["last_applied_roles_json", "desired_roles_json", "baseline_json"])
@pytest.mark.parametrize("overflow", [False, True])
def test_current_unicode_byte_boundary_before_text_loading(storage, field, overflow):
    s = storage
    with s.session.begin():
        snapshot = register(s)
        value = "中" * (MAX_SNAPSHOT_BYTES // 3) + ("a" if overflow else "")
        s.session.execute(
            sa.update(History)
            .where(History.id == str(snapshot.membership_id))
            .values({field: value})
            .execution_options(synchronize_session=False)
        )
        statements = capture(s)
        if overflow:
            with pytest.raises(CasdoorMembershipConflict, match="^config_conflict$"):
                inspect(s)
            assert not materializations(statements)
        else:
            assert getattr(inspect(s).managed, field) == value
            full = materializations(statements)
            assert len(full) == 1
            sql = str(full[0])
            assert sql.count("BETWEEN") == 3 and sql.count("length(CAST(") == 3


@pytest.mark.parametrize("current", [False, True])
def test_oversized_unrelated_text_unread_but_blocks_creation(storage, current):
    s = storage
    with s.session.begin():
        if current:
            register(s)
        old = old_history(s, oversized=True)
        statements = capture(s)
        assert inspect(s).decision is (
            OwnershipDecision.AUTHORIZATION_PENDING if current else OwnershipDecision.PRESERVE_UNMANAGED
        )
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        full = materializations(statements)
        assert len(full) == (2 if current else 0)
        for statement in full:
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            assert f"namespace_id = '{s.namespace.id}'" in sql
            assert old.namespace_id not in sql
        if not current:
            assert all("json" not in str(statement) for statement in history_selects(statements))


@pytest.mark.parametrize("field", ["id", "namespace_id", "identity_id", "join_id", "revision_id"])
@pytest.mark.parametrize("value", ["broken", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"])
def test_history_malformed_and_noncanonical_associations_stable(storage, field, value):
    s = storage
    with s.session.begin():
        row = old_history(s)
        # revision FK needs a corrupt fixture with the actual reference present.
        if field in ("revision_id", "namespace_id"):
            s.session.execute(sa.text("PRAGMA defer_foreign_keys=ON"))
        s.session.execute(
            sa.update(History)
            .where(History.id == row.id)
            .values({field: value})
            .execution_options(synchronize_session=False)
        )
        statements = capture(s)
        with pytest.raises(CasdoorMembershipConflict, match="^config_conflict$"):
            inspect(s)
        assert not materializations(statements)
        assert all(statement.is_select for statement in statements)
        s.session.rollback()


@pytest.mark.parametrize("field", ["id", "invited_by"])
@pytest.mark.parametrize("value", ["broken", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", ""])
def test_join_corruption_has_fixed_conflict(storage, field, value):
    s = storage
    with s.session.begin():
        receipt = TenantService.persist_tenant_member(s.workspace, s.account, s.session)
        s.session.execute(
            sa.update(TenantAccountJoin)
            .where(TenantAccountJoin.id == receipt.join.id)
            .values({field: value})
            .execution_options(synchronize_session=False)
        )
        statements = capture(s)
        with pytest.raises(CasdoorMembershipConflict, match="^config_conflict$"):
            inspect(s)
        assert all(statement.is_select for statement in statements)


@pytest.mark.parametrize("size", [2000, 2001])
def test_namespace_limit_and_sorted_locks_before_account(storage, size):
    s = storage
    with s.session.begin():
        s.session.execute(
            sa.insert(Namespace),
            [
                dict(
                    id=str(UUID(int=number)),
                    integration_id=s.integration.id,
                    expected_issuer=s.namespace.expected_issuer,
                    organization=f"Old{number}",
                    application="App",
                    client_id="Client",
                    core_fingerprint=f"{number:064x}",
                    lifecycle="archived",
                )
                for number in range(1, size)
            ],
        )
        statements = capture(s)
        if size > 2000:
            with pytest.raises(CasdoorMembershipConflict):
                inspect(s)
            assert len(statements) == 2
        else:
            assert inspect(s).decision is OwnershipDecision.NEW_JOIN_REQUIRED
        namespaces = statements[1]
        assert str(namespaces.get_final_froms()[0]) == "casdoor_namespace_extend"
        for dialect in (postgresql.dialect(), mysql.dialect()):
            sql = str(namespaces.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
            assert "ORDER BY casdoor_namespace_extend.id" in sql
            assert "LIMIT 2001" in sql and "FOR UPDATE" in sql
            assert "lifecycle =" not in sql
        if size == 2000:
            tables = [str(statement.get_final_froms()[0]) for statement in statements]
            assert tables[:7] == [
                "casdoor_integration_extend",
                "casdoor_namespace_extend",
                "casdoor_config_revision_extend",
                "accounts",
                "casdoor_identity_extend",
                "tenants",
                "tenant_account_joins",
            ]


def test_stale_orm_refreshed_and_materialization_rechecks_bytes(storage):
    s = storage
    with s.session.begin():
        snapshot = register(s)
        row = s.session.get(History, str(snapshot.membership_id))
        s.session.execute(
            sa.update(History)
            .where(History.id == row.id)
            .values(tombstone=True)
            .execution_options(synchronize_session=False)
        )
        assert row.tombstone is False
        assert inspect(s).decision is OwnershipDecision.PRESERVE_OVERRIDE
        assert row.tombstone is True
        statements = capture(s)
        changed = False

        @sa.event.listens_for(s.session, "do_orm_execute")
        def drift(state):
            nonlocal changed
            if not changed and state.statement.is_select and "baseline_json" in state.statement.selected_columns.keys():
                changed = True
                s.session.connection().execute(
                    sa.update(History).where(History.id == row.id).values(baseline_json="中" * MAX_SNAPSHOT_BYTES)
                )

        with pytest.raises(CasdoorMembershipConflict):
            inspect(s)
        assert changed and len(materializations(statements)) == 1
        assert row.baseline_json == snapshot.baseline_json  # Oversized value never entered identity map.


def test_actual_false_receipt_rejection_rolls_back_entire_caller_uow(storage):
    s = storage
    with s.session.begin():
        workspace = Tenant(name="Other")
        s.session.add(workspace)
        s.session.flush()
        join = TenantService.persist_tenant_member(workspace, s.account, s.session, "admin").join
        join_id = join.id
    with pytest.raises(CasdoorMembershipConflict), s.session.begin():
        token = s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        receipt = TenantService.persist_tenant_member(workspace, s.account, s.session, "normal")
        assert receipt.membership_created is False and receipt.join.role is TenantAccountRole.NORMAL
        s.account.name = "Must roll back too"
        s.session.flush()
        s.repo.register_new(token, receipt)
    with s.session.begin():
        assert s.session.get(TenantAccountJoin, join_id).role is TenantAccountRole.ADMIN
        assert s.account.name == "Synthetic"
        assert count(s, History) == 0


def test_registration_existence_recheck_is_scalar_and_all_namespaces(storage):
    s = storage
    with s.session.begin():
        token = s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        receipt = TenantService.persist_tenant_member(s.workspace, s.account, s.session)
        old_history(s, oversized=True)
        statements = capture(s)
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)
        reads = history_selects(statements)
        assert not materializations(statements)
        assert list(reads[-1].selected_columns.keys()) == ["id"]
        assert reads[-1]._limit_clause.value == 1
        assert "namespace_id =" not in str(reads[-1])


@pytest.mark.parametrize("current", [False, True])
def test_history_namespace_must_be_in_locked_integration_set(storage, current):
    s = storage
    with s.session.begin():
        if current:
            register(s)
        old = old_history(s)
        s.session.execute(sa.text("PRAGMA defer_foreign_keys=ON"))
        s.session.execute(
            sa.update(Namespace)
            .where(Namespace.id == old.namespace_id)
            .values(integration_id=str(uuid4()))
            .execution_options(synchronize_session=False)
        )
        statements = capture(s)
        with pytest.raises(CasdoorMembershipConflict):
            inspect(s)
        assert not materializations(statements)
        assert (
            len(
                [
                    statement
                    for statement in statements
                    if str(statement.get_final_froms()[0]) == "casdoor_namespace_extend"
                ]
            )
            == 1
        )
        s.session.rollback()


@pytest.mark.parametrize("field", ["identity_id", "join_id", "revision_id"])
def test_current_association_drift_rejected_before_materialization(storage, field):
    s = storage
    with s.session.begin():
        snapshot = register(s)
        row = s.session.get(History, str(snapshot.membership_id))
        statements = capture(s)
        changed = False
        if field == "revision_id":
            s.session.execute(sa.text("PRAGMA defer_foreign_keys=ON"))

        @sa.event.listens_for(s.session, "do_orm_execute")
        def drift(state):
            nonlocal changed
            if not changed and state.statement.is_select and "baseline_json" in state.statement.selected_columns.keys():
                changed = True
                s.session.connection().execute(
                    sa.update(History).where(History.id == row.id).values({field: str(uuid4())})
                )

        with pytest.raises(CasdoorMembershipConflict):
            inspect(s)
        assert changed and len(materializations(statements)) == 1
        assert getattr(row, field) == str(getattr(snapshot, field))
        s.session.rollback()


@pytest.mark.parametrize("dialect", [postgresql.dialect(), mysql.dialect()])
def test_server_byte_guard_uses_octet_length(storage, monkeypatch, dialect):
    s = storage
    from types import SimpleNamespace

    monkeypatch.setattr(s.session, "get_bind", lambda: SimpleNamespace(dialect=dialect))
    expression = s.repo._byte_length(History.baseline_json).between(0, MAX_SNAPSHOT_BYTES)
    sql = str(expression.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "octet_length(casdoor_managed_membership_extend.baseline_json) BETWEEN 0 AND 65535" == sql
