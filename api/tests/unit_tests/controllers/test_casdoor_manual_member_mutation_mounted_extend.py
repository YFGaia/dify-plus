"""D24-A1 real HTTP Resource dispatch and actual writer SQL, synthetic auth boundary.

Console cookie identity and OpenAPI bearer resolution are fixtures. Original
decorators, current membership authorization, controller parsing, shared writer,
ownership CAS, lifecycle changes and transaction owners remain actual code.
"""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from flask import Flask
from flask_restx import Api
from sqlalchemy.orm import scoped_session

from controllers.console.workspace import members
from controllers.openapi import bp as openapi_bp
from core.db.session_factory import session_factory
from libs.oauth_bearer import AuthContext, Scope, SubjectType, TokenType
from models.account import TenantAccountJoin
from models.casdoor_extend import CasdoorMembershipOwnership, CasdoorOperationState
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from services.account_service import AccountService
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
    app.login_manager = SimpleNamespace(
        _update_request_context_with_user=lambda user: None
    )
    api = Api(app)
    api.add_resource(
        members.MemberUpdateRoleApi,
        "/console/api/workspaces/current/members/<uuid:member_id>/update-role",
    )
    api.add_resource(
        members.MemberCancelInviteApi,
        "/console/api/workspaces/current/members/<uuid:member_id>",
    )
    api.add_resource(
        members.OwnerTransfer,
        "/console/api/workspaces/current/members/<uuid:member_id>/owner-transfer",
    )
    app.register_blueprint(openapi_bp)
    s.operator._current_tenant = s.workspace
    monkeypatch.setattr("libs.login.current_user", s.operator)
    monkeypatch.setattr("libs.login.check_csrf_token", lambda *args: None)
    monkeypatch.setattr("controllers.console.wraps._is_setup_completed", lambda: True)
    scoped = scoped_session(lambda: s.session)
    monkeypatch.setattr(members, "db", SimpleNamespace(session=scoped))

    @app.teardown_appcontext
    def remove_request_session(error):
        scoped.remove()

    monkeypatch.setattr(
        session_factory, "create_session", lambda: nullcontext(s.session)
    )
    identity = AuthContext(
        subject_type=SubjectType.ACCOUNT,
        subject_email=None,
        subject_issuer=None,
        account_id=UUID(s.operator.id),
        client_id=None,
        scopes=frozenset({Scope.WORKSPACE_WRITE}),
        token_id=uuid4(),
        token_type=TokenType.OAUTH_ACCOUNT,
        expires_at=None,
        token_hash="a" * 64,
    )
    monkeypatch.setattr(
        "controllers.openapi.auth.pipeline.get_authenticator",
        lambda: SimpleNamespace(authenticate=lambda token: identity),
    )
    return SimpleNamespace(s=s, app=app, client=app.test_client())


def request(m, surface, action, *, role="admin"):
    s = m.s
    if surface == "console":
        url = f"/console/api/workspaces/current/members/{s.account.id}"
        return (
            m.client.put(url + "/update-role", json={"role": role})
            if action == "role"
            else m.client.delete(url)
        )
    url = f"/openapi/v1/workspaces/{s.workspace.id}/members/{s.account.id}"
    headers = {"Authorization": "Bearer synthetic-d24-account"}
    return (
        m.client.patch(url, headers=headers, json={"role": role})
        if action == "role"
        else m.client.delete(url, headers=headers)
    )


@pytest.mark.parametrize("surface", ["console", "openapi"])
@pytest.mark.parametrize("action", ["role", "remove"])
def test_mounted_actual_mutation_commits_metadata_and_original_effects(
    mounted, surface, action
):
    m = mounted
    response = request(m, surface, action)
    assert response.status_code == 200, response.get_json()
    after = rows(m.s, History)[0]
    assert (
        after.ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
        and after.ownership_epoch == 4
    )
    assert after.tombstone == (action == "remove")
    assert rows(m.s, Lifecycle)[0].state == (
        "withdrawn" if action == "remove" else "active"
    )
    assert (
        m.s.session.get(TenantAccountJoin, m.s.join.id) is None
        if action == "remove"
        else m.s.join.role == "admin"
    )


@pytest.mark.parametrize("surface", ["console", "openapi"])
@pytest.mark.parametrize("action", ["role", "remove"])
def test_mounted_confirmed_completed_intent_is_not_manual_authority(
    mounted, surface, action
):
    m = mounted
    add_intent(m.s, CasdoorOperationState.APPLIED)
    before = rows(m.s, History), rows(m.s, TenantAccountJoin)
    response = request(m, surface, action)
    assert response.status_code == (
        403 if surface == "console" else 400
    ), response.get_json()
    assert "authorization pending" in str(response.get_json())
    assert before == (rows(m.s, History), rows(m.s, TenantAccountJoin))


@pytest.mark.parametrize("surface", ["console", "openapi"])
def test_mounted_original_non_admin_policy_blocks_before_metadata(mounted, surface):
    m = mounted
    m.s.owner_join.role = "normal"
    m.s.session.commit()
    before = rows(m.s, History), rows(m.s, TenantAccountJoin)
    response = request(m, surface, "role")
    assert response.status_code == 403, response.get_json()
    assert before == (rows(m.s, History), rows(m.s, TenantAccountJoin))


@pytest.mark.parametrize("deny", [False, True])
def test_actual_console_transfer_consumes_original_token_before_sql_boundary(
    mounted, monkeypatch, deny
):
    m = mounted
    consumed = []
    monkeypatch.setattr(
        "controllers.console.wraps.application_services",
        lambda: SimpleNamespace(
            feature_queries=SimpleNamespace(
                get_workspace_features=lambda workspace: SimpleNamespace(
                    is_allow_transfer_workspace=True
                )
            )
        ),
    )
    monkeypatch.setattr(
        "libs.workspace_permission.check_workspace_owner_transfer_permission",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        AccountService,
        "get_owner_transfer_data",
        lambda token: {"email": m.s.operator.email},
    )
    monkeypatch.setattr(
        AccountService,
        "revoke_owner_transfer_token",
        lambda token: consumed.append(token),
    )
    monkeypatch.setattr(
        AccountService, "send_new_owner_transfer_notify_email", lambda **kwargs: None
    )
    monkeypatch.setattr(
        AccountService, "send_old_owner_transfer_notify_email", lambda **kwargs: None
    )
    before = rows(m.s, History), rows(m.s, TenantAccountJoin)
    if deny:
        add_intent(m.s, CasdoorOperationState.APPLIED)
        with pytest.raises(ValueError, match="authorization pending"):
            m.client.post(
                f"/console/api/workspaces/current/members/{m.s.account.id}/owner-transfer",
                json={"token": "synthetic"},
            )
        assert before == (rows(m.s, History), rows(m.s, TenantAccountJoin))
    else:
        response = m.client.post(
            f"/console/api/workspaces/current/members/{m.s.account.id}/owner-transfer",
            json={"token": "synthetic"},
        )
        assert response.status_code == 200, response.get_json()
        assert m.s.owner_join.role == "normal" and m.s.join.role == "owner"
        assert (
            rows(m.s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
        )
    assert consumed == ["synthetic"]


@pytest.mark.parametrize("surface", ["console", "openapi"])
@pytest.mark.parametrize("action", ["role", "remove"])
def test_mounted_actual_writer_failure_uses_request_rollback_owner(
    mounted, surface, action
):
    m = mounted
    before = {
        model: rows(m.s, model) for model in (History, TenantAccountJoin, Lifecycle)
    }

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO invitation_authority_lifecycle_extend"):
            raise RuntimeError("actual authority SQL failure")

    sa.event.listen(m.s.engine, "before_cursor_execute", fail)
    try:
        if surface == "console":
            with pytest.raises(ValueError, match="actual authority SQL failure"):
                request(m, surface, action)
        else:
            response = request(m, surface, action)
            assert response.status_code == 500
    finally:
        sa.event.remove(m.s.engine, "before_cursor_execute", fail)
    assert not m.s.session.in_transaction()
    assert before == {model: rows(m.s, model) for model in before}


@pytest.mark.parametrize("surface", ["console", "openapi"])
def test_mounted_committed_unknown_receipt_does_not_undo_durable_effect(
    mounted, monkeypatch, surface
):
    m = mounted
    commit = m.s.session.commit

    def unknown():
        commit()
        raise RuntimeError("commit acknowledgement unknown")

    monkeypatch.setattr(m.s.session, "commit", unknown)
    if surface == "console":
        with pytest.raises(ValueError, match="acknowledgement unknown"):
            request(m, surface, "role")
    else:
        response = request(m, surface, "role")
        assert response.status_code == 500
    assert rows(m.s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
    assert rows(m.s, Lifecycle)[0].epoch == 1
