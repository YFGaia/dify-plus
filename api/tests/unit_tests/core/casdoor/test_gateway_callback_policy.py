"""Callback-only loopback policy through actual owners; synthetic offline I/O."""

import base64
import json
import time
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit
from uuid import UUID

import httpx
import pytest
from casdoor import CasdoorSDK
from core.casdoor import gateway
from core.casdoor.auth_transactions import CookiePolicy
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import CasdoorTokenGateway, GatewayError, GatewayOperation
from core.helper import ssrf_proxy

CALLBACK_PATH = "/console/api/auth/casdoor/callback"
HTTPS_CALLBACK = "https://console.example.test" + CALLBACK_PATH
HTTP_CALLBACKS = ["http://" + host + ":5001" + CALLBACK_PATH for host in ("localhost", "127.0.0.1", "[::1]")]
SECRET = "synthetic-secret +:/密钥"
VERIFIER = "v" * 43
_REAL_REQUEST = ssrf_proxy.make_request_with_deadline


def configuration() -> CasdoorConfiguration:
    return CasdoorConfiguration(
        browser_frontend_url="https://front.example.test:8000",
        backend_api_url="https://back.example.test:8000",
        expected_issuer="https://issuer.example.test",
        organization="synthetic-org",
        application="synthetic-app",
        client_id="synthetic-client +:/密钥",
        default_workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
    )


def operation(callback: str = HTTPS_CALLBACK, **changes: Any) -> GatewayOperation:
    return GatewayOperation(configuration(), SECRET, callback, **changes)


@pytest.fixture(autouse=True)
def deny_provider_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unexpected provider dispatch")

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", deny)


@pytest.mark.parametrize("callback", [HTTPS_CALLBACK, "https://console.example.test/old/arbitrary/callback"])
@pytest.mark.parametrize("kwargs", [{}, {"allow_loopback_http": False}, {"allow_loopback_http": True}])
def test_https_callback_and_legacy_paths_unchanged(callback: str, kwargs: dict[str, bool]) -> None:
    op = operation(callback, **kwargs)
    assert op.registered_redirect_uri == callback
    assert op.allow_loopback_http is kwargs.get("allow_loopback_http", False)


@pytest.mark.parametrize("callback", HTTP_CALLBACKS)
@pytest.mark.parametrize("kwargs", [{}, {"allow_loopback_http": False}])
def test_http_callback_requires_explicit_permission(callback: str, kwargs: dict[str, bool]) -> None:
    with pytest.raises(GatewayError, match="url_policy") as error:
        operation(callback, **kwargs)
    assert error.value.code == CasdoorErrorCode.CONFIG_CONFLICT


@pytest.mark.parametrize("flag", [0, 1, "true", "false", None, [], {}])
@pytest.mark.parametrize("callback", [HTTPS_CALLBACK, HTTP_CALLBACKS[0]])
def test_permission_is_strict_bool(callback: str, flag: Any) -> None:
    with pytest.raises(GatewayError, match="url_policy"):
        operation(callback, allow_loopback_http=flag)


@pytest.mark.parametrize("callback", HTTP_CALLBACKS + ["http://localhost" + CALLBACK_PATH])
def test_explicit_loopback_permission_uses_original_cookie_policy(callback: str) -> None:
    parsed = urlsplit(callback)
    assert not CookiePolicy(f"{parsed.scheme}://{parsed.netloc}", allow_loopback_http=True).secure
    assert operation(callback, allow_loopback_http=True).registered_redirect_uri == callback


@pytest.mark.parametrize(
    "callback",
    [
        "http://console.example.test" + CALLBACK_PATH,
        "http://localhost.example.test" + CALLBACK_PATH,
        "http://localhost." + CALLBACK_PATH,
        "http://127.1" + CALLBACK_PATH,
        "http://127.0.0.2" + CALLBACK_PATH,
        "http://2130706433" + CALLBACK_PATH,
        "http://0x7f000001" + CALLBACK_PATH,
        "http://[::ffff:127.0.0.1]" + CALLBACK_PATH,
        "http://[::1%25lo0]" + CALLBACK_PATH,
        "http://%6cocalhost" + CALLBACK_PATH,
        "http://localhost@evil.example.test" + CALLBACK_PATH,
        "http://user:password@localhost" + CALLBACK_PATH,
        "http://@localhost" + CALLBACK_PATH,
        "http://localhost:0" + CALLBACK_PATH,
        "http://localhost:65536" + CALLBACK_PATH,
        "http://localhost:invalid" + CALLBACK_PATH,
        "http://[::1" + CALLBACK_PATH,
        "http:///" + CALLBACK_PATH,
        "http://localhost" + CALLBACK_PATH + "?",
        "http://localhost" + CALLBACK_PATH + "?x=1",
        "http://localhost" + CALLBACK_PATH + "#",
        "http://localhost" + CALLBACK_PATH + "#fragment",
        "http://localhost" + CALLBACK_PATH + "\\suffix",
        "http://localhost" + CALLBACK_PATH + "\n",
        "http://localhost" + CALLBACK_PATH + "\t",
        "http://localhost" + CALLBACK_PATH + "\x00",
        "http://localhost" + CALLBACK_PATH + "\x7f",
        "http://localhost" + CALLBACK_PATH + " ",
        "http://localhost/" + "x" * 2048,
        "ftp://localhost" + CALLBACK_PATH,
    ],
)
def test_permission_preserves_callback_syntax_and_exact_loopback_restrictions(callback: str) -> None:
    with pytest.raises(GatewayError, match="url_policy") as error:
        operation(callback, allow_loopback_http=True)
    assert error.value.code == CasdoorErrorCode.CONFIG_CONFLICT
    assert error.value.retry_allowed is False
    assert str(error.value) == "casdoor_gateway_url_policy"


@pytest.mark.parametrize("field", ["backend_api_url", "browser_frontend_url", "expected_issuer"])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_callback_permission_never_allows_http_provider_urls(field: str, host: str) -> None:
    # Bypass the separate configuration validator to exercise the actual gateway.
    config = configuration().model_copy(update={field: "http://" + host + ":8000"})
    with pytest.raises(GatewayError, match="url_policy"):
        GatewayOperation(config, SECRET, HTTP_CALLBACKS[0], allow_loopback_http=True)


def test_unexpected_cookie_policy_exception_is_fixed_gateway_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_policy(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic-private-policy-error")

    monkeypatch.setattr(gateway, "CookiePolicy", broken_policy)
    with pytest.raises(GatewayError) as error:
        operation(HTTP_CALLBACKS[0], allow_loopback_http=True)
    assert error.value.code == CasdoorErrorCode.CONFIG_CONFLICT
    assert str(error.value) == "casdoor_gateway_url_policy"
    assert error.value.__suppress_context__ is True


def test_original_positional_deadline_and_keyword_only_permission() -> None:
    deadline = time.monotonic() + 20
    op = GatewayOperation(configuration(), SECRET, HTTP_CALLBACKS[0], deadline, allow_loopback_http=True)
    assert op.deadline == deadline
    with pytest.raises(TypeError):
        GatewayOperation(configuration(), SECRET, HTTP_CALLBACKS[0], deadline, True)


@pytest.mark.parametrize("key", ["authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri"])
@pytest.mark.parametrize("bad", ["http://localhost:5001/evil", "https://attacker.example.test/evil"])
def test_loopback_callback_does_not_expand_discovery_targets(
    monkeypatch: pytest.MonkeyPatch, key: str, bad: str
) -> None:
    op = operation(HTTP_CALLBACKS[0], allow_loopback_http=True)
    raw = {
        "issuer": op.config.expected_issuer,
        "authorization_endpoint": op.config.browser_frontend_url + "/login/oauth/authorize",
        "token_endpoint": op.config.backend_api_url + "/api/login/oauth/access_token",
        "userinfo_endpoint": op.config.backend_api_url + "/api/userinfo",
        "jwks_uri": op.config.backend_api_url + "/.well-known/jwks",
    }
    raw[key] = bad
    calls: list[str] = []

    def reply(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls.append(url)
        assert kwargs["deadline"] == op.deadline
        return httpx.Response(200, json=raw)

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", reply)
    with pytest.raises(GatewayError, match="discovery_endpoint"):
        op.discover()
    with pytest.raises(GatewayError, match="operation_stopped"):
        CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    assert calls == [op.config.backend_api_url + "/.well-known/openid-configuration"]


@pytest.mark.parametrize("callback", HTTP_CALLBACKS)
def test_original_ssrf_owner_sends_exact_http_callback_form_to_https_provider(
    monkeypatch: pytest.MonkeyPatch, callback: str
) -> None:
    deadline = time.monotonic() + 20
    op = operation(callback, deadline=deadline, allow_loopback_http=True)
    requests: list[httpx.Request] = []
    tokens = {"id_token": "synthetic-id", "access_token": "synthetic-access", "token_type": "Bearer"}

    async def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=json.dumps(tokens).encode(), headers={"Content-Type": "application/json"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(reply))

    def dispatch(method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert kwargs["deadline"] == deadline
        assert kwargs["max_retries"] == 0
        assert kwargs["follow_redirects"] is False
        assert kwargs["ssl_verify"] is True
        assert kwargs["request_timeout"] == 15.0
        assert kwargs["params"] is None
        return _REAL_REQUEST(method, url, **kwargs)

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", dispatch)
    monkeypatch.setattr(ssrf_proxy, "_build_deadline_ssrf_client", lambda *args, **kwargs: client)
    facade = CasdoorTokenGateway(op)
    assert isinstance(facade._sdk, CasdoorSDK)
    assert facade.exchange_code("synthetic-code", VERIFIER).payload == tokens
    assert client.is_closed and op.deadline == deadline
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == op.config.backend_api_url + "/api/login/oauth/access_token"
    assert request.url.query == b""
    assert parse_qs(request.content.decode(), strict_parsing=True) == {
        "grant_type": ["authorization_code"],
        "code": ["synthetic-code"],
        "redirect_uri": [callback],
        "code_verifier": [VERIFIER],
    }
    encoded = base64.b64decode(request.headers["Authorization"].removeprefix("Basic ")).decode("ascii")
    assert encoded == f"{quote_plus(op.config.client_id)}:{quote_plus(SECRET)}"
    assert request.headers["Accept-Encoding"] == "identity"
    with pytest.raises(GatewayError, match="exchange_already_started"):
        facade.exchange_code("synthetic-code", VERIFIER)
    assert len(requests) == 1


def test_http_callback_cannot_be_replaced_in_controlled_sdk_form() -> None:
    op = operation(HTTP_CALLBACKS[0], allow_loopback_http=True)
    with pytest.raises(GatewayError, match="exchange_input"):
        CasdoorTokenGateway(op)._sdk._oauth_token_request(
            {
                "grant_type": "authorization_code",
                "code": "synthetic-code",
                "redirect_uri": HTTP_CALLBACKS[1],
                "code_verifier": VERIFIER,
            }
        )
