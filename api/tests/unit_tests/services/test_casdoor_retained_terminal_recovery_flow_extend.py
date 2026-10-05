"""NEXT real avatar/invitation producers before native management recovery RED."""

from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa

from core.helper import ssrf_proxy
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.casdoor_extend import CasdoorIntentKind
from test_gateway import response as wire_response
from test_casdoor_cross_namespace_adoption_flow_extend import create_adoption, native_source, ordinary
from test_casdoor_diagnostic_flow_extend import diagnostic as original_diagnostic
from test_casdoor_diagnostic_flow_extend import begin as diagnostic_begin, complete as diagnostic_complete
from test_casdoor_identity_action_flow_extend import actions as original_actions, reviewed_reauth
from test_casdoor_invited_history_http_extend import ordinary_then_invited as original_ordinary_then_invited
from test_casdoor_local_http_extend import mounted as mounted
from test_casdoor_invited_local_recovery_http_extend import finish, new_attempt
from test_casdoor_local_lifecycle_flow_extend import lifecycle as original_lifecycle, review, send
from test_casdoor_namespace_reset_flow_extend import ROOT, rows
from test_casdoor_production_login_policy_extend import production as original_production
from test_casdoor_repeated_namespace_recovery_flow_extend import current_target, observed_failure as observed_failure

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)
ordinary_then_invited = original_ordinary_then_invited


def test_real_pending_avatar_then_native_manager_release_before_unlink_reset(lifecycle, monkeypatch):
    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    native_source(d)
    current = send(d, ROOT).json
    config = dict(current["draft"]["configuration"], avatar_sync=True, avatar_mode="fill_empty")
    saved = send(d, ROOT, method="PUT", json={"etag": current["etag"], "configuration": config})
    assert saved.status_code == 200, saved.json
    reviewed_reauth(d)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    validated = send(d, ROOT + "/validate", method="POST", json={
        "etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id),
    })
    assert validated.status_code == 200, validated.json
    snapshot, state = diagnostic_begin(d)
    assert diagnostic_complete(d, state).status_code == 302
    activated = send(d, ROOT + "/activate", method="POST", json={
        "etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id),
    })
    assert activated.status_code == 200, activated.json
    original_wire = ssrf_proxy.make_request_with_deadline

    def profile_picture(method, url, **kwargs):
        result = original_wire(method, url, **kwargs)
        if urlsplit(url).path.endswith("userinfo"):
            data = result.json()
            data["picture"] = "https://images.example.test/avatar.png?synthetic=signed"
            return wire_response(data)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", profile_picture)
    ordinary(d, 2)
    native_source(d)
    with d.f.service._session_factory() as session:
        intent = session.scalar(sa.select(Intent).where(Intent.kind == CasdoorIntentKind.PROFILE_AVATAR))
        assert intent is not None and intent.operation_state.value == "pending"
        assert intent.attempt_count == 0 and intent.sent_at is None
        assert session.scalar(sa.select(Audit.id).where(Audit.action == "avatar_pending"))
    before, intents = rows(d), rows(d, (Intent,))
    proof = review(d, current_target(d), "release")
    result = send(d, ROOT + "/local-membership/release", method="POST", json=proof)
    assert result.status_code == 200, result.json
    assert rows(d, (Intent,)) == intents
    assert rows(d)[History] != before[History]


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
def test_real_public_invite_finalization_then_native_manager_release_before_unlink_reset(ordinary_then_invited, monkeypatch, tmp_path):
    case = ordinary_then_invited
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    f = case.m.f
    with f.service._session_factory() as session:
        operation = session.scalar(sa.select(Intent))
        assert operation.kind == CasdoorIntentKind.INVITATION_FINALIZE
        assert operation.operation_state.value == "applied" and operation.termination_state.value == "confirmed"
        assert set(session.scalars(sa.select(Audit.action).where(Audit.correlation_id == operation.id))) == {
            "invited_local_membership_write", "invited_local_membership_finalization",
        }
        identity = session.scalar(sa.select(Identity).where(Identity.account_id == case.account))
        history = session.scalar(sa.select(History).where(History.account_id == case.account))
        target = {"identity_id": identity.id, "workspace_id": history.workspace_id}
    # Reuse the exact original signed deployment builder, mounted native account
    # loader/cookies/CSRF and manager review wire after the real invited producer.
    production = original_production.__wrapped__(f, tmp_path, monkeypatch)
    d = original_diagnostic.__wrapped__(production, monkeypatch)
    d = original_actions.__wrapped__(d)
    d = original_lifecycle.__wrapped__(d, monkeypatch)
    before, intents = rows(d), rows(d, (Intent,))
    proof = review(d, target, "release")
    result = send(d, ROOT + "/local-membership/release", method="POST", json=proof)
    assert result.status_code == 200, result.json
    assert rows(d, (Intent,)) == intents
    assert rows(d)[History] != before[History]
