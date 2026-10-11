"""Typed Console contracts for DingTalk settings and enterprise email lookup tests."""

from typing import Any, Literal

from fields.base import ResponseModel
from pydantic import BaseModel, ConfigDict, Field


class DingTalkConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: bool | None = None
    corp_id: str | None = None
    agent_id: str | None = None
    app_key: str | None = None
    app_secret: str | None = None
    app_id: str | None = None
    config: dict[str, Any] | None = None
    expected_revision: str | None = None


class DingTalkConfigResponse(ResponseModel):
    status: bool = False
    corp_id: str = ""
    agent_id: str = ""
    app_key: str = ""
    app_secret: str = ""
    app_id: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    revision: str = ""
    connection_verified: bool = False
    verified: bool = False
    secret_configured: bool = False
    callback_url: str = ""
    result: str | None = None


class DingTalkRegistrationPollPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    transaction_id: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class DingTalkRegistrationResponse(ResponseModel):
    transaction_id: str
    status: Literal["WAITING", "SUCCESS", "FAIL", "EXPIRED", "CANCELLED"]
    verification_uri_complete: str | None = None
    expires_in: int | None = None
    interval: int | None = None
    message: str | None = None


class DingTalkRegistrationStartResponse(ResponseModel):
    transaction_id: str
    status: Literal["WAITING"]
    verification_uri_complete: str
    expires_in: int
    interval: int


class DingTalkDiagnosticPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    bind_current_account: bool = False


class DingTalkDiagnosticResponse(ResponseModel):
    authorization_url: str


class DingTalkDisplayResponse(ResponseModel):
    enabled: bool
    button_text: str
    start_path: str


class DingTalkCallbackQuery(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    state: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")
    authCode: str = Field(min_length=1, max_length=499)


class IntegrationTestResponse(ResponseModel):
    result: str
    message: str | None = None
    status_code: int | None = None
    email: str | None = None


class EmailLookupTestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    config: dict[str, Any] | None = None
    user_id: str = ""
    url: str = ""
    key: str = ""
