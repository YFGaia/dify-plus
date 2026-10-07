"""Actual Flask routes/login decorator/CSRF with synthetic JWTs and SQLite only."""

from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from configs import dify_config
from constants import COOKIE_NAME_CSRF_TOKEN, HEADER_NAME_CSRF_TOKEN
from controllers.console import bp
from controllers.console import casdoor_config_extend as controller
from enums import DeploymentEdition
from extensions import ext_application_services
from flask import Flask, g, jsonify
from libs.passport import PassportService
from libs.token import generate_csrf_token
from models.account import Tenant, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import CasdoorIntegrationExtend, CasdoorValidationExtend

from tests.unit_tests.services import test_casdoor_configuration_service_extend as fixtures

PREFIX = "/console/api" + controller.PREFIX


@pytest.fixture
def database():
    yield from fixtures.database.__wrapped__()


@pytest.fixture
def actor(database):
    return fixtures.actor.__wrapped__(database)


@pytest.fixture(scope="module")
def certificate():
    return fixtures.certificate.__wrapped__()


@pytest.fixture
def harness(database, actor, monkeypatch):
    factory, workspace_id = database
    owner = fixtures.service(factory)
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME="console.example.test")
    app.login_manager = SimpleNamespace(
        load_user_from_request_context=lambda: None,
        unauthorized=lambda: (jsonify({"code": "unauthorized"}), 401),
    )
    state = {"account": actor}

    @app.before_request
    def account_context():
        g._login_user = state["account"]

    app.extensions["application_services"] = SimpleNamespace(casdoor_configuration=owner)
    app.register_blueprint(bp)
    for key, value in {
        "SECRET_KEY": "synthetic-i24a-csrf-key-32bytes-minimum",
        "LOGIN_DISABLED": False,
        "ADMIN_API_KEY_ENABLE": False,
        "CONSOLE_WEB_URL": "http://console.example.test",
        "CONSOLE_API_URL": "http://console.example.test",
        "COOKIE_DOMAIN": "",
    }.items():
        monkeypatch.setattr(dify_config, key, value)
    client = app.test_client()
    token = generate_csrf_token(actor.id)
    client.set_cookie(COOKIE_NAME_CSRF_TOKEN, token, domain="console.example.test")
    return SimpleNamespace(
        app=app,
        client=client,
        owner=owner,
        actor=actor,
        state=state,
        token=token,
        headers={HEADER_NAME_CSRF_TOKEN: token},
        workspace_id=workspace_id,
        factory=factory,
    )


def request(harness, method, suffix="", *, payload=None, headers=None):
    return harness.client.open(
        PREFIX + suffix,
        method=method,
        json=payload,
        content_type="application/json",
        headers=harness.headers if headers is None else headers,
    )


def set_membership_role(harness, role):
    with harness.factory.begin() as session:
        membership = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == harness.actor.id,
                TenantAccountJoin.tenant_id == harness.workspace_id,
            )
        )
        membership.role = role


def automatic_configuration(workspace_id):
    configuration = fixtures.automatic_foundation.automatic(workspace_id).model_dump(mode="json")
    configuration.pop("certificates")
    configuration.pop("signing_key_mode")
    return configuration


METHODS = [
    ("GET", "/permissions"),
    ("GET", ""),
    ("PUT", ""),
    ("POST", "/clear-secret"),
    ("POST", "/disable"),
    ("POST", "/activate"),
    ("POST", "/test-login"),
    ("POST", "/test-rp-logout"),
    ("POST", "/test-reauth"),
    ("GET", "/rp-logout-status"),
    ("POST", "/validate"),
    ("GET", "/workspaces"),
]
PRIVILEGED_METHODS = METHODS[1:]


@pytest.mark.parametrize(("method", "suffix"), METHODS)
def test_anonymous_actual_decorator_unauthorized_no_store(harness, method, suffix):
    harness.state["account"] = None
    response = request(harness, method, suffix, payload={})
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(("method", "suffix"), METHODS)
@pytest.mark.parametrize("invalid", ["missing", "mismatch", "wrong-account", "expired", "bad-signature"])
def test_real_csrf_owner_rejects_every_management_method(harness, method, suffix, invalid):
    headers = harness.headers.copy()
    if invalid == "missing":
        headers = {}
    elif invalid == "mismatch":
        headers[HEADER_NAME_CSRF_TOKEN] = "synthetic-mismatch"
    else:
        payload = {"sub": harness.actor.id, "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())}
        if invalid == "wrong-account":
            payload["sub"] = "20000000-0000-4000-8000-000000000002"
        if invalid == "expired":
            payload["exp"] = 1
        token = PassportService().issue(payload) if invalid != "bad-signature" else "synthetic-invalid-signature"
        harness.client.set_cookie(COOKIE_NAME_CSRF_TOKEN, token, domain="console.example.test")
        headers[HEADER_NAME_CSRF_TOKEN] = token
    response = request(harness, method, suffix, payload={}, headers=headers)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json["code"] == "unauthorized"
    with harness.factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0


@pytest.mark.parametrize(("method", "suffix"), PRIVILEGED_METHODS)
@pytest.mark.parametrize(
    "role", [TenantAccountRole.NORMAL, TenantAccountRole.EDITOR, TenantAccountRole.DATASET_OPERATOR]
)
@pytest.mark.parametrize("rbac_enabled", [False, True])
def test_privileged_guard_denies_non_manager_before_payload_or_side_effect(
    harness, monkeypatch, method, suffix, role, rbac_enabled
):
    harness.actor.role = role
    set_membership_role(harness, role)
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", rbac_enabled)
    monkeypatch.setattr(dify_config, "CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS", harness.actor.id)
    response = request(harness, method, suffix, payload={"synthetic-private-extra-field": "synthetic-value"})
    assert response.status_code == 403
    assert response.json["code"] == "casdoor_management_forbidden"
    assert response.headers["Cache-Control"] == "no-store"
    assert "synthetic" not in response.get_data(as_text=True)
    with harness.factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0


def test_permission_is_bool_only_and_tracks_current_workspace_role(harness):
    response = request(harness, "GET", "/permissions")
    assert response.json == {"can_manage_casdoor": True}
    assert response.headers["Cache-Control"] == "no-store"
    response = request(harness, "GET")
    assert response.status_code == 200
    assert response.json["etag"] == 0
    harness.actor.role = TenantAccountRole.NORMAL
    set_membership_role(harness, TenantAccountRole.NORMAL)
    assert request(harness, "GET", "/permissions").json == {"can_manage_casdoor": False}
    denied = request(harness, "GET")
    assert denied.status_code == 403
    assert denied.json["code"] == "casdoor_management_forbidden"


def test_http_missing_workspace_binds_current_account_workspace(harness):
    harness.actor._current_tenant = type("WorkspaceRef", (), {"id": harness.workspace_id})()
    configuration = automatic_configuration(harness.workspace_id)
    configuration.pop("default_workspace_id")
    saved = request(harness, "PUT", payload={"etag": 0, "configuration": configuration})
    assert saved.status_code == 200
    resolved = saved.json["draft"]["configuration"]
    assert resolved["default_workspace_id"] == harness.workspace_id
    loaded = request(harness, "GET")
    assert loaded.status_code == 200
    assert loaded.json["draft"]["configuration"]["default_workspace_id"] == harness.workspace_id


def test_http_missing_workspace_rejects_account_without_current_workspace(harness):
    configuration = automatic_configuration(harness.workspace_id)
    configuration.pop("default_workspace_id")
    harness.actor._current_tenant = None
    assert harness.actor.current_tenant_id is None
    failed = request(harness, "PUT", payload={"etag": 0, "configuration": configuration})
    assert failed.status_code == 400
    assert failed.json["code"] == "workspace_unavailable"
    assert failed.headers["Cache-Control"] == "no-store"
    assert request(harness, "GET").json["draft"] is None


def test_http_basic_save_returns_complete_resolved_configuration(harness):
    configuration = automatic_configuration(harness.workspace_id)
    configuration.pop("backend_api_url")
    configuration.pop("expected_issuer")
    saved = request(harness, "PUT", payload={"etag": 0, "configuration": configuration})
    assert saved.status_code == 200
    resolved = saved.json["draft"]["configuration"]
    assert resolved["backend_api_url"] == configuration["browser_frontend_url"]
    assert resolved["expected_issuer"] == configuration["browser_frontend_url"]
    assert resolved["certificates"] == []
    assert resolved["signing_key_mode"] == "automatic"
    loaded = request(harness, "GET")
    assert loaded.status_code == 200
    assert loaded.json["draft"]["configuration"] == resolved


def test_http_save_blank_keep_clear_static_and_safe_response(harness, monkeypatch):
    monkeypatch.setattr(harness.owner, "validate_static", partial(harness.owner.validate_static, now=fixtures.NOW))
    configuration = automatic_configuration(harness.workspace_id)
    secret = "synthetic-http-client-secret-private"
    saved = request(harness, "PUT", payload={"etag": 0, "configuration": configuration, "secret": secret})
    assert saved.status_code == 200
    first = saved.json
    assert first["draft"]["secret_configured"]
    assert first["draft"]["validation"] == []
    assert not first["enabled"]
    assert first["active"] is None
    assert secret not in saved.get_data(as_text=True)
    assert "encrypted_secret" not in saved.get_data(as_text=True)
    validated = request(harness, "POST", "/validate", payload={"etag": 1, "revision_id": first["draft_revision_id"]})
    assert validated.status_code == 200
    assert validated.json["kind"] == "static"
    assert validated.json["static_only"] is True
    assert validated.json["checked_at"].endswith("Z")
    assert validated.json["revision_id"] == first["draft_revision_id"]
    assert validated.json["certificate_summaries"] == []
    second = request(harness, "PUT", payload={"etag": 1, "configuration": configuration, "secret": ""})
    assert second.status_code == 200
    assert second.json["draft"]["secret_configured"]
    stale = request(harness, "POST", "/validate", payload={"etag": 2, "revision_id": first["draft_revision_id"]})
    assert stale.status_code == 409
    cleared = request(
        harness, "POST", "/clear-secret", payload={"etag": 2, "revision_id": second.json["draft_revision_id"]}
    )
    assert cleared.status_code == 200
    assert not cleared.json["draft"]["secret_configured"]
    assert cleared.json["etag"] == 3
    invalid = request(
        harness, "POST", "/validate", payload={"etag": 3, "revision_id": cleared.json["draft_revision_id"]}
    )
    assert invalid.status_code == 409
    assert invalid.json["code"] == "config_conflict"
    for response in [saved, validated, second, stale, cleared, invalid]:
        assert response.headers["Cache-Control"] == "no-store"
    with harness.factory() as session:
        checks = session.scalars(sa.select(CasdoorValidationExtend)).all()
        assert {(row.revision_id, row.kind, row.status) for row in checks} == {
            (first["draft_revision_id"], "static", "passed"),
            (cleared.json["draft_revision_id"], "static", "failed"),
        }
        assert len(checks) == 2


def test_http_disable_uses_csrf_guard_and_returns_namespace_reconciliation_state(harness):
    configuration = automatic_configuration(harness.workspace_id)
    saved = request(
        harness,
        "PUT",
        payload={"etag": 0, "configuration": configuration, "secret": "synthetic-disable-secret"},
    )
    assert saved.status_code == 200
    disabled = request(harness, "POST", "/disable", payload={"etag": saved.json["etag"]})
    assert disabled.status_code == 200
    assert disabled.json["configuration"]["enabled"] is False
    assert disabled.json["configuration"]["etag"] == saved.json["etag"] + 1
    assert disabled.json["reconciliation_required"] is False
    assert "synthetic-disable-secret" not in disabled.get_data(as_text=True)
    assert disabled.headers["Cache-Control"] == "no-store"


def test_activate_requires_current_diagnostic_and_diagnostic_requires_actual_source_access(
    harness, monkeypatch
):
    configuration = automatic_configuration(harness.workspace_id)
    saved = request(
        harness,
        "PUT",
        payload={"etag": 0, "configuration": configuration, "secret": "synthetic-activate-secret"},
    )
    assert saved.status_code == 200
    monkeypatch.setattr(harness.owner, "activate", partial(harness.owner.activate, now=fixtures.NOW))
    body = {"etag": saved.json["etag"], "revision_id": saved.json["draft_revision_id"]}

    activated = request(harness, "POST", "/activate", payload=body)
    tested = request(harness, "POST", "/test-login", payload=body)
    assert activated.status_code == 409
    assert activated.json["code"] == "config_conflict"
    # Missing actual current-revision validation still blocks activation after
    # removing the external signed-manifest gate; internal reasons stay private.
    assert activated.json["reason"] is None
    # A CSRF-authenticated management stub without request access/refresh is
    # insufficient for the independently browser-bound DIAGNOSTIC transaction.
    assert tested.status_code == 400
    assert tested.json["code"] == "invalid_transaction"
    for response in (activated, tested):
        assert response.headers["Cache-Control"] == "no-store"
        assert "synthetic-activate-secret" not in response.get_data(as_text=True)
    with harness.factory() as session:
        integration = session.scalar(sa.select(CasdoorIntegrationExtend))
        assert integration is not None
        assert integration.enabled is False
        assert integration.active_revision_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {"etag": 0, "configuration": {}, "secret": "synthetic-input-secret"},
        {"synthetic-secret-as-extra-key": "synthetic-private-value"},
        {"etag": 0, "configuration": {"expected_issuer": "synthetic-sensitive-provider-url"}},
        None,
    ],
)
def test_validation_error_privacy_and_no_store(harness, payload):
    response = request(harness, "PUT", payload=payload)
    assert response.status_code == 400
    assert response.json["code"] == "invalid_transaction"
    assert set(response.json) == {"code", "reason", "message", "correlation_id"}
    assert response.headers["Cache-Control"] == "no-store"
    text = response.get_data(as_text=True)
    assert "synthetic" not in text
    assert "input" not in text
    assert "ctx" not in text
    assert "loc" not in text


@pytest.mark.parametrize("query", ["?page=0", "?limit=101", "?page=abc", "?private=synthetic-private-value"])
def test_workspace_query_validation_private_values_omitted(harness, query):
    response = request(harness, "GET", "/workspaces" + query)
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert "synthetic-private-value" not in response.get_data(as_text=True)


def test_workspace_http_serialization_created_at_pagination_metadata(harness):
    response = request(harness, "GET", "/workspaces?page=1&limit=100")
    assert response.status_code == 200
    assert response.json["total"] == 1
    assert response.json["limit"] == 100
    created = response.json["workspaces"][0]["created_at"]
    assert created.endswith("Z")
    assert datetime.fromisoformat(created).utcoffset() == timedelta(0)
    assert response.json["earliest_created_workspace"]["created_at"] == created
    assert response.json["earliest_created_workspace"]["workspace_id"] == harness.workspace_id
    assert not response.json["earliest_created_ambiguous"]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("offset", [None, UTC, timezone(timedelta(hours=8))])
def test_response_status_times_use_utc_iso_contract(offset):
    from uuid import UUID

    from controllers.console.casdoor_schemas_extend import CasdoorValidationSummaryResponse

    value = datetime(2026, 10, 6, 8, 30, tzinfo=offset)
    response = CasdoorValidationSummaryResponse(
        kind="validation",
        status="passed",
        revision_id=UUID(int=1),
        checked_at=value,
        expires_at=value + timedelta(minutes=15),
    ).model_dump(mode="json")
    expected = value.replace(tzinfo=UTC) if offset is None else value.astimezone(UTC)
    assert response["checked_at"].endswith("Z")
    assert datetime.fromisoformat(response["checked_at"]) == expected
    assert datetime.fromisoformat(response["expires_at"]) == expected + timedelta(minutes=15)


@pytest.mark.parametrize("rbac_enabled", [False, True])
@pytest.mark.parametrize("legacy_admin_ids", ["", "malformed", "20000000-0000-4000-8000-000000000002"])
def test_factory_uses_current_workspace_role_and_ignores_deprecated_allowlist(
    harness, monkeypatch, rbac_enabled, legacy_admin_ids
):
    harness.actor.role = TenantAccountRole.ADMIN
    monkeypatch.setattr(dify_config, "CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS", legacy_admin_ids)
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", rbac_enabled)
    with patch.object(ext_application_services.SystemFeatureService, "is_trial_app_enabled", return_value=False):
        services = ext_application_services.build_application_services(
            database_client=harness.factory,
            deployment_edition=DeploymentEdition.COMMUNITY,
            initialization_password="",
            redis=MagicMock(),
        )
    owner = services.casdoor_configuration
    assert owner._session_factory is harness.factory
    assert owner.can_manage(harness.actor)
    with harness.factory() as session:
        repository = owner._repository(session)
        assert repository.rbac_mode == ("on" if rbac_enabled else "off")
        assert repository.deployment_proof_fingerprint is None
        assert repository.crypto.key_version == "v1"


@pytest.mark.usefixtures("harness")
def test_swagger_registered_actual_pydantic_models_and_query_docs():
    namespace = controller.console_ns
    schema = namespace.models["CasdoorSaveConfigurationPayload"]._schema
    assert schema["properties"]["secret"]["anyOf"][0]["writeOnly"] is True
    response_schema = namespace.models["CasdoorStaticValidationResponse"]._schema
    assert response_schema["properties"]["static_only"]["const"] is True
    query_docs = controller.CasdoorWorkspacesApi.get.__apidoc__["params"]
    assert set(query_docs) == {"page", "limit"}
    assert all(value["in"] == "query" for value in query_docs.values())
    assert "expect" not in controller.CasdoorWorkspacesApi.get.__apidoc__


def test_actual_openapi_endpoint_documents_management_operations(harness):
    harness.app.config["RESTX_INCLUDE_ALL_MODELS"] = True
    response = harness.client.get("/console/api/openapi.json")
    assert response.status_code == 200
    document = response.json
    assert document["openapi"] == "3.1.0"
    paths = document["paths"]
    for method, suffix in METHODS:
        operation = paths[controller.PREFIX + suffix][method.lower()]
        if suffix == "/test-login":
            assert operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].startswith(
                "#/components/schemas/Casdoor"
            )
        else:
            assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].startswith(
                "#/components/schemas/Casdoor"
            )
        if method == "GET":
            assert "requestBody" not in operation
    disable_schema = document["components"]["schemas"]["CasdoorDisableResponse"]
    assert set(disable_schema["properties"]) == {"configuration", "reconciliation_required"}
    query = paths[controller.PREFIX + "/workspaces"]["get"]["parameters"]
    assert {parameter["name"] for parameter in query} == {"page", "limit"}
    assert all(parameter["in"] == "query" for parameter in query)
    public_features = document["components"]["schemas"]["SystemFeatureModel"]["properties"]
    assert not any("casdoor" in field.lower() for field in public_features)


def test_unexpected_error_boundary_omits_driver_values_and_cache(harness, monkeypatch):
    def private_driver_failure(_account):
        raise RuntimeError("synthetic-driver-secret-input")

    monkeypatch.setattr(harness.owner, "get", private_driver_failure)
    response = request(harness, "GET")
    assert response.status_code == 500
    assert response.json["code"] == "internal_server_error"
    assert response.headers["Cache-Control"] == "no-store"
    assert "synthetic-driver" not in response.get_data(as_text=True)


def test_routing_errors_remain_no_store(harness):
    response = request(harness, "DELETE")
    assert response.status_code == 405
    assert response.headers["Cache-Control"] == "no-store"


def test_cache_hook_preserves_other_route_cache_policy(harness):
    harness.app.add_url_rule(
        "/ordinary-route",
        view_func=lambda: ("ordinary", 200, {"Cache-Control": "private, max-age=30"}),
    )
    response = harness.client.get("/ordinary-route")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, max-age=30"
