"""Exact registry token and preserved required-intent query contracts."""

from copy import copy
from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from models.casdoor_extend import CasdoorIntentKind, CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_invited_login_guard_repository_extend import (
    _REGISTRY,
    CasdoorInvitedLoginGuardRepository,
    _InvitationIntentGuard,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from repositories.casdoor_required_intent_repository_extend import CasdoorRequiredIntentRepository
from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import configuration_factory
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    invited_scope_case as original_case,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as original_writer,
)

invited_scope_case = original_case
writer_case = original_writer


def issue(owner, case):
    return owner.prelock(case.attempt, roles=case.roles, leases=case.leases, deadline=case.deadline)


def read(session, case, guard=None, **kwargs):
    return CasdoorRequiredIntentRepository(session).read_locked(
        kwargs.get("account", case.attempt.account_id),
        kwargs.get("workspace", case.attempt.workspace_id),
        invitation_guard=guard,
    )


def test_exact_guard_excludes_only_own_operation_full_scope_still_contains_it(writer_case):
    case = writer_case()
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        try:
            assert read(session, case) == ((str(case.pending.snapshot.operation_id),),)
            assert read(session, case, guard) == ()
            assert owner._scope(_REGISTRY[guard]).intents == case.observed.scope.intents
            assert read(session, case) == ((str(case.pending.snapshot.operation_id),),)
        finally:
            owner.revoke(guard)


@pytest.mark.parametrize(
    "kind",
    ["copy", "fabricated", "uuid", "dict", "other-session", "other-root", "account", "workspace", "revoked", "written"],
)
def test_nonexact_guard_fails_closed(writer_case, kind):
    case = writer_case()
    with case.db() as session:
        root = session.begin()
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        try:
            value = guard
            kwargs = {}
            if kind == "copy":
                value = copy(guard)
            elif kind == "fabricated":
                value = _InvitationIntentGuard()
            elif kind == "uuid":
                value = case.pending.snapshot.operation_id
            elif kind == "dict":
                value = {"completed": True}
            elif kind == "account":
                kwargs["account"] = uuid4()
            elif kind == "workspace":
                kwargs["workspace"] = uuid4()
            elif kind == "revoked":
                owner.revoke(guard)
            elif kind == "written":
                owner.persist_once(guard)
            elif kind == "other-root":
                root.rollback()
                root = session.begin()
            if kind == "other-session":
                with case.db() as other, other.begin(), pytest.raises(CasdoorLoginScopeConflict):
                    read(other, case, guard)
            else:
                with pytest.raises(CasdoorLoginScopeConflict):
                    read(session, case, value, **kwargs)
        finally:
            root.rollback()
            owner.revoke(guard)


@pytest.mark.parametrize("global_scope", [False, True])
@pytest.mark.parametrize("state", tuple(CasdoorOperationState))
@pytest.mark.parametrize("termination", tuple(CasdoorTerminationState))
@pytest.mark.parametrize("generation", [0, 1, 99])
def test_other_operation_all_states_generations_and_global_scope_remain_barriers(
    writer_case, global_scope, state, termination, generation
):
    case = writer_case()
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        try:
            original = dict(session.execute(sa.select(*Intent.__table__.columns)).one()._mapping)
            second = str(uuid4())
            session.execute(
                sa.insert(Intent).values(
                    **dict(
                        original,
                        id=second,
                        idempotency_key=uuid4().hex * 2,
                        kind=CasdoorIntentKind.MEMBER_REMOVE,
                        operation_state=state,
                        termination_state=termination,
                        generation=generation,
                        workspace_id=None if global_scope else case.ids["workspace"],
                    )
                )
            )
            assert read(session, case, guard) == ((second,),)
        finally:
            owner.revoke(guard)


@pytest.mark.parametrize("revoke_before_apply", [False, True])
def test_actual_local_role_prepare_and_apply_both_use_guard(writer_case, revoke_before_apply, monkeypatch):
    from core.casdoor.local_roles import LocalRoleOutcome
    from repositories import casdoor_invited_login_guard_repository_extend as guards
    from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
    from repositories.casdoor_local_role_repository_extend import CasdoorLocalRoleRepository

    case = writer_case(invite_target=True)
    original = guards._invited_target_guard
    visited = []

    def target_read(guard, session, workspace):
        selected = original(guard, session, workspace)
        if selected is not None:
            visited.append(workspace)
            plan = _REGISTRY[guard].plan
            target = next(item for item in plan.targets if item.workspace_id == workspace)
            version = GenerationPlanVersion(plan, case.context.fence_epoch, 1)
            local = CasdoorLocalRoleRepository(session)
            assert local.apply(local.prepare(version, target)).outcome is LocalRoleOutcome.PENDING
            prepared = local.prepare(version, target, invitation_guard=guard)
            if revoke_before_apply:
                CasdoorInvitedLoginGuardRepository.revoke(guard)
                with pytest.raises(CasdoorLoginScopeConflict):
                    local.apply(prepared)
            else:
                assert local.apply(prepared).outcome is LocalRoleOutcome.PRESERVED
        return selected

    monkeypatch.setattr(guards, "_invited_target_guard", target_read)
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        try:
            if revoke_before_apply:
                with pytest.raises(CasdoorLoginScopeConflict):
                    owner.persist_once(guard)
            else:
                owner.persist_once(guard)
                owner.verify_after(guard)
            assert visited == [case.attempt.workspace_id]
        finally:
            owner.revoke(guard)


def test_default_query_and_guard_query_have_identical_association_order_limit_and_lock(writer_case):
    case = writer_case()
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        queries = []

        def record(connection, clause, multiparams, params, options):
            if isinstance(clause, sa.sql.Select) and tuple(clause.selected_columns.keys()) == ("id",):
                queries.append(clause)

        sa.event.listen(case.engine, "before_execute", record)
        try:
            read(session, case)
            ordinary = queries[-1]
            read(session, case, guard)
            guarded = queries[-1]
            assert len(ordinary._where_criteria) == 2
            assert len(guarded._where_criteria) == 3
            assert all(a.compare(b) for a, b in zip(ordinary._where_criteria, guarded._where_criteria[:2], strict=True))
            assert guarded._where_criteria[2].compare(Intent.id != str(case.pending.snapshot.operation_id))
            assert ordinary._order_by_clause.compare(guarded._order_by_clause)
            assert ordinary._limit_clause.compare(guarded._limit_clause)
            assert ordinary._for_update_arg.compare(guarded._for_update_arg)
        finally:
            sa.event.remove(case.engine, "before_execute", record)
            owner.revoke(guard)


@pytest.mark.parametrize("dialect_name", ["sqlite", "postgresql", "mysql"])
def test_text_byte_bounds_compile_for_all_supported_dialects(dialect_name):
    from types import SimpleNamespace

    from sqlalchemy.dialects import mysql, postgresql, sqlite

    dialect = {"sqlite": sqlite, "postgresql": postgresql, "mysql": mysql}[dialect_name].dialect()
    queries = []

    class QueryRecorder:
        def get_bind(self):
            return SimpleNamespace(dialect=dialect)

        def execute(self, query):
            queries.append(query)
            return ()

    owner = CasdoorInvitedLoginGuardRepository(QueryRecorder(), configuration_factory)
    assert owner._rows(Intent, Intent.id == str(uuid4())) == ()
    sql = [str(query.compile(dialect=dialect, compile_kwargs={"literal_binds": True})) for query in queries]
    expected = "length(CAST(" if dialect_name == "sqlite" else "octet_length("
    assert expected in sql[0]
    assert expected in sql[1]
    assert "262144" in sql[1]
    assert "LIMIT 2049" in sql[0]
    assert "LIMIT 2049" in sql[1]


def test_oversized_operation_text_stops_at_length_header_without_materialization(writer_case):
    case = writer_case()
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        queries = []

        def record(connection, clause, multiparams, params, options):
            if isinstance(clause, sa.sql.Select):
                queries.append(clause)

        try:
            session.execute(sa.update(Intent).values(desired_json="界" * (256 * 1024 // 3 + 1)))
            sa.event.listen(case.engine, "before_execute", record)
            with pytest.raises(CasdoorLoginScopeConflict):
                read(session, case, guard)
            assert len(queries) == 1
            assert "desired_json" not in tuple(queries[0].selected_columns.keys())
            assert "length(CAST(" in str(queries[0])
        finally:
            sa.event.remove(case.engine, "before_execute", record)
            owner.revoke(guard)


@pytest.mark.parametrize("association", ["cross-namespace", "history-only", "cross-namespace-history"])
def test_other_namespace_and_history_only_associations_are_not_exempted(writer_case, association):
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from models.casdoor_extend import CasdoorNamespaceExtend as Namespace

    case = writer_case()
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        try:
            row = dict(session.execute(sa.select(*Intent.__table__.columns)).one()._mapping)
            second = str(uuid4())
            if "cross-namespace" in association:
                namespace = dict(session.execute(sa.select(*Namespace.__table__.columns)).one()._mapping)
                row["namespace_id"] = str(uuid4())
                session.execute(
                    sa.insert(Namespace).values(**dict(namespace, id=row["namespace_id"], lifecycle="archived"))
                )
            if "history" in association:
                hid = str(uuid4())
                session.execute(
                    sa.insert(History).values(
                        id=hid,
                        namespace_id=row["namespace_id"],
                        identity_id=row["identity_id"],
                        account_id=case.ids["account"],
                        workspace_id=case.ids["workspace"],
                        join_id=case.completed.facts.join_id,
                        ownership="released",
                        ownership_epoch=0,
                        source="mapping",
                        desired_generation=0,
                        revision_id=case.ids["revision"],
                        last_applied_fingerprint="0" * 64,
                        finalization="finalized",
                        tombstone=True,
                        last_applied_roles_json="{}",
                        desired_roles_json="{}",
                        baseline_json="{}",
                    )
                )
                row.update(account_id=str(uuid4()), workspace_id=str(uuid4()), membership_id=hid)
            session.execute(sa.insert(Intent).values(**dict(row, id=second, idempotency_key=uuid4().hex * 2)))
            assert read(session, case, guard) == ((second,),)
        finally:
            owner.revoke(guard)


@pytest.mark.parametrize("change", ["copied-plan", "withdrawal", "generation", "fence", "second-attempt"])
def test_guard_binds_actual_plan_and_one_real_b3_attempt(writer_case, change):
    case = writer_case()
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, configuration_factory)
        guard = issue(owner, case)
        try:
            value = _REGISTRY[guard]
            if change == "second-attempt":
                owner.persist_once(guard)
                with pytest.raises(CasdoorLoginScopeConflict):
                    owner.persist_once(guard)
            else:
                with pytest.raises(CasdoorLoginScopeConflict):
                    CasdoorLocalMembershipService(session).persist_local_memberships(
                        replace(value.plan) if change == "copied-plan" else value.plan,
                        expected_fence_epoch=case.context.fence_epoch + int(change == "fence"),
                        expected_generation=int(change == "generation"),
                        withdrawal_workspace_ids=(case.attempt.workspace_id,) if change == "withdrawal" else (),
                        invitation_guard=guard,
                    )
        finally:
            owner.revoke(guard)
