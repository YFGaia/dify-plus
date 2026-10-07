"""Offline DTO boundaries. No app bootstrap, .env, network, DB or real Secrets."""

import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import UUID

import pytest
from core.casdoor.configuration import CasdoorConfiguration, RoleRef
from core.casdoor.errors import CasdoorDecisionReason, CasdoorErrorCode
from fields.base import ResponseModel
from pydantic import ValidationError

# Loading this endpoint-owned module by filename avoids Console __init__, whose
# application bootstrap is intentionally outside this pure DTO package's scope.
SCHEMA_PATH = Path(__file__).resolve().parents[4] / "controllers/console/casdoor_schemas_extend.py"
SPEC = importlib.util.spec_from_file_location("casdoor_endpoint_dtos_offline", SCHEMA_PATH)
assert SPEC is not None
assert SPEC.loader is not None
schemas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schemas)

WORKSPACE = "10000000-0000-4000-8000-000000000001"
SECOND_WORKSPACE = "10000000-0000-4000-8000-000000000002"
REVISION = "20000000-0000-4000-8000-000000000001"
NAMESPACE = "30000000-0000-4000-8000-000000000001"
CORRELATION = "40000000-0000-4000-8000-000000000001"


def config_data(**updates: object) -> dict[str, object]:
    return {
        "browser_frontend_url": "https://idp.example.test/Frontend",
        "backend_api_url": "https://api.idp.example.test/Api",
        "expected_issuer": "https://idp.example.test/Issuer",
        "organization": "ExactOrg",
        "application": "Application",
        "client_id": "Client",
        "default_workspace_id": WORKSPACE,
        **updates,
    }


def role(name: str = "Administrators", organization: str = "ExactOrg") -> dict[str, str]:
    return {"organization": organization, "name": name}


def certificate(kid: str = "synthetic-kid") -> dict[str, str]:
    # A declaration only: authenticity remains I02's TrustedCertificate contract.
    return {
        "pem": "-----BEGIN CERTIFICATE-----\nSYNTHETIC\n-----END CERTIFICATE-----",
        "kid": kid,
        "not_before": "2026-09-30T00:00:00Z",
        "accept_until": "2026-10-01T00:00:00Z",
    }


def test_minimal_config_is_non_secret_and_default_closed() -> None:
    config = CasdoorConfiguration.model_validate(config_data())
    assert config.default_workspace_id == UUID(WORKSPACE)
    assert config.scope == "openid email profile"
    assert config.default_normal_fallback is True
    assert config.name_sync == "fill_empty"
    assert config.avatar_sync is config.rp_logout is config.self_unlink is False
    assert config.expected_issuer == "https://idp.example.test/Issuer"
    with pytest.raises(ValidationError):
        config.organization = "changed"


@pytest.mark.parametrize("missing", ["default_workspace_id", "organization", "application", "client_id"])
def test_required_configuration_fields(missing: str) -> None:
    data = config_data()
    del data[missing]
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(data)


@pytest.mark.parametrize(
    "updates",
    [
        {"default_workspace_id": "first"},
        {"schema_version": True},
        {"schema_version": 3},
        {"scope": "openid email profile roles"},
        {"default_normal_fallback": False},
        {"default_normal_fallback": 1},
        {"avatar_sync": 0},
        {"secret": "synthetic-rejected-secret"},
        {"encrypted_secret": "synthetic-rejected-envelope"},
        {"name_sync": "arbitrary"},
    ],
)
def test_noncanonical_policy_or_secret_fields_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(config_data(**updates))


def test_optional_draft_policy_is_representable_without_claiming_capability() -> None:
    for name_policy in ("off", "fill_empty", "managed"):
        config = CasdoorConfiguration.model_validate(
            config_data(name_sync=name_policy, avatar_sync=True, rp_logout=True, self_unlink=True)
        )
        assert config.name_sync == name_policy
        assert config.avatar_sync is config.rp_logout is config.self_unlink is True


def test_button_text_matches_storage_char_limit_and_keeps_utf8_byte_limit() -> None:
    for valid in ("b" * 120, "名" * 85):
        assert CasdoorConfiguration.model_validate(config_data(button_text=valid)).button_text == valid
        assert schemas.CasdoorDisplayResponse(button_text=valid).button_text == valid
    for invalid in ("b" * 121, "名" * 86):
        with pytest.raises(ValidationError):
            CasdoorConfiguration.model_validate(config_data(button_text=invalid))
        with pytest.raises(ValidationError):
            schemas.CasdoorDisplayResponse(button_text=invalid)
    field_schema = CasdoorConfiguration.model_json_schema()["properties"]["button_text"]
    assert field_schema["maxLength"] == 120
    assert field_schema["x-max-utf8-bytes"] == 255


def test_workspace_selector_uses_local_tenant_character_limit() -> None:
    local_name = "空间名" * 85
    assert len(local_name) == 255
    assert len(local_name.encode("utf-8")) > 255
    dto = schemas.CasdoorWorkspaceResponse(workspace_id=WORKSPACE, name=local_name, available=True)
    assert dto.name == local_name
    for invalid in (local_name + "a", 123):
        with pytest.raises(ValidationError):
            schemas.CasdoorWorkspaceResponse(workspace_id=WORKSPACE, name=invalid, available=True)


def test_decision_finalization_matches_frozen_membership_states() -> None:
    decision = {
        "workspace_id": WORKSPACE,
        "target_role": "normal",
        "reason": "default_normal_fallback",
        "snapshot_state": "known",
        "ownership": "managed",
        "finalization": "manual_recovery",
    }
    assert schemas.CasdoorWorkspaceDecisionResponse.model_validate(decision).finalization == "manual_recovery"
    with pytest.raises(ValidationError):
        schemas.CasdoorWorkspaceDecisionResponse.model_validate({**decision, "finalization": "failed"})


@pytest.mark.parametrize("field", ["organization", "application", "client_id"])
def test_exact_name_limit_is_utf8_bytes(field: str) -> None:
    assert len(("名" * 85).encode("utf-8")) == 255
    CasdoorConfiguration.model_validate(config_data(**{field: "名" * 85}))
    for invalid in ("名" * 86, "", "   ", 12):
        with pytest.raises(ValidationError):
            CasdoorConfiguration.model_validate(config_data(**{field: invalid}))


def test_role_references_preserve_case_and_slashes_without_guessing() -> None:
    ref = RoleRef.model_validate(role("Folder/Admin"))
    assert ref.name == "Folder/Admin"
    assert ref != RoleRef.model_validate(role("folder/admin"))
    with pytest.raises(ValidationError):
        RoleRef.model_validate("ExactOrg/Folder/Admin")
    with pytest.raises(ValidationError):
        RoleRef.model_validate(role("名" * 86))


def test_three_slots_cross_workspace_reuse_and_no_normal_slot_are_valid() -> None:
    mappings = [
        {"workspace_id": WORKSPACE, "admin": role("A"), "editor": role("E")},
        {"workspace_id": SECOND_WORKSPACE, "admin": role("A"), "normal": role("N")},
    ]
    config = CasdoorConfiguration.model_validate(config_data(workspace_mappings=mappings))
    assert config.workspace_mappings[0].normal is None
    assert config.workspace_mappings[0].admin == config.workspace_mappings[1].admin


@pytest.mark.parametrize(
    "mappings",
    [
        [{"workspace_id": WORKSPACE, "admin": role(), "normal": role()}],
        [{"workspace_id": WORKSPACE}, {"workspace_id": WORKSPACE}],
        [{"workspace_id": WORKSPACE, "owner": role()}],
        [{"workspace_id": WORKSPACE, "custom": role()}],
        [{"workspace_id": WORKSPACE, "dataset_operator": role()}],
        [{"workspace_id": WORKSPACE, "admin": [role("A"), role("B")]}],
        [{"workspace_id": WORKSPACE, "admin": role(organization="exactorg")}],
        [{"workspace_id": WORKSPACE, "admin": "ExactOrg/Admin"}],
    ],
)
def test_mapping_ambiguity_owner_custom_and_cross_org_rejected(mappings: object) -> None:
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(config_data(workspace_mappings=mappings))


def test_maximum_100_workspace_mappings() -> None:
    mappings = [{"workspace_id": WORKSPACE}] + [{"workspace_id": str(UUID(int=index + 1))} for index in range(99)]
    CasdoorConfiguration.model_validate(config_data(workspace_mappings=mappings))
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(
            config_data(workspace_mappings=mappings + [{"workspace_id": SECOND_WORKSPACE}])
        )
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(
            config_data(workspace_mappings=[{"workspace_id": str(UUID(int=i + 1))} for i in range(100)])
        )


@pytest.mark.parametrize("field", ["browser_frontend_url", "backend_api_url", "expected_issuer"])
@pytest.mark.parametrize(
    "url",
    [
        "ftp://idp.example.test",
        "https:///missing-host",
        "https://user:synthetic@idp.example.test",
        "https://idp.example.test#",
        "https://idp.example.test#fragment",
        "https://idp.example.test:0",
        "https://idp.example.test:invalid",
        "https://idp.example.test:65536",
        "https://idp.example.test\\evil",
        "https://idp.example.test\n",
        "http://idp.example.test",
        "http://127.0.0.1:8000",
    ],
)
def test_unsafe_url_shapes_and_implicit_http_rejected(field: str, url: str) -> None:
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(config_data(**{field: url}))


def test_explicit_deployment_loopback_exception_cannot_come_from_payload() -> None:
    context = {"allow_development_loopback_http": True}
    for url in ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"):
        config = CasdoorConfiguration.model_validate(config_data(backend_api_url=url), context=context)
        assert config.backend_api_url == url
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(config_data(backend_api_url="http://idp.example.test"), context=context)
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(
            config_data(backend_api_url="http://localhost", allow_development_loopback_http=True)
        )


def test_url_limit_and_strict_issuer_query_boundary() -> None:
    prefix = "https://idp.example.test/"
    exact = prefix + "a" * (2048 - len(prefix))
    CasdoorConfiguration.model_validate(config_data(expected_issuer=exact))
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(config_data(expected_issuer=exact + "a"))
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(config_data(expected_issuer="https://idp.example.test/Issuer?"))


def test_canonical_json_has_stable_order_roundtrip_and_digest() -> None:
    mappings = [
        {"workspace_id": SECOND_WORKSPACE, "admin": role("B")},
        {"workspace_id": WORKSPACE, "normal": role("A")},
    ]
    pins = [certificate("second"), certificate("first")]
    first = CasdoorConfiguration.model_validate(config_data(workspace_mappings=mappings, certificates=pins))
    second = CasdoorConfiguration.model_validate(
        config_data(workspace_mappings=mappings[::-1], certificates=pins[::-1])
    )
    assert first.canonical_json() == second.canonical_json()
    assert json.loads(first.canonical_json())["schema_version"] == 1
    assert first.config_digest() == hashlib.sha256(first.canonical_json().encode("utf-8")).hexdigest()
    assert CasdoorConfiguration.model_validate_json(first.canonical_json()).canonical_json() == first.canonical_json()
    case_changed = CasdoorConfiguration.model_validate(config_data(organization="exactorg"))
    assert case_changed.config_digest() != CasdoorConfiguration.model_validate(config_data()).config_digest()


@pytest.mark.parametrize(
    "pins",
    [
        [certificate("a"), certificate("b"), certificate("c")],
        [certificate("same"), certificate("same")],
        [{**certificate(), "pem": "-----BEGIN PRIVATE KEY-----\nSYNTHETIC"}],
        [{**certificate(), "pem": "-----BEGIN CERTIFICATE-----\n" + "密" * 5500}],
        [{**certificate(), "kid": "名" * 43}],
        [{**certificate(), "not_before": "2026-09-30T08:00:00+08:00"}],
        [{**certificate(), "not_before": "2026-10-01T00:00:00Z"}],
    ],
)
def test_public_certificate_declaration_limits(pins: object) -> None:
    with pytest.raises(ValidationError):
        CasdoorConfiguration.model_validate(config_data(certificates=pins))


def test_save_secret_is_synthetic_write_only_bounded_and_not_canonical() -> None:
    synthetic = "密" * 1365 + "x"
    assert len(synthetic.encode("utf-8")) == 4096
    payload = schemas.CasdoorSaveConfigurationPayload.model_validate(
        {"etag": 0, "configuration": config_data(), "secret": synthetic}
    )
    assert payload.secret.get_secret_value() == synthetic
    assert payload.secret_action == "replace"
    assert "secret" not in payload.model_dump()
    assert (
        synthetic
        not in str(payload) + payload.model_dump_json() + payload.configuration.to_configuration().canonical_json()
    )
    with pytest.raises(ValidationError) as error:
        schemas.CasdoorSaveConfigurationPayload.model_validate(
            {"etag": 0, "configuration": config_data(), "secret": synthetic + "x"}
        )
    assert synthetic not in str(error.value)
    empty = schemas.CasdoorSaveConfigurationPayload.model_validate(
        {"etag": 0, "configuration": config_data(), "secret": ""}
    )
    assert empty.secret.get_secret_value() == ""
    assert empty.secret_action == "keep"
    assert (
        schemas.CasdoorSaveConfigurationPayload.model_validate(
            {"etag": 0, "configuration": config_data()}
        ).secret_action
        == "keep"
    )
    with pytest.raises(ValidationError):
        schemas.CasdoorSaveConfigurationPayload.model_validate(
            {"etag": 0, "configuration": config_data(), "secret": "********"}
        )
    with pytest.raises(ValidationError):
        schemas.CasdoorClearSecretPayload.model_validate({"etag": 0, "revision_id": REVISION, "secret": "synthetic"})


@pytest.mark.parametrize("etag", [-1, "0", True])
def test_etag_must_be_nonnegative_integer(etag: object) -> None:
    with pytest.raises(ValidationError):
        schemas.CasdoorSaveConfigurationPayload.model_validate({"etag": etag, "configuration": config_data()})


def test_display_permission_and_result_minimal_fields() -> None:
    assert set(schemas.CasdoorDisplayResponse().model_dump()) == {"enabled", "button_text", "start_path"}
    assert set(schemas.CasdoorPermissionsResponse(can_manage_casdoor=False).model_dump()) == {"can_manage_casdoor"}
    result = schemas.CasdoorResultResponse(
        code=CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN, correlation_id=CORRELATION, retry_allowed=True
    )
    assert set(result.model_dump()) == {"code", "correlation_id", "retry_allowed"}
    assert "default_normal_fallback" not in {code.value for code in CasdoorErrorCode}
    with pytest.raises(ValidationError):
        schemas.CasdoorResultResponse.model_validate({**result.model_dump(), "access_token": "synthetic"})


def test_unknown_role_snapshot_never_expresses_fallback() -> None:
    decision = {
        "workspace_id": WORKSPACE,
        "target_role": "normal",
        "reason": "default_normal_fallback",
        "snapshot_state": "known",
        "ownership": "managed",
        "finalization": "pending",
    }
    assert schemas.CasdoorWorkspaceDecisionResponse.model_validate(decision).reason == (
        CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
    )
    for updates in ({"snapshot_state": "unknown"}, {"target_role": "admin"}, {"target_role": "owner"}):
        with pytest.raises(ValidationError):
            schemas.CasdoorWorkspaceDecisionResponse.model_validate({**decision, **updates})


def test_current_account_payloads_cannot_select_account_or_write_owned_fields() -> None:
    for cls in (schemas.CasdoorLinkIdentityPayload, schemas.CasdoorUnlinkIdentityPayload):
        cls.model_validate({})
        with pytest.raises(ValidationError):
            cls.model_validate({"account_id": WORKSPACE})
    for key in ("email", "normalized_email", "password", "quota", "total_quota", "role", "workspace", "account_id"):
        with pytest.raises(ValidationError):
            schemas.CasdoorProfilePayload.model_validate({"name": "Synthetic Name", key: "synthetic"})
    with pytest.raises(ValidationError):
        schemas.CasdoorAdoptMembershipPayload.model_validate(
            {
                "namespace_id": NAMESPACE,
                "identity_id": REVISION,
                "workspace_id": WORKSPACE,
                "confirm_permission_difference": True,
                "roles": ["owner"],
            }
        )
    with pytest.raises(ValidationError):
        schemas.CasdoorRetrySyncPayload.model_validate({"intent_id": REVISION, "account_id": WORKSPACE})


def test_callback_query_refuses_tokens_and_ambiguous_outcomes() -> None:
    schemas.CasdoorCallbackQuery.model_validate({"state": "synthetic-state", "code": "synthetic-code"})
    schemas.CasdoorCallbackQuery.model_validate({"state": "synthetic-state", "error": "access_denied"})
    for updates in (
        {"access_token": "synthetic"},
        {"id_token": "synthetic"},
        {"error": "access_denied"},
        {"error_description": "synthetic"},
    ):
        with pytest.raises(ValidationError):
            schemas.CasdoorCallbackQuery.model_validate({"state": "synthetic-state", "code": "synthetic", **updates})


def test_get_configuration_response_has_no_secret_or_ciphertext() -> None:
    dto = schemas.CasdoorConfigurationResponse.model_validate(
        {
            "draft_revision_id": REVISION,
            "draft": {
                "revision_id": REVISION,
                "namespace_id": NAMESPACE,
                "configuration": config_data(),
                "secret_configured": True,
            },
        }
    )
    assert dto.draft.secret_configured is True
    assert "secret" not in type(dto.draft).model_fields
    for key in ("secret", "encrypted_secret", "client_secret", "claims"):
        data = dto.model_dump(mode="json")
        data["draft"][key] = "synthetic"
        with pytest.raises(ValidationError):
            schemas.CasdoorConfigurationResponse.model_validate(data)


def test_identity_and_diagnostic_responses_refuse_full_pii_and_raw_directory() -> None:
    for key in ("subject", "remote_email", "email", "avatar_url", "organization", "claims", "roles", "token"):
        with pytest.raises(ValidationError):
            schemas.CasdoorIdentityResponse.model_validate({"bound": False, key: "synthetic"})
        with pytest.raises(ValidationError):
            schemas.CasdoorDiagnosticResponse.model_validate(
                {"revision_id": REVISION, "correlation_id": CORRELATION, "stages": [], key: "synthetic"}
            )


def test_session_requires_verified_source_and_local_logout_survives_without_handoff() -> None:
    schemas.CasdoorSessionResponse(source="local_only", verified=False)
    schemas.CasdoorLogoutResponse(status="local_only")
    for data in (
        {"source": "casdoor", "verified": False},
        {"source": "local_only", "verified": True, "rp_logout_available": True},
        {"source": "casdoor", "verified": True, "id_token": "synthetic"},
    ):
        with pytest.raises(ValidationError):
            schemas.CasdoorSessionResponse.model_validate(data)


@pytest.mark.parametrize(
    "path",
    [
        "https://idp.example.test/logout",
        "//evil.example.test",
        "/console/api/auth/casdoor/logout?id_token=synthetic",
        "/console/api/auth/casdoor/logout#synthetic",
        "/console/api/auth/casdoor/../other",
        "/console/api/auth/casdoor/%2e",
    ],
)
def test_navigation_cannot_expose_token_query_or_external_target(path: str) -> None:
    with pytest.raises(ValidationError):
        schemas.CasdoorNavigationResponse(handoff_path=path)


def test_response_schemas_inherit_response_model_and_publish_public_names_only() -> None:
    for name, cls in vars(schemas).items():
        if name.startswith("Casdoor") and name.endswith("Response"):
            assert issubclass(cls, ResponseModel)
            schema_json = json.dumps(cls.model_json_schema(mode="serialization"))
            for forbidden in ("encrypted_secret", "client_secret", "access_token", "id_token", "refresh_token"):
                assert forbidden not in schema_json
    assert schemas.CasdoorDisplayResponse.model_json_schema()["additionalProperties"] is False
    assert (
        schemas.CasdoorSaveConfigurationPayload.model_json_schema()["properties"]["secret"]["x-max-utf8-bytes"] == 4096
    )
