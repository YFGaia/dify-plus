"""Mounted native self GET, real SQL avatar producers and registered response DTO."""

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from constants import COOKIE_NAME_CSRF_TOKEN, HEADER_NAME_CSRF_TOKEN
from controllers.console import bp, console_ns, wraps
from controllers.console.workspace.casdoor_identity_extend import CasdoorSelfIdentityStatusResponse
from flask import Flask, Response, g
from libs.token import generate_csrf_token
from models.account import Account, Tenant, TenantAccountRole
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from pydantic import ValidationError
from repositories.casdoor_self_identity_repository_extend import CasdoorSelfIdentityRepository
from services.casdoor_self_identity_service_extend import CasdoorSelfIdentityService
from services.entities.feature_entities import LicenseStatus
from services.system_feature_service import SystemFeatureService
from sqlalchemy.orm import Session, sessionmaker
from test_casdoor_avatar_attachment_extend import attach, attachment
from test_casdoor_avatar_consumer_extend import consumer, run
from test_casdoor_avatar_intent_extend import URL, pending, storage_fixture
from test_casdoor_avatar_intent_extend import avatar as avatar_fixture
from test_casdoor_avatar_pre_storage_recovery_extend import produce
from test_casdoor_profile_repository_extend import NOW

avatar_fixture = avatar_fixture
storage_fixture = storage_fixture
attachment = attachment
consumer = consumer
PATH = "/console/api/account/casdoor-identity"


def mounted(s, monkeypatch, *, now=NOW):
    """Use original native admission/CSRF, with only user/session and license seams."""
    with Session(s.session.get_bind(), expire_on_commit=False) as reader:
        account = reader.get(Account, s.account.id)
        tenant = reader.get(Tenant, s.revision.default_workspace_id)
    account._current_tenant = tenant
    account.role = TenantAccountRole.NORMAL
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME="console.example.test", SECRET_KEY="synthetic-flask-only")
    state = {"account": account}
    app.login_manager = SimpleNamespace(
        load_user_from_request_context=lambda: None,
        unauthorized=lambda: Response(status=401),
    )

    @app.before_request
    def original_user_seam():
        g._login_user = state["account"]

    service = CasdoorSelfIdentityService(
        session_factory=sessionmaker(s.session.get_bind()), rbac_enabled=False, now=lambda: now
    )
    app.extensions["application_services"] = SimpleNamespace(casdoor_self_identity=service)
    app.register_blueprint(bp)
    for key, value in {
        "SECRET_KEY": "synthetic-csrf-only-key-32bytes-minimum",
        "LOGIN_DISABLED": False,
        "ADMIN_API_KEY_ENABLE": False,
        "CONSOLE_WEB_URL": "http://console.example.test",
        "CONSOLE_API_URL": "http://console.example.test",
        "COOKIE_DOMAIN": "",
    }.items():
        monkeypatch.setattr(dify_config, key, value)
    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    monkeypatch.setattr(SystemFeatureService, "get_license_status", lambda: LicenseStatus.ACTIVE)
    client = app.test_client()
    token = generate_csrf_token(account.id)
    client.set_cookie(COOKIE_NAME_CSRF_TOKEN, token, domain="console.example.test")
    return SimpleNamespace(app=app, client=client, headers={HEADER_NAME_CSRF_TOKEN: token}, state=state)


def response(http, query=""):
    result = http.client.get(PATH + query, headers=http.headers)
    assert result.status_code == 200, result.json
    assert result.headers["Cache-Control"] == "no-store"
    assert {"Cookie", "Authorization"} <= set(result.vary)
    CasdoorSelfIdentityStatusResponse.model_validate(result.json)
    encoded = result.get_data(as_text=True)
    for private in (
        URL,
        "SyntheticSubject",
        "url_ciphertext",
        "storage_key",
        "proof_ref",
        "raw_claims",
        "encrypted_secret",
    ):
        assert private not in encoded
    return result


def test_actual_registered_get_reports_db_attachment_time_not_physical_sync(attachment, monkeypatch):
    s = attachment
    attach(s)
    http = mounted(s, monkeypatch)
    result = response(http)
    value = result.json["identities"][0]
    assert value["avatar_status"] == "local_attachment_recorded"
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")
    assert value["avatar_last_reason"] is None and value["avatar_consistency"] == "current"
    assert value["avatar_current_local_differs_from_last_applied"] is False
    assert value["masked_identifier"] == "********"
    assert "synced" not in result.get_data(as_text=True)
    assert not s.io_calls


def test_public_t3_get_uses_exact_closed_v2_evidence(consumer, monkeypatch):
    s = consumer
    produce(s)
    http = mounted(s, monkeypatch)
    value = response(http).json["identities"][0]
    assert value["avatar_status"] == "failed_before_storage"
    assert value["avatar_last_reason"] == "fetch_failed"
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")
    with s.session.begin():
        s.session.execute(sa.delete(Audit).where(Audit.action == "avatar_pre_storage"))
    value = response(http).json["identities"][0]
    assert value["avatar_status"] == "unknown" and value["avatar_recorded_at"] is None


def test_actual_ambiguous_transport_failure_public_get_stays_unknown(consumer, monkeypatch):
    s = consumer
    s.http(mode="error")
    assert run(s).code == "unknown"
    value = response(mounted(s, monkeypatch)).json["identities"][0]
    assert value["avatar_status"] == "unknown" and value["avatar_last_reason"] == "fetch_failed"


def test_public_record_is_historical_after_identity_generation_change(attachment, monkeypatch):
    s = attachment
    attach(s)
    with s.session.begin():
        s.session.execute(sa.update(Identity).values(sync_generation=2))
    value = response(mounted(s, monkeypatch)).json["identities"][0]
    assert value["avatar_status"] == value["avatar_consistency"] == "historical"
    assert value["avatar_recorded_generation"] == 1


def test_public_source_expiry_uses_injected_clock_and_existing_self_scope(avatar_fixture, monkeypatch):
    s = avatar_fixture
    with s.session.begin():
        pending(s)
    http = mounted(s, monkeypatch, now=NOW + timedelta(seconds=300))
    assert response(http).json["identities"][0]["avatar_status"] == "source_expired"
    denied = http.client.get(PATH + "?account_id=" + str(uuid4()), headers=http.headers)
    assert denied.status_code == 400 and denied.json == {"code": "casdoor_self_invalid_query"}
    anonymous = http.client.get(PATH)
    assert anonymous.status_code == 401 and anonymous.headers["Cache-Control"] == "no-store"


def test_public_get_uses_one_reader_session_and_all_avatar_queries_recheck(consumer, monkeypatch):
    s = consumer
    http = mounted(s, monkeypatch)
    sessions = []
    original = CasdoorSelfIdentityRepository.recheck

    def observe_recheck(reader):
        sessions.append(reader.session)
        statements = [str(statement).lower() for statement, _ in reader.observations]
        assert any("casdoor_sync_intent_extend" in statement for statement in statements)
        assert any("accounts.name" in statement for statement in statements)
        assert not reader.session.autoflush
        assert not reader.session.new and not reader.session.dirty and not reader.session.deleted
        original(reader)

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "recheck", observe_recheck)
    assert response(http).json["identities"][0]["avatar_status"] == "pending"
    assert len(sessions) == 1


def test_registered_dto_and_native_schema_reject_success_or_sensitive_extra(consumer, monkeypatch):
    http = mounted(consumer, monkeypatch)
    value = response(http).json
    for field, invalid in (("avatar_status", "synced"), ("avatar_last_reason", "raw_provider_reason")):
        modified = json.loads(json.dumps(value))
        modified["identities"][0][field] = invalid
        with pytest.raises(ValidationError):
            CasdoorSelfIdentityStatusResponse.model_validate(modified)
    modified = json.loads(json.dumps(value))
    modified["identities"][0]["raw_claims"] = {"picture": URL}
    with pytest.raises(ValidationError):
        CasdoorSelfIdentityStatusResponse.model_validate(modified)
    assert "CasdoorSelfIdentityStatusResponse" in console_ns.models
    schema = CasdoorSelfIdentityStatusResponse.model_json_schema()["$defs"]["CasdoorSelfIdentityResponse"]
    assert "local_attachment_recorded" in schema["properties"]["avatar_status"]["enum"]
    assert "synced" not in schema["properties"]["avatar_status"]["enum"]
    assert "avatar_recorded_at" in schema["required"]
    assert "physical storage" in schema["properties"]["avatar_recorded_at"]["description"]
    with http.app.test_request_context():
        from controllers.console import api

        operation = api.__schema__["paths"]["/account/casdoor-identity"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "CasdoorSelfIdentityStatusResponse"
    )


def test_foreign_account_intent_never_becomes_self_status(consumer, monkeypatch):
    s = consumer
    with s.session.begin():
        s.session.execute(sa.update(Intent).values(account_id=str(uuid4())))
    assert response(mounted(s, monkeypatch)).json["identities"][0]["avatar_status"] == "no_record"
