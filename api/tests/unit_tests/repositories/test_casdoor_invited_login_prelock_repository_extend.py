"""Bounded SQLite/query-construction proofs; no PostgreSQL/MySQL lock claim."""

from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.crypto import CasdoorCrypto
from models.account import Account, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from repositories.casdoor_invitation_finalization_repository_extend import (
    CasdoorInvitationFinalizationRepository,
)
from repositories.casdoor_invited_login_scope_repository_extend import (
    CasdoorInvitedLoginScopeRepository,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
)
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
)
from services.account_service import TenantService
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import OperationalError
from test_casdoor_invited_login_scope_repository_extend import (
    add_history_scope,
    configuration_factory,
    discover,
)
from test_casdoor_invited_login_scope_repository_extend import (
    invited_scope_case as original_case,
)

invited_scope_case = original_case
PARENTS = [
    "casdoor_integration_extend",
    "casdoor_namespace_extend",
    "casdoor_config_revision_extend",
    "accounts",
    "casdoor_identity_extend",
    "tenants",
]


def prelock(case, attempt=None):
    with case.db() as session, session.begin():
        return CasdoorInvitedLoginScopeRepository(
            session, configuration_factory
        ).prelock_and_recheck_completed_invitation(case.attempt if attempt is None else attempt)


@contextmanager
def capture(case):
    queries = []

    def record(connection, clause, multiparams, params, options):
        queries.append(clause)

    sa.event.listen(case.engine, "before_execute", record)
    try:
        yield queries
    finally:
        sa.event.remove(case.engine, "before_execute", record)


def locks(queries):
    return [query for query in queries if getattr(query, "_for_update_arg", None) is not None]


def table(query):
    return query.get_final_froms()[0].name


def sql(query, dialect=postgresql):
    return str(query.compile(dialect=dialect.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("absent", [False, True], ids=["existing-join", "new-join"])
def test_complete_parent_order_nowait_and_exact_observation(invited_scope_case, monkeypatch, absent):
    case = invited_scope_case(absent=absent)
    history = add_history_scope(case)
    expected = discover(case)
    phases = []
    original_projection = CasdoorLoginScopeRepository._project_scope
    original_inspect = CasdoorInvitationFinalizationRepository.inspect

    def projected(owner, *args, **kwargs):
        assert not kwargs.get("_lock") and kwargs.get("_parents") is None
        phases.append(("projection", len(locks(queries))))
        return original_projection(owner, *args, **kwargs)

    def inspected(owner, attempt):
        phases.append(("p3l", len(locks(queries))))
        assert [table(query) for query in locks(queries)] == PARENTS
        result = original_inspect(owner, attempt)
        phases.append(("p3l-end", len(locks(queries))))
        return result

    monkeypatch.setattr(CasdoorLoginScopeRepository, "_project_scope", projected)
    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", inspected)
    with capture(case) as queries:
        result = prelock(case)
    assert result == expected
    assert phases[:3] == [("projection", 0), ("projection", 1), ("p3l", 6)]
    assert phases[-1] == ("projection", phases[-2][1])
    assert len(locks(queries)) == phases[-2][1]  # Final full read introduces no late lock.
    first = locks(queries)[:6]
    for query in first:
        assert query._for_update_arg.nowait is True
        assert query._limit_clause is not None
        for dialect in (postgresql, mysql):
            assert "FOR UPDATE NOWAIT" in sql(query, dialect)
    assert "ORDER BY casdoor_namespace_extend.id" in sql(first[1])
    assert "ORDER BY casdoor_identity_extend.namespace_id, casdoor_identity_extend.id" in sql(first[4])
    assert "ORDER BY tenants.id" in sql(first[5])
    assert history["namespace"] in sql(first[1]) and history["identity"] in sql(first[4])
    assert history["workspace"] in sql(first[5]) and case.ids["workspace"] in sql(first[5])
    assert all(isinstance(query, sa.sql.Select) for query in queries)
    assert not case.redis.calls


@pytest.mark.parametrize("prior", ["select", "connection", "p3ma", "p3l"], ids=["select", "connection", "p3ma", "p3l"])
def test_preexisting_sql_or_invitation_locks_cannot_be_extended(invited_scope_case, prior):
    case = invited_scope_case()
    with case.db() as session, session.begin():
        assert type(session.get_transaction()._connections) is dict and not session.get_transaction()._connections
        owner = CasdoorInvitedLoginScopeRepository(session, configuration_factory)
        if prior == "select":
            session.execute(sa.select(Account.id))
        elif prior == "connection":
            session.connection()
        elif prior == "p3ma":
            owner.discover_completed_invitation(case.attempt)
        else:
            CasdoorInvitationFinalizationRepository(session).inspect(case.attempt)
        assert len(session.get_transaction()._connections) == 2
        with capture(case) as queries, pytest.raises(CasdoorLoginScopeConflict):
            owner.prelock_and_recheck_completed_invitation(case.attempt)
        assert queries == [] and session.in_transaction()


@pytest.mark.parametrize(
    "state",
    ["no-root", "nested", "new", "dirty", "deleted", "autobegin"],
    ids=["no-root", "nested", "new", "dirty", "deleted", "autobegin"],
)
def test_explicit_clean_root_required_before_first_projection(invited_scope_case, state):
    case = invited_scope_case()
    with case.db() as session:
        if state != "no-root" and state != "autobegin":
            session.begin()
        if state == "nested":
            session.begin_nested()
        elif state == "new":
            session.add(Tenant(name="pending"))
        elif state == "dirty":
            session.get(Account, case.ids["account"]).name = "pending"
        elif state == "deleted":
            session.delete(session.get(Account, case.ids["account"]))
        elif state == "autobegin":
            session.execute(sa.select(Account.id))
        with capture(case) as queries, pytest.raises(CasdoorLoginScopeConflict):
            CasdoorInvitedLoginScopeRepository(session, configuration_factory).prelock_and_recheck_completed_invitation(
                case.attempt
            )
        assert queries == []


def mutate(session, case, kind, history):
    if kind == "namespace-add":
        original = dict(
            session.execute(sa.select(*Namespace.__table__.columns).where(Namespace.id == case.ids["namespace"]))
            .one()
            ._mapping
        )
        session.execute(sa.insert(Namespace).values(**dict(original, id=str(uuid4()), lifecycle="archived")))
    elif kind == "namespace-missing":
        session.execute(sa.delete(Namespace).where(Namespace.id == history["namespace"]))
    elif kind == "fence":
        session.execute(sa.update(Namespace).where(Namespace.id == case.ids["namespace"]).values(fence_epoch=1))
    elif kind == "revision":
        session.execute(sa.update(Revision).where(Revision.id == case.ids["revision"]).values(config_digest="0" * 64))
    elif kind == "account":
        session.execute(sa.update(Account).where(Account.id == case.ids["account"]).values(name="changed"))
    elif kind == "identity-missing":
        session.execute(sa.delete(Identity).where(Identity.id == history["identity"]))
    elif kind == "generation":
        session.execute(
            sa.update(Identity).where(Identity.namespace_id == case.ids["namespace"]).values(sync_generation=1)
        )
    elif kind == "workspace-missing":
        session.execute(sa.delete(Tenant).where(Tenant.id == history["workspace"]))
    elif kind == "workspace-add":
        wid = str(uuid4())
        session.execute(sa.insert(Tenant).values(id=wid, name="expanded"))
        session.execute(
            sa.insert(Join).values(id=str(uuid4()), account_id=case.ids["account"], tenant_id=wid, role="normal")
        )
    elif kind == "role":
        session.execute(sa.update(Join).where(Join.id == case.completed.facts.join_id).values(role="normal"))
    elif kind == "intent":
        session.execute(
            sa.update(Intent)
            .where(Intent.id == str(case.pending.snapshot.operation_id))
            .values(operation_state="unknown")
        )
    elif kind == "epoch":
        session.execute(sa.update(Lifecycle).where(Lifecycle.lifecycle_id == case.ids["lifecycle"]).values(epoch=99))
    elif kind == "proof":
        session.execute(
            sa.update(Intent).where(Intent.id == str(case.pending.snapshot.operation_id)).values(proof_ref="invalid")
        )
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "namespace-add",
        "namespace-missing",
        "fence",
        "revision",
        "account",
        "identity-missing",
        "generation",
        "workspace-missing",
        "workspace-add",
        "role",
        "intent",
    ],
    ids=[
        "namespace-add",
        "namespace-missing",
        "fence",
        "revision",
        "account",
        "identity-missing",
        "generation",
        "workspace-missing",
        "workspace-add",
        "role",
        "intent",
    ],
)
def test_drift_after_integration_lock_stops_before_any_remaining_parent(invited_scope_case, monkeypatch, kind):
    case = invited_scope_case()
    history = add_history_scope(case)
    original = CasdoorLoginScopeRepository._lock_invited_integration

    def lock_then_change(owner, context):
        original(owner, context)
        mutate(owner.session, case, kind, history)

    monkeypatch.setattr(CasdoorLoginScopeRepository, "_lock_invited_integration", lock_then_change)
    with capture(case) as queries, pytest.raises(CasdoorLoginScopeConflict):
        prelock(case)
    assert [table(query) for query in locks(queries)] == PARENTS[:1]
    assert not case.redis.calls


@pytest.mark.parametrize(
    "kind",
    ["namespace-add", "identity-missing", "workspace-add", "role", "intent"],
    ids=["namespace-add", "identity-missing", "workspace-add", "role", "intent"],
)
def test_final_projection_detects_scope_change_without_late_parent_locks(invited_scope_case, monkeypatch, kind):
    case = invited_scope_case()
    history = add_history_scope(case)
    original = CasdoorInvitationFinalizationRepository.inspect
    count = []

    def inspect_then_change(owner, attempt):
        facts = original(owner, attempt)
        count.append(len(locks(queries)))
        mutate(owner._session, case, kind, history)
        return facts

    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", inspect_then_change)
    with capture(case) as queries, pytest.raises(CasdoorLoginScopeConflict):
        prelock(case)
    assert len(count) == 1 and len(locks(queries)) == count[0]
    assert [table(query) for query in locks(queries)[:6]] == PARENTS


@pytest.mark.parametrize("kind", ["epoch", "proof", "role", "generation"], ids=["epoch", "proof", "role", "generation"])
def test_p3l_rejects_current_authority_change_after_parent_locks(invited_scope_case, monkeypatch, kind):
    case = invited_scope_case()
    original = CasdoorLoginScopeRepository._lock_invited_candidate_parents

    def parents_then_change(owner, context, candidate):
        original(owner, context, candidate)
        mutate(owner.session, case, kind, {})

    monkeypatch.setattr(CasdoorLoginScopeRepository, "_lock_invited_candidate_parents", parents_then_change)
    with pytest.raises(CasdoorLoginScopeConflict):
        prelock(case)


@pytest.mark.parametrize("stage", ["issued", "pending", "receipt-only"], ids=["issued", "pending", "receipt-only"])
def test_incomplete_completion_never_grants_parent_observation(invited_scope_case, stage):
    case = invited_scope_case(stage="issued" if stage == "issued" else "pending")
    if stage == "receipt-only":
        import json

        with case.db() as session, session.begin():
            facts = CasdoorInvitationFinalizationRepository(session).inspect(case.attempt)
            InvitationAuthorityRepository().record_consumption(
                session, receipt_json=json.loads(facts.snapshot.desired_json)["expected_receipt_json"]
            )
    with pytest.raises(CasdoorLoginScopeConflict):
        prelock(case)
    assert not case.redis.calls


@pytest.mark.parametrize(
    "kind",
    ["dict", "facts", "account", "workspace", "issuance", "generation"],
    ids=["dict", "facts", "account", "workspace", "issuance", "generation"],
)
def test_exact_attempt_only_no_caller_scope_or_rebinding(invited_scope_case, kind):
    case = invited_scope_case()
    value = case.attempt
    if kind == "dict":
        value = {"attempt": case.attempt, "scope": discover(case)}
    elif kind == "facts":
        value = case.completed.facts
    else:
        field = "expected_generation" if kind == "generation" else kind + "_id"
        value = replace(value, **{field: 1 if kind == "generation" else uuid4()})
    with pytest.raises(CasdoorLoginScopeConflict):
        prelock(case, value)


@pytest.mark.parametrize("kind", ["incomplete", "foreign"], ids=["incomplete", "foreign"])
def test_incomplete_or_foreign_owner_completion_cannot_bind(invited_scope_case, monkeypatch, kind):
    case = invited_scope_case()
    original = CasdoorInvitationFinalizationRepository.inspect

    def replaced(owner, attempt):
        facts = original(owner, attempt)
        if kind == "incomplete":
            return replace(facts, completed=False)
        return replace(facts, snapshot=replace(facts.snapshot, operation_id=uuid4()))

    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", replaced)
    with pytest.raises(CasdoorLoginScopeConflict):
        prelock(case)


@pytest.mark.parametrize(
    "index", [1, 2, 3, 4, 5, 6], ids=["integration", "namespaces", "revision", "account", "identities", "workspaces"]
)
def test_parent_nowait_failure_stops_without_retry_or_later_locks(invited_scope_case, monkeypatch, index):
    case = invited_scope_case()
    original = CasdoorLoginScopeRepository._invited_nowait_rows
    calls = []

    def contended(owner, statement, **kwargs):
        calls.append(table(statement))
        if len(calls) == index:
            raise OperationalError("synthetic NOWAIT", {}, RuntimeError("busy"))
        return original(owner, statement, **kwargs)

    monkeypatch.setattr(CasdoorLoginScopeRepository, "_invited_nowait_rows", contended)
    with pytest.raises(CasdoorLoginScopeConflict):
        prelock(case)
    assert calls == PARENTS[:index]


def test_completed_and_stale_applied_intents_still_block_ordinary_admission(invited_scope_case):
    case = invited_scope_case()
    history = add_history_scope(case)
    with case.db() as session, session.begin():
        session.execute(
            sa.update(Intent)
            .where(Intent.id == history["intent"])
            .values(generation=0, operation_state="applied", termination_state="confirmed")
        )
    result = prelock(case)
    assert {row.id for row in result.scope.intents} == {history["intent"], str(case.pending.snapshot.operation_id)}
    with case.db() as session, session.begin(), pytest.raises(CasdoorLoginScopeConflict):
        CasdoorLoginScopeRepository(session, configuration_factory)._intent_barrier(
            case.attempt.account_id, result.scope
        )


def test_no_dml_lifecycle_or_external_owner_calls(invited_scope_case, monkeypatch):
    case = invited_scope_case()
    add_history_scope(case)
    for owner, name in [
        (case.store, "observe_versioned_invitation"),
        (case.store, "consume_versioned_invitation"),
        (TenantService, "persist_tenant_member"),
        (CasdoorCrypto, "decrypt"),
        (case.owner, "persist_invited_login_account"),
    ]:
        monkeypatch.setattr(owner, name, Mock(side_effect=AssertionError("read/recheck only")))
    before = [len(effect.mock_calls) for effect in case.effects]
    with capture(case) as queries:
        with case.db() as session, session.begin():
            with monkeypatch.context() as guard:
                for name in ("flush", "commit", "rollback", "begin_nested"):
                    guard.setattr(session, name, Mock(side_effect=AssertionError("caller owns lifecycle")))
                result = CasdoorInvitedLoginScopeRepository(
                    session, configuration_factory
                ).prelock_and_recheck_completed_invitation(case.attempt)
                assert result.completion.completed and session.in_transaction()
    assert all(isinstance(query, sa.sql.Select) for query in queries)
    assert not case.redis.calls and [len(effect.mock_calls) for effect in case.effects] == before
