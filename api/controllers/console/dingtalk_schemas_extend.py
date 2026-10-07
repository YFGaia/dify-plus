"""Typed Console contracts for DingTalk settings and enterprise email lookup tests."""

from typing import Any

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


class DingTalkConfigResponse(ResponseModel):
    status: bool = False
    corp_id: str = ""
    agent_id: str = ""
    app_key: str = ""
    app_secret: str = ""
    app_id: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


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
