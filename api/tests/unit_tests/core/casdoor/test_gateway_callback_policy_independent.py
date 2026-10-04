"""Independent callback loopback boundary check using the real gateway owners."""

from uuid import UUID

import httpx
import pytest
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import CasdoorTokenGateway, GatewayError, GatewayOperation
from core.helper import ssrf_proxy


def test_loopback_callback_permission_does_not_allow_http_discovery_issuer_or_token_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CasdoorConfiguration(
        browser_frontend_url="https://front.example.test:8000",
        backend_api_url="https://back.example.test:8000",
        expected_issuer="https://issuer.example.test",
        organization="independent-org",
        application="independent-app",
        client_id="independent-client",
        default_workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
    )
    callback = "http://127.0.0.1:5001/console/api/auth/casdoor/callback"
    operation = GatewayOperation(
        config,
        "independent-synthetic-secret",
        callback,
        allow_loopback_http=True,
    )
    calls: list[tuple[str, str]] = []

    def reply(method: str, url: str, **kwargs: object) -> httpx.Response:
        calls.append((method, url))
        assert kwargs["deadline"] == operation.deadline
        assert kwargs["follow_redirects"] is False
        return httpx.Response(
            200,
            json={
                "issuer": "http://127.0.0.1:5001",
                "authorization_endpoint": config.browser_frontend_url + "/login/oauth/authorize",
                "token_endpoint": config.backend_api_url + "/api/login/oauth/access_token",
            },
        )

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", reply)
    with pytest.raises(GatewayError, match="discovery_issuer") as error:
        operation.discover()
    assert error.value.code == CasdoorErrorCode.PROVIDER_UNAVAILABLE
    assert calls == [("GET", config.backend_api_url + "/.well-known/openid-configuration")]

    with pytest.raises(GatewayError, match="operation_stopped"):
        CasdoorTokenGateway(operation).exchange_code("synthetic-code", "v" * 43)
    assert calls == [("GET", config.backend_api_url + "/.well-known/openid-configuration")]
