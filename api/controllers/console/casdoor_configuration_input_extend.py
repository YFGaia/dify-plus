"""Convenient management input that resolves to the complete Casdoor policy.

Defaults belong to this input boundary only. Revisions and responses retain the
strict domain model, including the administrator's explicit endpoint overrides.
New writes always create automatic signing key revisions. Missing workspace IDs are resolved only from the
authenticated account at conversion, never from a list or provider lookup.
"""

from typing import Any, Literal
from uuid import UUID

from core.casdoor.configuration import (
    ButtonText,
    CasdoorConfiguration,
    ExactName,
    PolicyModel,
    WorkspaceRoleMapping,
)
from pydantic import Field, StrictBool, StrictStr, ValidationInfo, field_validator, model_validator


class CasdoorConfigurationInput(PolicyModel):
    schema_version: Literal[2] = 2
    browser_frontend_url: StrictStr = Field(max_length=2048, min_length=1)
    backend_api_url: StrictStr | None = Field(default=None, max_length=2048, min_length=1)
    expected_issuer: StrictStr | None = Field(default=None, max_length=2048, min_length=1)
    organization: ExactName
    application: ExactName
    client_id: ExactName
    button_text: ButtonText = "Casdoor"
    scope: Literal["openid email profile"] = "openid email profile"
    default_workspace_id: UUID | None = None
    workspace_mappings: tuple[WorkspaceRoleMapping, ...] = Field(default=(), max_length=100)
    default_normal_fallback: Literal[True] = True
    name_sync: Literal["off", "fill_empty", "managed"] = "fill_empty"
    avatar_sync: StrictBool = False
    avatar_mode: Literal["fill_empty", "managed"] = "fill_empty"
    rp_logout: StrictBool = False
    self_unlink: StrictBool = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value: object) -> object:
        return CasdoorConfiguration.integer_schema_version(value)

    @field_validator("default_normal_fallback", mode="before")
    @classmethod
    def boolean_fallback(cls, value: object) -> object:
        return CasdoorConfiguration.boolean_policies(value)

    @model_validator(mode="before")
    @classmethod
    def resolve_shared_endpoint(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        url = data.get("browser_frontend_url")
        if isinstance(url, str):
            for field in ("backend_api_url", "expected_issuer"):
                if data.get(field) is None:
                    data[field] = url.rstrip("/")
        return data

    @model_validator(mode="after")
    def validate_complete_policy(self, info: ValidationInfo) -> "CasdoorConfigurationInput":
        if self.default_workspace_id is not None:
            self.to_configuration(context=info.context)
        return self

    def to_configuration(
        self, *, current_workspace_id: str | None = None, context: dict[str, Any] | None = None
    ) -> CasdoorConfiguration:
        """Resolve authenticated workspace context before strict domain validation.

        Explicit IDs remain compatible for existing management API clients. The
        caller must supply the current Account's trusted ID when input omits it.
        """
        data = self.model_dump()
        data["signing_key_mode"] = "automatic"
        if self.default_workspace_id is None:
            data["default_workspace_id"] = current_workspace_id
        return CasdoorConfiguration.model_validate(data, context=context)
