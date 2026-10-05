"""D24-A2 registered Console route, synthetic cookie boundary, actual SQL owners."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from flask import Flask
from sqlalchemy.orm import scoped_session

from controllers.console import bp as console_bp
from controllers.console.workspace import rbac
from models.account import TenantAccountJoin
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorMembershipOwnership, CasdoorOperationState
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from repositories.casdoor_manual_ownership_repository_extend import (
    CasdoorManualOwnershipRepository,
)
from tests.unit_tests.services.test_casdoor_manual_member_mutation_service_extend import (
    add_intent,
    local_mode as local_mode_fixture,
    rows,
    storage as storage_fixture,
)

local_mode = local_mode_fixture
storage = storage_fixture


@pytest.fixture
def mounted(storage, monkeypatch):
    s = storage
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(console_bp)
    s.operator._current_tenant = s.workspace
    monkeypatch.setattr("libs.login.current_user", s.operator)
    monkeypatch.setattr("libs.login.check_csrf_token", lambda *args: None)
    scoped = scoped_session(lambda: s.session)
    monkeypatch.setattr(rbac, "db", SimpleNamespace(session=scoped))

    @app.teardown_appcontext
    def remove_request_session(error):
        scoped.remove()

    url = f"/console/api/workspaces/current/rbac/members/{s.account.id}/rbac-roles"
    rule = next(
        rule
        for rule in app.url_map.iter_rules()
        if rule.rule
        == "/console/api/workspaces/current/rbac/members/<uuid:member_id>/rbac-roles"
    )
    assert app.view_functions[rule.endpoint].view_class is rbac.RBACMemberRolesApi
    return SimpleNamespace(s=s, app=app, client=app.test_client(), url=url)


def test_registered_route_normal_actor_cannot_mutate_another_member(mounted):
    m = mounted
    m.s.owner_join.role = "normal"
    m.s.session.commit()
    before = rows(m.s, History), rows(m.s, TenantAccountJoin)
    response = m.client.put(m.url, json={"role_ids": ["admin"]})
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["code"] == "forbidden"
    assert before == (rows(m.s, History), rows(m.s, TenantAccountJoin))


@pytest.mark.parametrize(
    "actor_role,target_role,new_role,allowed",
    [
        ("admin", "normal", "admin", True),
        ("owner", "normal", "editor", True),
        ("admin", "normal", "owner", False),
        ("admin", "owner", "normal", False),
        ("owner", "normal", "owner", True),
        ("normal", "normal", "normal", False),
        ("admin", "normal", "normal", True),
    ],
)
def test_registered_route_exact_current_db_authorization_and_original_success_shape(
    mounted, actor_role, target_role, new_role, allowed
):
    m = mounted
    m.s.owner_join.role = actor_role
    m.s.join.role = target_role
    m.s.session.commit()
    before = rows(m.s, History), rows(m.s, TenantAccountJoin)
    response = m.client.put(m.url, json={"role_ids": [new_role]})
    assert response.status_code == (200 if allowed else 403), response.get_json()
    if not allowed:
        assert response.get_json()["code"] == "forbidden"
        assert before == (rows(m.s, History), rows(m.s, TenantAccountJoin))
    else:
        assert response.get_json()["roles"][0]["id"] == new_role
        assert response.get_json()["account_id"] == m.s.account.id
        after = rows(m.s, History)[0]
        assert after.ownership == (
            CasdoorMembershipOwnership.MANAGED
            if new_role == target_role
            else CasdoorMembershipOwnership.LOCAL_OVERRIDE
        )


def test_registered_route_self_actor_cannot_change_own_role(mounted):
    m = mounted
    before = rows(m.s, History), rows(m.s, TenantAccountJoin)
    url = m.url.replace(m.s.account.id, m.s.operator.id)
    response = m.client.put(url, json={"role_ids": ["normal"]})
    assert response.status_code == 403 and response.get_json()["code"] == "forbidden"
    assert before == (rows(m.s, History), rows(m.s, TenantAccountJoin))


def test_registered_route_retained_confirmed_completion_denies_shared_change(mounted):
    m = mounted
    add_intent(m.s, CasdoorOperationState.APPLIED)
    before = rows(m.s, History), rows(m.s, TenantAccountJoin)
    response = m.client.put(m.url, json={"role_ids": ["admin"]})
    assert response.status_code == 403 and response.get_json()["code"] == "forbidden"
    assert before == (rows(m.s, History), rows(m.s, TenantAccountJoin))


@pytest.mark.parametrize("drift", ["actor", "target"])
def test_registered_postparent_actual_sql_drift_refuses_and_request_close_rolls_back_marker(
    mounted, monkeypatch, drift
):
    m = mounted
    before = rows(m.s, History), rows(m.s, TenantAccountJoin), rows(m.s, Lifecycle)
    original = CasdoorManualOwnershipRepository.mark_local_override

    def mark(repo, token):
        result = original(repo, token)
        join_id = m.s.owner_join.id if drift == "actor" else m.s.join.id
        m.s.session.execute(
            sa.update(TenantAccountJoin)
            .where(TenantAccountJoin.id == join_id)
            .values(role="normal" if drift == "actor" else "editor")
            .execution_options(synchronize_session=False)
        )
        return result

    monkeypatch.setattr(CasdoorManualOwnershipRepository, "mark_local_override", mark)
    response = m.client.put(m.url, json={"role_ids": ["admin"]})
    assert response.status_code == 403 and response.get_json()["code"] == "forbidden"
    assert not m.s.session.in_transaction()
    assert before == (
        rows(m.s, History),
        rows(m.s, TenantAccountJoin),
        rows(m.s, Lifecycle),
    )


@pytest.mark.parametrize("failure", ["authority", "commit-before", "commit-after"])
def test_registered_sql_failure_and_commit_unknown_have_original_http_and_durable_boundaries(
    mounted, monkeypatch, failure
):
    m = mounted
    before = rows(m.s, History), rows(m.s, TenantAccountJoin), rows(m.s, Lifecycle)
    commit = m.s.session.commit
    if failure == "authority":

        def fail(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith(
                "INSERT INTO invitation_authority_lifecycle_extend"
            ):
                raise RuntimeError("actual authority SQL failure")

        sa.event.listen(m.s.engine, "before_cursor_execute", fail)
    else:

        def fail_commit():
            if failure == "commit-after":
                commit()
            raise RuntimeError("commit acknowledgement unavailable")

        monkeypatch.setattr(m.s.session, "commit", fail_commit)
    try:
        response = m.client.put(m.url, json={"role_ids": ["admin"]})
        assert response.status_code == 500, response.get_json()
    finally:
        if failure == "authority":
            sa.event.remove(m.s.engine, "before_cursor_execute", fail)
    assert not m.s.session.in_transaction()
    if failure == "commit-after":
        assert (
            rows(m.s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
        )
        assert rows(m.s, Lifecycle)[0].epoch == 1
    else:
        assert before == (
            rows(m.s, History),
            rows(m.s, TenantAccountJoin),
            rows(m.s, Lifecycle),
        )
