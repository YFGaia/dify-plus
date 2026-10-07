"""Original saved unused namespaces and an independent current manager's closure."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from libs.passport import PassportService
from libs.token import _real_cookie_name, generate_csrf_token
from models.account import Account, AccountStatus, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorOperationState
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.casdoor_extend import CasdoorNamespaceLifecycle
from services.account_password_hasher import DefaultAccountPasswordHasher
from test_casdoor_diagnostic_flow_extend import begin as diagnostic_begin, complete as diagnostic_complete
from test_casdoor_identity_action_flow_extend import reviewed_reauth
from test_casdoor_local_lifecycle_flow_extend import send
from test_casdoor_namespace_reset_flow_extend import ROOT, RESET, prepare, reset_review, rows
from test_casdoor_namespace_reset_repository_extend import insert_intent
from test_casdoor_repeated_namespace_recovery_flow_extend import observed_failure as observed_failure
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


def independent_native_manager(d, monkeypatch):
    """Same real signer/admission and bottom refresh wire as the original fixture."""
    credential = DefaultAccountPasswordHasher().hash("SyntheticPassword12")
    with d.f.service._session_factory() as session, session.begin():
        session.expire_on_commit = False
        manager = Account(
            name="Synthetic independent instance manager", email="manager-component@example.test",
            status=AccountStatus.ACTIVE, initialized_at=datetime.now(UTC).replace(tzinfo=None),
            password=credential.password_hash, password_salt=credential.password_salt,
        )
        session.add(manager)
        session.flush()
        session.add(TenantAccountJoin(
            tenant_id=str(UUID(int=100)), account_id=manager.id, role=TenantAccountRole.OWNER, current=True,
        ))
    manager._current_tenant = SimpleNamespace(id=str(UUID(int=100)))
    token = "8" * 128
    d.source["refresh_token:" + token] = manager.id.encode()
    access = PassportService().issue({
        "user_id": manager.id, "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "iss": "COMMUNITY", "sub": "Console API Passport",
    })
    csrf = generate_csrf_token(manager.id)
    for name, value in {"access_token": access, "refresh_token": token, "csrf_token": csrf}.items():
        d.client.set_cookie(_real_cookie_name(name), value, domain="console.example.test", path="/")
    d.actor, d.token, d.csrf = manager, token, csrf


@pytest.mark.parametrize("state,association", [(None, "namespace_id")] + [
    (state, association)
    for state in (CasdoorOperationState.APPLIED, CasdoorOperationState.UNKNOWN)
    for association in ("namespace_id", "account_id", "identity_id", "membership_id")
])
def test_empty_target_independent_manager_checks_ancestor_namespace_intents(lifecycle, monkeypatch, state, association):
    d = prepare(lifecycle, monkeypatch)
    first = send(d, RESET, method="POST", json=reset_review(d))
    assert first.status_code == 200, first.json
    independent_native_manager(d, monkeypatch)
    current_response = send(d, ROOT)
    assert current_response.status_code == 200, current_response.json
    current = current_response.json
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": current["etag"]})
    assert disabled.status_code == 200, disabled.json
    d.reset_input = {
        "namespace_id": current["draft"]["namespace_id"],
        "etag": disabled.json["configuration"]["etag"], "confirm_management_review": True,
    }
    # First prove the independent source is a genuinely admitted manager.
    reset_review(d)
    if state is not None:
        with d.f.service._session_factory() as session, session.begin():
            insert_intent(session, d, state=state)
            intent = session.scalar(sa.select(Intent))
            for field in ("namespace_id", "account_id", "identity_id", "membership_id"):
                if field != association:
                    setattr(intent, field, str(uuid4()))
        before = rows(d)
        response = send(d, RESET + "/review", method="POST", json=d.reset_input)
        assert response.status_code in (400, 409, 503), response.json
        assert rows(d) == before
    else:
        before = rows(d)
        response = send(d, RESET, method="POST", json=reset_review(d))
        assert response.status_code == 200, response.json
        assert set(before[Account]) <= set(rows(d)[Account])


@pytest.mark.parametrize("child", [None, "identity", "history", "intent"])
@pytest.mark.parametrize("lifecycle_state", [CasdoorNamespaceLifecycle.ACTIVE, CasdoorNamespaceLifecycle.FENCING])
def test_original_unbound_core_saves_leave_unused_namespaces_and_reset_preserves_them(lifecycle, monkeypatch, child, lifecycle_state):
    d = lifecycle
    snapshot = d.services.casdoor_configuration.get(d.actor)
    original = snapshot.draft.configuration
    for config in (original.model_copy(update={"client_id": "unused-synthetic-client"}), original):
        current = send(d, ROOT).json
        response = send(d, ROOT, method="PUT", json={
            "etag": current["etag"], "configuration": config.model_dump(mode="json"),
        })
        assert response.status_code == 200, response.json
    reviewed_reauth(d)
    snapshot, state = diagnostic_begin(d)
    assert diagnostic_complete(d, state).status_code == 302
    response = send(d, ROOT + "/activate", method="POST", json={
        "etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id),
    })
    assert response.status_code == 200, response.json
    target = response.json["active"]["namespace_id"]
    with d.f.service._session_factory() as session:
        unused = set(session.scalars(sa.select(Namespace.id).where(Namespace.id != target)))
    assert len(unused) == 2
    d = prepare(d, monkeypatch)
    if child is not None:
        # Retain the actual archive under a different current native manager,
        # so the unused parent's responsibility cannot be found via that source.
        assert send(d, RESET, method="POST", json=reset_review(d)).status_code == 200
        independent_native_manager(d, monkeypatch)
        current = send(d, ROOT).json
        disabled = send(d, ROOT + "/disable", method="POST", json={"etag": current["etag"]})
        assert disabled.status_code == 200, disabled.json
        d.reset_input = {"namespace_id": current["draft"]["namespace_id"],
                         "etag": disabled.json["configuration"]["etag"], "confirm_management_review": True}
    if child is not None or lifecycle_state is CasdoorNamespaceLifecycle.FENCING:
        with d.f.service._session_factory() as session, session.begin():
            unused_id = sorted(unused)[0]
            session.get(Namespace, unused_id).lifecycle = lifecycle_state
            if child == "intent":
                insert_intent(session, d)
            model = {"identity": Identity, "history": History, "intent": Intent}.get(child)
            if model is not None:
                session.scalar(sa.select(model)).namespace_id = unused_id
    before = rows(d)
    if child is None:
        response = send(d, RESET, method="POST", json=reset_review(d))
        assert response.status_code == 200, response.json
        after = rows(d, (Namespace,))[Namespace]
        assert all(row in after for row in before[Namespace] if row[0] in unused)
    else:
        response = send(d, RESET + "/review", method="POST", json=d.reset_input)
        assert response.status_code == 400, response.json
        assert rows(d) == before


@pytest.mark.parametrize("failure", ["late_ancestor_namespace_intent", "late_ancestor_membership_intent", "ancestor_history_drift"])
def test_empty_target_actual_reset_readback_preserves_complete_ancestor_component(lifecycle, monkeypatch, failure):
    d = prepare(lifecycle, monkeypatch)
    assert send(d, RESET, method="POST", json=reset_review(d)).status_code == 200
    independent_native_manager(d, monkeypatch)
    current = send(d, ROOT).json
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": current["etag"]})
    assert disabled.status_code == 200, disabled.json
    d.reset_input = {"namespace_id": current["draft"]["namespace_id"],
                     "etag": disabled.json["configuration"]["etag"], "confirm_management_review": True}
    proof, before, intents_before = reset_review(d), rows(d), rows(d, (Intent,))
    original = CasdoorConfigurationRepository.reset_namespace
    injected = []

    def mutate_after_actual_write(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        injected.append(True)
        if failure == "ancestor_history_drift":
            owner.session.scalar(sa.select(History)).desired_roles_json = "{}"
        else:
            insert_intent(owner.session, d, state=CasdoorOperationState.UNKNOWN)
            intent = owner.session.scalar(sa.select(Intent))
            keep = "namespace_id" if failure == "late_ancestor_namespace_intent" else "membership_id"
            for field in ("namespace_id", "account_id", "identity_id", "membership_id"):
                if field != keep:
                    setattr(intent, field, str(uuid4()))
        owner.session.flush()
        return result

    monkeypatch.setattr(CasdoorConfigurationRepository, "reset_namespace", mutate_after_actual_write)
    response = send(d, RESET, method="POST", json=proof)
    assert injected == [True]
    assert response.status_code == 400, response.json
    assert rows(d) == before
    assert rows(d, (Intent,)) == intents_before
