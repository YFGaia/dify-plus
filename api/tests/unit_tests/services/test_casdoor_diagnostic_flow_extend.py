"""Mounted real diagnostic/writer/activate chain, synthetic offline wires only."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import sqlalchemy as sa
from configs import dify_config
from controllers.console import bp
from core.casdoor import auth_transactions as auth
from core.casdoor.permissions import CasdoorManagementPolicy
from enums import DeploymentEdition
from extensions.ext_application_services import build_application_services
from extensions.ext_login import DifyLoginManager, _load_user_from_request
from flask import Flask
from libs.passport import PassportService
from libs.token import _real_cookie_name, generate_csrf_token
from models.account import Account, AccountStatus, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import CasdoorValidationExtend, CasdoorValidationKind, CasdoorValidationStatus
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
from repositories.casdoor_validation_repository_extend import CasdoorValidationRepository
from sqlalchemy.orm import sessionmaker
from test_casdoor_local_http_service_extend import counts

pytest_plugins = ("test_casdoor_production_login_policy_extend",)
MANAGEMENT = "/console/api/system-manage-extend/integration/casdoor"


@pytest.fixture
def diagnostic(production, monkeypatch):
    f = production.flow
    actor = Account(name="Synthetic diagnostic administrator", email="diag@example.test")
    actor.id = str(UUID(int=811))
    actor.status = AccountStatus.ACTIVE
    actor.last_active_at = datetime.now(UTC).replace(tzinfo=None)
    with f.service._session_factory() as session, session.begin():
        session.expire_on_commit = False
        session.add(actor)
        session.add(
            TenantAccountJoin(
                tenant_id=str(f.local.config.default_workspace_id),
                account_id=actor.id,
                role=TenantAccountRole.NORMAL,
                current=True,
            )
        )
    token = "7" * 128
    source = {"refresh_token:" + token: actor.id.encode()}
    source_reads = []

    def get(name):
        assert not f.local.session.in_transaction()
        # The final bounded request-refresh GET is the explicitly documented
        # consistency exception. No other Redis/provider call enters SQL.
        if f.opened:
            assert len(f.opened) == 1 and next(iter(f.opened)).in_transaction()
        source_reads.append(name)
        assert not name.startswith("account_refresh_token:")
        return source.get(name)

    monkeypatch.setattr(f.redis, "get", get, raising=False)
    original_eval = f.init.eval

    def eval(script, numkeys, *args):
        if script == "return redis.call('GET', KEYS[1])":
            assert numkeys == 1
            value, expires = f.init.records.get(args[0], (None, 0))
            return value if expires > f.init.now else None
        return original_eval(script, numkeys, *args)

    monkeypatch.setattr(f.init, "eval", eval)
    for field in ("CONSOLE_API_URL", "CONSOLE_WEB_URL", "DEPLOY_ENV", "RBAC_ENABLED"):
        monkeypatch.setattr(dify_config, field, getattr(f.settings, field))
    monkeypatch.setattr(dify_config, "SECRET_KEY", "offline-http-key")
    monkeypatch.setattr(dify_config, "COOKIE_DOMAIN", "")
    monkeypatch.setattr(dify_config, "CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS", actor.id)
    monkeypatch.setattr(dify_config, "CASDOOR_DEPLOYMENT_AUTHORITY_PATH", str(production.authority))
    monkeypatch.setattr(dify_config, "CASDOOR_DEPLOYMENT_EVIDENCE_PATH", production.policy._evidence_path)

    class TrackedFactory(sessionmaker):
        def __call__(self, **kwargs):
            return f.service._session_factory()

    services = build_application_services(
        database_client=TrackedFactory(),
        deployment_edition=DeploymentEdition.COMMUNITY,
        initialization_password="",
        redis=f.redis,
    )
    services.casdoor_diagnostic._redis_runtime_factory = f.service._redis_runtime_factory
    services = replace(services, casdoor_local_http=f.service)
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME="console.example.test", SECRET_KEY="offline-flask")
    manager = DifyLoginManager()
    manager.init_app(app)

    @manager.request_loader
    def load(req):
        with f.service._session_factory() as session:
            return _load_user_from_request(req, session)

    app.extensions["application_services"] = services
    app.register_blueprint(bp)
    client = app.test_client()
    access = PassportService().issue(
        {
            "user_id": actor.id,
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iss": "COMMUNITY",
            "sub": "Console API Passport",
        }
    )
    csrf = generate_csrf_token(actor.id)
    original_cookies = {"access_token": access, "refresh_token": token, "csrf_token": csrf}
    for name, value in original_cookies.items():
        client.set_cookie(_real_cookie_name(name), value, domain="console.example.test", path="/")
    return SimpleNamespace(
        f=f,
        services=services,
        actor=actor,
        source=source,
        source_reads=source_reads,
        token=token,
        client=client,
        app=app,
        csrf=csrf,
        original_cookies=original_cookies,
        production=production,
    )


def send(d, path, *, method="GET", client=None, **kwargs):
    headers = {"X-CSRF-Token": d.csrf} if path.startswith(MANAGEMENT) or method not in ("GET", "HEAD") else {}
    return (client or d.client).open(
        path,
        method=method,
        headers=headers,
        base_url=d.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
        **kwargs,
    )


def prepare(d):
    snapshot = d.services.casdoor_configuration.get(d.actor)
    response = send(
        d,
        MANAGEMENT + "/test-login",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )
    assert response.status_code == 200, response.json
    return snapshot, response


def begin(d):
    snapshot, prepared = prepare(d)
    assert prepared.json["status"] == "started"
    assert prepared.json["reason"] is None
    first = send(d, prepared.json["handoff"]["handoff_path"])
    assert first.status_code == 302, first.json
    state = parse_qs(urlsplit(first.location).query)["state"][0]
    return snapshot, state


def complete(d, state, **query):
    # The synthetic IdP wire signs the most recently selected authorization.
    # Real code redemption returns the nonce for that code, even out of order.
    selected = next((created for created in d.f.control.created if created.state == state), None)
    if selected is not None:
        d.f.control.created.remove(selected)
        d.f.control.created.append(selected)
    return send(d, auth.COOKIE_PATH + "/callback", query_string={"state": state, "code": "synthetic-code"} | query)


def assert_source_unchanged(d):
    for name, value in d.original_cookies.items():
        assert d.client.get_cookie(_real_cookie_name(name), domain="console.example.test").value == value
    assert not d.f.control.tokens and not d.f.control.billing


def rows(d):
    with d.f.service._session_factory() as session:
        return session.scalars(sa.select(CasdoorValidationExtend).order_by(CasdoorValidationExtend.checked_at)).all()


def test_mounted_diagnostic_without_manifest_binds_revision_and_activates(diagnostic):
    d = diagnostic
    d.production.authority.unlink()
    before = counts(d.f)
    snapshot, state = begin(d)
    assert {row.kind for row in rows(d)} == {
        CasdoorValidationKind.STATIC,
        CasdoorValidationKind.PROTOCOL,
        CasdoorValidationKind.DIAGNOSTIC,
    }
    assert all(row.revision_id == str(snapshot.draft_revision_id) for row in rows(d))
    assert all(row.proof_fingerprint is None for row in rows(d))
    assert all(
        row.status != CasdoorValidationStatus.PASSED
        for row in rows(d)
        if row.kind in (CasdoorValidationKind.PROTOCOL, CasdoorValidationKind.DIAGNOSTIC)
    )
    response = complete(d, state)
    assert response.status_code == 302, response.json
    assert response.location == d.f.settings.CONSOLE_WEB_URL + "/system-manage-extend/system-integration?tab=casdoor"
    assert len(response.headers.getlist("Set-Cookie")) == 2
    assert counts(d.f) == before
    assert_source_unchanged(d)
    assert len(d.f.control.created) == len(d.f.control.consumed) == 1
    requests = d.f.control.requests
    assert [path for path, _ in requests][-5:] == [
        "/api/login/oauth/access_token",
        "/api/userinfo",
        "/api/get-organization",
        "/api/get-user",
        "/api/get-roles",
    ]
    assert len({deadline for _, deadline in requests[1:]}) == 1
    assert d.source_reads and all(name == "refresh_token:" + d.token for name in d.source_reads)
    current = send(d, MANAGEMENT).json["draft"]
    assert [summary["status"] for summary in current["validation"]] == ["passed", "passed"]
    preview = current["diagnostic"]
    assert preview["effective_role_count"] == 1 and len(preview["targets"]) == 2
    assert not any(value in json.dumps(preview) for value in ("new@example.test", "person", d.token, "id_token"))
    for row in rows(d):
        assert row.expires_at <= row.checked_at + timedelta(minutes=15)
    activated = send(
        d,
        MANAGEMENT + "/activate",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )
    assert activated.status_code == 200 and activated.json["enabled"] is True
    assert_source_unchanged(d)


@pytest.mark.parametrize(
    "failure",
    ["refresh_revoked", "account_banned", "bad_nonce", "missing_token", "wrong_token", "unknown_role", "replay", "draft_changed"],
)
def test_callback_rejects_revoked_sources_context_nonce_and_replay(diagnostic, failure):
    d = diagnostic
    d.production.authority.unlink()
    snapshot, state = begin(d)
    before = counts(d.f)
    if failure == "refresh_revoked":
        d.source.clear()
    elif failure == "account_banned":
        with d.f.service._session_factory() as session, session.begin():
            session.get(Account, d.actor.id).status = AccountStatus.BANNED
    elif failure == "bad_nonce":
        d.f.control.bad_nonce = True
    elif failure == "missing_token":
        d.f.control.token_variant = "missing_id_token"
    elif failure == "wrong_token":
        d.f.control.token_variant = "wrong_issuer"
    elif failure == "unknown_role":
        d.f.control.bad_roles = True
    elif failure == "draft_changed":
        d.services.casdoor_configuration.save(d.actor, configuration=d.f.local.config, etag=snapshot.etag, secret=None)
    else:
        assert complete(d, state).status_code == 302
    result = complete(d, state)
    assert (result.status_code == 302) is (
        failure in {"bad_nonce", "missing_token", "wrong_token", "unknown_role"}
    )
    assert counts(d.f) == before
    assert_source_unchanged(d)
    if failure in {"bad_nonce", "missing_token", "wrong_token", "unknown_role"}:
        current = send(d, MANAGEMENT).json["draft"]
        assert current["validation"][0]["status"] == "failed"
        assert current["diagnostic"] is None
        failed_rows = rows(d)
        assert any(
            row.kind is CasdoorValidationKind.PROTOCOL and row.status is CasdoorValidationStatus.FAILED
            for row in failed_rows
        )
        assert any(
            row.kind is CasdoorValidationKind.DIAGNOSTIC and row.status is CasdoorValidationStatus.UNKNOWN
            for row in failed_rows
        )
        if failure == "unknown_role":
            assert "/api/get-roles" in [path for path, _ in d.f.control.requests]


def test_cancel_after_old_success_supersedes_rows_and_activation_remains_closed(diagnostic):
    d = diagnostic
    _, first = begin(d)
    assert complete(d, first).status_code == 302
    snapshot, second = begin(d)
    cancelled = send(d, auth.COOKIE_PATH + "/callback", query_string={"state": second, "error": "access_denied"})
    assert cancelled.status_code == 302
    denied = send(
        d,
        MANAGEMENT + "/activate",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )
    assert denied.status_code == 409
    assert_source_unchanged(d)


def test_ordinary_callback_does_not_open_diagnostic_runtime_or_double_limiter(diagnostic, monkeypatch):
    d = diagnostic
    monkeypatch.setattr(
        d.services.casdoor_diagnostic, "_run", lambda *a, **k: pytest.fail("diagnostic ingress for LOGIN")
    )
    first = send(d, auth.COOKIE_PATH + "/login")
    second = send(d, urlsplit(first.location).path, query_string=urlsplit(first.location).query)
    state = parse_qs(urlsplit(second.location).query)["state"][0]
    scopes = len(d.f.control.runtime_scopes)
    limiter_calls = len(d.f.control.limits)
    response = complete(d, state)
    assert response.status_code == 302, response.json
    assert len(d.f.control.runtime_scopes) == scopes + 1
    assert len(d.f.control.limits) == limiter_calls + 1


def test_two_diagnostic_transactions_preserve_auth_scope_and_both_complete(diagnostic):
    d = diagnostic
    _, first = begin(d)
    scope = d.client.get_cookie(auth.SCOPE_COOKIE_NAME, domain="console.example.test", path=auth.COOKIE_PATH).value
    _, second = begin(d)
    assert (
        d.client.get_cookie(auth.SCOPE_COOKIE_NAME, domain="console.example.test", path=auth.COOKIE_PATH).value == scope
    )
    assert complete(d, first).status_code == 302
    assert complete(d, second).status_code == 302
    assert (
        sum(
            row.kind is CasdoorValidationKind.DIAGNOSTIC and row.status is CasdoorValidationStatus.PASSED
            for row in rows(d)
        )
        == 2
    )
    assert_source_unchanged(d)


def test_ordinary_login_survives_a_later_diagnostic_handoff(diagnostic):
    d = diagnostic
    first = send(d, auth.COOKIE_PATH + "/login")
    ordinary = send(d, urlsplit(first.location).path, query_string=urlsplit(first.location).query)
    state = parse_qs(urlsplit(ordinary.location).query)["state"][0]
    _, diagnostic_state = begin(d)
    assert complete(d, diagnostic_state).status_code == 302
    assert complete(d, state).status_code == 302


def test_diagnostic_action_limit_charges_verified_source_once_without_manifest(diagnostic):
    d = diagnostic
    d.production.authority.unlink()
    _, response = prepare(d)
    assert response.json["status"] == "started"
    scopes = [key for key in d.f.control.limits if ":diagnostic:" in key]
    assert len(scopes) == 2
    assert sum(":ip:" in key for key in scopes) == 1
    assert sum(":account:" in key for key in scopes) == 1


@pytest.mark.parametrize("failure", ["refresh_revoked", "account_banned", "deadline"])
def test_final_writer_gap_rejects_source_changes_and_rolls_back_all_pass_rows(diagnostic, monkeypatch, failure):
    d = diagnostic
    _, state = begin(d)
    owner = d.services.casdoor_diagnostic
    original = owner._write
    previous_passes = sum(row.status is CasdoorValidationStatus.PASSED for row in rows(d))

    def write(draft, account, correlation, summaries, **kwargs):
        if (CasdoorValidationKind.PROTOCOL, CasdoorValidationStatus.PASSED) in summaries:
            if failure == "refresh_revoked":
                d.source.clear()
            elif failure == "account_banned":
                with d.f.service._session_factory() as session, session.begin():
                    session.get(Account, d.actor.id).status = AccountStatus.BANNED
            else:
                monkeypatch.setattr(time, "monotonic", lambda: kwargs["deadline"] + 1)
        return original(draft, account, correlation, summaries, **kwargs)

    monkeypatch.setattr(owner, "_write", write)
    response = complete(d, state)
    assert response.status_code != 302
    assert sum(row.status is CasdoorValidationStatus.PASSED for row in rows(d)) == previous_passes
    assert_source_unchanged(d)


def test_manifest_removal_at_activation_commit_does_not_block_enabled_revision(diagnostic, monkeypatch):
    d = diagnostic
    snapshot, state = begin(d)
    assert complete(d, state).status_code == 302
    original = CasdoorConfigurationRepository.activate

    def activate(owner, **kwargs):
        result = original(owner, **kwargs)
        d.production.authority.unlink()
        return result

    monkeypatch.setattr(CasdoorConfigurationRepository, "activate", activate)
    response = send(
        d,
        MANAGEMENT + "/activate",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )
    assert response.status_code == 200
    current = d.services.casdoor_configuration.get(d.actor)
    assert current.enabled is True and current.etag == snapshot.etag + 1


@pytest.mark.parametrize("failure", ["cross_browser", "expired", "init_replay"])
def test_browser_ownership_expiry_and_initialization_one_use(diagnostic, failure):
    d = diagnostic
    _, prepared = prepare(d)
    path = prepared.json["handoff"]["handoff_path"]
    first = send(d, path)
    assert first.status_code == 302
    state = parse_qs(urlsplit(first.location).query)["state"][0]
    passes = sum(row.status is CasdoorValidationStatus.PASSED for row in rows(d))
    if failure == "init_replay":
        assert send(d, path).status_code != 302
        assert len(d.f.control.created) == 1
    elif failure == "expired":
        d.f.init.now += 301
        assert complete(d, state).status_code != 302
    else:
        other = d.app.test_client()
        other.set_cookie(
            auth.diagnostic_cookie_name(state),
            "diagnostic",
            domain="console.example.test",
            path=auth.COOKIE_PATH + "/callback",
        )
        denied = send(
            d, auth.COOKIE_PATH + "/callback", client=other, query_string={"state": state, "code": "synthetic-code"}
        )
        assert denied.status_code != 302
        assert not d.f.control.consumed
    assert sum(row.status is CasdoorValidationStatus.PASSED for row in rows(d)) == passes
    assert_source_unchanged(d)


@pytest.mark.parametrize("failure", ["revoked", "get_failure", "get_deadline"])
def test_after_pass_row_flush_final_refresh_and_budget_barrier_rollback(diagnostic, monkeypatch, failure):
    d = diagnostic
    _, state = begin(d)
    previous_passes = sum(row.status is CasdoorValidationStatus.PASSED for row in rows(d))
    original = CasdoorValidationRepository.record
    read = d.f.redis.get
    triggered = False

    def record(writer, binding, **kwargs):
        nonlocal triggered
        result = original(writer, binding, **kwargs)
        if kwargs["kind"] is CasdoorValidationKind.PROTOCOL and kwargs["status"] is CasdoorValidationStatus.PASSED:
            triggered = True
            if failure == "revoked":
                d.source.clear()
        return result

    def get(name):
        if triggered and d.f.opened:
            if failure == "get_failure":
                raise TimeoutError("synthetic private source owner failure")
            if failure == "get_deadline":
                deadline = d.f.control.budgets[-1]
                monkeypatch.setattr(time, "monotonic", lambda: deadline + 1)
        return read(name)

    monkeypatch.setattr(CasdoorValidationRepository, "record", record)
    monkeypatch.setattr(d.f.redis, "get", get)
    assert complete(d, state).status_code != 302
    assert triggered
    assert sum(row.status is CasdoorValidationStatus.PASSED for row in rows(d)) == previous_passes
    assert_source_unchanged(d)


def test_revoked_source_cannot_write_static_or_pass(diagnostic):
    d = diagnostic
    d.source.clear()
    snapshot = d.services.casdoor_configuration.get(d.actor)
    response = send(
        d,
        MANAGEMENT + "/test-login",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )
    assert response.status_code == 400
    assert not rows(d) and not d.f.control.requests


def test_no_manifest_does_not_bypass_management_allowlist(diagnostic):
    d = diagnostic
    d.production.authority.unlink()
    snapshot = d.services.casdoor_configuration.get(d.actor)
    d.services.casdoor_configuration._management_policy = CasdoorManagementPolicy.from_deployment("")

    response = send(
        d,
        MANAGEMENT + "/test-login",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )

    assert response.status_code == 403
    assert response.json["code"] == "casdoor_management_forbidden"
    assert not d.f.control.runtime_scopes and not d.f.control.requests
    assert not rows(d) and not d.f.control.tokens and not d.f.control.billing


@pytest.mark.parametrize("failure", ["source_revoked", "refresh_read_failure"])
def test_pending_row_writer_gap_rolls_back_protocol_and_diagnostic_rows(diagnostic, monkeypatch, failure):
    d = diagnostic
    d.production.authority.unlink()
    original = CasdoorValidationRepository.record
    source_get = d.f.redis.get
    triggered = False

    def record(writer, binding, **kwargs):
        nonlocal triggered
        result = original(writer, binding, **kwargs)
        if (
            kwargs["kind"] is CasdoorValidationKind.PROTOCOL
            and kwargs["status"] is CasdoorValidationStatus.PENDING
        ):
            triggered = True
            if failure == "source_revoked":
                d.source.clear()
        return result

    def get(name):
        if triggered and failure == "refresh_read_failure":
            raise TimeoutError("synthetic private source read failure")
        return source_get(name)

    monkeypatch.setattr(CasdoorValidationRepository, "record", record)
    monkeypatch.setattr(d.f.redis, "get", get)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    response = send(
        d,
        MANAGEMENT + "/test-login",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )

    assert triggered
    assert response.status_code != 200
    persisted = rows(d)
    assert len(persisted) == 1
    assert persisted[0].kind is CasdoorValidationKind.STATIC
    assert persisted[0].status is CasdoorValidationStatus.PASSED
    assert not d.f.control.requests and not d.f.control.tokens and not d.f.control.billing
