"""Focused independent checks for the I04-A cross-layer DTO fixes."""

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest
from core.casdoor.configuration import CasdoorConfiguration
from pydantic import ValidationError

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "controllers/console/casdoor_schemas_extend.py"
SPEC = importlib.util.spec_from_file_location("casdoor_endpoint_dtos_patch_independent", SCHEMA_PATH)
assert SPEC is not None and SPEC.loader is not None
schemas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schemas)


def config(**updates: object) -> CasdoorConfiguration:
    data: dict[str, object] = {
        "browser_frontend_url": "https://login.example.test/Frontend",
        "backend_api_url": "https://login.example.test/Api",
        "expected_issuer": "https://login.example.test/Issuer",
        "organization": "ExactOrg",
        "application": "ExactApp",
        "client_id": "ExactClient",
        "default_workspace_id": "10000000-0000-4000-8000-000000000001",
    }
    data.update(updates)
    return CasdoorConfiguration.model_validate(data)


def test_button_text_matches_120_character_and_255_utf8_byte_contract() -> None:
    for text in ("a" * 120, "界" * 85):
        assert config(button_text=text).button_text == text
        assert schemas.CasdoorDisplayResponse(button_text=text).button_text == text

    for text in ("a" * 121, "界" * 86):
        with pytest.raises(ValidationError):
            config(button_text=text)
        with pytest.raises(ValidationError):
            schemas.CasdoorDisplayResponse(button_text=text)

    schema = CasdoorConfiguration.model_json_schema()["properties"]["button_text"]
    assert schema["maxLength"] == 120
    assert schema["x-max-utf8-bytes"] == 255


def test_workspace_selector_accepts_existing_255_character_unicode_tenant_name() -> None:
    name = "界" * 255
    assert len(name) == 255 and len(name.encode("utf-8")) == 765
    dto = schemas.CasdoorWorkspaceResponse(
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"), name=name, available=True
    )
    assert dto.name == name
    with pytest.raises(ValidationError):
        schemas.CasdoorWorkspaceResponse(workspace_id=dto.workspace_id, name=name + "界", available=True)


def test_decision_dto_covers_database_manual_recovery_state_but_not_failed() -> None:
    decision = {
        "workspace_id": "10000000-0000-4000-8000-000000000001",
        "target_role": "normal",
        "reason": "default_normal_fallback",
        "snapshot_state": "known",
        "ownership": "managed",
        "finalization": "manual_recovery",
    }
    assert schemas.CasdoorWorkspaceDecisionResponse.model_validate(decision).finalization == "manual_recovery"
    with pytest.raises(ValidationError):
        schemas.CasdoorWorkspaceDecisionResponse.model_validate({**decision, "finalization": "failed"})
