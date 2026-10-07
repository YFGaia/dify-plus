"""Actual Console/factory/SQLite display checks; synthetic active rows are not auth proof."""

import re
from dataclasses import FrozenInstanceError, dataclass, fields
from io import BytesIO
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from flask import Flask, Request
from pydantic import SecretStr
from sqlalchemy.orm import Session

from configs import dify_config
from controllers.console import bp
from controllers.console.auth import casdoor_display_extend as controller
from core.casdoor.crypto import CasdoorCrypto, CryptoError
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.request_safety import format_public_error
from enums import DeploymentEdition
from extensions import ext_application_services
from models.account import Tenant
from models.casdoor_extend import CasdoorConfigRevisionExtend, CasdoorIntegrationExtend, CasdoorNamespaceExtend
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError, _DisplayMetadata
from tests.unit_tests.services import test_casdoor_configuration_service_extend as fixtures

PATH = "/console/api/auth/casdoor/display"
MISSING = "30000000-0000-4000-8000-000000000099"


@dataclass
class Harness:
    app: Flask
    client: object
    registry: ext_application_services.ApplicationServices
    factory: object
    workspace_id: str
    actor: object
    redis: MagicMock

    @property
    def owner(self):
        return self.registry.casdoor_configuration


@pytest.fixture
def database():
    yield from fixtures.database.__wrapped__()


@pytest.fixture(scope="module")
def certificate():
    return fixtures.certificate.__wrapped__()


@pytest.fixture
def harness(database, monkeypatch):
    factory, workspace_id = database
    actor = fixtures.actor.__wrapped__()
    monkeypatch.setattr(dify_config, "SECRET_KEY", "synthetic-display-deployment-key")
    monkeypatch.setattr(dify_config, "CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS", actor.id)
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    redis = MagicMock()
    with patch.object(ext_application_services.SystemFeatureService, "is_trial_app_enabled", return_value=False):
        registry = ext_application_services.build_application_services(
            database_client=factory,
            deployment_edition=DeploymentEdition.COMMUNITY,
            initialization_password="",
            redis=redis,
        )
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME="display.example.test")
    app.extensions["application_services"] = registry
    app.register_blueprint(bp)
    app.login_manager = MagicMock()
    app.login_manager.load_user_from_request_context.side_effect = AssertionError("unexpected authentication")
    app.login_manager.unauthorized.side_effect = AssertionError("unexpected authentication")
    redis.reset_mock()
    yield Harness(app, app.test_client(), registry, factory, workspace_id, actor, redis)
    assert not redis.mock_calls
    assert not app.login_manager.mock_calls


def seed(harness, certificate, *, active=True, button="Casdoor"):
    configuration = fixtures.foundation.config(harness.workspace_id, certificate, button_text=button)
    saved = harness.owner.save(
        harness.actor, configuration=configuration, etag=0, secret=SecretStr("synthetic-display-private-secret")
    )
    if active:
        # Manually select valid metadata, without producing readiness/activation proof.
        with harness.factory.begin() as session:
            integration = session.scalar(sa.select(CasdoorIntegrationExtend))
            integration.enabled = True
            integration.active_revision_id = str(saved.draft_revision_id)
            integration.draft_revision_id = None
    return str(saved.draft_revision_id)


def headers(response):
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "Set-Cookie" not in response.headers


def success(response, *, enabled=False, button="Casdoor"):
    assert response.status_code == 200
    assert response.json == {
        "enabled": enabled,
        "button_text": button,
        "start_path": "/console/api/auth/casdoor/login",
    }
    headers(response)


def error_response(response, code, status):
    assert response.status_code == status
    assert set(response.json) == {"code", "correlation_id", "retry_allowed"}
    assert response.json["code"] == code
    assert response.json["retry_allowed"] is False
    assert UUID(response.json["correlation_id"]).version == 4
    assert "synthetic" not in response.get_data(as_text=True)
    headers(response)


def forbid(*args, **kwargs):
    raise AssertionError("unexpected side effect")


@pytest.fixture
def readonly(harness, monkeypatch):
    statements = []

    def trace(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT")
        tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)", statement, re.IGNORECASE))
        assert tables <= {
            CasdoorIntegrationExtend.__tablename__,
            CasdoorConfigRevisionExtend.__tablename__,
            CasdoorNamespaceExtend.__tablename__,
        }

    engine = harness.factory.kw["bind"]
    sa.event.listen(engine, "before_cursor_execute", trace)
    for name in ("flush", "commit"):
        monkeypatch.setattr(Session, name, forbid)
    monkeypatch.setattr(CasdoorCrypto, "encrypt", forbid)
    monkeypatch.setattr(CasdoorCrypto, "decrypt", forbid)
    monkeypatch.setattr(SecretStr, "get_secret_value", forbid)
    monkeypatch.setattr(harness.owner, "require_management", forbid)
    try:
        yield statements
    finally:
        sa.event.remove(engine, "before_cursor_execute", trace)


@pytest.mark.parametrize("state", ["unconfigured", "disabled", "draft-only"])
def test_unconfigured_disabled_and_draft_only(harness, certificate, state, request):
    if state == "draft-only":
        seed(harness, certificate, active=False)
    elif state == "disabled":
        with harness.factory.begin() as session:
            session.add(CasdoorIntegrationExtend(enabled=False, active_revision_id=MISSING, draft_revision_id=MISSING))
    statements = request.getfixturevalue("readonly")
    success(harness.client.get(PATH))
    assert len(statements) == 1


@pytest.mark.parametrize("button", ["Casdoor", "企业统一登录", "<strong>Sign in</strong>"])
def test_legal_active_exact_plain_display(harness, certificate, button, request):
    seed(harness, certificate, button=button)
    statements = request.getfixturevalue("readonly")
    success(harness.client.get(PATH), enabled=True, button=button)
    assert statements


@pytest.mark.parametrize("bad_draft", ["missing-pointer", "json", "owner", "digest"])
def test_bad_draft_does_not_change_active_display(harness, certificate, bad_draft, request):
    active_id = seed(harness, certificate)
    if bad_draft == "missing-pointer":
        draft_id = MISSING
    else:
        saved = harness.owner.save(
            harness.actor,
            configuration=fixtures.foundation.config(harness.workspace_id, certificate, button_text="Draft only"),
            etag=1,
            secret=SecretStr("synthetic-replacement-secret"),
        )
        draft_id = str(saved.draft_revision_id)
    with harness.factory.begin() as session:
        integration = session.scalar(sa.select(CasdoorIntegrationExtend))
        assert integration.active_revision_id == active_id
        integration.draft_revision_id = draft_id
        if bad_draft != "missing-pointer":
            if bad_draft == "json":
                values = {"policy_json": "{synthetic-invalid"}
            elif bad_draft == "owner":
                values = {"integration_id": MISSING}
            else:
                values = {"config_digest": "0" * 64}
            # Test-only storage corruption; production revisions stay immutable.
            session.execute(
                sa.update(CasdoorConfigRevisionExtend)
                .where(CasdoorConfigRevisionExtend.id == draft_id)
                .values(**values)
            )
    statements = request.getfixturevalue("readonly")
    success(harness.client.get(PATH), enabled=True)
    assert all(draft_id not in statement for statement in statements)


@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [
        ("active-missing", 409, "config_conflict"),
        ("active-pointer-missing", 409, "config_conflict"),
        ("revision-owner", 409, "config_conflict"),
        ("namespace-missing", 409, "config_conflict"),
        ("namespace-owner", 409, "config_conflict"),
        ("namespace-core", 409, "config_conflict"),
        ("namespace-fingerprint", 409, "config_conflict"),
        ("digest", 409, "config_conflict"),
        ("policy-shape", 409, "config_conflict"),
        ("policy-json", 503, "provider_unavailable"),
        ("mappings-json", 503, "provider_unavailable"),
        ("certificates-json", 503, "provider_unavailable"),
        ("button", 503, "provider_unavailable"),
        ("fencing", 409, "config_conflict"),
        ("archived", 409, "config_conflict"),
    ],
)
def test_invalid_active_metadata_fails_safely(harness, certificate, fault, status, code, request, caplog):
    revision_id = seed(harness, certificate)
    with harness.factory.begin() as session:
        integration = session.scalar(sa.select(CasdoorIntegrationExtend))
        revision = session.get(CasdoorConfigRevisionExtend, revision_id)
        namespace = session.get(CasdoorNamespaceExtend, revision.namespace_id)
        revision_values = {}
        if fault == "active-missing":
            integration.active_revision_id = None
        elif fault == "active-pointer-missing":
            integration.active_revision_id = MISSING
        elif fault == "revision-owner":
            revision_values = {"integration_id": MISSING}
        elif fault == "namespace-missing":
            revision_values = {"namespace_id": MISSING}
        elif fault == "namespace-owner":
            namespace.integration_id = MISSING
        elif fault == "namespace-core":
            namespace.client_id = "synthetic-wrong-client"
        elif fault == "namespace-fingerprint":
            namespace.core_fingerprint = "0" * 64
        elif fault == "digest":
            revision_values = {"config_digest": "0" * 64}
        elif fault == "policy-shape":
            revision_values = {"policy_json": "{}"}
        elif fault.endswith("-json"):
            revision_values = {fault.replace("-", "_"): "{synthetic-private-invalid-json"}
        elif fault == "button":
            revision_values = {"button_text": "x" * 121}
        else:
            namespace.lifecycle = fault
        if revision_values:
            session.execute(
                sa.update(CasdoorConfigRevisionExtend)
                .where(CasdoorConfigRevisionExtend.id == revision_id)
                .values(**revision_values)
            )
    request.getfixturevalue("readonly")
    first = harness.client.get(PATH)
    second = harness.client.get(PATH)
    error_response(first, code, status)
    error_response(second, code, status)
    assert first.json["correlation_id"] != second.json["correlation_id"]
    assert not caplog.records


@pytest.mark.parametrize("query", ["?enabled=true", "?token=synthetic-private", "?x=1&x=2", "?x", "?=secret"])
def test_query_rejected_before_service(harness, query, readonly, monkeypatch):
    monkeypatch.setattr(harness.owner, "display", forbid)
    error_response(harness.client.get(PATH + query), "invalid_transaction", 400)
    assert readonly == []


@pytest.mark.parametrize("body", [b"x", b"{}", b"synthetic-private-body", b"x" * 8192])
@pytest.mark.parametrize("content_type", ["application/json", "application/octet-stream"])
def test_body_rejected_before_service(harness, body, content_type, readonly, monkeypatch):
    monkeypatch.setattr(harness.owner, "display", forbid)
    error_response(harness.client.get(PATH, data=body, content_type=content_type), "invalid_transaction", 400)
    assert readonly == []


def test_chunked_body_is_bounded_and_rejected(harness, readonly, monkeypatch):
    monkeypatch.setattr(harness.owner, "display", forbid)
    stream = BytesIO(b"synthetic-chunked-body")
    response = harness.client.get(
        PATH, environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True, "wsgi.input": stream}
    )
    error_response(response, "invalid_transaction", 400)
    assert stream.tell() <= 1
    assert readonly == []


def test_body_stream_error_is_private(harness, readonly, monkeypatch, caplog):
    def failure(*args, **kwargs):
        raise RuntimeError("synthetic-private-stream-failure")

    monkeypatch.setattr(Request, "get_data", failure)
    monkeypatch.setattr(harness.owner, "display", forbid)
    error_response(harness.client.get(PATH), "invalid_transaction", 400)
    assert readonly == []
    assert not caplog.records


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT"])
def test_method_matrix_never_runs_get(harness, method, readonly, monkeypatch):
    monkeypatch.setattr(harness.owner, "display", forbid)
    response = harness.client.open(PATH, method=method)
    assert response.status_code == (204 if method == "OPTIONS" else 405)
    if method in ("HEAD", "OPTIONS"):
        assert response.data == b""
    headers(response)
    assert readonly == []


@pytest.mark.parametrize("failure", ["empty-key", "sql"])
def test_original_key_and_sql_errors_are_safe(harness, failure, readonly, monkeypatch, caplog):
    if failure == "empty-key":
        monkeypatch.setattr(harness.owner, "_secret_key", "")
    else:

        def driver_failure(*args, **kwargs):
            raise sa.exc.OperationalError("synthetic-sql-secret", {}, RuntimeError("synthetic-driver-private"))

        monkeypatch.setattr(Session, "scalar", driver_failure)
    expected = ("invalid_transaction", 400) if failure == "empty-key" else ("provider_unavailable", 503)
    error_response(harness.client.get(PATH), *expected)
    assert not caplog.records


def test_serialization_error_uses_original_safe_formatter(harness, readonly, monkeypatch, caplog):
    original_dump = controller.dump_response

    def fault(model, value):
        if model is controller.CasdoorDisplayResponse:
            raise ValueError("synthetic-private-serialization")
        return original_dump(model, value)

    monkeypatch.setattr(controller, "dump_response", fault)
    error_response(harness.client.get(PATH), "provider_unavailable", 503)
    assert not caplog.records


def test_exact_known_error_mapping_and_unknown_properties_are_never_inspected(harness, readonly, monkeypatch, caplog):
    class HostileError(Exception):
        @property
        def code(self):
            raise AssertionError("error property accessed")

        def __str__(self):
            raise AssertionError("error string accessed")

        def __repr__(self):
            raise AssertionError("error repr accessed")

    for fault in (
        CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "synthetic-private-reason"),
        CryptoError("casdoor_crypto_version_unsupported"),
        HostileError(),
    ):

        def fail(*args, error=fault, **kwargs):
            raise error

        monkeypatch.setattr(Session, "scalar", fail)
        expected = format_public_error(fault.code if type(fault) is CasdoorConfigurationError else fault)
        error_response(harness.client.get(PATH), expected.code.value, expected.status)
    assert not caplog.records


def test_projection_frozen_private_and_pending_work_not_flushed(harness, certificate, monkeypatch):
    seed(harness, certificate)
    with harness.factory() as session:
        pending = Tenant(name="synthetic-pending-unrelated")
        session.add(pending)
        monkeypatch.setattr(session, "flush", forbid)
        projection = harness.owner._repository(session).display()
        assert type(projection) is _DisplayMetadata
        assert {field.name for field in fields(projection)} == {"enabled", "button_text"}
        assert "enabled" not in repr(projection)
        with pytest.raises(FrozenInstanceError):
            projection.button_text = "changed"
        assert pending in session.new


def test_service_owns_independent_closed_read_sessions(harness, readonly):
    sessions = []

    def opened(session, transaction, connection):
        sessions.append(session)

    sa.event.listen(Session, "after_begin", opened)
    try:
        success(harness.client.get(PATH))
        success(harness.client.get(PATH))
    finally:
        sa.event.remove(Session, "after_begin", opened)
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert all(not session.in_transaction() for session in sessions)


def test_actual_openapi_public_get_original_response_refs(harness):
    response = harness.client.get("/console/api/openapi.json")
    assert response.status_code == 200
    document = response.json
    operation = document["paths"]["/auth/casdoor/display"]["get"]
    assert operation["security"] == []
    assert "requestBody" not in operation
    assert operation.get("parameters", []) == []
    expected = {"200": "CasdoorDisplayResponse", "400": "CasdoorResultResponse", "409": "CasdoorResultResponse"}
    for status, model in expected.items():
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/" + model}
    schema = document["components"]["schemas"]["CasdoorDisplayResponse"]
    assert set(schema["properties"]) == {"enabled", "button_text", "start_path"}
    assert schema["properties"]["start_path"]["const"] == "/console/api/auth/casdoor/login"
    assert not any(
        "casdoor" in key.lower() for key in document["components"]["schemas"]["SystemFeatureModel"]["properties"]
    )
