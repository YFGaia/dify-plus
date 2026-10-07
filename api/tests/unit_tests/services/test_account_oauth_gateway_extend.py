"""Configured fork provider traverses the new OAuth boundary without global DB sessions."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlalchemy.orm import Session, sessionmaker

from libs.oauth import OaOAuth, decode_oauth_state
from models.system_extend import SystemIntegrationClassify, SystemIntegrationExtend
from services.account_errors import (
    InvalidOAuthProviderError,
    OAuthProviderAuthorizationError,
    OAuthProviderRequestError,
)
from services.account_oauth_gateway_extend import ForkOAuthProviderGateway
from services.entities.account_oauth_entities import OAuthAuthorizationRequest


@pytest.fixture
def gateway(sqlite_session_factory: sessionmaker[Session]) -> ForkOAuthProviderGateway:
    with sqlite_session_factory() as session:
        session.add(
            SystemIntegrationExtend(
                id=1,
                classify=SystemIntegrationClassify.SYSTEM_INTEGRATION_OAUTH_TWO,
                status=True,
                app_id="client-id",
                app_secret="",
                config=json.dumps(
                    {
                        "server_url": "https://provider.invalid",
                        "authorize_url": "/authorize",
                        "token_url": "/token",
                        "userinfo_url": "/userinfo",
                        "scope": "openid email profile",
                    }
                ),
            )
        )
        session.commit()
    return ForkOAuthProviderGateway(session_factory=sqlite_session_factory)


def test_authorization_preserves_state_and_config_snapshot(gateway, config_overrides):
    config_overrides(CONSOLE_API_URL="https://api.invalid")
    url = gateway.get_authorization_url(
        OAuthAuthorizationRequest(
            invite_token="invite", timezone="Asia/Shanghai", language="zh-Hans", redirect_url="/apps?x=1#fragment"
        )
    )
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "provider.invalid", "/authorize")
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://api.invalid/console/api/oauth/authorize/oauth2"]
    assert decode_oauth_state(query["state"][0]) == {
        "invite_token": "invite",
        "timezone": "Asia/Shanghai",
        "language": "zh-Hans",
        "redirect_url": "/apps?x=1#fragment",
    }


@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        (None, "https://api.invalid/console/api/oauth/authorize/oauth2"),
        ("", "https://api.invalid/console/api/oauth/authorize/oauth2"),
        ("  ", "https://api.invalid/console/api/oauth/authorize/oauth2"),
        ("https://public.invalid/sso/callback?tenant=a", "https://public.invalid/sso/callback?tenant=a"),
        (" https://public.invalid/sso/callback ", "https://public.invalid/sso/callback"),
    ],
)
def test_callback_configuration_is_used_in_authorization_and_exchange(
    gateway, sqlite_session_factory, config_overrides, monkeypatch, redirect_uri, expected
):
    config_overrides(CONSOLE_API_URL="https://api.invalid/")
    with sqlite_session_factory() as session:
        row = session.get(SystemIntegrationExtend, 1)
        config = json.loads(row.config)
        if redirect_uri is not None:
            config["redirect_uri"] = redirect_uri
        row.config = json.dumps(config)
        session.commit()

    query = parse_qs(urlsplit(gateway.get_authorization_url(OAuthAuthorizationRequest())).query)
    assert query["redirect_uri"] == [expected]

    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["data"])
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "provider-token"})

    monkeypatch.setattr("libs.oauth.requests.post", post)
    monkeypatch.setattr(OaOAuth, "get_raw_user_info", lambda _self, _token: {"sub": "id", "email": "a@example.com"})
    gateway.get_identity("code")
    assert calls[0]["redirect_uri"] == expected


@pytest.mark.parametrize("redirect_uri", ["/callback", "javascript:alert(1)", "https://public.invalid/cb#x", 123])
def test_invalid_callback_configuration_is_rejected(gateway, sqlite_session_factory, monkeypatch, redirect_uri):
    with sqlite_session_factory() as session:
        row = session.get(SystemIntegrationExtend, 1)
        config = json.loads(row.config)
        config["redirect_uri"] = redirect_uri
        row.config = json.dumps(config)
        session.commit()
    with pytest.raises(OAuthProviderAuthorizationError, match="Invalid OAuth2 configuration"):
        gateway.get_authorization_url(OAuthAuthorizationRequest())

    def post(*_args, **_kwargs):
        pytest.fail("Invalid callback must not reach token endpoint")

    monkeypatch.setattr("libs.oauth.requests.post", post)
    with pytest.raises(OAuthProviderAuthorizationError):
        gateway.get_identity("code")


@pytest.mark.parametrize(
    "token_response", ["provider-token", {"access_token": "provider-token", "id_token": "casdoor-id"}]
)
def test_real_oa_exchange_and_userinfo_mapping(gateway, monkeypatch, token_response):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: token_response)

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            status_code=200, json=lambda: {"sub": "provider-user", "name": "User", "email": "user@example.com"}
        )

    monkeypatch.setattr("libs.oauth.requests.post", post)
    monkeypatch.setattr("libs.oauth.requests.get", get)
    identity = gateway.get_identity("code-1")
    assert (identity.id, identity.name, identity.email) == ("provider-user", "User", "user@example.com")
    assert identity.id_token == ("casdoor-id" if isinstance(token_response, dict) else None)
    assert calls[0][0] == "https://provider.invalid/token"
    assert calls[0][1]["data"]["code"] == "code-1"
    assert calls[1][1]["headers"] == {"Authorization": "Bearer provider-token"}


@pytest.mark.parametrize("response", [None, "", " ", {}, {"access_token": 123}, ["token"]])
def test_malformed_access_token_is_rejected(gateway, monkeypatch, response):
    monkeypatch.setattr(OaOAuth, "get_access_token", lambda _self, _code: response)
    with pytest.raises(OAuthProviderAuthorizationError, match="Invalid OAuth2 provider response"):
        gateway.get_identity("code")


@pytest.mark.parametrize("identity", [{}, {"sub": "id"}, {"email": "user@example.com"}])
def test_missing_identity_fields_are_rejected(gateway, monkeypatch, identity):
    monkeypatch.setattr(OaOAuth, "get_raw_user_info", lambda _self, _token: identity)
    with pytest.raises(OAuthProviderAuthorizationError):
        gateway.get_identity_from_token("token")


def test_token_callback_skips_exchange(gateway, monkeypatch):
    def unexpected(_self, _code):
        pytest.fail("Token callback must not perform code exchange")

    monkeypatch.setattr(OaOAuth, "get_access_token", unexpected)
    monkeypatch.setattr(OaOAuth, "get_raw_user_info", lambda _self, token: {"sub": token, "email": "user@example.com"})
    assert gateway.get_identity_from_token("existing-provider-token").id == "existing-provider-token"


@pytest.mark.parametrize("enabled", [False, None])
def test_missing_or_disabled_provider_is_rejected(gateway, sqlite_session_factory, enabled):
    with sqlite_session_factory() as session:
        row = session.get(SystemIntegrationExtend, 1)
        if enabled is None:
            session.delete(row)
        else:
            row.status = enabled
        session.commit()
    with pytest.raises(InvalidOAuthProviderError):
        gateway.get_identity("code")


@pytest.mark.parametrize("config", [None, "not-json", "[]"])
def test_invalid_configuration_returns_domain_error(gateway, sqlite_session_factory, config):
    with sqlite_session_factory() as session:
        session.get(SystemIntegrationExtend, 1).config = config
        session.commit()
    with pytest.raises(OAuthProviderAuthorizationError, match="Invalid OAuth2 configuration"):
        gateway.get_authorization_url(OAuthAuthorizationRequest())


def test_provider_network_error_is_sanitized(gateway, monkeypatch):
    def fail(_self, _code):
        raise requests.RequestException("provider-secret")

    monkeypatch.setattr(OaOAuth, "get_access_token", fail)
    with pytest.raises(OAuthProviderRequestError) as error:
        gateway.get_identity("code")
    assert "provider-secret" not in str(error.value)
