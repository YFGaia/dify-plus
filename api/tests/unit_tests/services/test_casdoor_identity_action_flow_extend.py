"""Full mounted source-action callers with original account/session/policy owners.

Provider signatures, SQLite and bottom Redis/HTTP wires are synthetic offline data.
"""

import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor import auth_transactions as auth
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from models.account import Account, AccountStatus, TenantAccountJoin
from models.casdoor_extend import (
    CasdoorIdentityExtend,
)
from services.account_password_hasher import DefaultAccountPasswordHasher
from test_casdoor_diagnostic_flow_extend import assert_source_unchanged
from test_casdoor_diagnostic_flow_extend import diagnostic as diagnostic
from test_casdoor_local_http_service_extend import counts
from test_claims import sign as real_sign

pytest_plugins = ("test_casdoor_production_login_policy_extend",)
IDENTITY = "/console/api/account/casdoor-identity"


@pytest.fixture
def actions(diagnostic):
    d = diagnostic
    d.services.casdoor_identity_action._redis_runtime_factory = d.f.service._redis_runtime_factory
    return d


def send(d, path, *, method="GET", client=None, headers=None, **kwargs):
    headers = (
        headers
        if headers is not None
        else ({"X-CSRF-Token": d.csrf} if path.startswith(IDENTITY) or method != "GET" else {})
    )
    return (client or d.client).open(
        path,
        method=method,
        headers=headers,
        base_url=d.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
        **kwargs,
    )


def begin(d, kind="link"):
    response = send(d, IDENTITY + "/" + kind, method="POST", json={})
    assert response.status_code == 200, response.json
    first = send(d, response.json["handoff_path"])
    assert first.status_code == 302, first.json
    state = parse_qs(urlsplit(first.location).query)["state"][0]
    return state, first


def complete(d, state, **extra):
    # Select only the synthetic IdP authorization whose code is being redeemed.
    selected = next((created for created in d.f.control.created if created.state == state), None)
    if selected is not None:
        d.f.control.created.remove(selected)
        d.f.control.created.append(selected)
    return send(
        d, "/console/api/auth/casdoor/callback", query_string={"state": state, "code": "synthetic-code"} | extra
    )


def bindings(d):
    with d.f.service._session_factory() as session:
        return session.scalars(sa.select(CasdoorIdentityExtend)).all()


def test_mounted_link_binds_current_source_only_without_admission_session_or_membership(actions):
    d = actions
    state, _ = begin(d)
    result = complete(d, state)
    assert result.status_code == 302, result.json
    assert result.location.endswith("/account?casdoor_identity=linked")
    bound = bindings(d)
    assert len(bound) == 1
    assert bound[0].account_id == d.actor.id
    assert_source_unchanged(d)
    assert len(d.f.control.created) == 1
    assert d.f.control.created[0].mode.value == "link"
    assert not d.f.lease.data


def test_missing_reauth_capability_is_denied_before_provider_navigation(actions):
    d = actions
    state, _ = begin(d)
    assert complete(d, state).status_code == 302
    status = send(d, IDENTITY + "/actions", headers={"X-CSRF-Token": d.csrf})
    assert status.status_code == 200, status.json
    assert status.json == {
        "link": False,
        "reauthenticate": False,
        "unlink": False,
        "reason": "reauthentication_unavailable",
    }
    response = send(d, IDENTITY + "/reauthenticate", method="POST", json={})
    assert response.status_code == 400
    assert_source_unchanged(d)


def reviewed_reauth(d):
    manifest = dict(d.production.manifest)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    revision = snapshot.draft
    manifest["binding"] = dict(
        manifest["binding"],
        namespace_id=str(revision.namespace_id),
        revision_id=str(revision.revision_id),
        configuration_digest=revision.configuration.config_digest(),
    )
    with d.f.service._session_factory() as session:
        owner = d.services.casdoor_configuration._repository(session)
        row = owner._revision(owner._integration().id, str(revision.revision_id))
        manifest["binding"]["config_digest"] = row.config_digest
    manifest["reauthentication"] = {
        "profile": "prompt_login_max_age_zero_auth_time_v1",
        **{
            name: {"record_id": "synthetic/" + name, "sha256": "c" * 64}
            for name in ("prompt_login_enforcement", "max_age_zero_enforcement", "signed_auth_time_contract")
        },
    }
    key = Ed25519PrivateKey.generate()
    payload = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()

    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    d.production.authority.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "authority_id": manifest["authority_id"],
                "public_key": b64(key.public_key().public_bytes_raw()),
                "accepted_manifest_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    )
    with open(d.production.policy._evidence_path, "w") as target:
        json.dump({"manifest": manifest, "signature": b64(key.sign(payload))}, target)
    d.production.manifest = manifest
    return snapshot


def auth_time(monkeypatch, *, offset=0, missing=False):
    def sign(key, claims):
        if "nonce" in claims and not missing:
            claims = claims | {"auth_time": datetime.now(UTC).timestamp() + offset}
        return real_sign(key, claims)

    monkeypatch.setattr("test_casdoor_local_http_service_extend.sign", sign)


def enable_unlink(d, monkeypatch):
    auth_time(monkeypatch)
    old = d.services.casdoor_configuration.get(d.actor)
    config = old.draft.configuration.model_copy(update={"self_unlink": True})
    saved = d.services.casdoor_configuration.save(d.actor, configuration=config, etag=old.etag, secret=None)
    reviewed_reauth(d)
    # All required rows come through the actual D04 caller, not an artificial gate.
    prepared = send(
        d,
        "/console/api/system-manage-extend/integration/casdoor/test-login",
        method="POST",
        json={"etag": saved.etag, "revision_id": str(saved.draft_revision_id)},
    )
    assert prepared.status_code == 200, prepared.json
    first = send(d, prepared.json["handoff"]["handoff_path"])
    state = parse_qs(urlsplit(first.location).query)["state"][0]
    assert complete(d, state).status_code == 302
    prepared = send(
        d,
        "/console/api/system-manage-extend/integration/casdoor/test-reauth",
        method="POST",
        json={"etag": saved.etag, "revision_id": str(saved.draft_revision_id)},
    )
    assert prepared.status_code == 200, prepared.json
    first = send(d, prepared.json["handoff_path"])
    assert parse_qs(urlsplit(first.location).query)["prompt"] == ["login"]
    assert parse_qs(urlsplit(first.location).query)["max_age"] == ["0"]
    state = parse_qs(urlsplit(first.location).query)["state"][0]
    result = complete(d, state)
    assert result.status_code == 302, result.json
    activation = send(
        d,
        "/console/api/system-manage-extend/integration/casdoor/activate",
        method="POST",
        json={"etag": saved.etag, "revision_id": str(saved.draft_revision_id)},
    )
    assert activation.status_code == 200, activation.json
    digest = DefaultAccountPasswordHasher().hash("SyntheticPassword12")
    with d.f.service._session_factory() as session, session.begin():
        actor = session.get(Account, d.actor.id)
        actor.password, actor.password_salt = digest.password_hash, digest.password_salt
    monkeypatch.setattr(dify_config, "ENABLE_EMAIL_PASSWORD_LOGIN", True)


def test_full_reviewed_diagnostic_activation_reauth_one_use_proof_and_unlink(actions, monkeypatch):
    d = actions
    state, _ = begin(d)
    assert complete(d, state).location.endswith("=linked")
    enable_unlink(d, monkeypatch)
    before = counts(d.f)
    state, start = begin(d, "reauthenticate")
    assert parse_qs(urlsplit(start.location).query)["prompt"] == ["login"]
    result = complete(d, state)
    assert result.location.endswith("=ready")
    status = send(d, IDENTITY + "/actions")
    assert status.json["unlink"] is True
    result = send(d, IDENTITY + "/unlink", method="POST", json={})
    assert result.status_code == 200, result.json
    assert result.json == {"status": "unlinked"}
    assert bindings(d) == []
    assert before[Account] == counts(d.f)[Account]
    assert send(d, IDENTITY + "/unlink", method="POST", json={}).status_code == 400
    assert_source_unchanged(d)


def test_link_same_identity_is_idempotent_and_preserves_existing_fields(actions):
    d = actions
    before = counts(d.f)
    for _ in range(2):
        state, _ = begin(d)
        assert complete(d, state).location.endswith("=linked")
    assert len(bindings(d)) == 1
    after = counts(d.f)
    assert after[Account] == before[Account]
    assert after[TenantAccountJoin] == before[TenantAccountJoin]
    assert_source_unchanged(d)


@pytest.mark.parametrize("failure", ["revoked", "banned", "policy", "nonce", "replay", "crossbrowser"])
def test_link_failure_has_no_binding_or_new_session(actions, failure):
    d = actions
    state, _ = begin(d)
    if failure == "revoked":
        d.source.clear()
    elif failure == "banned":
        with d.f.service._session_factory() as session, session.begin():
            session.get(Account, d.actor.id).status = AccountStatus.BANNED
    elif failure == "policy":
        d.production.authority.unlink()
    elif failure == "nonce":
        d.f.control.bad_nonce = True
    elif failure == "replay":
        assert complete(d, state).status_code == 302
        existing = bindings(d)[0].id
        assert complete(d, state).status_code == 400
        assert bindings(d)[0].id == existing
        assert_source_unchanged(d)
        return
    elif failure == "crossbrowser":
        d.client.delete_cookie(auth.SCOPE_COOKIE_NAME, domain="console.example.test", path=auth.COOKIE_PATH)
    response = complete(d, state)
    assert response.status_code in (302, 400, 409, 503), response.json
    if response.status_code == 302:
        assert response.location.endswith("=failed")
    assert not bindings(d)
    assert_source_unchanged(d)


@pytest.mark.parametrize("failure", ["missing", "old", "future"])
def test_reauth_signed_auth_time_is_required_before_action_proof(actions, monkeypatch, failure):
    d = actions
    state, _ = begin(d)
    assert complete(d, state).location.endswith("=linked")
    enable_unlink(d, monkeypatch)
    auth_time(monkeypatch, offset={"old": -360, "future": 30}.get(failure, 0), missing=failure == "missing")
    state, _ = begin(d, "reauthenticate")
    assert complete(d, state).location.endswith("=failed")
    assert send(d, IDENTITY + "/unlink", method="POST", json={}).status_code == 400
    assert len(bindings(d)) == 1
    assert_source_unchanged(d)


@pytest.mark.parametrize("kind", ["role_replace", "resource_grant", "profile_avatar", "history"])
def test_any_history_or_intent_blocks_unlink_even_recorded_terminal(actions, monkeypatch, kind):
    from models.casdoor_extend import (
        CasdoorFinalizationState,
        CasdoorIntentKind,
        CasdoorManagedMembershipExtend,
        CasdoorMembershipOwnership,
        CasdoorMembershipSource,
        CasdoorOperationState,
        CasdoorSyncIntentExtend,
        CasdoorTerminationState,
    )

    d = actions
    state, _ = begin(d)
    assert complete(d, state).location.endswith("=linked")
    enable_unlink(d, monkeypatch)
    identity = bindings(d)[0]
    snapshot = d.services.casdoor_identity_action._active()
    with d.f.service._session_factory() as session, session.begin():
        if kind == "history":
            session.add(
                CasdoorManagedMembershipExtend(
                    namespace_id=identity.namespace_id,
                    identity_id=identity.id,
                    account_id=d.actor.id,
                    workspace_id=str(snapshot.configuration.default_workspace_id),
                    join_id=None,
                    ownership=CasdoorMembershipOwnership.RELEASED,
                    source=CasdoorMembershipSource.FALLBACK,
                    revision_id=str(snapshot.binding.revision_id),
                    last_applied_roles_json="{}",
                    desired_roles_json="{}",
                    baseline_json="{}",
                    finalization=CasdoorFinalizationState.FINALIZED,
                    tombstone=True,
                )
            )
        else:
            session.add(
                CasdoorSyncIntentExtend(
                    namespace_id=identity.namespace_id,
                    identity_id=identity.id,
                    account_id=d.actor.id,
                    workspace_id=None,
                    membership_id=None,
                    revision_id=str(snapshot.binding.revision_id),
                    generation=0,
                    ownership_epoch=0,
                    fence_epoch=snapshot.binding.fence_epoch,
                    kind=CasdoorIntentKind(kind),
                    scope_digest="1" * 64,
                    idempotency_key="2" * 64,
                    desired_json="{}",
                    operation_state=CasdoorOperationState.APPLIED,
                    termination_state=CasdoorTerminationState.CONFIRMED,
                )
            )
    status = send(d, IDENTITY + "/actions")
    assert status.json["reauthenticate"] is False
    assert status.json["reason"] == "managed_history_requires_release"
    assert send(d, IDENTITY + "/reauthenticate", method="POST", json={}).status_code == 400
    assert len(bindings(d)) == 1
    assert_source_unchanged(d)


@pytest.mark.parametrize("failure", ["no_password", "malformed_password", "disabled_password"])
def test_actual_remaining_login_credential_and_enabled_policy_are_required(actions, monkeypatch, failure):
    d = actions
    state, _ = begin(d)
    assert complete(d, state).location.endswith("=linked")
    enable_unlink(d, monkeypatch)
    if failure == "disabled_password":
        monkeypatch.setattr(dify_config, "ENABLE_EMAIL_PASSWORD_LOGIN", False)
    else:
        with d.f.service._session_factory() as session, session.begin():
            account = session.get(Account, d.actor.id)
            account.password = None if failure == "no_password" else "bad"
    response = send(d, IDENTITY + "/reauthenticate", method="POST", json={})
    assert response.status_code == 400
    assert send(d, IDENTITY + "/actions").json["reason"] == "other_login_unavailable"
    assert len(bindings(d)) == 1


@pytest.mark.parametrize("failure", ["revoked", "get_failure", "deadline"])
def test_link_final_write_source_and_deadline_barrier_rolls_back(actions, monkeypatch, failure):
    from repositories.casdoor_identity_repository_extend import CasdoorIdentityRepository

    d = actions
    state, _ = begin(d)
    original = CasdoorIdentityRepository.bind

    def bind(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        if failure == "revoked":
            d.source.clear()
        elif failure == "get_failure":

            def unavailable(*args):
                raise TimeoutError("synthetic private session read fault")

            monkeypatch.setattr(d.f.redis, "get", unavailable)
        else:
            clock = time.monotonic
            monkeypatch.setattr("services.casdoor_identity_action_service_extend.time.monotonic", lambda: clock() + 50)
        return result

    monkeypatch.setattr(CasdoorIdentityRepository, "bind", bind)
    assert complete(d, state).status_code == (503 if failure == "get_failure" else 400)
    assert not bindings(d)
    assert_source_unchanged(d)


def test_draft_save_does_not_invalidate_active_link(actions):
    d = actions
    state, _ = begin(d)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    d.services.casdoor_configuration.save(
        d.actor,
        configuration=snapshot.draft.configuration.model_copy(update={"button_text": "Changed draft only"}),
        etag=snapshot.etag,
        secret=None,
    )
    assert complete(d, state).location.endswith("=linked")
    assert len(bindings(d)) == 1


def test_unlink_consumed_proof_cannot_survive_source_revocation(actions, monkeypatch):
    d = actions
    state, _ = begin(d)
    assert complete(d, state).location.endswith("=linked")
    enable_unlink(d, monkeypatch)
    state, _ = begin(d, "reauthenticate")
    assert complete(d, state).location.endswith("=ready")
    d.source.clear()
    assert send(d, IDENTITY + "/unlink", method="POST", json={}).status_code == 400
    assert len(bindings(d)) == 1
    assert_source_unchanged(d)


def test_two_link_transactions_keep_the_same_browser_scope_and_both_complete(actions):
    d = actions
    first, _ = begin(d)
    scope = d.client.get_cookie(auth.SCOPE_COOKIE_NAME, domain="console.example.test", path=auth.COOKIE_PATH).value
    second, _ = begin(d)
    assert (
        d.client.get_cookie(auth.SCOPE_COOKIE_NAME, domain="console.example.test", path=auth.COOKIE_PATH).value == scope
    )
    assert complete(d, first).location.endswith("=linked")
    assert complete(d, second).location.endswith("=linked")
    assert len(bindings(d)) == 1
    assert_source_unchanged(d)


def test_ordinary_login_transaction_survives_later_link_handoff(actions):
    d = actions
    first = send(d, auth.COOKIE_PATH + "/login")
    ordinary = send(d, urlsplit(first.location).path, query_string=urlsplit(first.location).query)
    ordinary_state = parse_qs(urlsplit(ordinary.location).query)["state"][0]
    linked_state, _ = begin(d)
    assert complete(d, linked_state).location.endswith("=linked")
    assert complete(d, ordinary_state).status_code == 302
    assert len(bindings(d)) == 1


def test_link_cancellation_keeps_primary_when_lease_cleanup_also_fails(actions, monkeypatch):
    from core.casdoor.leases import CasdoorLeases
    from repositories.casdoor_identity_repository_extend import CasdoorIdentityRepository

    d = actions
    state, _ = begin(d)
    primary = KeyboardInterrupt("synthetic cancellation")

    def interrupted(*args, **kwargs):
        raise primary

    def cleanup_failed(*args):
        raise TimeoutError("synthetic lease cleanup failure")

    monkeypatch.setattr(CasdoorIdentityRepository, "bind", interrupted)
    monkeypatch.setattr(CasdoorLeases, "release", cleanup_failed)
    with pytest.raises(KeyboardInterrupt) as caught:
        complete(d, state)
    assert caught.value is primary
    assert not bindings(d)
    assert_source_unchanged(d)


def test_unlink_proof_expires_and_is_bound_to_its_browser(actions, monkeypatch):
    d = actions
    state, _ = begin(d)
    assert complete(d, state).location.endswith("=linked")
    enable_unlink(d, monkeypatch)
    state, _ = begin(d, "reauthenticate")
    assert complete(d, state).location.endswith("=ready")
    d.f.init.now += 61
    assert send(d, IDENTITY + "/unlink", method="POST", json={}).status_code == 400
    assert len(bindings(d)) == 1
    assert_source_unchanged(d)


def test_logical_link_action_charges_ip_and_source_account_once_not_per_guard_or_status(actions):
    d = actions
    state, _ = begin(d)
    link_scopes = [key for key in d.f.control.limits if ":link:" in key]
    assert len(link_scopes) == 2
    assert sum(":ip:" in key for key in link_scopes) == 1
    assert sum(":account:" in key for key in link_scopes) == 1
    assert not any(d.actor.id in key for key in link_scopes)
    assert complete(d, state).location.endswith("=linked")
    assert send(d, IDENTITY + "/actions").status_code == 200
    assert [key for key in d.f.control.limits if ":link:" in key] == link_scopes


def test_link_ip_and_account_action_limits_use_429_without_mutation_or_raw_logging(actions, monkeypatch, caplog):
    d = actions
    caplog.set_level("INFO", logger="core.casdoor.request_safety")
    monkeypatch.setattr(d.f.redis, "zcard", lambda key: 5 if ":link:account:" in key else 0)
    response = send(d, IDENTITY + "/link", method="POST", json={})
    assert response.status_code == 429, response.json
    assert response.headers["Retry-After"] == "60"
    assert response.json["retry_allowed"] is False
    assert UUID(response.json["correlation_id"])
    assert not bindings(d) and not d.f.control.created
    records = [record.casdoor_event for record in caplog.records if hasattr(record, "casdoor_event")]
    assert len(records) == 1
    assert records[0]["correlation_id"] == response.json["correlation_id"]
    assert records[0]["action"] == "link"
    assert "refresh_token" not in caplog.text and "synthetic-csrf" not in caplog.text
    assert_source_unchanged(d)


def test_revoked_source_is_never_charged_as_an_authenticated_action_account(actions):
    d = actions
    d.source.clear()
    response = send(d, IDENTITY + "/link", method="POST", json={})
    assert response.status_code == 400
    scopes = [key for key in d.f.control.limits if ":link:" in key]
    assert len(scopes) == 1 and ":ip:" in scopes[0]


def test_passive_status_does_not_consume_five_action_quota(actions):
    d = actions
    d.f.control.limit_count = 5
    assert send(d, IDENTITY + "/actions").status_code == 200
    assert not any(":link:" in key or ":reauth:" in key or ":diagnostic:" in key for key in d.f.control.limits)
