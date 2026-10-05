"""Actual composition and mounted RP producer/activation; synthetic offline OP only."""

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from services.casdoor_rp_logout_service_extend import RP_CALLBACK_PATH
from test_casdoor_diagnostic_flow_extend import MANAGEMENT, assert_source_unchanged, begin, complete, rows, send
from test_casdoor_local_http_service_extend import counts

pytest_plugins = ("test_casdoor_rp_logout_service_extend", "test_casdoor_diagnostic_flow_extend")


@pytest.fixture
def mounted(rp, diagnostic):
    d = diagnostic
    services = d.services
    services.casdoor_deployment_policy._authority_path = str(rp.authority)
    services.casdoor_deployment_policy._evidence_path = str(rp.envelope)
    services.casdoor_deployment_policy._now = lambda: datetime.now(UTC)
    services.casdoor_rp_logout._runtime = rp.runtime
    assert services.casdoor_session._rp_logout_service is services.casdoor_rp_logout
    assert services.casdoor_diagnostic._rp_logout_service is services.casdoor_rp_logout
    assert services.casdoor_configuration._rp_logout_service is services.casdoor_rp_logout
    return SimpleNamespace(d=d, rp=rp, service=services.casdoor_rp_logout)


def rp_round_trip(m):
    d = m.d
    snapshot = d.services.casdoor_configuration.get(d.actor)
    prepared = send(
        d,
        MANAGEMENT + "/test-rp-logout",
        method="POST",
        json={
            "etag": snapshot.etag,
            "revision_id": str(snapshot.draft_revision_id),
        },
    )
    assert prepared.status_code == 200, prepared.json
    assert prepared.json["status"] == "started", prepared.json
    authorization = send(d, prepared.json["handoff"]["handoff_path"])
    assert authorization.status_code == 302, authorization.json
    state = parse_qs(urlsplit(authorization.location).query)["state"][0]
    native = complete(d, state)
    assert native.status_code == 303, native.json
    assert native.location.startswith("/console/api/auth/casdoor/logout/")
    assert "id_token" not in native.location
    provider = send(d, native.location)
    assert provider.status_code == 303
    parameters = parse_qs(urlsplit(provider.location).query)
    assert set(parameters) == {"id_token_hint", "post_logout_redirect_uri", "state"}
    returned = send(d, RP_CALLBACK_PATH, query_string={"state": parameters["state"][0]})
    assert returned.status_code == 303
    assert returned.location == d.f.settings.CONSOLE_WEB_URL + "/system-manage-extend/system-integration?tab=casdoor"
    return snapshot, provider, parameters, returned


def activate(d, snapshot):
    return send(
        d,
        MANAGEMENT + "/activate",
        method="POST",
        json={
            "etag": snapshot.etag,
            "revision_id": str(snapshot.draft_revision_id),
        },
    )


def test_actual_rp_producer_does_not_replace_four_gates_and_admits_only_fresh_observation(mounted):
    m, d = mounted, mounted.d
    before = counts(d.f)
    snapshot, state = begin(d)
    assert complete(d, state).status_code == 302
    mandatory = [(row.kind, row.status, row.correlation_id) for row in rows(d)]
    assert activate(d, snapshot).status_code == 409
    snapshot, provider, _, returned = rp_round_trip(m)
    assert [(row.kind, row.status, row.correlation_id) for row in rows(d)] == mandatory
    assert counts(d.f) == before
    assert_source_unchanged(d)
    status = send(d, MANAGEMENT + "/rp-logout-status", query_string={"revision_id": str(snapshot.draft_revision_id)})
    assert status.status_code == 200, status.json
    assert status.json["status"] == "passed"
    assert status.json["profile_available"] is True
    assert status.json["checked_at"].endswith("Z")
    assert status.json["expires_at"].endswith("Z")
    assert "id_token" not in str(status.json)
    assert "id_token" not in returned.location
    assert "id_token_hint=" in provider.location  # Only the reviewed OP Location carries the actual native slot.
    result = activate(d, snapshot)
    assert result.status_code == 200, result.json
    assert result.json["enabled"] is True


@pytest.mark.parametrize(
    "failure",
    [
        "missing_static_gate",
        "missing_deployment_gate",
        "missing_protocol_gate",
        "missing_diagnostic_gate",
        "revoked",
        "expired",
        "missing_observation",
        "account_banned",
        "public_flag",
        "wrong_fingerprint",
        "wrong_binding",
        "future_check",
    ],
)
def test_opt_in_activation_or_diagnostic_stays_closed_on_actual_owner_failure(mounted, failure):
    m, d = mounted, mounted.d
    snapshot, state = begin(d)
    assert complete(d, state).status_code == 302
    snapshot, _, _, _ = rp_round_trip(m)
    if failure.startswith("missing_") and failure.endswith("_gate"):
        from models.casdoor_extend import CasdoorValidationExtend

        with d.f.service._session_factory() as session, session.begin():
            kind = failure.removeprefix("missing_").removesuffix("_gate")
            missing = [row for row in rows(d) if row.kind.value == kind]
            assert missing
            for row in missing:
                session.delete(session.get(CasdoorValidationExtend, row.id))
    elif failure == "revoked":
        m.rp.authority.unlink()
    elif failure == "expired":
        m.rp.wire.values.clear()
        m.rp.wire.expiries.clear()
    elif failure == "missing_observation":
        for key in list(m.rp.wire.values):
            if ":observation:" in key:
                del m.rp.wire.values[key]
                del m.rp.wire.expiries[key]
    elif failure == "account_banned":
        from models.account import Account, AccountStatus

        with d.f.service._session_factory() as session, session.begin():
            session.get(Account, d.actor.id).status = AccountStatus.BANNED
    else:
        from dataclasses import replace
        from datetime import timedelta

        observed = m.service.observation(m.rp.binding)
        if failure == "public_flag":
            replacement = {"passed": True, "proof_fingerprint": observed.proof_fingerprint}
        elif failure == "wrong_fingerprint":
            replacement = replace(observed, proof_fingerprint="f" * 64)
        elif failure == "wrong_binding":
            replacement = replace(observed, binding=observed.binding.model_copy(update={"config_digest": "f" * 64}))
        else:
            replacement = replace(observed, checked_at=datetime.now(UTC) + timedelta(minutes=1))
        m.service.observation = lambda binding: replacement
    before = d.services.casdoor_configuration.get(d.actor).etag
    result = activate(d, snapshot)
    if failure == "account_banned":
        assert result.status_code == 401
    else:
        assert result.status_code == 409, result.json
    current = d.services.casdoor_configuration.get(d.actor)
    assert current.etag == before
    assert current.active_revision_id != snapshot.draft_revision_id


def test_rp_handoff_head_does_not_consume_and_callback_replay_creates_no_session(mounted):
    m, d = mounted, mounted.d
    snapshot, _, params, _ = rp_round_trip(m)
    before = counts(d.f)
    replay = send(d, RP_CALLBACK_PATH, query_string={"state": params["state"][0]})
    assert replay.status_code == 303
    assert replay.location.endswith("/signin/casdoor-logout?status=unavailable")
    assert counts(d.f) == before
    assert_source_unchanged(d)
    assert send(d, RP_CALLBACK_PATH, method="HEAD").status_code == 405
    assert send(d, "/console/api/auth/casdoor/logout/" + "A" * 43, method="HEAD").status_code == 405


def normal_session(m, monkeypatch):
    from configs import dify_config
    from controllers.console import wraps
    from enums import DeploymentEdition
    from extensions.ext_application_services import build_application_services
    from sqlalchemy.orm import sessionmaker

    d = m.d
    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    snapshot, state = begin(d)
    assert complete(d, state).status_code == 302
    snapshot, _, _, _ = rp_round_trip(m)
    assert activate(d, snapshot).status_code == 200
    monkeypatch.setattr(dify_config, "CASDOOR_DEPLOYMENT_AUTHORITY_PATH", str(m.rp.authority))
    monkeypatch.setattr(dify_config, "CASDOOR_DEPLOYMENT_EVIDENCE_PATH", str(m.rp.envelope))

    class TrackedFactory(sessionmaker):
        def __call__(self, **kwargs):
            return d.f.service._session_factory()

    services = build_application_services(
        database_client=TrackedFactory(),
        deployment_edition=DeploymentEdition.COMMUNITY,
        initialization_password="",
        redis=d.f.redis,
    )
    services.casdoor_diagnostic._redis_runtime_factory = d.f.service._redis_runtime_factory
    services.casdoor_local_http._redis_runtime_factory = d.f.service._redis_runtime_factory
    services.casdoor_rp_logout._runtime = m.rp.runtime
    services.casdoor_session._runtime = m.rp.runtime
    services.accounts.authentication._sessions._redis = m.rp.wire
    d.app.extensions["application_services"] = services
    d.services = services

    bridged = set()

    def bridge_actual_writes():
        assert all(scope.finish_calls == 1 for scope in d.f.control.runtime_scopes)
        for key, ttl, value in d.f.control.tokens:
            if key not in bridged:
                m.rp.wire.set(key, value, ex=ttl.total_seconds(), nx=True)
                bridged.add(key)

    m.rp.runtime.before_open = bridge_actual_writes
    deleted = []

    def delete(*keys):
        for key in keys:
            physical = m.rp.wire._key(key)
            deleted.append(key)
            m.rp.wire.values.pop(physical, None)
            m.rp.wire.expiries.pop(physical, None)
        return len(keys)

    monkeypatch.setattr(m.rp.wire, "delete", delete, raising=False)
    d.f.control.created.clear()
    d.f.control.consumed.clear()
    d.f.control.operations.clear()
    d.f.control.requests.clear()
    authorization = send(d, "/console/api/auth/casdoor/login")
    if authorization.status_code == 303:
        target = urlsplit(authorization.location)
        authorization = send(d, target.path + "?" + target.query)
    assert authorization.status_code == 302, authorization.json
    state = parse_qs(urlsplit(authorization.location).query)["state"][0]
    result = complete(d, state)
    assert result.status_code == 302
    return services, deleted


def test_actual_factory_ordinary_login_then_original_logout_delivers_only_local_opaque_handoff(mounted, monkeypatch):
    from libs.token import _real_cookie_name

    m, d = mounted, mounted.d
    services, deleted = normal_session(m, monkeypatch)
    csrf = d.client.get_cookie(_real_cookie_name("csrf_token"), domain="console.example.test").value
    status = d.client.get(
        "/console/api/auth/casdoor/session",
        headers={"X-CSRF-Token": csrf},
        base_url=d.f.settings.CONSOLE_API_URL,
    )
    assert status.status_code == 200, status.json
    assert status.json["source"] == "casdoor"
    assert status.json["rp_logout_available"] is True
    native_tokens = [operation for operation in d.f.control.operations if operation._exchange_started]
    assert native_tokens
    issue = services.casdoor_rp_logout.issue_local_success

    def after_local_success(*args, **kwargs):
        assert any(key.startswith("account_refresh_token:") for key in deleted)
        assert d.client.get_cookie(_real_cookie_name("refresh_token"), domain="console.example.test") is not None
        return issue(*args, **kwargs)

    monkeypatch.setattr(services.casdoor_rp_logout, "issue_local_success", after_local_success)
    logout = d.client.post("/console/api/logout", headers={"X-CSRF-Token": csrf}, base_url=d.f.settings.CONSOLE_API_URL)
    assert logout.status_code == 200, logout.json
    assert logout.json["result"] == "success"
    assert logout.json["casdoor_logout"]["status"] == "handoff_ready"
    local = logout.json["casdoor_logout"]["handoff"]["handoff_path"]
    assert "id_token" not in str(logout.json)
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert d.client.get_cookie(_real_cookie_name(name), domain="console.example.test") is None
    navigation = send(d, local)
    assert navigation.status_code == 303
    assert navigation.location.startswith(m.rp.config.expected_issuer + "/api/logout?")
    params = parse_qs(urlsplit(navigation.location).query)
    returned = send(d, RP_CALLBACK_PATH, query_string={"state": params["state"][0]})
    assert returned.status_code == 303
    assert returned.location.endswith("/signin/casdoor-logout?status=returned")
    assert "id_token" not in returned.location
    assert send(d, local).location.endswith("/signin/casdoor-logout?status=unavailable")


@pytest.mark.parametrize("failure", ["revoke", "optional_close", "mandatory_logout", "mandatory_flask_logout"])
def test_original_local_result_and_optional_ordering_survive_owner_faults(mounted, monkeypatch, failure):
    from libs.token import _real_cookie_name

    m, d = mounted, mounted.d
    services, deleted = normal_session(m, monkeypatch)
    csrf = d.client.get_cookie(_real_cookie_name("csrf_token"), domain="console.example.test").value
    original_source = d.client.get_cookie(services.casdoor_session.cookie_name(), domain="console.example.test").value
    issued = []
    real_issue = services.casdoor_rp_logout.issue_local_success
    monkeypatch.setattr(
        services.casdoor_rp_logout,
        "issue_local_success",
        lambda *a, **kw: (issued.append(True), real_issue(*a, **kw))[1],
    )
    if failure == "revoke":
        m.rp.authority.unlink()
    elif failure == "optional_close":
        m.rp.runtime.fail_close = True
    else:

        def fail(*args):
            raise RuntimeError("synthetic original local owner failure")

        if failure == "mandatory_logout":
            monkeypatch.setattr(services.accounts.authentication, "logout", fail)
        else:
            monkeypatch.setattr("controllers.console.auth.login.flask_login.logout_user", fail)
    result = d.client.post("/console/api/logout", headers={"X-CSRF-Token": csrf}, base_url=d.f.settings.CONSOLE_API_URL)
    assert issued == []
    if failure.startswith("mandatory"):
        assert result.status_code == 500
        assert (
            d.client.get_cookie(services.casdoor_session.cookie_name(), domain="console.example.test").value
            == original_source
        )
        assert "casdoor_logout" not in result.json
    else:
        assert result.status_code == 200
        assert result.json == {"result": "success"}
        assert d.client.get_cookie(_real_cookie_name("refresh_token"), domain="console.example.test") is None
        assert d.client.get_cookie(services.casdoor_session.cookie_name(), domain="console.example.test") is None
        assert any(key.startswith("account_refresh_token:") for key in deleted)


def test_actual_anonymous_retry_preserves_local_logout_and_invalidates_previous_state(mounted, monkeypatch):
    from libs.token import _real_cookie_name

    m, d = mounted, mounted.d
    normal_session(m, monkeypatch)
    csrf = d.client.get_cookie(_real_cookie_name("csrf_token"), domain="console.example.test").value
    logout = d.client.post("/console/api/logout", headers={"X-CSRF-Token": csrf}, base_url=d.f.settings.CONSOLE_API_URL)
    local = logout.json["casdoor_logout"]["handoff"]["handoff_path"]
    navigation = send(d, local)
    first_state = parse_qs(urlsplit(navigation.location).query)["state"][0]
    error = send(d, RP_CALLBACK_PATH, query_string={"state": first_state, "error": "synthetic_provider_error"})
    assert error.location.endswith("/signin/casdoor-logout?status=unavailable")
    retry_path = "/console/api/auth/casdoor/logout/retry"
    untrusted = d.client.post(
        retry_path, headers={"Origin": "https://attacker.example"}, base_url=d.f.settings.CONSOLE_API_URL
    )
    assert untrusted.json["status"] == "local_only"
    retried = d.client.post(
        retry_path, headers={"Origin": d.f.settings.CONSOLE_WEB_URL}, base_url=d.f.settings.CONSOLE_API_URL
    )
    assert retried.json["status"] == "handoff_ready", retried.json
    provider = send(d, retried.json["handoff"]["handoff_path"])
    next_state = parse_qs(urlsplit(provider.location).query)["state"][0]
    assert next_state != first_state
    assert send(d, RP_CALLBACK_PATH, query_string={"state": first_state}).location.endswith("status=unavailable")
    assert send(d, RP_CALLBACK_PATH, query_string={"state": next_state}).location.endswith("status=returned")
    assert d.client.get_cookie(_real_cookie_name("access_token"), domain="console.example.test") is None
