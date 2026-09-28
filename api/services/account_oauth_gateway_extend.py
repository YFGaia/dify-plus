"""Fork OAuth2/Casdoor adapter for the upstream account OAuth application service."""

import json
from dataclasses import dataclass
from typing import TypedDict

import httpx
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from libs.oauth import OaOAuth
from models.system_extend import SystemIntegrationClassify, SystemIntegrationExtend
from services.account_errors import (
    InvalidOAuthProviderError,
    OAuthProviderAuthorizationError,
    OAuthProviderRequestError,
)
from services.entities.account_oauth_entities import OAuthAuthorizationRequest, OAuthIdentity


@dataclass(frozen=True)
class _IntegrationSnapshot:
    app_id: str
    status: bool = True


class _OAuthConfig(TypedDict):
    integration: _IntegrationSnapshot
    passwd: str
    config: dict[str, object]


class _ConfiguredOAuth(OaOAuth):
    def __init__(self, config: _OAuthConfig) -> None:
        super().__init__(client_id=config["integration"].app_id, client_secret=config["passwd"], redirect_uri="")
        self._config = config

    def get_auto2_conf(self) -> _OAuthConfig:
        return self._config


class ForkOAuthProviderGateway:
    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _client(self) -> _ConfiguredOAuth:
        # Materialize configuration and secret while the session is open; provider
        # network calls happen only after closing this read transaction.
        with self._session_factory() as session:
            integration = session.scalar(
                select(SystemIntegrationExtend).where(
                    SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_OAUTH_TWO,
                    SystemIntegrationExtend.status.is_(True),
                )
            )
            if integration is None:
                raise InvalidOAuthProviderError
            try:
                config = json.loads(integration.config)
                if not isinstance(config, dict) or not integration.app_id:
                    raise ValueError("Invalid OAuth2 configuration")
                snapshot: _OAuthConfig = {
                    "integration": _IntegrationSnapshot(app_id=integration.app_id),
                    "passwd": integration.decodeSecret(),
                    "config": config,
                }
            except (ValueError, TypeError) as exc:
                raise OAuthProviderAuthorizationError("Invalid OAuth2 configuration") from exc
        return _ConfiguredOAuth(snapshot)

    def get_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        url = self._client().get_authorization_url(
            invite_token=request.invite_token,
            timezone=request.timezone,
            language=request.language,
            redirect_url=request.redirect_url,
        )
        if not isinstance(url, str) or not url:
            raise InvalidOAuthProviderError
        return url

    def get_identity(self, code: str) -> OAuthIdentity:
        client = self._client()
        try:
            response: object = client.get_access_token(code)
            id_token = None
            if isinstance(response, dict):
                token = response.get("access_token")
                candidate = response.get("id_token")
                id_token = candidate if isinstance(candidate, str) else None
            else:
                token = response
            return self._identity(client, token, id_token=id_token)
        except (requests.RequestException, httpx.HTTPError) as exc:
            raise OAuthProviderRequestError from exc
        except ValueError as exc:
            raise OAuthProviderAuthorizationError("Invalid OAuth2 provider response") from exc

    def get_identity_from_token(self, token: str) -> OAuthIdentity:
        """Preserve the existing OAuth2 token callback, without logging query secrets."""
        try:
            return self._identity(self._client(), token)
        except (requests.RequestException, httpx.HTTPError) as exc:
            raise OAuthProviderRequestError from exc
        except ValueError as exc:
            raise OAuthProviderAuthorizationError("Invalid OAuth2 provider response") from exc

    @staticmethod
    def _identity(client: OaOAuth, token: object, *, id_token: str | None = None) -> OAuthIdentity:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Missing OAuth2 access token")
        user = client.get_user_info(token)
        if not isinstance(user.id, str) or not user.id or not isinstance(user.email, str) or not user.email.strip():
            raise ValueError("Missing OAuth2 identity")
        return OAuthIdentity(id=user.id, name=user.name or "", email=user.email, id_token=id_token)
