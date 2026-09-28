from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import pytest
from pytest_mock import MockerFixture

from enums import DeploymentEdition
from machinery.context import RequestContext
from services.entities.feature_entities import (
    FeatureModel,
    LicenseLimitationModel,
    LicenseModel,
    LicenseStatus,
    LimitationModel,
    SystemFeatureModel,
    VectorSpaceLimitationModel,
)
from services.feature_query_service import FeatureQueryService


def _request_context() -> RequestContext:
    return RequestContext(
        request_id="request_123",
        trace_id=None,
        account_id="account_123",
        active_workspace_id="tenant_123",
    )


def _install_application_services(mocker: MockerFixture) -> MagicMock:
    feature_queries = create_autospec(FeatureQueryService, instance=True, spec_set=True)
    services = SimpleNamespace(feature_queries=feature_queries)
    mocker.patch("controllers.console.feature.application_services", return_value=services)
    return feature_queries


class TestFeatureApi:
    def test_get_tenant_features_success(self, mocker: MockerFixture) -> None:
        from controllers.console.feature import FeatureApi

        features = FeatureModel(
            apps=LimitationModel(size=3, limit=10),
            vector_space=LimitationModel(size=1, limit=2),
            next_credit_reset_date=1775001600,
        )
        feature_queries = _install_application_services(mocker)
        get_features = feature_queries.get_features
        get_features.return_value = features

        api = FeatureApi()

        raw_get = unwrap(FeatureApi.get)
        request_context = _request_context()
        result = raw_get(api, request_context)

        assert result == features.model_dump(mode="json")
        assert result["apps"] == {"size": 3, "limit": 10}
        retired_fields = {
            "vector_space",
            "next_credit_reset_date",
            "docs_processing",
            "knowledge_rate_limit",
            "dataset_operator_enabled",
        }
        schema = FeatureModel.model_json_schema(mode="serialization")
        assert retired_fields.isdisjoint(result)
        assert set(schema["properties"]) == set(schema["required"]) == set(result)
        get_features.assert_called_once_with(request_context)


class TestFeatureVectorSpaceApi:
    def test_get_vector_space_success(self, mocker: MockerFixture) -> None:
        from controllers.console.feature import FeatureVectorSpaceApi

        feature_queries = _install_application_services(mocker)
        get_vector_space = feature_queries.get_vector_space
        get_vector_space.return_value = VectorSpaceLimitationModel(size=5120, limit=20480)

        api = FeatureVectorSpaceApi()

        raw_get = unwrap(FeatureVectorSpaceApi.get)
        request_context = _request_context()
        result = raw_get(api, request_context)

        assert result == {"size": 5120, "limit": 20480}
        get_vector_space.assert_called_once_with(request_context)

    def test_get_vector_space_preserves_unknown_usage(self, mocker: MockerFixture) -> None:
        from controllers.console.feature import FeatureVectorSpaceApi

        feature_queries = _install_application_services(mocker)
        get_vector_space = feature_queries.get_vector_space
        get_vector_space.return_value = VectorSpaceLimitationModel(size=0, limit=50, usage_unknown=True)

        request_context = _request_context()
        result = unwrap(FeatureVectorSpaceApi.get)(FeatureVectorSpaceApi(), request_context)

        assert result == {"size": 0, "limit": 50, "usage_unknown": True}
        get_vector_space.assert_called_once_with(request_context)

    def test_vector_space_response_schema_marks_usage_unknown_optional(self) -> None:
        schema = VectorSpaceLimitationModel.model_json_schema(mode="serialization")

        assert schema["required"] == ["size", "limit"]
        assert schema["properties"]["usage_unknown"]["type"] == "boolean"
        assert "usage_unknown" not in schema["required"]


class TestTrialModelsApi:
    def test_get_trial_models_success(self, mocker: MockerFixture) -> None:
        from controllers.console.feature import TrialModelsApi

        feature_queries = _install_application_services(mocker)
        get_trial_models = feature_queries.get_trial_models
        get_trial_models.return_value = ["langgenius/openai/openai"]

        api = TrialModelsApi()

        raw_get = unwrap(TrialModelsApi.get)
        request_context = _request_context()
        result = raw_get(api, request_context)

        assert result == {"trial_models": ["langgenius/openai/openai"]}
        get_trial_models.assert_called_once_with(request_context)


class TestAppDslVersionApi:
    def test_get_app_dsl_version_success(self, mocker: MockerFixture) -> None:
        from controllers.console.feature import AppDslVersionApi

        feature_queries = _install_application_services(mocker)
        get_app_dsl_version = feature_queries.get_app_dsl_version
        get_app_dsl_version.return_value = "0.6.0"

        api = AppDslVersionApi()

        result = api.get()

        assert result == {"app_dsl_version": "0.6.0"}
        get_app_dsl_version.assert_called_once_with()


class TestSystemFeatureApi:
    def test_get_system_features_public(self, mocker: MockerFixture) -> None:
        """The public endpoint returns system features without any authentication input."""

        from controllers.console.feature import SystemFeatureApi

        system_features = SystemFeatureModel(
            deployment_edition=DeploymentEdition.COMMUNITY,
            is_allow_register=True,
            enable_learn_app=True,
        )
        feature_queries = _install_application_services(mocker)
        get_system_features = feature_queries.get_public_system_features
        get_system_features.return_value = system_features

        api = SystemFeatureApi()
        result = api.get()

        assert result == system_features.model_dump()
        assert result["is_allow_register"] is True
        assert result["enable_learn_app"] is True
        assert result["license"] == {"status": LicenseStatus.NONE}
        assert result["sso_enforced_for_signin_protocol"] is None
        assert result["webapp_auth"]["sso_config"]["protocol"] is None
        get_system_features.assert_called_once_with()


class TestSystemFeatureLicenseApi:
    def test_get_license_success(self, mocker: MockerFixture) -> None:
        from controllers.console.feature import SystemFeatureLicenseApi

        license_model = LicenseModel(
            status=LicenseStatus.ACTIVE,
            expired_at="2025-12-31",
            seats=LicenseLimitationModel(enabled=True, limit=5, size=2),
        )
        feature_queries = _install_application_services(mocker)
        get_license = feature_queries.get_license
        get_license.return_value = license_model

        api = SystemFeatureLicenseApi()
        raw_get = unwrap(SystemFeatureLicenseApi.get)
        result = raw_get(api, _request_context())

        assert result == license_model.model_dump()
        assert result["seats"] == {"enabled": True, "limit": 5, "size": 2}
        get_license.assert_called_once_with()


# Fork contract tests exercise the registered HTTP routes and their real admission wrappers.
@pytest.fixture
def feature_http(mocker: MockerFixture, config_overrides):
    from flask import Flask

    from controllers.console import bp
    from extensions.ext_login import login_manager

    config_overrides(SECRET_KEY="m02-test-secret-key-with-32-characters", LOGIN_DISABLED=False)
    app = Flask("m02-feature-http")
    app.config["TESTING"] = True
    app.secret_key = "m02-test-secret-key-with-32-characters"
    app.register_blueprint(bp)
    login_manager.init_app(app)
    mocker.patch("controllers.console.wraps._is_setup_completed", return_value=True)
    return app


class TestForkLoginConfig:
    def test_cookie_and_header_keep_public_license_shape(self, feature_http, mocker: MockerFixture, sqlite_session):
        from constants import COOKIE_NAME_LOGIN_CONFIG_TOKEN, HEADER_NAME_LOGIN_CONFIG_TOKEN
        from models.system_extend import SystemIntegrationExtend

        sqlite_session.add(SystemIntegrationExtend(id=1, classify=1, status=True, app_key="client", corp_id="corp"))
        sqlite_session.commit()
        queries = _install_application_services(mocker)
        queries.get_public_system_features.return_value = SystemFeatureModel(
            deployment_edition=DeploymentEdition.COMMUNITY,
            license=LicenseModel(status=LicenseStatus.ACTIVE, expired_at="2099-01-01"),
        )
        client = feature_http.test_client()
        bootstrap = client.get("/console/api/login_config_bootstrap")
        assert bootstrap.status_code == 200
        assert "HttpOnly" in bootstrap.headers["Set-Cookie"]
        assert "SameSite=Lax" in bootstrap.headers["Set-Cookie"]
        assert "Max-Age=3600" in bootstrap.headers["Set-Cookie"]
        token = bootstrap.json["token"]
        cookie_result = client.get("/console/api/login_config")
        assert cookie_result.status_code == 200
        assert cookie_result.json["ding_talk_client_id"] == "client"
        assert cookie_result.json["license"] == {"status": "active"}
        assert "branding" in cookie_result.json
        assert cookie_result.headers["Cache-Control"] == "no-store"
        client.delete_cookie(COOKIE_NAME_LOGIN_CONFIG_TOKEN)
        header_result = client.get("/console/api/login_config", headers={HEADER_NAME_LOGIN_CONFIG_TOKEN: token})
        assert header_result.json == cookie_result.json
        assert queries.get_license.call_count == 0

    @pytest.mark.parametrize("invalid", ["missing", "signature", "expired", "ip", "purpose", "no-exp"])
    def test_rejects_invalid_bootstrap(self, invalid, feature_http, mocker: MockerFixture):
        from datetime import UTC, datetime, timedelta

        import jwt

        from configs import dify_config
        from constants import HEADER_NAME_LOGIN_CONFIG_TOKEN

        payload = {"ip": "127.0.0.1", "exp": datetime.now(UTC) + timedelta(hours=1), "type": "login_config"}
        key = dify_config.SECRET_KEY
        if invalid == "expired":
            payload["exp"] = datetime.now(UTC) - timedelta(seconds=1)
        elif invalid == "signature":
            key = "wrong-secret-key-with-at-least-32-characters"
        elif invalid == "ip":
            payload["ip"] = "192.0.2.1"
        elif invalid == "purpose":
            payload["type"] = "access"
        elif invalid == "no-exp":
            payload.pop("exp")
        token = jwt.encode(payload, key, algorithm="HS256")
        queries = _install_application_services(mocker)
        headers = {} if invalid == "missing" else {HEADER_NAME_LOGIN_CONFIG_TOKEN: token}
        response = feature_http.test_client().get("/console/api/login_config", headers=headers)
        assert response.status_code == 403
        queries.get_public_system_features.assert_not_called()
        queries.get_license.assert_not_called()

    @pytest.mark.parametrize("bootstrap", ["none", "custom-header", "bearer", "access-cookie"])
    def test_anonymous_and_bootstrap_cannot_read_license(self, bootstrap, feature_http, mocker: MockerFixture):
        from constants import COOKIE_NAME_ACCESS_TOKEN, HEADER_NAME_LOGIN_CONFIG_TOKEN
        from libs.token import _real_cookie_name

        queries = _install_application_services(mocker)
        client = feature_http.test_client()
        headers = {}
        if bootstrap != "none":
            token = client.get("/console/api/login_config_bootstrap").json["token"]
            if bootstrap == "custom-header":
                headers[HEADER_NAME_LOGIN_CONFIG_TOKEN] = token
            elif bootstrap == "bearer":
                headers["Authorization"] = f"Bearer {token}"
            else:
                client.set_cookie(_real_cookie_name(COOKIE_NAME_ACCESS_TOKEN), token)
        assert client.get("/console/api/system-features/license", headers=headers).status_code == 401
        queries.get_license.assert_not_called()

    def test_admitted_account_gets_license_after_csrf(self, feature_http, mocker: MockerFixture):
        from werkzeug.exceptions import Unauthorized

        from models.account import Account, AccountStatus, Tenant

        account = Account(name="Owner", email="owner@example.com", status=AccountStatus.ACTIVE)
        account._current_tenant = Tenant(name="Workspace")
        mocker.patch("libs.login._resolve_current_user", return_value=account)
        csrf = mocker.patch("libs.login.check_csrf_token")
        queries = _install_application_services(mocker)
        queries.get_license.return_value = LicenseModel(status=LicenseStatus.ACTIVE, expired_at="2099-01-01")
        client = feature_http.test_client()
        response = client.get("/console/api/system-features/license")
        assert response.status_code == 200
        assert response.json["expired_at"] == "2099-01-01"
        csrf.assert_called_once()
        queries.get_license.assert_called_once()
        queries.get_license.reset_mock()
        csrf.side_effect = Unauthorized()
        assert client.get("/console/api/system-features/license").status_code == 401
        queries.get_license.assert_not_called()

    @pytest.mark.parametrize("role", ["owner", "admin", "editor", "normal"])
    @pytest.mark.parametrize("super_account", [False, True])
    def test_summary_identity_flags_follow_account_and_workspace(
        self, role, super_account, feature_http, mocker: MockerFixture, sqlite_session, config_overrides
    ):
        from datetime import datetime

        from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole

        config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
        first = Account(name="First", email="first@example.com", status=AccountStatus.ACTIVE)
        ordinary = Account(name="Other", email="other@example.com", status=AccountStatus.ACTIVE)
        first.created_at, ordinary.created_at = datetime(2025, 1, 1), datetime(2025, 1, 2)
        tenants = [Tenant(name="First workspace"), Tenant(name="Other workspace")]
        tenants[0].created_at, tenants[1].created_at = datetime(2025, 1, 1), datetime(2025, 1, 2)
        actor = first if super_account else ordinary
        sqlite_session.add_all([first, ordinary, *tenants])
        sqlite_session.add_all(
            TenantAccountJoin(tenant_id=tenant.id, account_id=actor.id, role=TenantAccountRole(role))
            for tenant in tenants
        )
        sqlite_session.commit()
        mocker.patch("libs.login._resolve_current_user", return_value=actor)
        mocker.patch("libs.login.check_csrf_token")
        client = feature_http.test_client()
        for index, tenant in enumerate(tenants):
            actor.set_current_tenant_with_session(tenant, session=sqlite_session)
            response = client.get("/console/api/workspaces/current/summary")
            assert response.status_code == 200
            assert response.json == {
                "id": tenant.id,
                "name": tenant.name,
                "role": role,
                "plan": None,
                "credits": None,
                "admin_extend": super_account,
                "tenant_extend": index == 0,
            }
            assert actor.role == role

    def test_public_route_and_fork_routes_are_registered_once(self, feature_http, mocker: MockerFixture):
        queries = _install_application_services(mocker)
        queries.get_public_system_features.return_value = SystemFeatureModel(
            deployment_edition=DeploymentEdition.COMMUNITY
        )
        response = feature_http.test_client().get("/console/api/system-features")
        assert response.status_code == 200
        assert "branding" in response.json
        assert "ping" not in response.json
        assert response.json["license"] == {"status": "none"}
        paths = [rule.rule for rule in feature_http.url_map.iter_rules() if "GET" in rule.methods]
        for route in (
            "system-features",
            "system-features/license",
            "login_config",
            "login_config_bootstrap",
            "account/money",
        ):
            assert paths.count(f"/console/api/{route}") == 1
        all_paths = [rule.rule for rule in feature_http.url_map.iter_rules()]
        for route in (
            "extend/<path:path>",
            "apps/<uuid:app_id>/sync",
            "installed/apps",
            "message/context",
            "ding-talk/login",
            "ding-talk/third-party/login",
            "system-manage-extend/integration/dingtalk",
            "system-manage-extend/integration/dingtalk/test",
            "system-manage-extend/integration/dingtalk/test-callback",
            "system-manage-extend/integration/oauth2",
            "system-manage-extend/integration/oauth2/test",
            "system-manage-extend/integration/email-api/test",
            "system-manage-extend/forward-tokens",
            "system-manage-extend/forward-tokens/<int:seq>",
            "system-manage-extend/quota-management",
            "system-manage-extend/quota-management/set",
            "system-manage-extend/code-execution-control",
            "system-manage-extend/code-execution-control/<string:record_id>",
        ):
            assert all_paths.count(f"/console/api/{route}") == 1
        queries.get_license.assert_not_called()
