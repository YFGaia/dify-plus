"""Real Console export excludes non-business methods without changing GET contracts."""

import pytest
from flask_restx.swagger import Swagger


@pytest.fixture(scope="module")
def console_openapi():
    from configs import dify_config
    from libs.flask_restx_compat import finalize_openapi_payload

    from dev.generate_swagger_specs import create_spec_app

    # The exporter's settings are temporary and never initialize backend services.
    with pytest.MonkeyPatch.context() as patch:
        for name in ("SECRET_KEY", "STORAGE_TYPE", "STORAGE_LOCAL_PATH", "SWAGGER_UI_ENABLED"):
            patch.setattr(dify_config, name, getattr(dify_config, name))
        patch.setenv("SECRET_KEY", "spec-export")
        patch.setenv("STORAGE_TYPE", "local")
        patch.setenv("STORAGE_LOCAL_PATH", "/tmp/dify-storage")
        app = create_spec_app()
        from controllers.console import api

        with app.test_request_context("/console/api/openapi.json"):
            payload = Swagger(api).as_dict()
        yield finalize_openapi_payload(payload)


@pytest.mark.parametrize("route", ["login", "callback", "result"])
def test_non_business_methods_are_absent_from_real_export(console_openapi, route):
    operation = console_openapi["paths"][f"/auth/casdoor/{route}"]
    assert "get" in operation
    assert "head" not in operation
    assert "options" not in operation


@pytest.mark.parametrize(
    ("route", "operation_id", "statuses", "success_model", "queries"),
    [
        (
            "login",
            "get_casdoor_login_api",
            {"302", "303", "400", "409", "429", "503"},
            None,
            {
                "init": (False, {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}),
                "locale": (False, {"type": "string", "maxLength": 64}),
                "return_path": (False, {"type": "string", "maxLength": 2048}),
                "timezone": (False, {"type": "string", "maxLength": 64}),
            },
        ),
        (
            "callback",
            "get_casdoor_callback_api",
            {"302", "400", "403", "409", "429", "503"},
            None,
            {
                "code": (False, {"type": "string", "minLength": 1, "maxLength": 4096}),
                "error": (False, {"type": "string", "minLength": 1, "maxLength": 255}),
                "error_description": (False, {"type": "string", "maxLength": 4096}),
                "state": (True, {"type": "string", "minLength": 1, "maxLength": 512}),
            },
        ),
        (
            "result",
            "get_casdoor_restricted_result_api",
            {"200", "400", "403", "409", "429", "503"},
            "CasdoorRestrictedResultResponse",
            {
                "handoff": (
                    True,
                    {"type": "string", "minLength": 43, "maxLength": 43, "pattern": "^[A-Za-z0-9_-]{43}$"},
                ),
            },
        ),
        (
            "display",
            "get_casdoor_display_api",
            {"200", "400", "409", "503"},
            "CasdoorDisplayResponse",
            {},
        ),
    ],
    ids=["login", "callback", "result", "display"],
)
def test_get_contracts_keep_status_models_and_query_constraints(
    console_openapi, route, operation_id, statuses, success_model, queries
):
    operation = console_openapi["paths"][f"/auth/casdoor/{route}"]["get"]
    assert operation["operationId"] == operation_id
    assert operation["security"] == []
    assert "requestBody" not in operation
    assert set(operation["responses"]) == statuses
    parameters = operation.get("parameters", [])
    assert len(parameters) == len(queries)
    assert {parameter["name"] for parameter in parameters} == set(queries)
    for parameter in parameters:
        required, schema = queries[parameter["name"]]
        assert parameter["in"] == "query"
        assert parameter["required"] is required
        assert parameter["schema"] == schema
    for status, response in operation["responses"].items():
        if status in {"302", "303"}:
            assert "content" not in response
        else:
            model = success_model if status == "200" else "CasdoorResultResponse"
            assert response["content"] == {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}},
            }


def test_restricted_result_success_schema_stays_closed(console_openapi):
    schema = console_openapi["components"]["schemas"]["CasdoorRestrictedResultResponse"]
    properties = schema["properties"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(properties) == {"code", "correlation_id", "retry_allowed"}
    assert properties["code"]["type"] == "string"
    assert set(properties["code"]["enum"]) == {
        "authorization_pending",
        "role_snapshot_unknown",
        "workspace_unavailable",
    }
    assert properties["correlation_id"]["type"] == "string"
    assert properties["correlation_id"]["format"] == "uuid"
    assert properties["retry_allowed"]["type"] == "boolean"
    assert properties["retry_allowed"]["const"] is False


def test_display_options_hidden_keeps_head_and_minimal_get_contract(console_openapi):
    path = console_openapi["paths"]["/auth/casdoor/display"]
    assert "options" not in path
    for method, statuses in (("head", {"405"}), ("get", {"200", "400", "409", "503"})):
        operation = path[method]
        assert operation["operationId"] == f"{method}_casdoor_display_api"
        assert operation["security"] == []
        assert operation.get("parameters", []) == []
        assert "requestBody" not in operation
        assert set(operation["responses"]) == statuses
        for status, response in operation["responses"].items():
            model = "CasdoorDisplayResponse" if status == "200" else "CasdoorResultResponse"
            assert response["content"] == {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}},
            }

    schema = console_openapi["components"]["schemas"]["CasdoorDisplayResponse"]
    properties = schema["properties"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(properties) == {"enabled", "button_text", "start_path"}
    assert schema.get("required", []) == []
    assert properties["enabled"]["type"] == "boolean"
    assert properties["enabled"]["default"] is False
    assert properties["button_text"]["type"] == "string"
    assert properties["button_text"]["default"] == "Casdoor"
    assert properties["button_text"]["minLength"] == 1
    assert properties["button_text"]["maxLength"] == 120
    assert properties["button_text"]["x-max-utf8-bytes"] == 255
    assert properties["start_path"]["type"] == "string"
    assert properties["start_path"]["const"] == "/console/api/auth/casdoor/login"
    assert properties["start_path"]["default"] == "/console/api/auth/casdoor/login"
