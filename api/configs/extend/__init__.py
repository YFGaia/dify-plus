import decimal
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class ExtendInfo(BaseSettings):

    OAUTH2_CLIENT_ID: Optional[str] = Field(
        description="OA client id for OAuth",
        default=None,
    )

    OAUTH2_CLIENT_SECRET: Optional[str] = Field(
        description="OA client secret key for OAuth2",
        default=None,
    )

    OAUTH2_CLIENT_URL: Optional[str] = Field(
        description="OA client url for OAuth2",
        default=None,
    )

    OAUTH2_TOKEN_URL: Optional[str] = Field(
        description="OA token url for OAuth2",
        default=None,
    )

    OAUTH2_USER_INFO_URL: Optional[str] = Field(
        description="OA user_info url for OAuth2",
        default=None,
    )

    EMAIL_DOMAIN: Optional[str] = Field(
        description="邮箱域名",
        default=None,
    )

    ADMIN_GROUP_ID: Optional[str] = Field(
        description="后台超级管理员权限组id",
        default="888",
    )

    RMB_TO_USD_RATE: Optional[decimal.Decimal] = Field(
        description="人民币兑美元汇率",
        default="7.26",
    )

    ACCOUNT_TOTAL_QUOTA: Optional[decimal.Decimal] = Field(
        description="用户额度初始总额度",
        default="15",
    )

    DEFAULT_LANGUAGE: Optional[str] = Field(
        description="默认语言",
        default="zh-Hans",
    )

    FULL_CODE_EXECUTION_ENDPOINT: str = Field(
        description="Full code execution endpoint",
        default="http://full_sandbox:8195",
    )

    BEDROCK_PROXY: Optional[str] = Field(
        description="Bedrock Proxy URL",
        default=None,
    )

    # Extend: 记忆上下文功能
    DEFAULT_NUMBER_CONTEXT: Optional[int] = Field(
        description="Default number of context retention (记忆窗口默认值)",
        default=5,
    )
    # Extend: 记忆上下文功能


class ExtendConfig(ExtendInfo):
    pass
