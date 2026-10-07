"""Isolated synthetic gateway tests; no real credentials, IdP or environment reads."""

import base64
import json
import time
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlencode
from uuid import UUID

import httpx
import pytest
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

SECRET = "synthetic-secret +:/密钥"
SUBJECT = "synthetic-subject"
VERIFIER = "v" * 43


def configuration(**changes: Any) -> CasdoorConfiguration:
    return CasdoorConfiguration(
        browser_frontend_url=changes.get("browser_frontend_url", "https://front.example.test:8000"),
        backend_api_url=changes.get("backend_api_url", "https://back.example.test:8000"),
        expected_issuer=changes.get("expected_issuer", "https://issuer.example.test"),
        organization="synthetic-org",
        application="synthetic-app",
        client_id="client +:/密钥",
        default_workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
    )


def operation(**kwargs: Any) -> GatewayOperation:
    return GatewayOperation(
        config=kwargs.pop("config", configuration()),
        client_secret=SECRET,
        registered_redirect_uri="https://console.example.test/console/api/casdoor/callback",
        **kwargs,
    )


def proof(op: GatewayOperation) -> DirectoryDeploymentProof:
    c = op.config
    return DirectoryDeploymentProof(
        c.expected_issuer, c.organization, c.application, c.client_id, "a" * 64, "b" * 64, "c" * 64
    )


class SyntheticStrategy:
    """Unit-only trusted strategy; never evidence of real Basic directory support."""

    def __init__(self, op: GatewayOperation):
        self.proof = proof(op)

    def authorization(self, client_id: str, client_secret: str) -> str:
        return "Synthetic synthetic-offline-application-credential"


def directory(op: GatewayOperation) -> CasdoorDirectoryGateway:
    return CasdoorDirectoryGateway(op, verified_subject=SUBJECT, credential_strategy=SyntheticStrategy(op))


def response(raw: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(raw).encode(), headers=headers or {"Content-Type": "application/json"}
    )


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], list[Any]]:
    calls: list[dict[str, Any]] = []
    replies: list[Any] = []

    def fake(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", fake)
    return calls, replies


def tokens() -> dict[str, str]:
    return {"access_token": "synthetic-access", "id_token": "synthetic-id", "token_type": "Bearer"}


def user(**changes: Any) -> dict[str, Any]:
    return {
        "owner": "synthetic-org",
        "id": SUBJECT,
        "name": "synthetic-name",
        "isForbidden": False,
        "isDeleted": False,
        **changes,
    }


def discovery(op: GatewayOperation) -> dict[str, str]:
    return {
        "issuer": op.config.expected_issuer,
        "authorization_endpoint": op._frontend + "/login/oauth/authorize",
        "token_endpoint": op._backend + "/api/login/oauth/access_token",
        "userinfo_endpoint": op._backend + "/api/userinfo",
        "jwks_uri": op._backend + "/.well-known/jwks",
    }


def test_sdk_seam_form_basic_and_no_port_rewrite(dispatch, monkeypatch) -> None:
    calls, replies = dispatch
    op = operation()
    facade = CasdoorTokenGateway(op)
    assert isinstance(facade._sdk, CasdoorSDK)
    assert facade._sdk.front_endpoint == "https://front.example.test:8000"
    assert not hasattr(facade, "get_oauth_token")
    for name in ("get_auth_link", "get_user", "get_roles", "get_organization", "get_oauth_token"):
        monkeypatch.setattr(CasdoorSDK, name, lambda *a, **k: pytest.fail("unsafe SDK method called"))
    replies.append(response(tokens()))
    raw = facade.exchange_code("synthetic-code", VERIFIER)
    assert isinstance(raw, RawTokens) and raw.payload == tokens()
    assert "synthetic-id" not in repr(raw) and SECRET not in repr(op)
    call = calls[0]
    basic = base64.b64decode(call["headers"]["Authorization"].split()[1]).decode()
    client_id, secret = basic.split(":")
    assert parse_qs("x=" + client_id)["x"] == [op.config.client_id]
    assert parse_qs("x=" + secret)["x"] == [SECRET]
    assert call["data"] == {
        "grant_type": "authorization_code",
        "code": "synthetic-code",
        "redirect_uri": op.registered_redirect_uri,
        "code_verifier": VERIFIER,
    }
    assert "client_secret" not in call["data"]
    assert SECRET not in call["url"] and "synthetic-code" not in call["url"]
    with pytest.raises(GatewayError, match="exchange_already_started"):
        facade.exchange_code("another-code", VERIFIER)
    assert len(calls) == 1


def test_shared_deadline_all_requests_fixed_routes_and_limits(dispatch) -> None:
    calls, replies = dispatch
    op = operation()
    d = directory(op)
    replies.extend(
        [
            response(discovery(op)),
            response(tokens()),
            response({"sub": SUBJECT}),
            response({"status": "ok", "data": user(), "data2": None}),
            response({"status": "ok", "data": [{"owner": "synthetic-org", "name": "r"}]}),
            response(
                {
                    "status": "ok",
                    "data": {
                        "owner": "admin",
                        "name": "synthetic-org",
                        "accountItems": [],
                        "viewRule": "synthetic",
                        "discard": 1,
                    },
                }
            ),
        ]
    )
    endpoints = op.discover()
    assert endpoints.authorization_endpoint.startswith(op._frontend)
    CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    assert op.userinfo("synthetic-access") == {"sub": SUBJECT}
    assert d.get_verified_user() == user()
    assert d.get_complete_roles() == [{"owner": "synthetic-org", "name": "r"}]
    assert "discard" not in d.get_organization_visibility()
    assert d.deployment_proof == proof(op)
    assert [c["params"] for c in calls[-3:]] == [
        {"owner": "synthetic-org", "userId": SUBJECT},
        {"owner": "synthetic-org"},
        {"id": "admin/synthetic-org"},
    ]
    for index, call in enumerate(calls):
        assert call["deadline"] == op.deadline
        assert call["request_timeout"] == 15
        assert call["max_response_bytes"] == (ROLES_LIMIT if index == 4 else JSON_LIMIT)
        assert call["max_retries"] == 0 and not call["follow_redirects"] and call["ssl_verify"]
        assert call["headers"]["Accept-Encoding"] == "identity"
        assert "clientSecret" not in urlencode(call["params"] or {})
        assert SECRET not in call["url"]
    assert calls[2]["headers"]["Authorization"] == "Bearer synthetic-access"
    assert all(c["headers"]["Authorization"].startswith("Synthetic ") for c in calls[-3:])


@pytest.mark.parametrize("method", ["get_verified_user", "get_complete_roles", "get_organization_visibility"])
def test_unsupported_directory_never_dispatches(dispatch, method) -> None:
    calls, _ = dispatch
    op = operation()
    d = CasdoorDirectoryGateway(op, verified_subject=SUBJECT)
    assert d.deployment_proof is None
    with pytest.raises(GatewayError) as error:
        getattr(d, method)()
    assert error.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    with pytest.raises(GatewayError, match="operation_stopped"):
        op.userinfo("synthetic-access")
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization", "other-org"),
        ("client_id", "other-client"),
        ("application", "other-app"),
        ("expected_issuer", "https://other.test"),
        ("release_fingerprint", "missing"),
        ("creator_proof_fingerprint", ""),
        ("credential_proof_fingerprint", ""),
    ],
)
def test_directory_proof_binding_precedes_dispatch(dispatch, field, value) -> None:
    calls, _ = dispatch
    op = operation()
    strategy = SyntheticStrategy(op)
    strategy.proof = replace(strategy.proof, **{field: value})
    d = CasdoorDirectoryGateway(op, verified_subject=SUBJECT, credential_strategy=strategy)
    with pytest.raises(GatewayError, match="directory_proof"):
        d.get_verified_user()
    assert not calls


@pytest.mark.parametrize("bad", ["Bearer synthetic-user-token", "Synthetic x\r\nHost: attacker", ""])
def test_untrusted_or_user_bearer_directory_auth_rejected(dispatch, bad) -> None:
    calls, _ = dispatch
    op = operation()
    strategy = SyntheticStrategy(op)
    strategy.authorization = lambda *args: bad
    d = CasdoorDirectoryGateway(op, verified_subject=SUBJECT, credential_strategy=strategy)
    with pytest.raises(GatewayError):
        d.get_verified_user()
    assert not calls


@pytest.mark.parametrize(
    "key,bad",
    [
        ("issuer", "https://issuer.example.test/"),
        ("token_endpoint", "https://attacker.test/api/login/oauth/access_token"),
        ("token_endpoint", "https://back.example.test:8000/api/get-user"),
        ("authorization_endpoint", "https://back.example.test:8000/login/oauth/authorize"),
        ("userinfo_endpoint", "https://back.example.test:8000/api/userinfo?q=secret"),
        ("jwks_uri", "https://attacker.test/key"),
    ],
)
def test_discovery_untrusted_targets_halt(dispatch, key, bad) -> None:
    calls, replies = dispatch
    op = operation()
    raw = discovery(op)
    raw[key] = bad
    replies.append(response(raw))
    with pytest.raises(GatewayError):
        op.discover()
    with pytest.raises(GatewayError, match="operation_stopped"):
        CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,url", [("backend_api_url", "https://back.test/prefix"), ("browser_frontend_url", "https://front.test/?q=x")]
)
def test_base_path_and_query_rejected(dispatch, field, url) -> None:
    calls, _ = dispatch
    with pytest.raises(GatewayError):
        operation(config=configuration(**{field: url}))
    assert not calls


@pytest.mark.parametrize("status", [201, 302, 400, 401, 403, 429, 500, 503])
def test_http_status_no_retry_no_provider_leak(dispatch, status) -> None:
    calls, replies = dispatch
    op = operation()
    replies.append(response({"error": SECRET}, status=status))
    with pytest.raises(GatewayError) as error:
        CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    assert error.value.retry_allowed is False and SECRET not in str(error.value)
    with pytest.raises(GatewayError):
        directory(op).get_complete_roles()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body,headers,reason",
    [
        (b'{"x":1,"x":2}', {"Content-Type": "application/json"}, "response_json"),
        (b'{"x":{"a":1,"a":2}}', {"Content-Type": "application/json"}, "response_json"),
        (b"\xff", {"Content-Type": "application/json"}, "response_json"),
        (b'{"x":NaN}', {"Content-Type": "application/json"}, "response_json"),
        (b"{}", {"Content-Type": "text/html"}, "response_media_type"),
        (b"{}", {"Content-Type": "application/json; charset=latin-1"}, "response_media_type"),
        (b"{}", {"Content-Type": "application/json", "Content-Encoding": "gzip"}, "response_encoding"),
        (b"x" * (JSON_LIMIT + 1), {"Content-Type": "application/json"}, "response_bounds"),
        (b"[]", {"Content-Type": "application/json"}, "response_schema"),
    ],
)
def test_raw_bounded_encoding_duplicate_json(dispatch, body, headers, reason) -> None:
    calls, replies = dispatch
    op = operation()
    # HTTPX would automatically decompress in its constructor. Set wire headers
    # afterwards to simulate the consumed raw response passed by the owner.
    r = httpx.Response(200, content=body)
    r.headers.update(headers)
    replies.append(r)
    with pytest.raises(GatewayError, match=reason):
        op.discover()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure,reason",
    [
        (ssrf_proxy.RequestTerminationUnconfirmedError(SECRET), "termination_unconfirmed"),
        (ssrf_proxy.RequestDeadlineExceededError(SECRET), "deadline"),
        (ssrf_proxy.ResponseTooLargeError(SECRET), "response_bounds"),
        (httpx.ConnectError(SECRET), "transport"),
    ],
)
def test_transport_failure_stops_whole_operation(dispatch, failure, reason) -> None:
    calls, replies = dispatch
    op = operation()
    replies.append(failure)
    with pytest.raises(GatewayError, match=reason) as error:
        CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    assert SECRET not in str(error.value)
    assert error.value.termination_confirmed == (reason != "termination_unconfirmed")
    for followup in (
        op.discover,
        lambda: op.userinfo("synthetic-access"),
        directory(op).get_complete_roles,
        lambda: CasdoorTokenGateway(op).exchange_code("second-code", VERIFIER),
    ):
        with pytest.raises(GatewayError, match="operation_stopped"):
            followup()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "error", "data": []},
        {"status": "ok"},
        {"status": "ok", "data": None},
        {"status": "ok", "data": [], "data2": "partial"},
        {"status": "ok", "data": [{"owner": "other-org", "name": "r"}]},
        {"status": "ok", "data": [{"owner": "synthetic-org"}]},
    ],
)
def test_directory_unknown_never_empty_defaults(dispatch, payload) -> None:
    _, replies = dispatch
    op = operation()
    replies.append(response(payload))
    with pytest.raises(GatewayError) as error:
        directory(op).get_complete_roles()
    assert error.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN


@pytest.mark.parametrize(
    "changes", [{"owner": "other-org"}, {"id": "unverified-sub"}, {"isForbidden": None}, {"isDeleted": 0}, {"name": ""}]
)
def test_user_required_state_and_subject(dispatch, changes) -> None:
    _, replies = dispatch
    op = operation()
    replies.append(response({"status": "ok", "data": user(**changes)}))
    with pytest.raises(GatewayError, match="user_schema"):
        directory(op).get_verified_user()


def test_expired_budget_and_overlarge_budget_rejected_without_dispatch(dispatch) -> None:
    calls, _ = dispatch
    for deadline in (time.monotonic() - 1, time.monotonic() + 46, float("nan"), float("inf")):
        with pytest.raises(GatewayError, match="deadline"):
            operation(deadline=deadline)
    assert not calls


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "synthetic", "token_type": "Bearer"},
        {"id_token": "synthetic", "token_type": "Bearer"},
        {"access_token": "synthetic", "id_token": "synthetic", "token_type": 3},
        {"access_token": "synthetic", "id_token": "\ud800", "token_type": "Bearer"},
        {"error": "synthetic-private-provider-message"},
    ],
)
def test_token_payload_missing_wrong_and_surrogate_are_stable(dispatch, payload) -> None:
    calls, replies = dispatch
    op = operation()
    replies.append(response(payload))
    with pytest.raises(GatewayError):
        CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    with pytest.raises(GatewayError, match="operation_stopped"):
        directory(op).get_verified_user()
    assert len(calls) == 1


def test_private_sdk_grants_cannot_bypass_form_policy(dispatch) -> None:
    calls, _ = dispatch
    op = operation()
    facade = CasdoorTokenGateway(op)
    with pytest.raises(GatewayError, match="exchange_input"):
        facade._sdk.oauth_token_request()
    assert not calls


def test_real_deadline_owner_consumes_sdk_form_once(monkeypatch) -> None:
    op = operation()
    requests: list[httpx.Request] = []

    async def fake(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(tokens())

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(ssrf_proxy, "_build_deadline_ssrf_client", lambda *args, **kwargs: client)
    result = CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    assert result.payload == tokens() and client.is_closed
    assert len(requests) == 1
    form = parse_qs(requests[0].content.decode())
    assert form["redirect_uri"] == [op.registered_redirect_uri]
    assert form["code_verifier"] == [VERIFIER]
    assert "client_secret" not in form and "clientSecret" not in str(requests[0].url)


def test_real_owner_unknown_cleanup_halts_gateway(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    class FailingClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(tokens()).encode()

        async def aclose(self):
            raise RuntimeError("synthetic-private-close-message")

    async def fake(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=FailingClose())

    op = operation()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(ssrf_proxy, "_build_deadline_ssrf_client", lambda *args, **kwargs: client)
    with pytest.raises(GatewayError, match="termination_unconfirmed") as error:
        CasdoorTokenGateway(op).exchange_code("synthetic-code", VERIFIER)
    assert error.value.termination_confirmed is False and not error.value.retry_allowed
    assert "synthetic-private" not in str(error.value)
    with pytest.raises(GatewayError, match="operation_stopped"):
        directory(op).get_complete_roles()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "method,path,params",
    [
        ("GET", "/api/get-application", {}),
        ("GET", "/api/../get-user", {"owner": "synthetic-org", "userId": SUBJECT}),
        ("POST", "/api/get-roles", {"owner": "synthetic-org"}),
        ("GET", "/api/get-roles", {"owner": "other-org"}),
        ("GET", "/api/get-roles", {"owner": "synthetic-org", "clientSecret": SECRET}),
        ("GET", "/api/get-organization", {"id": "other-admin/synthetic-org"}),
    ],
)
def test_private_route_policy_rejects_path_owner_method_query(dispatch, method, path, params) -> None:
    calls, _ = dispatch
    op = operation()
    with pytest.raises(GatewayError, match="request_policy"):
        op._request(method, path, params=params, directory=True)
    assert not calls


def test_redirect_override_is_rejected_before_dispatch(dispatch) -> None:
    calls, _ = dispatch
    op = operation()
    sdk = CasdoorTokenGateway(op)._sdk
    with pytest.raises(GatewayError, match="exchange_input"):
        sdk._oauth_token_request(
            {
                "grant_type": "authorization_code",
                "code": "synthetic-code",
                "redirect_uri": "https://attacker.test/callback",
                "code_verifier": VERIFIER,
            }
        )
    assert not calls


def test_roles_byte_limit_applies_before_parsing(dispatch) -> None:
    calls, replies = dispatch
    op = operation()
    payload = b'{"status":"ok","data":[]}'
    payload += b" " * (ROLES_LIMIT + 1 - len(payload))
    replies.append(httpx.Response(200, content=payload, headers={"Content-Type": "application/json"}))
    with pytest.raises(GatewayError, match="response_bounds") as error:
        directory(op).get_complete_roles()
    assert error.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert calls[0]["max_response_bytes"] == ROLES_LIMIT


@pytest.mark.parametrize("extra", [{"partial": True}, {"count": 200}, {"next": "synthetic-page"}, {"msg": None}])
def test_unknown_directory_envelope_metadata_is_not_discarded_as_complete(dispatch, extra) -> None:
    _, replies = dispatch
    op = operation()
    replies.append(response({"status": "ok", "data": [], **extra}))
    with pytest.raises(GatewayError, match="directory_envelope"):
        directory(op).get_complete_roles()


@pytest.mark.parametrize("label", ["", "synthetic-envelope-label", "密" * 85])
def test_documented_directory_metadata_is_accepted_without_replacing_payload_identity(dispatch, label) -> None:
    _, replies = dispatch
    op = operation()
    gateway = directory(op)
    metadata = {"sub": label, "name": label, "data2": None, "data3": None}
    role = {"owner": op.config.organization, "name": "synthetic-role"}
    organization = {"owner": "admin", "name": op.config.organization, "viewRule": "public"}
    replies.extend(
        response({"status": "ok", "msg": "", "data": payload, **metadata})
        for payload in (user(), [role], organization)
    )
    assert gateway.get_verified_user() == user()
    assert gateway.get_complete_roles() == [role]
    assert gateway.get_organization_visibility() == organization


@pytest.mark.parametrize("field", ["sub", "name"])
@pytest.mark.parametrize("value", [None, 42, {}, [], True, "x" * 256, "密" * 86, "bad\nlabel", "\ud800"])
def test_documented_directory_metadata_must_be_bounded_strings(dispatch, field, value) -> None:
    _, replies = dispatch
    op = operation()
    replies.append(response({"status": "ok", "data": [], field: value}))
    with pytest.raises(GatewayError, match="directory_envelope"):
        directory(op).get_complete_roles()


@pytest.mark.parametrize("field", ["data2", "data3"])
@pytest.mark.parametrize("value", [False, 0, "", [], {"next": "cursor"}])
def test_directory_additional_payloads_are_rejected_even_with_documented_metadata(dispatch, field, value) -> None:
    _, replies = dispatch
    op = operation()
    replies.append(response({"status": "ok", "data": [], "sub": "", "name": "", field: value}))
    with pytest.raises(GatewayError, match="directory_envelope"):
        directory(op).get_complete_roles()


def test_directory_metadata_does_not_authorize_a_different_payload_subject(dispatch) -> None:
    _, replies = dispatch
    op = operation()
    replies.append(response({"status": "ok", "data": user(id="another-user"), "sub": SUBJECT, "name": ""}))
    with pytest.raises(GatewayError, match="user_schema"):
        directory(op).get_verified_user()
