"""Independent contract checks for the controlled Casdoor gateway boundary."""

import base64
import json
import time
from dataclasses import replace
from typing import Any
from urllib.parse import unquote_plus
from uuid import UUID

import httpx
import pytest
import requests
from casdoor import CasdoorSDK
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import (
    JSON_LIMIT,
    ROLES_LIMIT,
    CasdoorDirectoryGateway,
    CasdoorTokenGateway,
    DirectoryDeploymentProof,
    GatewayError,
    GatewayOperation,
    RawTokens,
)
from core.helper import ssrf_proxy
from pydantic import ValidationError

ORG = "independent-org"
SUB = "verified-subject-from-claims-owner"
CLIENT_ID = "client +:/密钥"
CLIENT_SECRET = "secret +:/密钥"
REDIRECT = "https://console.example.test/casdoor/callback"
VERIFIER = "v" * 43


def make_operation(**changes: Any) -> GatewayOperation:
    config = CasdoorConfiguration(
        browser_frontend_url=changes.pop("frontend", "https://front.example.test:8000"),
        backend_api_url=changes.pop("backend", "https://back.example.test:8000"),
        expected_issuer=changes.pop("issuer", "https://issuer.example.test"),
        organization=ORG,
        application="independent-app",
        client_id=CLIENT_ID,
        default_workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
    )
    return GatewayOperation(config, CLIENT_SECRET, REDIRECT, **changes)


def synthetic_proof(operation: GatewayOperation) -> DirectoryDeploymentProof:
    config = operation.config
    return DirectoryDeploymentProof(
        config.expected_issuer,
        config.organization,
        config.application,
        config.client_id,
        "1" * 64,
        "2" * 64,
        "3" * 64,
    )


class SyntheticDirectoryCredential:
    """A test-only credential protocol, with no claim of live-server support."""

    def __init__(self, operation: GatewayOperation):
        self.proof = synthetic_proof(operation)

    def authorization(self, client_id: str, client_secret: str) -> str:
        assert client_id == CLIENT_ID
        assert client_secret == CLIENT_SECRET
        return "Synthetic test-only credential"


def directory(operation: GatewayOperation) -> CasdoorDirectoryGateway:
    return CasdoorDirectoryGateway(
        operation,
        verified_subject=SUB,
        credential_strategy=SyntheticDirectoryCredential(operation),
    )


def json_response(
    value: Any, *, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(value, ensure_ascii=False).encode("utf-8"),
        headers=headers or {"Content-Type": "application/json"},
    )


@pytest.fixture
def fake_transport(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []
    replies: list[httpx.Response | Exception] = []

    def send(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", send)
    return calls, replies


def test_token_facade_uses_only_the_sdk_instance_seam_and_encoded_basic_auth(
    fake_transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    facade = CasdoorTokenGateway(operation)
    assert isinstance(facade._sdk, CasdoorSDK)
    assert facade._sdk.endpoint == "https://back.example.test:8000"
    assert facade._sdk.front_endpoint == "https://front.example.test:8000"

    # Unsafe helpers and module level requests remain outside the adapter path.
    for name in (
        "get_auth_link",
        "get_oauth_token",
        "get_user",
        "get_roles",
        "get_organization",
    ):
        monkeypatch.setattr(
            CasdoorSDK,
            name,
            lambda *args, _name=name, **kwargs: pytest.fail(
                f"SDK helper {_name} called"
            ),
        )
    for name in ("get", "post", "request"):
        monkeypatch.setattr(
            requests,
            name,
            lambda *args, **kwargs: pytest.fail("global requests path used"),
        )

    expected_tokens = {
        "id_token": "synthetic-id-token",
        "access_token": "synthetic-access",
        "token_type": "Bearer",
    }
    replies.append(json_response(expected_tokens))
    raw = facade.exchange_code("synthetic-code", VERIFIER)
    assert isinstance(raw, RawTokens)
    assert raw.payload == expected_tokens
    assert repr(raw) == "RawTokens(<redacted>)"
    assert "synthetic-id-token" not in repr(raw)
    assert "synthetic-id-token" not in repr(operation)

    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://back.example.test:8000/api/login/oauth/access_token"
    assert call["data"] == {
        "grant_type": "authorization_code",
        "code": "synthetic-code",
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    assert "client_secret" not in call["data"]
    basic = base64.b64decode(
        call["headers"]["Authorization"].removeprefix("Basic ")
    ).decode("ascii")
    encoded_id, encoded_secret = basic.split(":", maxsplit=1)
    assert unquote_plus(encoded_id) == CLIENT_ID
    assert unquote_plus(encoded_secret) == CLIENT_SECRET
    assert CLIENT_SECRET not in call["url"] and "synthetic-code" not in call["url"]
    assert call["params"] is None
    with pytest.raises(GatewayError, match="exchange_already_started"):
        facade.exchange_code("second-code", VERIFIER)
    assert len(calls) == 1


def test_fixed_routes_shared_budget_and_no_default_directory_auth(
    fake_transport,
) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    assert 0 < operation.deadline - time.monotonic() <= 45
    organization_data = {"owner": "admin", "name": ORG, "accountItems": []}
    replies.extend(
        [
            json_response(
                {
                    "issuer": operation.config.expected_issuer,
                    "authorization_endpoint": "https://front.example.test:8000/login/oauth/authorize",
                    "token_endpoint": "https://back.example.test:8000/api/login/oauth/access_token",
                }
            ),
            json_response(
                {
                    "status": "ok",
                    "data": {
                        "owner": ORG,
                        "id": SUB,
                        "name": "person",
                        "isForbidden": False,
                        "isDeleted": False,
                    },
                }
            ),
            json_response({"status": "ok", "data": [{"owner": ORG, "name": "role"}]}),
            json_response({"status": "ok", "data": organization_data}),
        ]
    )

    assert operation.discover().token_endpoint.endswith("/api/login/oauth/access_token")
    no_strategy = CasdoorDirectoryGateway(operation, verified_subject=SUB)
    with pytest.raises(GatewayError, match="directory_credential_unsupported") as error:
        no_strategy.get_verified_user()
    assert error.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    # The unsupported attempt stopped the shared operation before any directory GET.
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("frontend", "backend"),
    [
        ("https://front.example.test:8000/path", "https://back.example.test:8000"),
        ("https://front.example.test:8000", "https://user:pw@back.example.test:8000"),
        ("https://front.example.test:8000", "https://back.example.test:8000?x=y"),
    ],
)
def test_configured_frontend_and_backend_reject_ambiguous_roots(
    fake_transport, frontend, backend
) -> None:
    calls, _ = fake_transport
    with pytest.raises((GatewayError, ValidationError)):
        make_operation(frontend=frontend, backend=backend)
    assert calls == []


def test_discovery_issuer_must_exactly_match_configuration(fake_transport) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    replies.append(
        json_response(
            {
                "issuer": "https://issuer.example.test/",
                "authorization_endpoint": "https://front.example.test:8000/login/oauth/authorize",
                "token_endpoint": "https://back.example.test:8000/api/login/oauth/access_token",
            }
        )
    )
    with pytest.raises(GatewayError, match="discovery_issuer"):
        operation.discover()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "advertised",
    [
        "https://attacker.example.test/api/login/oauth/access_token",
        "https://back.example.test:8000/api/login/oauth/access_token?next=x",
        "https://back.example.test:8000/prefix/api/login/oauth/access_token",
        "http://back.example.test:8000/api/login/oauth/access_token",
    ],
)
def test_discovery_advertisements_cannot_expand_dispatch_targets(
    fake_transport, advertised
) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    replies.append(
        json_response(
            {
                "issuer": operation.config.expected_issuer,
                "authorization_endpoint": "https://front.example.test:8000/login/oauth/authorize",
                "token_endpoint": advertised,
            }
        )
    )
    with pytest.raises(GatewayError, match="discovery_endpoint"):
        operation.discover()
    with pytest.raises(GatewayError, match="operation_stopped"):
        CasdoorTokenGateway(operation).exchange_code("code", VERIFIER)
    assert len(calls) == 1


def test_directory_gets_are_exact_and_bound_to_deployment_fingerprints(
    fake_transport,
) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    strategy = SyntheticDirectoryCredential(operation)
    gateway = CasdoorDirectoryGateway(
        operation, verified_subject=SUB, credential_strategy=strategy
    )
    replies.extend(
        [
            json_response(
                {
                    "status": "ok",
                    "data": {
                        "owner": ORG,
                        "id": SUB,
                        "name": "person",
                        "isForbidden": False,
                        "isDeleted": False,
                    },
                }
            ),
            json_response({"status": "ok", "data": [{"owner": ORG, "name": "role"}]}),
            json_response(
                {
                    "status": "ok",
                    "data": {"owner": "admin", "name": ORG, "viewRule": "public"},
                }
            ),
        ]
    )
    assert gateway.get_verified_user()["id"] == SUB
    assert gateway.get_complete_roles() == [{"owner": ORG, "name": "role"}]
    # This is a raw response candidate; absent accountItems stays absent.
    assert gateway.get_organization_visibility() == {
        "owner": "admin",
        "name": ORG,
        "viewRule": "public",
    }
    assert gateway.deployment_proof == strategy.proof

    assert [(call["method"], call["url"], call["params"]) for call in calls] == [
        (
            "GET",
            "https://back.example.test:8000/api/get-user",
            {"owner": ORG, "userId": SUB},
        ),
        ("GET", "https://back.example.test:8000/api/get-roles", {"owner": ORG}),
        (
            "GET",
            "https://back.example.test:8000/api/get-organization",
            {"id": f"admin/{ORG}"},
        ),
    ]
    for call in calls:
        assert call["headers"]["Authorization"] == "Synthetic test-only credential"
        assert CLIENT_SECRET not in call["url"]
        assert call["headers"]["Accept-Encoding"] == "identity"
        assert call["max_retries"] == 0
        assert call["follow_redirects"] is False
        assert call["ssl_verify"] is True
        assert call["deadline"] == operation.deadline
        assert call["request_timeout"] == 15.0
    assert [call["max_response_bytes"] for call in calls] == [
        JSON_LIMIT,
        ROLES_LIMIT,
        JSON_LIMIT,
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_issuer", "https://other-issuer.example.test"),
        ("organization", "other-org"),
        ("application", "other-app"),
        ("client_id", "other-client"),
        ("release_fingerprint", "invalid"),
        ("creator_proof_fingerprint", "invalid"),
        ("credential_proof_fingerprint", "invalid"),
    ],
)
def test_deployment_proof_mismatch_fails_before_directory_dispatch(
    fake_transport, field, value
) -> None:
    calls, _ = fake_transport
    operation = make_operation()
    strategy = SyntheticDirectoryCredential(operation)
    strategy.proof = replace(strategy.proof, **{field: value})
    with pytest.raises(GatewayError, match="directory_proof"):
        CasdoorDirectoryGateway(
            operation, verified_subject=SUB, credential_strategy=strategy
        ).get_verified_user()
    assert calls == []


@pytest.mark.parametrize(
    "body,headers",
    [
        (b'{"a":1,"a":2}', {"Content-Type": "application/json"}),
        (b'{"outer":{"a":1,"a":2}}', {"Content-Type": "application/json"}),
        (b'{"number":1e9999}', {"Content-Type": "application/json"}),
        (b'{"n":NaN}', {"Content-Type": "application/json"}),
        (b"\xff", {"Content-Type": "application/json"}),
        (b"{}", {"Content-Type": "application/json", "Content-Encoding": "gzip"}),
        (b"{}", {"Content-Type": "application/json; charset=iso-8859-1"}),
        (b"x" * (JSON_LIMIT + 1), {"Content-Type": "application/json"}),
    ],
)
def test_strict_raw_json_and_response_policy(fake_transport, body, headers) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    reply = httpx.Response(200, content=body)
    reply.headers.update(headers)
    replies.append(reply)
    with pytest.raises(GatewayError):
        operation.discover()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok"},
        {"status": "ok", "data": None},
        {"status": "ok", "data": [], "data2": {"next": "cursor"}},
        {"status": "ok", "data": [], "partial": False},
        {"status": "error", "data": []},
    ],
)
def test_directory_schema_unknowns_are_not_coerced_to_empty_or_complete(
    fake_transport, payload
) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    replies.append(json_response(payload))
    with pytest.raises(GatewayError) as error:
        directory(operation).get_complete_roles()
    assert error.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert error.value.retry_allowed is False
    assert len(calls) == 1


def test_unknown_dispatch_termination_stops_later_requests_without_retry(
    fake_transport,
) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    replies.append(ssrf_proxy.RequestTerminationUnconfirmedError(CLIENT_SECRET))
    with pytest.raises(GatewayError, match="termination_unconfirmed") as error:
        CasdoorTokenGateway(operation).exchange_code("synthetic-code", VERIFIER)
    assert error.value.termination_confirmed is False
    assert error.value.retry_allowed is False
    assert CLIENT_SECRET not in str(error.value)
    with pytest.raises(GatewayError, match="operation_stopped"):
        directory(operation).get_complete_roles()
    assert len(calls) == 1


def test_provider_exception_text_is_never_returned_or_retried(fake_transport) -> None:
    calls, replies = fake_transport
    operation = make_operation()
    replies.append(httpx.ConnectError(CLIENT_SECRET))
    with pytest.raises(GatewayError) as error:
        CasdoorTokenGateway(operation).exchange_code("synthetic-code", VERIFIER)
    assert error.value.retry_allowed is False
    assert CLIENT_SECRET not in str(error.value)
    with pytest.raises(GatewayError, match="operation_stopped"):
        operation.discover()
    assert len(calls) == 1


def test_expired_and_overlong_callback_budgets_never_dispatch(fake_transport) -> None:
    calls, _ = fake_transport
    now = time.monotonic()
    for deadline in (now - 1, now + 46, float("nan"), float("inf")):
        with pytest.raises(GatewayError, match="deadline"):
            make_operation(deadline=deadline)
    assert calls == []
