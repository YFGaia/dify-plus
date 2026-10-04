"""Independent offline checks for the Casdoor management API composition."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from configs import dify_config
from constants import COOKIE_NAME_CSRF_TOKEN, HEADER_NAME_CSRF_TOKEN
from controllers.console import bp
from controllers.console import casdoor_config_extend as controller
from core.casdoor.permissions import CasdoorManagementPolicy
from enums import DeploymentEdition
from extensions import ext_application_services
from flask import Flask, g
from libs.token import generate_csrf_token
from models.account import Account, AccountStatus, Tenant, TenantAccountRole
from models.base import Base
from models.casdoor_extend import CasdoorIntegrationExtend
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from sqlalchemy.orm import sessionmaker

ROOT = "/console/api" + controller.PREFIX
ACTOR_ID = "20000000-0000-4000-8000-000000000011"
OTHER_ID = "20000000-0000-4000-8000-000000000012"


@pytest.fixture
def setup_api(monkeypatch):
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Tenant.__table__, CasdoorIntegrationExtend.__table__])
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = Tenant(name="Independent synthetic workspace")
        session.add(workspace)
        session.flush()
    actor = Account(name="Independent synthetic actor", email="actor@example.test")
    actor.id = ACTOR_ID
    actor.status = AccountStatus.ACTIVE
    actor.role = TenantAccountRole.NORMAL
    owner = CasdoorConfigurationService(
        session_factory=factory,
        management_policy=CasdoorManagementPolicy.from_deployment(ACTOR_ID),
        secret_key="independent synthetic deployment key",
        rbac_enabled=False,
    )
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME="independent.example.test")
    app.login_manager = SimpleNamespace(
        load_user_from_request_context=lambda: None,
        unauthorized=lambda: ({"code": "unauthorized"}, 401),
    )
    state = {"account": actor}

    @app.before_request
    def inject_account():
        g._login_user = state["account"]

    @app.get("/unrelated")
    def unrelated():
        return "ok", 200, {"Cache-Control": "private, max-age=17"}

    app.extensions["application_services"] = SimpleNamespace(casdoor_configuration=owner)
    app.register_blueprint(bp)
    for key, value in {
        "SECRET_KEY": "independent synthetic csrf signing key",
        "LOGIN_DISABLED": False,
        "ADMIN_API_KEY_ENABLE": False,
        "CONSOLE_WEB_URL": "http://independent.example.test",
        "CONSOLE_API_URL": "http://independent.example.test",
        "COOKIE_DOMAIN": "",
    }.items():
        monkeypatch.setattr(dify_config, key, value)
    client = app.test_client()
    csrf = generate_csrf_token(ACTOR_ID)
    client.set_cookie(COOKIE_NAME_CSRF_TOKEN, csrf, domain="independent.example.test")
    yield SimpleNamespace(
        app=app,
        client=client,
        actor=actor,
        state=state,
        owner=owner,
        factory=factory,
        workspace_id=workspace.id,
        csrf=csrf,
        headers={HEADER_NAME_CSRF_TOKEN: csrf},
    )
    engine.dispose()


def call(harness, method="GET", suffix="", *, headers=None):
    return harness.client.open(
        ROOT + suffix,
        method=method,
        headers=harness.headers if headers is None else headers,
        content_type="application/json",
        json={},
    )


def test_real_route_guard_rejects_anonymous_and_invalid_csrf(setup_api):
    harness = setup_api
    harness.state["account"] = None
    anonymous = call(harness)
    assert anonymous.status_code == 401
    assert anonymous.headers["Cache-Control"] == "no-store"

    harness.state["account"] = harness.actor
    invalid = call(harness, "POST", "/validate", headers={HEADER_NAME_CSRF_TOKEN: "synthetic-invalid"})
    assert invalid.status_code == 401
    assert invalid.json["code"] == "unauthorized"
    assert invalid.headers["Cache-Control"] == "no-store"
    with harness.factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0


@pytest.mark.parametrize(
    ("role", "allowlist"),
    [
        (TenantAccountRole.NORMAL, ""),
        (TenantAccountRole.NORMAL, "malformed"),
        (TenantAccountRole.OWNER, ""),
        (TenantAccountRole.OWNER, "malformed"),
        (TenantAccountRole.ADMIN, OTHER_ID),
    ],
)
def test_actual_service_allowlist_denies_empty_invalid_or_nonmatching_account(setup_api, role, allowlist):
    harness = setup_api
    harness.actor.role = role
    harness.owner._management_policy = CasdoorManagementPolicy.from_deployment(allowlist)
    denied = call(harness, "GET", "/workspaces")
    assert denied.status_code == 403
    assert denied.json["code"] == "casdoor_management_forbidden"
    assert denied.headers["Cache-Control"] == "no-store"

    harness.owner._management_policy = CasdoorManagementPolicy.from_deployment(ACTOR_ID)
    allowed = call(harness, "GET", "/workspaces")
    assert allowed.status_code == 200
    assert allowed.json["total"] == 1


def test_routing_failure_is_uncached_and_regular_route_keeps_its_policy(setup_api):
    harness = setup_api
    response = call(harness, "DELETE")
    assert response.status_code == 405
    assert response.headers["Cache-Control"] == "no-store"
    ordinary = harness.client.get("/unrelated")
    assert ordinary.headers["Cache-Control"] == "private, max-age=17"


@pytest.mark.parametrize("rbac_enabled", [False, True])
def test_actual_factory_wires_deployment_allowlist_and_disables_unproven_activation(
    setup_api, monkeypatch, rbac_enabled
):
    harness = setup_api
    monkeypatch.setattr(dify_config, "CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS", ACTOR_ID)
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", rbac_enabled)
    with patch.object(ext_application_services.SystemFeatureService, "is_trial_app_enabled", return_value=False):
        registry = ext_application_services.build_application_services(
            database_client=harness.factory,
            deployment_edition=DeploymentEdition.COMMUNITY,
            initialization_password="",
            redis=MagicMock(),
        )
    service = registry.casdoor_configuration
    assert service._session_factory is harness.factory
    assert service.can_manage(harness.actor)
    harness.app.extensions["application_services"] = registry
    assert call(harness, "GET", "/workspaces").status_code == 200
    with harness.factory() as session:
        repo = service._repository(session)
        assert repo.rbac_mode == ("on" if rbac_enabled else "off")
        assert repo.deployment_proof_fingerprint is None
        assert repo.crypto.key_version == "v1"


@pytest.mark.parametrize(
    ("role", "allowlist"),
    [
        (TenantAccountRole.NORMAL, ""),
        (TenantAccountRole.NORMAL, "malformed"),
        (TenantAccountRole.OWNER, OTHER_ID),
        (TenantAccountRole.ADMIN, OTHER_ID),
    ],
)
def test_factory_enforces_empty_or_nonmatching_allowlist_against_every_workspace_role(
    setup_api, monkeypatch, role, allowlist
):
    harness = setup_api
    harness.actor.role = role
    monkeypatch.setattr(dify_config, "CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS", allowlist)
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    with patch.object(ext_application_services.SystemFeatureService, "is_trial_app_enabled", return_value=False):
        registry = ext_application_services.build_application_services(
            database_client=harness.factory,
            deployment_edition=DeploymentEdition.COMMUNITY,
            initialization_password="",
            redis=MagicMock(),
        )
    harness.app.extensions["application_services"] = registry
    response = call(harness, "GET", "/workspaces")
    assert response.status_code == 403
    assert response.json["code"] == "casdoor_management_forbidden"


def test_openapi_uses_real_registered_models_and_query_parameters(setup_api):
    harness = setup_api
    harness.app.config["RESTX_INCLUDE_ALL_MODELS"] = True
    response = harness.client.get("/console/api/openapi.json")
    assert response.status_code == 200
    document = response.json
    operation = document["paths"][controller.PREFIX + "/workspaces"]["get"]
    assert {item["name"] for item in operation["parameters"]} == {"page", "limit"}
    assert all(item["in"] == "query" for item in operation["parameters"])
    assert "requestBody" not in operation
    payload = document["components"]["schemas"]["CasdoorSaveConfigurationPayload"]
    assert payload["properties"]["secret"]["anyOf"][0]["writeOnly"] is True
    config_operation = document["paths"][controller.PREFIX]["get"]
    response_schema = config_operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["$ref"].endswith("/CasdoorConfigurationResponse")
    write_operation = document["paths"][controller.PREFIX]["put"]
    payload_ref = write_operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert payload_ref.endswith("/CasdoorSaveConfigurationPayload")
    properties = document["components"]["schemas"]["SystemFeatureModel"]["properties"]
    assert all("casdoor" not in field.lower() for field in properties)


def test_invalid_request_details_are_hidden_and_uncached(setup_api):
    harness = setup_api
    response = call(harness, "PUT")
    assert response.status_code == 400
    assert response.json["code"] == "invalid_transaction"
    assert set(response.json) == {"code", "message", "correlation_id"}
    assert response.headers["Cache-Control"] == "no-store"
    serialized = response.get_data(as_text=True)
    for field in ('"loc"', '"input"', '"ctx"'):
        assert field not in serialized
