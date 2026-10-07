"""Independent boundary checks for the offline Casdoor DTO foundation.

These tests intentionally exercise cross-field and adversarial cases rather
than duplicating the implementation's validator-by-validator test matrix.
"""

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest
from core.casdoor.configuration import CasdoorConfiguration
from pydantic import ValidationError

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "controllers/console/casdoor_schemas_extend.py"
SPEC = importlib.util.spec_from_file_location("casdoor_endpoint_dtos_independent", SCHEMA_PATH)
assert SPEC is not None and SPEC.loader is not None
schemas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schemas)

DEFAULT = "10000000-0000-4000-8000-000000000001"


def configuration(**changes: object) -> CasdoorConfiguration:
    data: dict[str, object] = {
        "browser_frontend_url": "https://login.example.test/Frontend",
        "backend_api_url": "https://login.example.test/Api",
        "expected_issuer": "https://login.example.test/Issuer/v1",
        "organization": "ExactOrg",
        "application": "ExactApp",
        "client_id": "ExactClient",
        "default_workspace_id": DEFAULT,
    }
    data.update(changes)
    return CasdoorConfiguration.model_validate(data)


def test_default_workspace_counts_toward_100_even_when_unmapped() -> None:
    valid = [{"workspace_id": f"10000000-0000-4000-8000-{number:012d}"} for number in range(2, 101)]
    assert len(configuration(workspace_mappings=valid).workspace_mappings) == 99

    too_many = valid + [{"workspace_id": "10000000-0000-4000-8000-000000000101"}]
    with pytest.raises(ValidationError):
        configuration(workspace_mappings=too_many)


def test_required_default_is_a_uuid_and_issuer_path_is_kept_exactly() -> None:
    config = configuration()
    assert config.default_workspace_id == UUID(DEFAULT)
    assert config.expected_issuer == "https://login.example.test/Issuer/v1"
    assert (
        configuration(expected_issuer="https://login.example.test/issuer/V1").config_digest() != config.config_digest()
    )

    with pytest.raises(ValidationError):
        configuration(default_workspace_id="first-workspace")


def test_role_names_use_exact_utf8_byte_limits_without_normalization() -> None:
    configuration(organization="o" * 255, application="a" * 255, client_id="c" * 255)
    with pytest.raises(ValidationError):
        configuration(organization="界" * 86)  # 258 UTF-8 bytes despite only 86 code points.

    role = {"organization": "ExactOrg", "name": "Admin/Team"}
    mapped = configuration(workspace_mappings=[{"workspace_id": DEFAULT, "admin": role}])
    assert mapped.workspace_mappings[0].admin.name == "Admin/Team"
    with pytest.raises(ValidationError):
        configuration(organization="exactorg", workspace_mappings=[{"workspace_id": DEFAULT, "admin": role}])


def test_slot_model_cannot_express_owner_or_custom_target() -> None:
    with pytest.raises(ValidationError):
        configuration(
            workspace_mappings=[{"workspace_id": DEFAULT, "owner": {"organization": "ExactOrg", "name": "Owner"}}]
        )

    decision = {
        "workspace_id": DEFAULT,
        "target_role": "normal",
        "reason": "default_normal_fallback",
        "snapshot_state": "known",
        "ownership": "unmanaged",
        "finalization": "finalized",
    }
    assert schemas.CasdoorWorkspaceDecisionResponse.model_validate(decision).target_role == "normal"
    for state, target in (("unknown", "normal"), ("known", "owner"), ("known", "admin")):
        with pytest.raises(ValidationError):
            schemas.CasdoorWorkspaceDecisionResponse.model_validate(
                {**decision, "snapshot_state": state, "target_role": target}
            )


def test_role_ref_reverse_uniqueness_is_per_workspace_not_global() -> None:
    shared = {"organization": "ExactOrg", "name": "Readers"}
    first = {"workspace_id": "10000000-0000-4000-8000-000000000002", "normal": shared}
    second = {"workspace_id": "10000000-0000-4000-8000-000000000003", "editor": shared}
    assert len(configuration(workspace_mappings=[first, second]).workspace_mappings) == 2

    conflicting = {"workspace_id": "10000000-0000-4000-8000-000000000002", "editor": shared, "normal": shared}
    with pytest.raises(ValidationError):
        configuration(workspace_mappings=[conflicting])


def test_canonical_digest_is_order_independent_but_case_sensitive() -> None:
    a = {"workspace_id": "10000000-0000-4000-8000-000000000002", "normal": {"organization": "ExactOrg", "name": "N"}}
    b = {"workspace_id": "10000000-0000-4000-8000-000000000003", "admin": {"organization": "ExactOrg", "name": "A"}}
    assert (
        configuration(workspace_mappings=[a, b]).canonical_json()
        == configuration(workspace_mappings=[b, a]).canonical_json()
    )
    assert configuration(organization="exactorg").config_digest() != configuration().config_digest()
    assert "secret" not in configuration().canonical_json().lower()


def test_http_requires_deployment_context_and_only_loopback_is_allowed() -> None:
    local = "http://127.0.0.1:8000"
    data = {
        "browser_frontend_url": local,
        "backend_api_url": "https://api.example.test",
        "expected_issuer": "https://issuer.example.test",
        "organization": "ExactOrg",
        "application": "ExactApp",
        "client_id": "ExactClient",
        "default_workspace_id": DEFAULT,
    }
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(data)
    assert (
        CasdoorConfiguration.model_validate(
            data, context={"allow_development_loopback_http": True}
        ).browser_frontend_url
        == local
    )
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(
            {**data, "browser_frontend_url": "http://outside.example.test"},
            context={"allow_development_loopback_http": True},
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@idp.example.test/",
        "https://idp.example.test/path#fragment",
        "https://idp.example.test/path?query=1",
    ],
)
def test_issuer_rejects_credentials_fragment_and_query(url: str) -> None:
    with pytest.raises(ValidationError):
        configuration(expected_issuer=url)


def test_secret_is_write_only_and_keep_replace_clear_are_distinct() -> None:
    cfg = configuration()
    keep = schemas.CasdoorSaveConfigurationPayload.model_validate({"etag": 4, "configuration": cfg})
    empty = schemas.CasdoorSaveConfigurationPayload.model_validate({"etag": 4, "configuration": cfg, "secret": ""})
    replace = schemas.CasdoorSaveConfigurationPayload.model_validate(
        {"etag": 4, "configuration": cfg, "secret": "synthetic-secret-value"}
    )
    assert keep.secret_action == empty.secret_action == "keep"
    assert replace.secret_action == "replace"
    assert "synthetic-secret-value" not in repr(replace) + replace.model_dump_json() + cfg.canonical_json()
    assert replace.secret is not None and replace.secret.get_secret_value() == "synthetic-secret-value"
    assert schemas.CasdoorClearSecretPayload.model_validate(
        {"etag": 4, "revision_id": "20000000-0000-4000-8000-000000000001"}
    )
    with pytest.raises(ValidationError):
        schemas.CasdoorSaveConfigurationPayload.model_validate({"etag": 4, "configuration": cfg, "secret": "********"})
    with pytest.raises(ValidationError):
        schemas.CasdoorClearSecretPayload.model_validate(
            {"etag": 4, "revision_id": "20000000-0000-4000-8000-000000000001", "secret": "replacement"}
        )


def test_sensitive_validation_input_is_removed_only_with_explicit_error_options() -> None:
    with pytest.raises(ValidationError) as caught:
        schemas.CasdoorSaveConfigurationPayload.model_validate(
            {"etag": 4, "configuration": configuration(), "secret": "********"}
        )
    raw = caught.value.errors(include_input=False, include_context=False)
    assert all("input" not in error and "ctx" not in error for error in raw)
    assert "********" not in str(caught.value)


def test_display_permission_identity_profile_and_get_shapes_are_closed() -> None:
    assert set(schemas.CasdoorDisplayResponse().model_dump()) == {"enabled", "button_text", "start_path"}
    assert set(schemas.CasdoorPermissionsResponse(can_manage_casdoor=False).model_dump()) == {"can_manage_casdoor"}
    assert set(schemas.CasdoorProfilePayload(name="New Name").model_dump()) == {"name"}
    with pytest.raises(ValidationError):
        schemas.CasdoorProfilePayload.model_validate({"name": "New Name", "email": "private@example.test"})
    for key in ("email", "password", "quota", "role", "workspace", "account_id"):
        with pytest.raises(ValidationError):
            schemas.CasdoorLinkIdentityPayload.model_validate({key: "synthetic"})
    for key in ("secret", "encrypted_secret", "client_secret", "claims", "access_token", "id_token", "organization"):
        with pytest.raises(ValidationError):
            schemas.CasdoorConfigurationResponse.model_validate({key: "synthetic"})


def test_name_strategy_and_optional_capabilities_are_draft_defaults() -> None:
    default = configuration()
    assert default.default_normal_fallback is True
    assert default.name_sync == "fill_empty"
    assert (default.avatar_sync, default.rp_logout, default.self_unlink) == (False, False, False)
    for mode in ("off", "fill_empty", "managed"):
        assert configuration(name_sync=mode).name_sync == mode
    draft = configuration(name_sync="managed", avatar_sync=True, rp_logout=True, self_unlink=True)
    assert (draft.name_sync, draft.avatar_sync, draft.rp_logout, draft.self_unlink) == ("managed", True, True, True)
    with pytest.raises(ValidationError):
        configuration(default_normal_fallback=False)
    for unsupported in ("on", "always", "enabled"):
        with pytest.raises(ValidationError):
            configuration(name_sync=unsupported)
