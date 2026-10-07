"""Real LOCAL reset/link/adoption owners; synthetic offline provider/session wires."""

from urllib.parse import parse_qs, urlsplit
from datetime import UTC, datetime
from uuid import UUID

from configs import dify_config
from models.account import Account, TenantAccountJoin, TenantAccountRole
from services.account_password_hasher import DefaultAccountPasswordHasher
from test_casdoor_local_lifecycle_flow_extend import managed
from test_casdoor_local_http_extend import begin as ordinary_begin
from libs.token import _real_cookie_name

import pytest
import sqlalchemy as sa
from test_casdoor_diagnostic_flow_extend import begin as diagnostic_begin
from test_casdoor_diagnostic_flow_extend import complete as diagnostic_complete
from test_casdoor_identity_action_flow_extend import begin, complete, reviewed_reauth
from test_casdoor_local_lifecycle_flow_extend import review, send
from test_casdoor_namespace_reset_flow_extend import ROOT, RESET, reset_review, rows

from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorMembershipOwnership

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


def reset_link(d, monkeypatch):
    credential = DefaultAccountPasswordHasher().hash("SyntheticPassword12")
    with d.f.service._session_factory() as session, session.begin():
        actor = session.get(Account, d.actor.id)
        actor.password, actor.password_salt = credential.password_hash, credential.password_salt
        actor.initialized_at = datetime.now(UTC).replace(tzinfo=None)
    monkeypatch.setattr(dify_config, "ENABLE_EMAIL_PASSWORD_LOGIN", True)
    first = managed(d)
    proof = review(d, first, "release")
    assert send(d, ROOT + "/local-membership/release", method="POST", json=proof).status_code == 200
    proof = review(d, first, "adopt")
    assert send(d, ROOT + "/local-membership/adopt", method="POST", json=proof).status_code == 200
    with d.f.service._session_factory() as session, session.begin():
        session.add(
            TenantAccountJoin(tenant_id=str(UUID(int=200)), account_id=d.actor.id, role=TenantAccountRole.NORMAL)
        )
    mapped = first | {"workspace_id": str(UUID(int=200))}
    proof = review(d, mapped, "adopt")
    assert send(d, ROOT + "/local-membership/adopt", method="POST", json=proof).status_code == 200
    snapshot = d.services.casdoor_configuration.get(d.actor)
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": snapshot.etag})
    assert disabled.status_code == 200, disabled.json
    for target in (first, mapped):
        proof = review(d, target, "release")
        assert send(d, ROOT + "/local-membership/release", method="POST", json=proof).status_code == 200
    d.reset_input = {
        "namespace_id": str(snapshot.active.namespace_id),
        "etag": disabled.json["configuration"]["etag"],
        "confirm_management_review": True,
    }
    d.old_rows = rows(d, (History, Identity))
    response = send(d, RESET, method="POST", json=reset_review(d))
    assert response.status_code == 200, response.json
    current = send(d, ROOT).json
    saved = send(
        d, ROOT, method="PUT", json={"etag": current["etag"], "configuration": current["draft"]["configuration"]}
    )
    assert saved.status_code == 200, saved.json
    # Fresh synthetic signed deployment manifest, consumed by the original policy owner.
    reviewed_reauth(d)
    snapshot, state = diagnostic_begin(d)
    assert diagnostic_complete(d, state).status_code == 302
    activated = send(
        d,
        ROOT + "/activate",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )
    assert activated.status_code == 200, activated.json
    state, _ = begin(d)
    linked = complete(d, state)
    assert linked.status_code == 302 and linked.location.endswith("=linked"), linked.json
    with d.f.service._session_factory() as session:
        identity = session.scalar(
            sa.select(Identity).where(
                Identity.namespace_id == activated.json["active"]["namespace_id"], Identity.account_id == d.actor.id
            )
        )
    assert identity is not None
    return {"identity_id": identity.id, "workspace_id": str(UUID(int=200))}


def create_adoption(lifecycle, monkeypatch):
    d = lifecycle
    import traceback
    from services.casdoor_local_http_service_extend import CasdoorLocalHttpService

    original_error = CasdoorLocalHttpService._public_error
    d.errors = []

    def observed_error(error, *args):
        d.errors.append(
            [(frame.name, frame.lineno) for frame in traceback.extract_tb(getattr(error, "__traceback__", None))]
        )
        return original_error(error, *args)

    monkeypatch.setattr(CasdoorLocalHttpService, "_public_error", staticmethod(observed_error))
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
    from services.casdoor_local_login_coordinator_service_extend import CasdoorLocalLoginCoordinatorService

    original_coordinate = CasdoorLocalLoginCoordinatorService._coordinate_local_login

    def observed_coordinate(owner, *args, **kwargs):
        try:
            return original_coordinate(owner, *args, **kwargs)
        except Exception as error:
            d.errors.append(
                [(frame.name, frame.lineno) for frame in traceback.extract_tb(getattr(error, "__traceback__", None))]
            )
            raise

    monkeypatch.setattr(CasdoorLocalLoginCoordinatorService, "_coordinate_local_login", observed_coordinate)
    original_admission = CasdoorLocalLoginCoordinatorService._admission

    def observed_admission(owner, *args, **kwargs):
        try:
            return original_admission(owner, *args, **kwargs)
        except Exception as error:
            d.errors.append(
                [(frame.name, frame.lineno) for frame in traceback.extract_tb(getattr(error, "__traceback__", None))]
            )
            raise

    monkeypatch.setattr(CasdoorLocalLoginCoordinatorService, "_admission", observed_admission)
    original_project = CasdoorLoginScopeRepository._project_scope

    def observed_project(owner, *args, **kwargs):
        try:
            return original_project(owner, *args, **kwargs)
        except Exception as error:
            d.errors.append(
                [(frame.name, frame.lineno) for frame in traceback.extract_tb(getattr(error, "__traceback__", None))]
            )
            raise

    monkeypatch.setattr(CasdoorLoginScopeRepository, "_project_scope", observed_project)
    d.completed = []
    original_complete = d.f.service.complete

    def observed_complete(*args, **kwargs):
        result = original_complete(*args, **kwargs)
        d.completed.append(result)
        return result

    monkeypatch.setattr(d.f.service, "complete", observed_complete)
    target = reset_link(d, monkeypatch)
    from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository
    from uuid import UUID

    with d.f.service._session_factory() as session, session.begin():
        CasdoorLocalLifecycleRepository(session).inspect(
            UUID(target["identity_id"]), UUID(target["workspace_id"]), source_account_id=UUID(d.actor.id)
        )
    proof = review(d, target, "adopt")
    adopted = send(d, ROOT + "/local-membership/adopt", method="POST", json=proof)
    assert adopted.status_code == 200, adopted.json
    after = rows(d, (History, Identity))
    assert set(d.old_rows[History]) < set(after[History])
    assert set(d.old_rows[Identity]) < set(after[Identity])
    with d.f.service._session_factory() as session:
        old = session.scalar(
            sa.select(History).where(
                History.namespace_id == d.reset_input["namespace_id"], History.workspace_id == target["workspace_id"]
            )
        )
        current = session.scalar(sa.select(History).where(History.id == adopted.json["membership_id"]))
        assert old.ownership is CasdoorMembershipOwnership.RELEASED
        assert current.namespace_id != old.namespace_id and current.baseline_json


def ordinary(d, number):
    state = ordinary_begin(d, return_path="/apps/cross-namespace")
    result = complete(d, state)
    detail = None
    if result.location and "?handoff=" in result.location:
        handle = parse_qs(urlsplit(result.location).query)["handoff"][0]
        detail = send(d, "/console/api/auth/casdoor/result", query_string={"handoff": handle}).json
    assert result.status_code == 302 and result.location == d.f.settings.CONSOLE_WEB_URL + "/apps/cross-namespace", (
        result.json,
        result.location,
        detail,
        getattr(d, "errors", ()),
    )
    assert len(result.headers.getlist("Set-Cookie")) == 4
    assert all(
        d.client.get_cookie(_real_cookie_name(name), domain="console.example.test")
        for name in ("access_token", "refresh_token", "csrf_token")
    )
    completion = d.completed[-1]
    assert completion.phases.local_outcome == completion.phases.finalization_outcome == "committed"
    assert completion.phases.token_outcome == "issued" and completion.phases.cleanup_released is True
    with d.f.service._session_factory() as session:
        current = session.scalar(
            sa.select(Identity).where(
                Identity.namespace_id == str(completion.provenance.namespace_id),
                Identity.account_id == str(completion.provenance.account_id),
            )
        )
        assert current.namespace_id != d.reset_input["namespace_id"]
        assert current.sync_generation == number + 1
    return completion


def test_two_ordinary_after_actual_cross_namespace_adoption(lifecycle, monkeypatch):
    create_adoption(lifecycle, monkeypatch)
    first = ordinary(lifecycle, 1)
    second = ordinary(lifecycle, 2)
    assert first.correlation_id != second.correlation_id
    assert not lifecycle.f.lease.data
    after = rows(lifecycle, (History, Identity))
    assert all(row in after[History] for row in lifecycle.old_rows[History])
    assert all(row in after[Identity] for row in lifecycle.old_rows[Identity])


def operator(d):
    from models.account import AccountStatus
    from models.model import App
    from models.dataset import Dataset
    from models.agent import Agent

    for model in (App, Dataset, Agent):
        model.__table__.create(d.f.local.engine, checkfirst=True)
    with d.f.service._session_factory() as session, session.begin():
        owner = Account(
            name="Synthetic workspace owner",
            email="owner-cross@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(owner)
        session.flush()
        session.add(TenantAccountJoin(tenant_id=str(UUID(int=200)), account_id=owner.id, role=TenantAccountRole.OWNER))
        owner_id = owner.id
    return owner_id


@pytest.mark.parametrize("mutation", ["tenant_role", "tenant_remove", "rbac_local"])
def test_actual_current_manual_writers_preserve_all_archived_rows(lifecycle, monkeypatch, mutation):
    from models.account import Tenant
    from services.account_service import TenantService
    from services.enterprise.rbac_service import RBACService

    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    owner_id = operator(d)
    with d.f.service._session_factory() as session:
        session.expire_on_commit = False
        tenant = session.get(Tenant, str(UUID(int=200)))
        actor = session.get(Account, d.actor.id)
        owner = session.get(Account, owner_id)
        if mutation == "tenant_role":
            TenantService.update_member_role(tenant, actor, "editor", owner, session=session)
        elif mutation == "tenant_remove":
            TenantService.remove_member_from_tenant(tenant, actor, owner, session=session)
        else:
            result = RBACService.MemberRoles.replace(tenant.id, owner.id, actor.id, ["editor"], session=session)
            assert result
        session.flush()
    after = rows(d, (History, Identity))
    assert all(row in after[History] for row in d.old_rows[History])
    assert all(row in after[Identity] for row in d.old_rows[Identity])
    with d.f.service._session_factory() as session:
        current = session.scalar(
            sa.select(History).where(
                History.namespace_id != d.reset_input["namespace_id"], History.workspace_id == str(UUID(int=200))
            )
        )
        assert current.ownership.value == "local_override"
        assert current.tombstone is (mutation == "tenant_remove")
        joined = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == d.actor.id, TenantAccountJoin.tenant_id == str(UUID(int=200))
            )
        )
        assert joined is None if mutation == "tenant_remove" else joined.role is TenantAccountRole.EDITOR
        old = session.scalar(
            sa.select(History).where(
                History.namespace_id == d.reset_input["namespace_id"], History.workspace_id == str(UUID(int=200))
            )
        )
        assert old.ownership is CasdoorMembershipOwnership.RELEASED


def test_reconnected_disable_then_actual_review_release(lifecycle, monkeypatch):
    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    # The original signed session gateway is the producer of these modeled records.
    for key, _ttl, value in d.f.control.tokens:
        if key.startswith("refresh_token:"):
            d.source[key] = value.encode() if isinstance(value, str) else value
    d.csrf = d.client.get_cookie(_real_cookie_name("csrf_token"), domain="console.example.test").value
    snapshot = send(d, ROOT).json
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": snapshot["etag"]})
    assert disabled.status_code == 200, disabled.json
    with d.f.service._session_factory() as session:
        identity = session.scalar(
            sa.select(Identity).where(
                Identity.namespace_id == snapshot["active"]["namespace_id"], Identity.account_id == d.actor.id
            )
        )
    proof = review(d, {"identity_id": identity.id, "workspace_id": str(UUID(int=200))}, "release")
    released = send(d, ROOT + "/local-membership/release", method="POST", json=proof)
    assert released.status_code == 200, released.json
    assert released.json["status"] == "released"
    assert all(row in rows(d, (History,))[History] for row in d.old_rows[History])


def native_source(d):
    for key, _ttl, value in d.f.control.tokens:
        if key.startswith("refresh_token:"):
            d.source[key] = value.encode() if isinstance(value, str) else value
    d.csrf = d.client.get_cookie(_real_cookie_name("csrf_token"), domain="console.example.test").value


def test_current_generation_withdraw_regrant_retains_old_release(lifecycle, monkeypatch):
    from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping
    from core.helper import ssrf_proxy
    from test_gateway import response as wire_response

    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    ordinary(d, 2)
    native_source(d)
    with d.f.service._session_factory() as session:
        current = session.scalar(sa.select(History).where(History.namespace_id != d.reset_input["namespace_id"]))
        initial_id, initial_join, initial_baseline = current.id, current.join_id, current.baseline_json
    snapshot = d.services.casdoor_configuration.get(d.actor)
    config = snapshot.draft.configuration.model_copy(
        update={
            "workspace_mappings": (
                WorkspaceRoleMapping(workspace_id=UUID(int=200), editor=RoleRef(organization="Org", name="operators")),
            )
        }
    )
    saved = send(d, ROOT, method="PUT", json={"etag": snapshot.etag, "configuration": config.model_dump(mode="json")})
    assert saved.status_code == 200, saved.json
    reviewed_reauth(d)
    snapshot, state = diagnostic_begin(d)
    assert diagnostic_complete(d, state).status_code == 302
    assert (
        send(
            d,
            ROOT + "/activate",
            method="POST",
            json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
        ).status_code
        == 200
    )
    ordinary(d, 3)
    operator(d)
    hit = [False]
    original_transport = ssrf_proxy.make_request_with_deadline

    def transport(method, url, **kwargs):
        result = original_transport(method, url, **kwargs)
        if urlsplit(url).path == "/api/get-roles":
            data = result.json()
            data["data"][0]["users"] = ["Org/person"] if hit[0] else []
            return wire_response(data)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    ordinary(d, 4)
    with d.f.service._session_factory() as session:
        assert session.get(TenantAccountJoin, initial_join) is None
        withdrawn = tuple(session.execute(sa.select(*History.__table__.columns).where(History.id == initial_id)).one())
    ordinary(d, 5)
    with d.f.service._session_factory() as session:
        assert (
            tuple(session.execute(sa.select(*History.__table__.columns).where(History.id == initial_id)).one())
            == withdrawn
        )
    hit[0] = True
    ordinary(d, 6)
    with d.f.service._session_factory() as session:
        current = session.get(History, initial_id)
        assert current.join_id != initial_join and current.baseline_json == initial_baseline
        assert current.ownership_epoch == 3
        assert session.get(TenantAccountJoin, current.join_id).role is TenantAccountRole.EDITOR
    assert all(row in rows(d, (History,))[History] for row in d.old_rows[History])


def self_status(d, monkeypatch):
    from models.model import UploadFile
    from controllers.console import wraps
    from services.system_feature_service import SystemFeatureService
    from services.entities.feature_entities import LicenseStatus

    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    monkeypatch.setattr(SystemFeatureService, "get_license_status", lambda: LicenseStatus.ACTIVE)
    UploadFile.__table__.create(d.f.local.engine, checkfirst=True)
    native_source(d)
    response = send(d, "/console/api/account/casdoor-identity")
    assert response.status_code == 200, response.json
    return response.json


def test_actual_cookie_self_current_and_archived_history_are_distinguished(lifecycle, monkeypatch):
    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    ordinary(d, 2)
    response = self_status(d, monkeypatch)
    current = [row for row in response["memberships"] if row["namespace_id"] != d.reset_input["namespace_id"]]
    assert len(current) == 1 and current[0]["state"] == "recorded_managed", current
    old = [row for row in response["memberships"] if row["namespace_id"] == d.reset_input["namespace_id"]]
    assert len(old) == 2 and all(row["state"] == "unmanaged" and row["consistency"] == "historical" for row in old), old
    assert all(row["remote_actual_state"] == "unknown" for row in response["memberships"])
    assert not any(response["actions"].values())


@pytest.mark.parametrize("failure", ["last_source", "ack_unknown"])
def test_initial_generation_and_adopt_share_original_atomic_uow(lifecycle, monkeypatch, failure):
    from weakref import WeakSet
    from sqlalchemy.orm import Session
    from core.casdoor.auth_transactions import AuthTransactionError
    from models.casdoor_extend import CasdoorAuditExtend as Audit

    d = lifecycle
    target = reset_link(d, monkeypatch)
    proof = review(d, target, "adopt")
    before = rows(d)
    marked = WeakSet()
    if failure == "last_source":
        original = d.services.casdoor_local_lifecycle._final_source

        def reject(*args, **kwargs):
            original(*args, **kwargs)
            raise AuthTransactionError("source_session_invalid")

        monkeypatch.setattr(d.services.casdoor_local_lifecycle, "_final_source", reject)

    def flushed(session, context):
        if any(isinstance(row, History) and row.namespace_id != d.reset_input["namespace_id"] for row in session.new):
            marked.add(session)

    def committed(session):
        if session in marked:
            marked.discard(session)
            raise RuntimeError("synthetic manager commit acknowledgment unknown")

    if failure == "ack_unknown":
        sa.event.listen(Session, "after_flush", flushed)
        sa.event.listen(Session, "after_commit", committed)
    try:
        response = send(d, ROOT + "/local-membership/adopt", method="POST", json=proof)
    finally:
        if failure == "ack_unknown":
            sa.event.remove(Session, "after_flush", flushed)
            sa.event.remove(Session, "after_commit", committed)
    assert response.status_code != 200
    with d.f.service._session_factory() as session:
        identity = session.get(Identity, target["identity_id"])
        current = session.scalar(sa.select(History).where(History.namespace_id == identity.namespace_id))
        if failure == "last_source":
            assert identity.sync_generation == 0 and current is None
            # Whole UOW includes generation, new history and its original audit.
            assert rows(d, (History, Identity, Audit)) == {model: before[model] for model in (History, Identity, Audit)}
        else:
            assert identity.sync_generation == 1 and current.desired_generation == 1
    assert not d.f.lease.data


@pytest.mark.parametrize("failure", ["c1_root", "i19_root", "i19_ack"])
def test_fresh_archive_root_failure_and_unknown_ack_never_issue_session(lifecycle, monkeypatch, failure):
    from weakref import WeakSet
    from sqlalchemy.orm import Session
    from models.casdoor_extend import CasdoorAuditExtend as Audit
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
    from services.casdoor_local_login_service_extend import CasdoorLocalLoginService

    d = lifecycle
    create_adoption(d, monkeypatch)
    before = rows(d, (History, Identity, Audit))
    marked = WeakSet()
    invoked = []
    if failure == "c1_root":
        original = CasdoorLoginScopeRepository.recheck_before_commit

        def change(owner, *args, **kwargs):
            owner.session.execute(
                sa.update(Audit).where(Audit.action == "local_namespace_reset_v1").values(summary_json="{}")
            )
            invoked.append(True)
            return original(owner, *args, **kwargs)

        monkeypatch.setattr(CasdoorLoginScopeRepository, "recheck_before_commit", change)
    elif failure == "i19_root":
        original = CasdoorLocalLoginService._persist_local_login

        def change(owner, *args, **kwargs):
            result = original(owner, *args, **kwargs)
            with d.f.service._session_factory() as session, session.begin():
                session.execute(
                    sa.update(Audit).where(Audit.action == "local_namespace_reset_v1").values(summary_json="{}")
                )
            invoked.append(True)
            return result

        monkeypatch.setattr(CasdoorLocalLoginService, "_persist_local_login", change)

    def flushed(session, context):
        if any(isinstance(row, Account) and row.last_login_ip == "192.0.2.7" for row in session.dirty):
            marked.add(session)

    def committed(session):
        if session in marked:
            marked.discard(session)
            invoked.append(True)
            raise RuntimeError("synthetic I19 commit acknowledgment unknown")

    if failure == "i19_ack":
        sa.event.listen(Session, "after_flush", flushed)
        sa.event.listen(Session, "after_commit", committed)
    try:
        state = ordinary_begin(d, return_path="/apps/cross-namespace")
        response = complete(d, state)
    finally:
        if failure == "i19_ack":
            sa.event.remove(Session, "after_flush", flushed)
            sa.event.remove(Session, "after_commit", committed)
    assert invoked and response.location != d.f.settings.CONSOLE_WEB_URL + "/apps/cross-namespace"
    assert not any(
        _real_cookie_name(name) + "=" in value
        for name in ("access_token", "refresh_token")
        for value in response.headers.getlist("Set-Cookie")
    )
    completion = d.completed[-1]
    assert completion.phases.token_outcome != "issued" and completion.phases.cleanup_released is True
    if failure == "c1_root":
        assert rows(d, (History, Identity, Audit)) == before
    else:
        assert completion.phases.local_outcome == "committed"
        if failure == "i19_ack":
            assert completion.phases.finalization_outcome == "unknown"
    assert not d.f.lease.data


def test_actual_reset_save_activate_explicit_link_manager_adopt(lifecycle, monkeypatch):
    create_adoption(lifecycle, monkeypatch)


def test_self_final_readback_detects_changed_archive_fact(lifecycle, monkeypatch):
    from models.casdoor_extend import CasdoorAuditExtend as Audit
    from repositories.casdoor_self_identity_repository_extend import CasdoorSelfIdentityRepository

    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    before = rows(d, (Audit,))[Audit]
    original = CasdoorSelfIdentityRepository.recheck
    injected = []

    def drift(reader):
        # SQLite's fixture connections share a participating read transaction;
        # inject actual SQL on that connection rather than claim PG concurrency.
        reader.session.connection().execute(
            sa.update(Audit).where(Audit.action == "local_namespace_reset_v1").values(summary_json="{}")
        )
        injected.append(True)
        return original(reader)

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "recheck", drift)
    from models.model import UploadFile
    from controllers.console import wraps
    from services.system_feature_service import SystemFeatureService
    from services.entities.feature_entities import LicenseStatus

    UploadFile.__table__.create(d.f.local.engine, checkfirst=True)
    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    monkeypatch.setattr(SystemFeatureService, "get_license_status", lambda: LicenseStatus.ACTIVE)
    native_source(d)
    response = send(d, "/console/api/account/casdoor-identity")
    assert injected == [True]
    assert response.status_code == 409 and response.json == {"code": "casdoor_self_read_conflict"}, response.json
    assert response.headers["Cache-Control"] == "no-store"
    assert rows(d, (Audit,))[Audit] == before
