"""Repeated original reset producer provenance, not synthetic successful ledgers."""

import json
from uuid import uuid4

import pytest
import sqlalchemy as sa

from core.casdoor.auth_transactions import AuthTransactionError
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIntentKind, CasdoorOperationState
from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository, canonical
from test_casdoor_cross_namespace_adoption_flow_extend import create_adoption, native_source, ordinary
from test_casdoor_namespace_reset_flow_extend import rows
from test_casdoor_namespace_reset_repository_extend import insert_intent
from test_casdoor_repeated_namespace_recovery_flow_extend import current_target, reconnect_adopt, repeat_reset

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


@pytest.fixture
def repeated(lifecycle, monkeypatch):
    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    repeat_reset(d)
    reconnect_adopt(d)
    ordinary(d, 1)
    native_source(d)
    return d


def observed(d):
    target = current_target(d)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    with d.f.service._session_factory() as session, session.begin():
        return CasdoorLocalLifecycleRepository(session)._adopted_current_refs(
            account_id=d.actor.id, namespace_id=snapshot.active.namespace_id,
            identity_id=target["identity_id"], workspace_id=target["workspace_id"],
        )


@pytest.mark.parametrize("failure", [
    "missing", "duplicate", "cycle", "fork", "foreign", "bool", "wrong_revision", "malformed", "oversize",
])
def test_repeated_graph_facts_fail_closed(repeated, failure):
    d = repeated
    assert observed(d)[1]
    with d.f.service._session_factory() as session, session.begin():
        ledgers = session.scalars(sa.select(Audit).where(
            Audit.action == "local_namespace_reset_v1",
        ).order_by(Audit.created_at, Audit.id)).all()
        assert len(ledgers) == 2
        first = ledgers[0]
        value = json.loads(first.summary_json)
        if failure == "missing":
            session.delete(first)
        elif failure == "duplicate":
            session.add(Audit(**{c.name: getattr(first, c.name) for c in Audit.__table__.columns
                                 if c.name not in ("id", "created_at")}))
        elif failure == "malformed":
            first.summary_json = "{}"
        elif failure == "oversize":
            first.summary_json = "界" * 21846
        elif failure == "cycle":
            second = ledgers[1]
            value = json.loads(second.summary_json)
            value["new_namespace_id"] = first.namespace_id
            value["new_revision_id"] = first.revision_id
            second.summary_json = canonical(value)
        elif failure == "fork":
            second = json.loads(ledgers[1].summary_json)
            value["new_namespace_id"] = second["new_namespace_id"]
            value["new_revision_id"] = second["new_revision_id"]
            first.summary_json = canonical(value)
        else:
            key, replacement = {
                "foreign": ("new_namespace_id", str(uuid4())),
                "bool": ("fence_epoch", True),
                "wrong_revision": ("new_revision_id", first.revision_id),
            }[failure]
            value[key] = replacement
            first.summary_json = canonical(value)
    before = rows(d)
    with pytest.raises((AuthTransactionError, ValueError)):
        observed(d)
    assert rows(d) == before


@pytest.mark.parametrize("kind", list(CasdoorIntentKind))
@pytest.mark.parametrize("state", [CasdoorOperationState.APPLIED, CasdoorOperationState.UNKNOWN])
def test_repeated_graph_all_associated_intents_remain_barriers(repeated, kind, state):
    d = repeated
    assert observed(d)[1]
    with d.f.service._session_factory() as session, session.begin():
        insert_intent(session, d, kind=kind, state=state)
    before = rows(d)
    with pytest.raises((AuthTransactionError, ValueError)):
        observed(d)
    assert rows(d) == before


@pytest.fixture
def repeated_resettable(lifecycle, monkeypatch):
    from test_casdoor_local_lifecycle_flow_extend import send
    from test_casdoor_namespace_reset_flow_extend import ROOT
    from test_casdoor_repeated_namespace_recovery_flow_extend import release_current

    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    native_source(d)
    target = current_target(d)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": snapshot.etag})
    assert disabled.status_code == 200, disabled.json
    release_current(d, target)
    d.reset_input = {
        "namespace_id": str(snapshot.active.namespace_id),
        "etag": disabled.json["configuration"]["etag"],
        "confirm_management_review": True,
    }
    return d


@pytest.mark.parametrize("failure", ["source_after_write", "lease_lost", "late_join", "late_intent"])
def test_second_reset_original_whole_root_guards(repeated_resettable, monkeypatch, failure):
    from test_casdoor_namespace_reset_flow_extend import test_whole_root_rollback_after_archive_and_draft

    test_whole_root_rollback_after_archive_and_draft(repeated_resettable, monkeypatch, failure)


@pytest.mark.parametrize("failure", ["commit_ack_unknown", "cleanup_unknown"])
def test_second_reset_original_unknown_ack_receipt(repeated_resettable, monkeypatch, failure):
    from test_casdoor_namespace_reset_repository_extend import (
        test_committed_unknown_has_real_get_receipt_and_no_blind_retry,
    )

    test_committed_unknown_has_real_get_receipt_and_no_blind_retry(repeated_resettable, monkeypatch, failure)


@pytest.mark.parametrize("failure", ["ledger_drift", "audit_growth", "byte_growth"])
def test_second_reset_final_readback_rejects_new_ancestor_changes(repeated_resettable, monkeypatch, failure):
    from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from test_casdoor_namespace_reset_flow_extend import RESET, reset_review, send

    d = repeated_resettable
    proof, before = reset_review(d), rows(d)
    original = CasdoorConfigurationRepository.reset_namespace

    def after_write(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        ledger = owner.session.scalar(sa.select(Audit).where(
            Audit.action == "local_namespace_reset_v1", Audit.namespace_id != d.reset_input["namespace_id"],
        ))
        assert ledger is not None
        if failure == "ledger_drift":
            value = json.loads(ledger.summary_json)
            value["review_sha256"] = "a" * 64
            owner.session.execute(sa.update(Audit).where(Audit.id == ledger.id).values(summary_json=canonical(value)))
        elif failure == "audit_growth":
            owner.session.add(Audit(
                namespace_id=ledger.namespace_id, account_id=d.actor.id, action="profile_sync",
                result_code="success", correlation_id=str(uuid4()), summary_json="{}",
            ))
            owner.session.flush()
        else:
            owner.session.execute(sa.update(History).where(History.namespace_id == ledger.namespace_id)
                                  .values(desired_roles_json="界" * 21846))
        return result

    monkeypatch.setattr(CasdoorConfigurationRepository, "reset_namespace", after_write)
    response = send(d, RESET, method="POST", json=proof)
    assert response.status_code in (400, 409, 503), response.json
    assert rows(d) == before


def test_second_reset_acquires_exact_all_namespace_subject_member_union(repeated_resettable, monkeypatch):
    from core.casdoor.leases import CasdoorLeases
    from models.casdoor_extend import CasdoorIdentityExtend as Identity, CasdoorManagedMembershipExtend as History
    from test_casdoor_namespace_reset_flow_extend import RESET, reset_review, send

    d = repeated_resettable
    with d.f.service._session_factory() as session:
        subjects = set(session.execute(sa.select(Identity.namespace_id, Identity.subject)))
        members = set(session.execute(sa.select(History.workspace_id, History.account_id)))
    original_create, original_acquire = CasdoorLeases.for_scopes, CasdoorLeases.acquire
    selected, acquired = [], []

    def create(cls, client, scopes, **kwargs):
        owner = original_create(client, scopes, **kwargs)
        if scopes[0].subject == "namespace-reset":
            assert {(str(s.namespace_id), s.subject) for s in scopes[1:]} == subjects
            assert {(str(m.workspace_id), str(m.account_id)) for m in scopes[0].members} == members
            selected.append(owner)
        return owner

    def acquire(owner):
        keys = owner.canonical_keys
        result = original_acquire(owner)
        if owner in selected:
            assert tuple(lock.name for lock in owner._held) == keys
            acquired.append(owner)
        assert owner.canonical_keys == keys
        return result

    monkeypatch.setattr(CasdoorLeases, "for_scopes", classmethod(create))
    monkeypatch.setattr(CasdoorLeases, "acquire", acquire)
    response = send(d, RESET, method="POST", json=reset_review(d))
    assert response.status_code == 200, response.json
    assert len(selected) == len(acquired) == 2
    assert not d.f.lease.data


def test_current_unlink_uses_original_union_for_callback_and_delete(lifecycle, monkeypatch):
    from core.casdoor.leases import CasdoorLeases
    from test_casdoor_repeated_namespace_recovery_flow_extend import (
        test_current_recent_auth_unlink_after_actual_archive_release,
    )
    import traceback

    selected = []
    original = CasdoorLeases.for_scopes

    def create(cls, client, scopes, **kwargs):
        owner = original(client, scopes, **kwargs)
        if any(f.name == "_identity_action_leases" for f in traceback.extract_stack()):
            assert len({s.namespace_id for s in scopes}) == 2
            assert len({m.workspace_id for m in scopes[0].members}) == 2
            selected.append(owner)
        return owner

    monkeypatch.setattr(CasdoorLeases, "for_scopes", classmethod(create))
    test_current_recent_auth_unlink_after_actual_archive_release(lifecycle, monkeypatch)
    assert len(selected) == 2
    assert not lifecycle.f.lease.data
