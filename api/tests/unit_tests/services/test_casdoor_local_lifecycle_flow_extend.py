"""Mounted LOCAL maintenance with actual SQL/source owners and offline bottom wires."""

import time
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json, roles_fingerprint
from models.account import TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorManagedMembershipExtend,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
)
from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository
from services.casdoor_local_lifecycle_service_extend import _CONSUME_REVIEW
from test_casdoor_diagnostic_flow_extend import diagnostic as diagnostic
from test_casdoor_identity_action_flow_extend import actions as actions
from test_casdoor_identity_action_flow_extend import begin, complete, enable_unlink
from test_casdoor_identity_action_flow_extend import send as action_send

pytest_plugins = ("test_casdoor_production_login_policy_extend",)
PATH = "/console/api/system-manage-extend/integration/casdoor/local-membership"


def send(d, path, **kwargs):
    return action_send(d, path, headers={"X-CSRF-Token": d.csrf}, **kwargs)


@pytest.fixture
def lifecycle(actions, monkeypatch):
    d = actions
    service = d.services.casdoor_local_lifecycle
    service._redis_runtime_factory = d.f.service._redis_runtime_factory
    records = {}

    def set_review(name, value, *, ex, nx):
        assert not d.f.opened and nx and ex == 60
        if name in records:
            return False
        records[name] = (value, time.time() + ex)
        return True

    original = d.f.redis.eval

    def eval_review(script, numkeys, *args):
        if script == _CONSUME_REVIEW:
            assert numkeys == 1 and not d.f.opened
            value = records.pop(args[0], (None, 0))
            return value[0] if value[1] > time.time() else None
        return original(script, numkeys, *args)

    monkeypatch.setattr(d.f.redis, "set", set_review, raising=False)
    monkeypatch.setattr(d.f.redis, "eval", eval_review)
    d.review_records = records
    return d


def managed(d):
    state, _ = begin(d)
    assert complete(d, state).status_code == 302
    with d.f.service._session_factory() as session, session.begin():
        identity = session.scalar(
            sa.select(CasdoorIdentityExtend).where(CasdoorIdentityExtend.account_id == d.actor.id)
        )
        join = session.scalar(sa.select(TenantAccountJoin).where(TenantAccountJoin.account_id == d.actor.id))
        snapshot = d.services.casdoor_configuration._repository(session)
        revision = snapshot._revision(snapshot._integration().id, snapshot._integration().active_revision_id)
        view = MembershipObservation(
            UUID(join.tenant_id), UUID(d.actor.id), UUID(join.id), join.role, MembershipBackend.LOCAL
        )
        history = CasdoorManagedMembershipExtend(
            namespace_id=identity.namespace_id,
            identity_id=identity.id,
            account_id=d.actor.id,
            workspace_id=join.tenant_id,
            join_id=join.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            source=CasdoorMembershipSource.MAPPING,
            desired_generation=identity.sync_generation,
            revision_id=revision.id,
            last_applied_roles_json=role_baseline_json(view),
            last_applied_fingerprint=roles_fingerprint(view),
            baseline_json=role_baseline_json(view),
            desired_roles_json="{}",
            finalization=CasdoorFinalizationState.FINALIZED,
        )
        session.add(history)
        target = {"identity_id": identity.id, "workspace_id": join.tenant_id}
    return target


def review(d, target, operation):
    inspected = send(d, PATH, query_string=target)
    assert inspected.status_code == 200, inspected.json
    response = send(
        d, PATH + "/review", method="POST", json=target | {"operation": operation, "etag": inspected.json["etag"]}
    )
    assert response.status_code == 200, response.json
    return {"review_id": response.json["review_id"], "etag": response.json["etag"]}


def test_mounted_manager_release_then_adopt_real_readback_and_once_review(lifecycle):
    d = lifecycle
    target = managed(d)
    proof = review(d, target, "release")
    released = send(d, PATH + "/release", method="POST", json=proof)
    assert released.status_code == 200 and released.json["status"] == "released", released.json
    assert send(d, PATH + "/release", method="POST", json=proof).status_code == 400
    observed = send(d, PATH, query_string=target)
    assert observed.json["ownership"] == "released" and observed.json["current_role"] == "normal"
    proof = review(d, target, "adopt")
    adopted = send(d, PATH + "/adopt", method="POST", json=proof)
    assert adopted.status_code == 200 and adopted.json["status"] == "adopted", adopted.json
    assert send(d, PATH, query_string=target).json["ownership"] == "managed"
    assert not d.f.control.tokens


def test_released_actual_receipt_allows_recent_auth_unlink_without_deleting_member(lifecycle, monkeypatch):
    d = lifecycle
    enable_unlink(d, monkeypatch)
    target = managed(d)
    proof = review(d, target, "release")
    assert send(d, PATH + "/release", method="POST", json=proof).status_code == 200
    state, _ = begin(d, "reauthenticate")
    assert complete(d, state).status_code == 302
    result = send(d, "/console/api/account/casdoor-identity/unlink", method="POST", json={})
    assert result.status_code == 200, result.json
    with d.f.service._session_factory() as session:
        assert (
            session.scalar(sa.select(CasdoorIdentityExtend.id).where(CasdoorIdentityExtend.account_id == d.actor.id))
            is None
        )
        assert (
            session.scalar(sa.select(TenantAccountJoin.role).where(TenantAccountJoin.account_id == d.actor.id))
            is TenantAccountRole.NORMAL
        )
        assert (
            session.scalar(sa.select(CasdoorManagedMembershipExtend.ownership)) is CasdoorMembershipOwnership.RELEASED
        )
    assert not d.f.control.tokens


def test_mounted_review_rejects_client_role_flags_and_revoked_original_refresh(lifecycle):
    d = lifecycle
    target = managed(d)
    extra = target | {"operation": "release", "etag": 0, "quiescent": True, "target_role": "admin"}
    assert send(d, PATH + "/review", method="POST", json=extra).status_code == 400
    proof = review(d, target, "release")
    d.source.clear()
    assert send(d, PATH + "/release", method="POST", json=proof).status_code == 400
    with d.f.service._session_factory() as session:
        assert session.scalar(sa.select(CasdoorManagedMembershipExtend.ownership)) is CasdoorMembershipOwnership.MANAGED


@pytest.mark.parametrize("failure", ["revoked", "get_failure", "deadline", "allowlist"])
def test_release_last_write_window_source_failure_rolls_back_actual_receipt(lifecycle, monkeypatch, failure):
    from dataclasses import replace

    d = lifecycle
    proof = review(d, managed(d), "release")
    original = CasdoorLocalLifecycleRepository.release

    def release(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        if failure == "revoked":
            d.source.clear()
        elif failure == "get_failure":

            def unavailable(*args):
                raise TimeoutError("synthetic bounded original refresh read failure")

            monkeypatch.setattr(d.f.redis, "get", unavailable)
        elif failure == "allowlist":
            configuration = d.services.casdoor_configuration
            monkeypatch.setattr(
                configuration, "_management_policy", replace(configuration._management_policy, account_ids=frozenset())
            )
        else:
            clock = time.monotonic
            monkeypatch.setattr("services.casdoor_local_lifecycle_service_extend.time.monotonic", lambda: clock() + 50)
        return result

    monkeypatch.setattr(CasdoorLocalLifecycleRepository, "release", release)
    response = send(d, PATH + "/release", method="POST", json=proof)
    expected = 503 if failure in ("get_failure", "allowlist") else 400
    assert response.status_code == expected, response.json
    with d.f.service._session_factory() as session:
        assert session.scalar(sa.select(CasdoorManagedMembershipExtend.ownership)) is CasdoorMembershipOwnership.MANAGED
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(CasdoorAuditExtend)
                .where(CasdoorAuditExtend.action == "local_membership_release_v1")
            )
            == 0
        )
    assert not d.f.control.tokens


def test_actual_target_navigation_has_original_source_csrf_closed_cursor_and_no_store(lifecycle):
    d = lifecycle
    target = managed(d)
    response = send(d, PATH + "/targets", query_string={"limit": "1"})
    assert response.status_code == 200, response.json
    assert response.json["items"][0]["identity_id"] == target["identity_id"]
    assert response.headers["Cache-Control"] == "no-store"
    for query in ({"limit": "true"}, {"after_identity_id": target["identity_id"]}, {"quiescent": "true"}):
        assert send(d, PATH + "/targets", query_string=query).status_code == 400
    # The inherited source-action formatter closes original LoginManager CSRF
    # failures as a safe unavailable response, with no manager data or writes.
    assert action_send(d, PATH + "/targets", headers={}).status_code == 503


@pytest.mark.parametrize("failure", ["expired", "role_changed"])
def test_review_expiry_and_changed_actual_join_deny_mutation(lifecycle, failure):
    d = lifecycle
    proof = review(d, managed(d), "release")
    if failure == "expired":
        name = next(iter(d.review_records))
        raw, _ = d.review_records[name]
        d.review_records[name] = (raw, 0)
    else:
        with d.f.service._session_factory() as session, session.begin():
            session.execute(
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.account_id == d.actor.id)
                .values(role=TenantAccountRole.EDITOR)
            )
    assert send(d, PATH + "/release", method="POST", json=proof).status_code == 400
    with d.f.service._session_factory() as session:
        assert session.scalar(sa.select(CasdoorManagedMembershipExtend.ownership)) is CasdoorMembershipOwnership.MANAGED


def test_target_directory_lost_lease_stops_before_next_network_read(lifecycle):
    d = lifecycle
    target = managed(d)
    proof = review(d, target, "release")
    assert send(d, PATH + "/release", method="POST", json=proof).status_code == 200
    inspected = send(d, PATH, query_string=target).json
    before = len(d.f.control.requests)

    def lose(path):
        if path == "/api/get-organization":
            d.f.lease.data.clear()

    d.f.control.hook = lose
    response = send(d, PATH + "/review", method="POST", json=target | {"operation": "adopt", "etag": inspected["etag"]})
    assert response.status_code == 503, response.json
    assert [path for path, _ in d.f.control.requests[before:]] == ["/api/get-organization"]
    assert not d.review_records


def test_target_directory_stable_id_mismatch_cannot_create_review(lifecycle, monkeypatch):
    from urllib.parse import urlsplit

    from core.helper import ssrf_proxy

    d = lifecycle
    target = managed(d)
    proof = review(d, target, "release")
    assert send(d, PATH + "/release", method="POST", json=proof).status_code == 200
    inspected = send(d, PATH, query_string=target).json
    original = ssrf_proxy.make_request_with_deadline

    def transport(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if urlsplit(url).path == "/api/get-user":
            import json

            body = response.json()
            body["data"]["id"] = "a-different-stable-id"
            response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    response = send(d, PATH + "/review", method="POST", json=target | {"operation": "adopt", "etag": inspected["etag"]})
    assert response.status_code == 503, response.json
    assert not d.review_records
