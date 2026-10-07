"""Repeated native reset/adopt and recent-auth unlink; offline bottom wires."""

import traceback

import pytest
import sqlalchemy as sa

from test_casdoor_cross_namespace_adoption_flow_extend import create_adoption, native_source, operator, ordinary, self_status
from test_casdoor_diagnostic_flow_extend import begin as diagnostic_begin
from test_casdoor_diagnostic_flow_extend import complete as diagnostic_complete
from test_casdoor_identity_action_flow_extend import begin, complete, enable_unlink, reviewed_reauth
from test_casdoor_local_lifecycle_flow_extend import review, send
from test_casdoor_namespace_reset_flow_extend import ROOT, RESET, reset_review, rows
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


@pytest.fixture(autouse=True)
def observed_failure(monkeypatch):
    from repositories import casdoor_local_lifecycle_repository_extend as module

    original = module._fail

    def trace_failure():
        print("D31_OWNER_FAILURE", [(f.name, f.lineno) for f in traceback.extract_stack(limit=12)])
        return original()

    monkeypatch.setattr(module, "_fail", trace_failure)


def current_target(d):
    snapshot = d.services.casdoor_configuration.get(d.actor)
    with d.f.service._session_factory() as session:
        history = session.scalar(sa.select(History).where(
            History.account_id == d.actor.id,
            History.namespace_id == str(snapshot.active.namespace_id),
        ))
        assert history is not None
        return {"identity_id": history.identity_id, "workspace_id": history.workspace_id}


def release_current(d, target):
    proof = review(d, target, "release")
    response = send(d, ROOT + "/local-membership/release", method="POST", json=proof)
    assert response.status_code == 200, response.json


def repeat_reset(d):
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
    before = rows(d, (History, Identity))
    response = send(d, RESET, method="POST", json=reset_review(d))
    assert response.status_code == 200, response.json
    assert rows(d, (History, Identity)) == before
    return response


def reconnect_adopt(d):
    current = send(d, ROOT).json
    saved = send(d, ROOT, method="PUT", json={
        "etag": current["etag"], "configuration": current["draft"]["configuration"],
    })
    assert saved.status_code == 200, saved.json
    reviewed_reauth(d)
    snapshot, state = diagnostic_begin(d)
    assert diagnostic_complete(d, state).status_code == 302
    activated = send(d, ROOT + "/activate", method="POST", json={
        "etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id),
    })
    assert activated.status_code == 200, activated.json
    state, _ = begin(d)
    response = complete(d, state)
    assert response.location.endswith("=linked"), (response.location, response.json)
    with d.f.service._session_factory() as session:
        identity = session.scalar(sa.select(Identity).where(
            Identity.namespace_id == activated.json["active"]["namespace_id"],
            Identity.account_id == d.actor.id,
        ))
        assert identity is not None
        from uuid import UUID
        retained_workspaces = set(session.scalars(sa.select(History.workspace_id).where(History.account_id == d.actor.id)))
        if str(UUID(int=200)) in retained_workspaces:
            workspace_id = str(UUID(int=200))
        else:
            assert len(retained_workspaces) == 1
            workspace_id = next(iter(retained_workspaces))
        target = {"identity_id": identity.id, "workspace_id": workspace_id}
    proof = review(d, target, "adopt")
    response = send(d, ROOT + "/local-membership/adopt", method="POST", json=proof)
    assert response.status_code == 200, response.json
    return target


def test_actual_second_reset_after_adopt_and_ordinary(lifecycle, monkeypatch):
    create_adoption(lifecycle, monkeypatch)
    ordinary(lifecycle, 1)
    ordinary(lifecycle, 2)
    repeat_reset(lifecycle)


def test_second_reset_after_actual_current_role_withdraw_regrant(lifecycle, monkeypatch):
    from test_casdoor_cross_namespace_adoption_flow_extend import (
        test_current_generation_withdraw_regrant_retains_old_release,
    )

    d = lifecycle
    test_current_generation_withdraw_regrant_retains_old_release(d, monkeypatch)
    repeat_reset(d)
    old = rows(d, (History, Identity))
    reconnect_adopt(d)
    ordinary(d, 1)
    final = rows(d, (History, Identity))
    assert all(row in final[History] for row in old[History])
    assert all(row in final[Identity] for row in old[Identity])


def test_actual_third_reset_and_explicit_adopt_ordinary(lifecycle, monkeypatch):
    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    repeat_reset(d)
    reconnect_adopt(d)
    ordinary(d, 1)
    ordinary(d, 2)
    repeat_reset(d)
    old = rows(d, (History, Identity))
    reconnect_adopt(d)
    ordinary(d, 1)
    ordinary(d, 2)
    after = rows(d, (History, Identity))
    assert all(row in after[History] for row in old[History])
    assert all(row in after[Identity] for row in old[Identity])
    assert len(after[Identity]) == 4
    snapshot = d.services.casdoor_configuration.get(d.actor)
    response = self_status(d, monkeypatch)
    current = [r for r in response["memberships"] if r["namespace_id"] == str(snapshot.active.namespace_id)]
    ancestors = [r for r in response["memberships"] if r["namespace_id"] != str(snapshot.active.namespace_id)]
    assert len(current) == 1 and current[0]["state"] == "recorded_managed", current
    assert len(ancestors) == 4 and all(r["consistency"] == "historical" for r in ancestors), ancestors
    from models.account import Account, Tenant
    from services.account_service import TenantService
    from uuid import UUID
    owner_id = operator(d)
    with d.f.service._session_factory() as session:
        session.expire_on_commit = False
        TenantService.update_member_role(
            session.get(Tenant, str(UUID(int=200))), session.get(Account, d.actor.id), "editor",
            session.get(Account, owner_id), session=session,
        )
        session.flush()
    final = rows(d, (History, Identity))
    assert all(row in final[History] for row in old[History])
    assert final[Identity] == after[Identity]


def test_reset_without_intermediate_binding_then_explicit_adopt(lifecycle, monkeypatch):
    from test_casdoor_namespace_reset_flow_extend import prepare

    d = prepare(lifecycle, monkeypatch)
    d.completed = []
    original_complete = d.f.service.complete

    def observed_complete(*args, **kwargs):
        completion = original_complete(*args, **kwargs)
        d.completed.append(completion)
        return completion

    monkeypatch.setattr(d.f.service, "complete", observed_complete)
    first = send(d, RESET, method="POST", json=reset_review(d))
    assert first.status_code == 200, first.json
    old = rows(d, (History, Identity))
    # The newly created namespace remains disabled, and no identity is linked.
    # Native disable fences that exact draft namespace before the next reset.
    current = send(d, ROOT).json
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": current["etag"]})
    assert disabled.status_code == 200, disabled.json
    d.reset_input = {
        "namespace_id": current["draft"]["namespace_id"],
        "etag": disabled.json["configuration"]["etag"],
        "confirm_management_review": True,
    }
    second = send(d, RESET, method="POST", json=reset_review(d))
    assert second.status_code == 200, second.json
    assert rows(d, (History, Identity)) == old
    reconnect_adopt(d)
    ordinary(d, 1)
    after = rows(d, (History, Identity))
    assert all(row in after[History] for row in old[History])
    assert all(row in after[Identity] for row in old[Identity])
    assert len(after[Identity]) == 2


def test_current_recent_auth_unlink_after_actual_archive_release(lifecycle, monkeypatch):
    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    native_source(d)
    target = current_target(d)
    enable_unlink(d, monkeypatch)
    release_current(d, target)
    before = rows(d, (History, Identity))
    state, _ = begin(d, "reauthenticate")
    response = complete(d, state)
    assert response.location.endswith("=ready"), (response.location, response.json)
    response = send(d, "/console/api/account/casdoor-identity/unlink", method="POST", json={})
    assert response.status_code == 200, response.json
    after = rows(d, (History, Identity))
    assert after[History] == before[History]
    assert len(after[Identity]) == len(before[Identity]) - 1
    assert all(row in before[Identity] for row in after[Identity])
