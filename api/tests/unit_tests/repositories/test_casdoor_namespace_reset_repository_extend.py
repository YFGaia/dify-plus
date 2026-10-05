"""Actual bounded SQL closure, original lease owner and reset transaction failures."""

import json
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_casdoor_namespace_reset_flow_extend import RESET, reset_review, rows, send

from core.casdoor.leases import CasdoorLeases, CasdoorLeaseScope, WorkspaceMemberScope
from models.account import Account, AccountStatus
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
    CasdoorValidationKind,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.casdoor_extend import (
    CasdoorValidationExtend as Validation,
)
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository

pytest_plugins = ("test_casdoor_namespace_reset_flow_extend",)


def insert_intent(session, d, *, kind=CasdoorIntentKind.INVITATION_FINALIZE, state=CasdoorOperationState.APPLIED):
    history = session.scalar(sa.select(History))
    session.add(
        Intent(
            id=str(uuid4()),
            namespace_id=history.namespace_id,
            identity_id=history.identity_id,
            account_id=history.account_id,
            workspace_id=history.workspace_id,
            membership_id=history.id,
            revision_id=history.revision_id,
            generation=history.desired_generation,
            ownership_epoch=history.ownership_epoch,
            fence_epoch=0,
            kind=kind,
            scope_digest="a" * 64,
            idempotency_key="b" * 64,
            desired_json="{}",
            operation_state=state,
            termination_state=CasdoorTerminationState.CONFIRMED,
        )
    )
    session.flush()


@pytest.mark.parametrize(
    "failure",
    [
        "terminal_invite",
        "unknown",
        "avatar",
        "unreleased",
        "remote",
        "tombstone",
        "pending",
        "foreign",
        "rbac_on",
        "unknown_audit",
        "oversize_text",
        "overflow_rows",
    ],
)
def test_real_closure_refuses_all_retained_and_unproved_scope(resettable, failure):
    d = resettable
    with d.f.service._session_factory() as session, session.begin():
        history = session.scalar(sa.select(History))
        if failure in ("terminal_invite", "unknown", "avatar"):
            insert_intent(
                session,
                d,
                kind=CasdoorIntentKind.PROFILE_AVATAR if failure == "avatar" else CasdoorIntentKind.INVITATION_FINALIZE,
                state=CasdoorOperationState.UNKNOWN if failure == "unknown" else CasdoorOperationState.APPLIED,
            )
        elif failure == "unreleased":
            session.execute(sa.update(History).values(ownership=CasdoorMembershipOwnership.MANAGED))
        elif failure == "remote":
            value = json.loads(history.last_applied_roles_json)
            value["backend"] = "remote"
            session.execute(sa.update(History).values(last_applied_roles_json=json.dumps(value)))
        elif failure == "tombstone":
            session.execute(sa.update(History).values(tombstone=True))
        elif failure == "pending":
            session.execute(sa.update(History).values(finalization=CasdoorFinalizationState.PENDING))
        elif failure == "foreign":
            old = session.get(Namespace, d.reset_input["namespace_id"])
            foreign = Namespace(
                id=str(uuid4()),
                integration_id=old.integration_id,
                expected_issuer=old.expected_issuer,
                organization=old.organization,
                application=old.application,
                client_id=old.client_id,
                core_fingerprint=old.core_fingerprint,
                lifecycle=CasdoorNamespaceLifecycle.FENCING,
            )
            session.add(foreign)
            session.flush()
            session.execute(sa.update(History).values(namespace_id=foreign.id))
        elif failure == "rbac_on":
            from models.casdoor_extend import CasdoorConfigRevisionExtend

            revision = session.get(CasdoorConfigRevisionExtend, history.revision_id)
            session.add(
                Validation(
                    revision_id=revision.id,
                    config_digest=revision.config_digest,
                    kind=CasdoorValidationKind.STATIC,
                    rbac_mode="on",
                    summary_json="{}",
                    correlation_id=str(uuid4()),
                )
            )
        elif failure == "oversize_text":
            session.execute(sa.update(History).values(desired_roles_json="x" * 65536))
        else:
            for _ in range(101 if failure == "overflow_rows" else 1):
                session.add(
                    Audit(
                        namespace_id=history.namespace_id,
                        revision_id=history.revision_id,
                        actor_account_id=d.actor.id,
                        action="draft_save" if failure == "overflow_rows" else "remote_resource_write",
                        result_code="success",
                        correlation_id=str(uuid4()),
                        summary_json="{}",
                    )
                )
    before = rows(d)
    response = send(d, RESET + "/review", method="POST", json=d.reset_input)
    assert response.status_code == 400, response.json
    assert rows(d) == before


def test_union_uses_one_original_owner_sorted_keys_and_shared_member_once(resettable):
    d = resettable
    account_id = UUID(d.actor.id)
    with d.f.service._session_factory() as session:
        workspace_id = UUID(session.scalar(sa.select(History.workspace_id)))
    namespace_id = UUID(d.reset_input["namespace_id"])
    member = WorkspaceMemberScope(workspace_id, account_id)
    scopes = (
        CasdoorLeaseScope(namespace_id, "subject-a", account_ids=(account_id,), members=(member,)),
        CasdoorLeaseScope(namespace_id, "subject-b", account_ids=(account_id,), members=(member,)),
    )
    owner = CasdoorLeases.for_scopes(d.f.lease, scopes, deadline=time.monotonic() + 45, ttl_seconds=45)
    assert owner.canonical_keys == tuple(sorted(set(scopes[0].canonical_keys) | set(scopes[1].canonical_keys)))
    owner.acquire()
    assert len(owner._held) == len(owner.canonical_keys)
    owner.ensure_owned()
    assert owner.release() is True
    assert not d.f.control.tokens


def test_multiple_actual_subjects_complete_before_acquire(resettable):
    d = resettable
    with d.f.service._session_factory() as session, session.begin():
        source = session.get(Account, d.actor.id)
        identity = session.scalar(sa.select(Identity))
        account = Account(
            name="Second fixture",
            email="second-reset@example.test",
            interface_language="en-US",
            status=AccountStatus.ACTIVE,
            password=source.password,
            password_salt=source.password_salt,
        )
        session.add(account)
        session.flush()
        session.add(
            Identity(
                id=str(uuid4()),
                namespace_id=identity.namespace_id,
                account_id=account.id,
                subject="second-synthetic-subject",
                issuer=identity.issuer,
                organization=identity.organization,
                last_applied_json=identity.last_applied_json,
                profile_sync_json=identity.profile_sync_json,
            )
        )
    before = rows(d, (Account, Identity, History))
    response = send(d, RESET, method="POST", json=reset_review(d))
    assert response.status_code == 200, response.json
    assert rows(d, (Account, Identity, History)) == before
    assert not d.f.control.tokens


def test_namespace_final_cas_zero_rolls_back_whole_root(resettable, monkeypatch):
    d = resettable
    proof, before = reset_review(d), rows(d)
    from sqlalchemy.orm import Session

    original = Session.execute

    def losing(session, statement, *args, **kwargs):
        if isinstance(statement, sa.sql.dml.Update) and statement.table.name == Namespace.__tablename__:
            return SimpleNamespace(rowcount=0)
        return original(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", losing)
    response = send(d, RESET, method="POST", json=proof)
    assert response.status_code == 400, response.json
    assert rows(d) == before
    assert send(d, RESET, method="POST", json=proof).status_code == 400


@pytest.mark.parametrize("failure", ["cleanup_unknown", "commit_ack_unknown"])
def test_committed_unknown_has_real_get_receipt_and_no_blind_retry(resettable, monkeypatch, failure):
    d = resettable
    proof = reset_review(d)
    if failure == "cleanup_unknown":
        original = CasdoorLeases.release

        def uncertain(owner):
            original(owner)
            return False

        monkeypatch.setattr(CasdoorLeases, "release", uncertain)
    else:
        from sqlalchemy.orm import Session

        target = []
        original = CasdoorConfigurationRepository.reset_namespace

        def remember(owner, *args, **kwargs):
            result = original(owner, *args, **kwargs)
            target.append(owner.session)
            return result

        monkeypatch.setattr(CasdoorConfigurationRepository, "reset_namespace", remember)

        def lost_ack(session):
            if target and session is target[0]:
                raise RuntimeError("synthetic commit acknowledgement lost")

        sa.event.listen(Session, "after_commit", lost_ack)
    try:
        response = send(d, RESET, method="POST", json=proof)
    finally:
        if failure == "commit_ack_unknown":
            sa.event.remove(Session, "after_commit", lost_ack)
    assert response.status_code == (400 if failure == "cleanup_unknown" else 503), response.json
    # Original authenticated GET sees the committed disabled draft. No replay can mint another namespace.
    actual = d.services.casdoor_configuration.get(d.actor)
    assert actual.enabled is False
    assert actual.active is None
    assert str(actual.draft.namespace_id) != d.reset_input["namespace_id"]
    assert actual.etag == proof["etag"] + 1
    before = rows(d)
    assert send(d, RESET, method="POST", json=proof).status_code == 400
    assert rows(d) == before


def test_oversize_text_stops_before_any_full_history_fetch(resettable):
    d = resettable
    with d.f.service._session_factory() as session, session.begin():
        session.execute(sa.update(History).values(desired_roles_json="界" * 21846))
    with d.f.service._session_factory() as session:
        engine = session.get_bind()

    def guard(connection, clause, *args):
        if isinstance(clause, sa.sql.Select) and any(
            value is History.__table__.c.desired_roles_json for value in clause.selected_columns
        ):
            raise AssertionError("full oversized history fetched before byte gate")

    sa.event.listen(engine, "before_execute", guard)
    try:
        response = send(d, RESET + "/review", method="POST", json=d.reset_input)
    finally:
        sa.event.remove(engine, "before_execute", guard)
    assert response.status_code == 400, response.json


def test_length_to_hydrate_drift_cannot_materialize_oversize_text(resettable):
    d = resettable
    with d.f.service._session_factory() as session:
        engine = session.get_bind()
    injected = []

    def gap(connection, clause, *args):
        if (
            isinstance(clause, sa.sql.Select)
            and any(value is History.__table__.c.desired_roles_json for value in clause.selected_columns)
            and not injected
        ):
            injected.append(True)
            connection.execute(sa.update(History).values(desired_roles_json="界" * 21846))

    def materialized(connection, cursor, statement, parameters, context, executemany):
        clause = context.compiled.statement if context.compiled else None
        if isinstance(clause, sa.sql.Select) and any(
            value is History.__table__.c.desired_roles_json for value in clause.selected_columns
        ):
            # The same SELECT's repeated byte predicate filters the drifted row.
            # Consuming an empty cursor does not alter the owner's failed readback.
            assert cursor.fetchall() == []

    sa.event.listen(engine, "before_execute", gap)
    sa.event.listen(engine, "after_cursor_execute", materialized)
    try:
        response = send(d, RESET + "/review", method="POST", json=d.reset_input)
    finally:
        sa.event.remove(engine, "before_execute", gap)
        sa.event.remove(engine, "after_cursor_execute", materialized)
    assert injected == [True]
    assert response.status_code == 400, response.json
