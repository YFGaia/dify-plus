"""Closed SQL archive responsibility; actual producers and bounded fresh reads."""

import json
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_casdoor_cross_namespace_adoption_flow_extend import (
    create_adoption,
)
from test_casdoor_namespace_reset_flow_extend import rows
from test_casdoor_namespace_reset_repository_extend import insert_intent

from core.casdoor.auth_transactions import AuthTransactionError
from core.casdoor.leases import CasdoorLeases
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorOperationState, CasdoorIntentKind
from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository, canonical

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


@pytest.fixture
def adopted(lifecycle, monkeypatch):
    create_adoption(lifecycle, monkeypatch)
    return lifecycle


def observed(d):
    with d.f.service._session_factory() as session, session.begin():
        identity = session.scalar(
            sa.select(Identity).where(
                Identity.account_id == d.actor.id, Identity.namespace_id != d.reset_input["namespace_id"]
            )
        )
        return CasdoorLocalLifecycleRepository(session)._adopted_current_refs(
            account_id=d.actor.id,
            namespace_id=identity.namespace_id,
            identity_id=identity.id,
            workspace_id=UUID(int=200),
        )


@pytest.mark.parametrize(
    "failure",
    [
        "reset_missing",
        "reset_duplicate",
        "reset_foreign",
        "reset_bool",
        "release_hash",
        "release_role",
        "adopt_bool",
        "adopt_revision",
        "adopt_actor",
        "baseline",
        "subject_digest",
        "current_archived",
        "old_not_archived",
        "terminal",
        "unknown",
        "avatar",
        "oversize",
        "unknown_audit",
        "desired_reason",
        "revision_digest",
        "validation_nullable",
        "foreign_history",
    ],
)
def test_forged_missing_changed_archive_ledger_blocks(adopted, failure):
    d = adopted
    with d.f.service._session_factory() as session, session.begin():
        ledger = session.scalar(sa.select(Audit).where(Audit.action == "local_namespace_reset_v1"))
        current = session.scalar(sa.select(History).where(History.namespace_id != d.reset_input["namespace_id"]))
        initial = session.scalar(
            sa.select(Audit).where(
                Audit.action == "local_membership_adopt_v1", Audit.namespace_id == current.namespace_id
            )
        )
        if failure == "reset_missing":
            session.delete(ledger)
        elif failure == "reset_duplicate":
            session.add(
                Audit(
                    **{
                        c.name: getattr(ledger, c.name)
                        for c in Audit.__table__.columns
                        if c.name not in ("id", "created_at")
                    }
                )
            )
        elif failure in ("reset_foreign", "reset_bool"):
            value = json.loads(ledger.summary_json)
            value["new_namespace_id" if failure == "reset_foreign" else "fence_epoch"] = (
                str(uuid4()) if failure == "reset_foreign" else True
            )
            ledger.summary_json = canonical(value)
        elif failure in ("release_hash", "release_role"):
            for audit in session.scalars(sa.select(Audit).where(Audit.action == "local_membership_release_v1")):
                value = json.loads(audit.summary_json)
                value["row_sha256" if failure == "release_hash" else "retained_role"] = (
                    "a" * 64 if failure == "release_hash" else "owner"
                )
                audit.summary_json = canonical(value)
        elif failure in ("adopt_bool", "adopt_actor", "adopt_revision"):
            if failure == "adopt_bool":
                value = json.loads(initial.summary_json)
                value["ownership_epoch"] = True
                initial.summary_json = canonical(value)
            elif failure == "adopt_actor":
                initial.actor_account_id = None
            else:
                initial.revision_id = ledger.revision_id
        elif failure == "baseline":
            value = json.loads(current.baseline_json)
            value["join_role"] = "editor" if value["join_role"] != "editor" else "normal"
            session.execute(sa.update(History).where(History.id == current.id).values(baseline_json=canonical(value)))
        elif failure == "subject_digest":
            session.execute(
                sa.update(Identity)
                .where(Identity.namespace_id == d.reset_input["namespace_id"])
                .values(subject_digest="0" * 64)
            )
        elif failure in ("current_archived", "old_not_archived"):
            from models.casdoor_extend import CasdoorNamespaceLifecycle

            session.execute(
                sa.update(Namespace)
                .where(
                    Namespace.id
                    == (current.namespace_id if failure == "current_archived" else d.reset_input["namespace_id"])
                )
                .values(
                    lifecycle=CasdoorNamespaceLifecycle.ARCHIVED
                    if failure == "current_archived"
                    else CasdoorNamespaceLifecycle.ACTIVE
                )
            )
        elif failure in ("terminal", "unknown", "avatar"):
            insert_intent(
                session,
                d,
                kind=CasdoorIntentKind.PROFILE_AVATAR if failure == "avatar" else CasdoorIntentKind.INVITATION_FINALIZE,
                state=CasdoorOperationState.UNKNOWN if failure == "unknown" else CasdoorOperationState.APPLIED,
            )
        elif failure == "desired_reason":
            for old in session.scalars(sa.select(History).where(History.namespace_id == d.reset_input["namespace_id"])):
                value = json.loads(old.desired_roles_json)
                value["reason"] = "unobserved_remote_operation"
                old.desired_roles_json = canonical(value)
        elif failure == "revision_digest":
            from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision

            session.execute(sa.update(Revision).where(Revision.id == ledger.revision_id).values(config_digest="z" * 64))
        elif failure == "validation_nullable":
            from models.casdoor_extend import CasdoorValidationExtend as Validation
            from models.casdoor_extend import CasdoorValidationKind
            from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision

            revision = session.get(Revision, ledger.revision_id)
            session.add(
                Validation(
                    revision_id=revision.id,
                    config_digest=revision.config_digest,
                    kind=CasdoorValidationKind.STATIC,
                    rbac_mode=None,
                    correlation_id=str(uuid4()),
                    summary_json="{}",
                )
            )
        elif failure == "foreign_history":
            from models.account import Account

            other = Account(name="Synthetic foreign association", email="foreign-archive@example.test")
            session.add(other)
            session.flush()
            session.execute(
                sa.update(History)
                .where(History.namespace_id == d.reset_input["namespace_id"])
                .values(account_id=other.id)
            )
        elif failure == "oversize":
            session.execute(
                sa.update(History)
                .where(History.namespace_id == d.reset_input["namespace_id"])
                .values(desired_roles_json="界" * 21846)
            )
        else:
            session.add(
                Audit(
                    namespace_id=current.namespace_id,
                    account_id=d.actor.id,
                    action="remote_resource_write",
                    result_code="success",
                    correlation_id=str(uuid4()),
                    summary_json="{}",
                )
            )
    before = rows(d)
    with pytest.raises((AuthTransactionError, ValueError)):
        observed(d)
    assert rows(d) == before


def test_actual_union_acquired_once_with_all_old_current_subject_and_workspace_keys(lifecycle, monkeypatch):
    original_create, original_acquire = CasdoorLeases.for_scopes, CasdoorLeases.acquire
    selected, acquired = [], []

    def create(cls, client, scopes, **kwargs):
        owner = original_create(client, scopes, **kwargs)
        if len(scopes) > 1 and scopes[0].subject != "namespace-reset":
            selected.append((owner, scopes, owner.canonical_keys))
        return owner

    def acquire(owner):
        before = owner.canonical_keys
        result = original_acquire(owner)
        acquired.append((owner, before, tuple(lock.name for lock in owner._held)))
        assert owner.canonical_keys == before
        return result

    monkeypatch.setattr(CasdoorLeases, "for_scopes", classmethod(create))
    monkeypatch.setattr(CasdoorLeases, "acquire", acquire)
    create_adoption(lifecycle, monkeypatch)
    assert len(selected) == 2
    for owner, scopes, keys in selected:
        assert len({scope.namespace_id for scope in scopes}) == 2
        assert sum(item[0] is owner for item in acquired) == 1
        assert len(keys) == len(set(keys)) and keys == tuple(sorted(keys))
        assert sum(":subject:" in key for key in keys) == 2
        assert sum(":account:" in key for key in keys) == 1
        for workspace in (UUID(int=100), UUID(int=200)):
            assert f"casdoor:lease:v1:member:{workspace}:{lifecycle.actor.id}" in keys
        assert all(
            {member.workspace_id for member in scope.members} == {UUID(int=100), UUID(int=200)} for scope in scopes
        )
    assert not lifecycle.f.lease.data


@pytest.mark.parametrize("drift", [False, True])
def test_portable_sql_byte_gate_prevents_full_oversize_materialization(adopted, drift):
    d = adopted
    engine = d.f.local.engine
    injected = []
    if not drift:
        with d.f.service._session_factory() as session, session.begin():
            session.execute(
                sa.update(History)
                .where(History.namespace_id == d.reset_input["namespace_id"])
                .values(desired_roles_json="界" * 21846)
            )

    def full_history(clause):
        return isinstance(clause, sa.sql.Select) and any(
            column is History.__table__.c.desired_roles_json for column in clause.selected_columns
        )

    def gap(connection, clause, *args):
        if full_history(clause):
            if not drift:
                raise AssertionError("oversized TEXT reached full materialization")
            if not injected:
                injected.append(True)
                connection.execute(
                    sa.update(History)
                    .where(History.namespace_id == d.reset_input["namespace_id"])
                    .values(desired_roles_json="界" * 21846)
                )

    def hydrated(connection, cursor, statement, parameters, context, executemany):
        if drift and context.compiled and full_history(context.compiled.statement):
            values = cursor.fetchall()
            index = [column[0] for column in cursor.description].index("desired_roles_json")
            assert all(len(value[index].encode()) <= 65535 for value in values)
            # The oversize archived rows themselves must be excluded in SQL.
            assert len(values) == 1

    sa.event.listen(engine, "before_execute", gap)
    sa.event.listen(engine, "after_cursor_execute", hydrated)
    try:
        with pytest.raises(AuthTransactionError):
            observed(d)
    finally:
        sa.event.remove(engine, "before_execute", gap)
        sa.event.remove(engine, "after_cursor_execute", hydrated)
    assert injected == ([True] if drift else [])


def test_complete_relevant_audit_navigation_has_100_plus_1_boundary(adopted):
    d = adopted
    with d.f.service._session_factory() as session, session.begin():
        session.add_all(
            [
                Audit(
                    account_id=d.actor.id,
                    action="start",
                    result_code="success",
                    correlation_id=str(uuid4()),
                    summary_json="{}",
                )
                for _ in range(101)
            ]
        )
    with pytest.raises(AuthTransactionError):
        observed(d)
