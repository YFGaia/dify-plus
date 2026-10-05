"""Actual unlink/LINK/ADOPT lineage; negative mutations never seed success."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.auth_transactions import AuthTransactionError
from models.account import Account, Tenant, TenantAccountJoin
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_local_lifecycle_repository_extend import (
    CasdoorLocalLifecycleRepository,
    canonical,
)
from test_casdoor_after_current_unlink_recovery_flow_extend import (
    test_disable_reset_without_relink_after_current_unlink as produce_direct_reset,
)
from test_casdoor_cross_namespace_adoption_flow_extend import (
    native_source,
    operator,
    ordinary as original_ordinary,
    self_status,
)
from test_casdoor_identity_action_flow_extend import begin, complete
from test_casdoor_local_lifecycle_flow_extend import PATH, review, send
from test_casdoor_namespace_reset_flow_extend import ROOT, rows
from test_casdoor_repeated_namespace_recovery_flow_extend import (
    observed_failure as observed_failure,
)
from test_casdoor_repeated_namespace_recovery_flow_extend import (
    reconnect_adopt,
    release_current,
)
from test_casdoor_repeated_namespace_recovery_flow_extend import (
    test_current_recent_auth_unlink_after_actual_archive_release as produce_current_unlink,
)

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


@pytest.fixture(autouse=True)
def observed_exception(monkeypatch):
    import traceback

    original = CasdoorLocalLifecycleRepository.adopt

    def trace(owner, *args, **kwargs):
        try:
            return original(owner, *args, **kwargs)
        except Exception:
            traceback.print_exc()
            raise

    monkeypatch.setattr(CasdoorLocalLifecycleRepository, "adopt", trace)


def ordinary(d, number):
    try:
        return original_ordinary(d, number)
    except AssertionError as error:
        print("NEXT_ORDINARY_FAILURE", error.args)
        raise


def relink(d, count=1):
    for _ in range(count):
        state, _ = begin(d)
        response = complete(d, state)
        assert response.location.endswith("=linked"), (response.location, response.json)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    with d.f.service._session_factory() as session:
        identity = session.scalar(sa.select(Identity).where(
            Identity.account_id == d.actor.id, Identity.namespace_id == str(snapshot.active.namespace_id),
        ))
        assert identity is not None and identity.sync_generation == 0
        return {"identity_id": identity.id, "workspace_id": str(UUID(int=200))}


def adopt(d, target):
    proof = review(d, target, "adopt")
    response = send(d, PATH + "/adopt", method="POST", json=proof)
    assert response.status_code == 200, response.json
    assert send(d, PATH + "/adopt", method="POST", json=proof).status_code == 400


@pytest.fixture
def retired(lifecycle, monkeypatch):
    produce_current_unlink(lifecycle, monkeypatch)
    lifecycle.next_before = rows(lifecycle, (History, Identity, Audit))
    lifecycle.next_target = relink(lifecycle)
    return lifecycle


@pytest.fixture
def reassigned(retired):
    adopt(retired, retired.next_target)
    return retired


def observed(d):
    target = d.next_target
    snapshot = d.services.casdoor_configuration.get(d.actor)
    with d.f.service._session_factory() as session, session.begin():
        return CasdoorLocalLifecycleRepository(session)._adopted_current_refs(
            account_id=d.actor.id, namespace_id=snapshot.active.namespace_id,
            identity_id=target["identity_id"], workspace_id=target["workspace_id"],
        )


def test_full_same_namespace_reassignment_actual_link_set_login_manual_self(retired, monkeypatch):
    d = retired
    target = relink(d, 2)
    assert target == d.next_target
    with d.f.service._session_factory() as session:
        identity = session.get(Identity, target["identity_id"])
        old = session.scalar(sa.select(History).where(History.namespace_id == identity.namespace_id))
        old_id, baseline, old_epoch = old.id, old.baseline_json, old.ownership_epoch
    adopt(d, target)
    with d.f.service._session_factory() as session:
        current = session.get(History, old_id)
        assert current.identity_id == target["identity_id"] and current.baseline_json == baseline
        assert current.ownership_epoch == old_epoch + 1 and current.desired_generation == 1
        assert session.get(Identity, target["identity_id"]).sync_generation == 1
        fact = session.scalar(sa.select(Audit).where(Audit.action == "local_membership_reassignment_v1"))
        assert len(json.loads(json.loads(fact.summary_json)["links_json"])) == 3
        assert all(row in rows(d, (Audit,))[Audit] for row in d.next_before[Audit])
    first, second = ordinary(d, 1), ordinary(d, 2)
    assert first.correlation_id != second.correlation_id
    owner_id = operator(d)
    from services.account_service import TenantService

    with d.f.service._session_factory() as session:
        session.expire_on_commit = False
        TenantService.update_member_role(session.get(Tenant, str(UUID(int=200))), session.get(Account, d.actor.id),
            "editor", session.get(Account, owner_id), session=session)
        session.flush()
    status = self_status(d, monkeypatch)
    current = [r for r in status["memberships"] if r["namespace_id"] != d.reset_input["namespace_id"]]
    assert len(current) == 1 and current[0]["state"] == "local_override", current
    assert observed(d)[0] == old_id
    assert all(row in rows(d, (Identity,))[Identity] for row in d.next_before[Identity])
    assert not d.f.lease.data


def test_unlink_no_relink_reset_actual_reconnect_adopt_two_ordinary_self(lifecycle, monkeypatch):
    d = lifecycle
    produce_direct_reset(d, monkeypatch)
    retained = rows(d, (History, Identity, Audit))
    # A deleted binding cannot supply a current test-reauth capability. Use the
    # original disabled draft/save policy until a new actual LINK exists.
    current = send(d, ROOT).json
    config = dict(current["draft"]["configuration"], self_unlink=False)
    saved = send(d, ROOT, method="PUT", json={"etag": current["etag"], "configuration": config})
    assert saved.status_code == 200, saved.json
    d.next_target = reconnect_adopt(d)
    ordinary(d, 1)
    ordinary(d, 2)
    status = self_status(d, monkeypatch)
    assert status["binding"] == "linked"
    after = rows(d, (History, Identity, Audit))
    assert all(row in after[History] for row in retained[History])
    assert all(row in after[Identity] for row in retained[Identity])
    assert all(row in after[Audit] for row in retained[Audit])
    assert not d.f.lease.data


def test_second_same_namespace_reassignment_chain(reassigned, monkeypatch):
    d = reassigned
    ordinary(d, 1)
    native_source(d)
    release_current(d, d.next_target)
    state, _ = begin(d, "reauthenticate")
    response = complete(d, state)
    assert response.location.endswith("=ready"), (response.location, response.json)
    response = send(d, "/console/api/account/casdoor-identity/unlink", method="POST", json={})
    assert response.status_code == 200, response.json
    old = d.next_target
    d.next_target = relink(d)
    assert d.next_target["identity_id"] != old["identity_id"]
    adopt(d, d.next_target)
    assert observed(d)[1]
    ordinary(d, 1)
    ordinary(d, 2)
    self_status(d, monkeypatch)
    with d.f.service._session_factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Audit).where(
            Audit.action == "local_membership_reassignment_v1")) == 2


@pytest.mark.parametrize("failure", [
    "missing_unlink", "duplicate_unlink", "malformed_unlink", "foreign_unlink", "unlink_namespace",
    "unlink_revision", "unlink_actor", "unlink_time", "release_after_unlink", "release_bytes",
    "history_bytes", "identity_reuse", "link_before_unlink", "link_foreign", "link_bytes", "link_revision",
])
def test_retired_binding_native_inspect_rejects_actual_proof_drift(retired, failure):
    d = retired
    with d.f.service._session_factory() as session, session.begin():
        old = session.scalar(sa.select(History).where(History.namespace_id == session.get(Identity, d.next_target["identity_id"]).namespace_id))
        unlink = session.scalar(sa.select(Audit).where(Audit.identity_id == old.identity_id, Audit.action == "identity_unlink"))
        release = session.scalar(sa.select(Audit).where(Audit.identity_id == old.identity_id, Audit.action == "local_membership_release_v1"))
        link = session.scalar(sa.select(Audit).where(Audit.identity_id == d.next_target["identity_id"], Audit.action == "identity_link"))
        if failure == "missing_unlink":
            session.delete(unlink)
        elif failure == "duplicate_unlink":
            session.add(Audit(**{c.name: getattr(unlink, c.name) for c in Audit.__table__.columns if c.name != "id"}))
        elif failure == "malformed_unlink":
            unlink.summary_json = "{ }"
        elif failure in ("foreign_unlink", "unlink_namespace", "unlink_revision", "unlink_actor"):
            setattr(unlink, {"foreign_unlink": "account_id", "unlink_namespace": "namespace_id",
                            "unlink_revision": "revision_id", "unlink_actor": "actor_account_id"}[failure], str(uuid4()))
        elif failure == "unlink_time":
            unlink.created_at = release.created_at - timedelta(microseconds=1)
        elif failure == "release_after_unlink":
            release.created_at = unlink.created_at + timedelta(microseconds=1)
        elif failure == "release_bytes":
            value = json.loads(release.summary_json)
            value["row_sha256"] = "0" * 64
            release.summary_json = canonical(value)
        elif failure == "history_bytes":
            old.desired_generation += 1
        elif failure == "identity_reuse":
            session.get(Identity, d.next_target["identity_id"]).id = old.identity_id
        elif failure == "link_before_unlink":
            link.created_at = unlink.created_at - timedelta(microseconds=1)
        elif failure == "link_foreign":
            link.account_id = str(uuid4())
        elif failure == "link_bytes":
            link.summary_json = "{ }"
        elif failure == "link_revision":
            link.revision_id = session.scalar(sa.select(Audit.revision_id).where(
                Audit.action == "local_namespace_reset_v1"))
    before = rows(d)
    response = send(d, PATH, query_string=d.next_target)
    assert response.status_code in (400, 409, 503), response.json
    assert rows(d) == before


@pytest.mark.parametrize("failure", [
    "missing", "duplicate", "malformed", "before_bytes", "unlink_bytes", "release_bytes", "link_growth",
    "link_bytes", "adopt_bytes", "old_adopt_borrow", "arbitrary_epoch", "initial_generation",
    "identity_created", "foreign", "baseline", "fork", "cycle",
])
def test_reassignment_initial_responsibility_never_borrows_or_ignores_drift(reassigned, failure):
    d = reassigned
    assert observed(d)[1]
    with d.f.service._session_factory() as session, session.begin():
        fact = session.scalar(sa.select(Audit).where(Audit.action == "local_membership_reassignment_v1"))
        value = json.loads(fact.summary_json)
        old = json.loads(value["before_history_json"])
        unlink = session.scalar(sa.select(Audit).where(Audit.identity_id == old["identity_id"], Audit.action == "identity_unlink"))
        release = session.scalar(sa.select(Audit).where(Audit.identity_id == old["identity_id"], Audit.action == "local_membership_release_v1"))
        link = session.scalar(sa.select(Audit).where(Audit.identity_id == d.next_target["identity_id"], Audit.action == "identity_link"))
        if failure == "missing":
            session.delete(fact)
        elif failure in ("duplicate", "fork", "cycle"):
            copied = {c.name: getattr(fact, c.name) for c in Audit.__table__.columns if c.name != "id"}
            if failure == "fork":
                value["new_identity_id"] = str(uuid4())
            if failure == "cycle":
                value["new_identity_id"] = old["identity_id"]
            copied["summary_json"] = canonical(value)
            session.add(Audit(**copied))
        elif failure == "malformed":
            fact.summary_json = "{}"
        elif failure == "unlink_bytes":
            unlink.correlation_id = str(uuid4())
        elif failure == "release_bytes":
            release.actor_account_id = str(uuid4())
        elif failure == "link_growth":
            session.add(Audit(**{c.name: getattr(link, c.name) for c in Audit.__table__.columns if c.name != "id"}))
        elif failure == "link_bytes":
            link.correlation_id = str(uuid4())
        elif failure == "adopt_bytes":
            normal = session.get(Audit, value["adopt_audit_id"])
            summary = json.loads(normal.summary_json)
            summary["target_role"] = "normal" if summary["target_role"] == "admin" else "admin"
            normal.summary_json = canonical(summary)
        elif failure == "identity_created":
            session.get(Identity, d.next_target["identity_id"]).created_at += timedelta(microseconds=1)
        elif failure == "foreign":
            fact.account_id = str(uuid4())
        else:
            if failure == "before_bytes":
                old["desired_generation"] += 1
                value["before_history_json"] = canonical(old)
            elif failure == "old_adopt_borrow":
                value["adopt_audit_id"] = session.scalar(sa.select(Audit.id).where(
                    Audit.action == "local_membership_adopt_v1", Audit.identity_id == old["identity_id"]))
            elif failure == "arbitrary_epoch":
                value["ownership_epoch"] += 3
            elif failure == "initial_generation":
                value["initial_generation"] = True
            elif failure == "baseline":
                session.get(History, value["membership_id"]).baseline_json = "{}"
            fact.summary_json = canonical(value)
    before = rows(d)
    with pytest.raises((AuthTransactionError, ValueError)):
        observed(d)
    assert rows(d) == before


@pytest.mark.parametrize("failure", ["cas_zero", "audit_insert", "generation", "source_unknown", "lease_unknown", "ack_unknown"])
def test_reassignment_original_atomic_root_and_unknown_ack(retired, monkeypatch, failure):
    from types import SimpleNamespace
    from weakref import WeakSet

    from core.casdoor.leases import CasdoorLeases
    from repositories.casdoor_generation_repository_extend import (
        CasdoorGenerationRepository,
    )
    from sqlalchemy.orm import Session

    d = retired
    proof = review(d, d.next_target, "adopt")
    before = rows(d, (History, Identity, Audit, TenantAccountJoin))
    marked = WeakSet()
    hit = []
    if failure == "cas_zero":
        original = Session.execute

        def execute(session, statement, *args, **kwargs):
            result = original(session, statement, *args, **kwargs)
            if isinstance(statement, sa.sql.dml.Update) and statement.table.name == History.__tablename__:
                hit.append("cas")
                return SimpleNamespace(rowcount=0)
            return result

        monkeypatch.setattr(Session, "execute", execute)
    elif failure == "generation":
        original = CasdoorGenerationRepository.allocate

        def allocate(owner, *args, **kwargs):
            original(owner, *args, **kwargs)
            hit.append("allocated")
            raise AuthTransactionError("context_changed")

        monkeypatch.setattr(CasdoorGenerationRepository, "allocate", allocate)
    elif failure == "source_unknown":
        original = d.services.casdoor_local_lifecycle._final_source

        def final(*args, **kwargs):
            original(*args, **kwargs)
            hit.append("source")
            raise AuthTransactionError("source_session_invalid")

        monkeypatch.setattr(d.services.casdoor_local_lifecycle, "_final_source", final)
    elif failure == "lease_unknown":
        original = CasdoorLeases.ensure_owned

        def ensure(leases):
            original(leases)
            if marked:
                hit.append("lease")
                raise AuthTransactionError("lease_unknown")

        monkeypatch.setattr(CasdoorLeases, "ensure_owned", ensure)

    def before_flush(session, context, instances):
        if any(isinstance(row, Audit) and row.action == "local_membership_reassignment_v1" for row in session.new):
            marked.add(session)
            if failure == "audit_insert":
                hit.append("audit")
                raise RuntimeError("synthetic private responsibility audit insert failure")

    def after_commit(session):
        if session in marked and failure == "ack_unknown":
            hit.append("commit")
            marked.discard(session)
            raise RuntimeError("synthetic real commit acknowledgment unknown")

    sa.event.listen(Session, "before_flush", before_flush)
    sa.event.listen(Session, "after_commit", after_commit)
    try:
        response = send(d, PATH + "/adopt", method="POST", json=proof)
    finally:
        sa.event.remove(Session, "before_flush", before_flush)
        sa.event.remove(Session, "after_commit", after_commit)
    assert hit and response.status_code != 200, (response.json, hit)
    assert send(d, PATH + "/adopt", method="POST", json=proof).status_code == 400
    after = rows(d, (History, Identity, Audit, TenantAccountJoin))
    if failure == "ack_unknown":
        with d.f.service._session_factory() as session:
            current = session.get(Identity, d.next_target["identity_id"])
            assert current.sync_generation == 1
            history = session.scalar(sa.select(History).where(History.identity_id == current.id))
            assert history.desired_generation == 1
        assert observed(d)[1]
    else:
        assert after == before
    assert not d.f.lease.data


@pytest.mark.parametrize("failure", ["actual_link_growth", "history_bytes", "parent_bytes", "directory_changed"])
def test_reassignment_review_fresh_scope_rejects_changed_actual_facts(retired, monkeypatch, failure):
    d = retired
    proof = review(d, d.next_target, "adopt")
    if failure == "actual_link_growth":
        assert relink(d) == d.next_target
    elif failure in ("history_bytes", "parent_bytes"):
        with d.f.service._session_factory() as session, session.begin():
            current = session.get(Identity, d.next_target["identity_id"])
            if failure == "history_bytes":
                old = session.scalar(sa.select(History).where(History.namespace_id == current.namespace_id))
                old.updated_at += timedelta(microseconds=1)
            else:
                from models.casdoor_extend import CasdoorNamespaceExtend as Namespace

                session.get(Namespace, current.namespace_id).created_at += timedelta(microseconds=1)
    else:
        from urllib.parse import urlsplit

        from core.helper import ssrf_proxy
        from test_gateway import response as wire_response

        original = ssrf_proxy.make_request_with_deadline

        def transport(method, url, **kwargs):
            result = original(method, url, **kwargs)
            if urlsplit(url).path == "/api/get-roles":
                value = result.json()
                value["data"][0]["users"] = []
                return wire_response(value)
            return result

        monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    before = rows(d)
    response = send(d, PATH + "/adopt", method="POST", json=proof)
    assert response.status_code != 200, response.json
    assert rows(d) == before
