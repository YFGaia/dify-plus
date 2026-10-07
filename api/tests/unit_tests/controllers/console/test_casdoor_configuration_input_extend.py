"""New management writes always create certificate-free automatic revisions."""

from uuid import UUID

import pytest
from controllers.console.casdoor_configuration_input_extend import CasdoorConfigurationInput
from controllers.console.casdoor_schemas_extend import CasdoorSaveConfigurationPayload
from core.casdoor.configuration import CasdoorConfiguration
from pydantic import ValidationError


def basic_configuration():
    return {
        "browser_frontend_url": "https://synthetic.example.test/",
        "organization": "SyntheticOrg",
        "application": "SyntheticApp",
        "client_id": "SyntheticClient",
        "default_workspace_id": str(UUID("10000000-0000-4000-8000-000000000001")),
    }


def test_basic_payload_resolves_complete_automatic_policy():
    payload = CasdoorSaveConfigurationPayload.model_validate({"etag": 0, "configuration": basic_configuration()})
    configuration = payload.configuration.to_configuration()
    assert type(configuration) is CasdoorConfiguration
    assert configuration.schema_version == 2
    assert configuration.signing_key_mode == "automatic"
    assert configuration.backend_api_url == "https://synthetic.example.test"
    assert configuration.expected_issuer == "https://synthetic.example.test"
    assert configuration.certificates == ()
    assert configuration.workspace_mappings == ()
    assert configuration.default_normal_fallback is True


def test_explicit_endpoints_are_preserved():
    values = basic_configuration()
    values.update(backend_api_url="https://internal.example.test/", expected_issuer="https://issuer.example.test/")
    configuration = CasdoorConfigurationInput.model_validate(values).to_configuration()
    assert configuration.backend_api_url == values["backend_api_url"]
    assert configuration.expected_issuer == values["expected_issuer"]


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": 1},
        {"certificates": []},
        {"certificates": [{"pem": "anything"}]},
        {"signing_key_mode": "automatic"},
    ],
)
def test_input_cannot_select_legacy_or_manual_key_policy(updates):
    with pytest.raises(ValidationError):
        CasdoorConfigurationInput.model_validate(basic_configuration() | updates)


def test_domain_and_response_contracts_stay_complete():
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(basic_configuration())
    schema = CasdoorSaveConfigurationPayload.model_json_schema()
    input_schema = schema["$defs"]["CasdoorConfigurationInput"]
    assert "backend_api_url" not in input_schema["required"]
    assert "expected_issuer" not in input_schema["required"]
    assert "default_workspace_id" not in input_schema["required"]
    assert "certificates" not in input_schema["properties"]
    assert "signing_key_mode" not in input_schema["properties"]
    assert input_schema["properties"]["schema_version"]["const"] == 2
    assert "default_workspace_id" in CasdoorConfiguration.model_json_schema()["required"]


@pytest.mark.parametrize(("field", "value"), [("schema_version", True), ("default_normal_fallback", 1)])
def test_input_keeps_domain_strict_scalar_rules(field, value):
    values = basic_configuration()
    values[field] = value
    with pytest.raises(ValidationError):
        CasdoorConfigurationInput.model_validate(values)
