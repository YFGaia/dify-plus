"""Automatic Casdoor Discovery contract checks; synthetic HTTP only."""

from urllib.parse import quote

import httpx
import pytest
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import GatewayError

from tests.unit_tests.core.casdoor.test_gateway import (
    configuration,
    discovery,
    operation,
    response,
)

pytest_plugins = ("tests.unit_tests.core.casdoor.test_gateway",)


def _signing_discovery(op, *, jwks_uri=None):
    value = discovery(op)
    app = quote(op.config.application, safe="")
    value["jwks_uri"] = jwks_uri or f"{op._backend}/.well-known/{app}/jwks"
    return value


def test_application_discovery_is_selected_and_jwks_has_no_credentials(dispatch):
    calls, replies = dispatch
    op = operation()
    replies.extend(
        [
            response(_signing_discovery(op)),
            response({"keys": [{"kty": "RSA", "kid": "synthetic"}]}),
        ]
    )

    source, keys, profile = op.discover_signing_keys()

    assert profile == "application"
    assert source == "https://back.example.test:8000/.well-known/synthetic-app/jwks"
    assert keys == {"keys": [{"kty": "RSA", "kid": "synthetic"}]}
    assert [call["url"] for call in calls] == [
        "https://back.example.test:8000/.well-known/synthetic-app/openid-configuration",
        source,
    ]
    assert all(
        call["method"] == "GET"
        and call["headers"]
        == {"Accept": "application/json", "Accept-Encoding": "identity"}
        for call in calls
    )
    assert all(call["params"] is None and call["data"] is None for call in calls)


@pytest.mark.parametrize("status", [404, 405])
def test_only_missing_application_route_falls_back_to_global(dispatch, status):
    calls, replies = dispatch
    op = operation()
    global_metadata = _signing_discovery(op, jwks_uri=f"{op._backend}/.well-known/jwks")
    replies.extend(
        [response({}, status=status), response(global_metadata), response({"keys": []})]
    )

    source, _, profile = op.discover_signing_keys()

    assert profile == "global"
    assert source == f"{op._backend}/.well-known/jwks"
    assert [call["url"] for call in calls] == [
        f"{op._backend}/.well-known/synthetic-app/openid-configuration",
        f"{op._backend}/.well-known/openid-configuration",
        source,
    ]


@pytest.mark.parametrize("status", [403, 500])
def test_non_route_error_never_falls_back_and_transient_failure_does_not_halt(
    dispatch, status
):
    calls, replies = dispatch
    op = operation()
    replies.append(response({}, status=status))

    with pytest.raises(GatewayError):
        op.discover_signing_keys()

    assert len(calls) == 1
    if status == 500:
        assert calls[0]["url"].endswith("/synthetic-app/openid-configuration")
        assert not op._halted
        replies.extend([response(_signing_discovery(op)), response({"keys": []})])
        assert op.discover_signing_keys()[2] == "application"
    else:
        assert op._halted
        with pytest.raises(GatewayError, match="operation_stopped"):
            op.discover_signing_keys()


def test_transport_failure_is_retryable_but_bad_issuer_latches_operation(dispatch):
    calls, replies = dispatch
    op = operation()
    replies.append(httpx.ConnectError("synthetic transient"))

    with pytest.raises(GatewayError) as transient:
        op.discover_signing_keys()

    assert transient.value.reason == "signing_keys_transport"
    assert not op._halted
    bad = _signing_discovery(op)
    bad["issuer"] += "/wrong"
    replies.append(response(bad))
    with pytest.raises(GatewayError, match="discovery_issuer"):
        op.discover_signing_keys()
    assert op._halted
    assert len(calls) == 2


def test_unknown_profile_is_rejected_before_network_and_jwks_uri_cannot_expand_origin(
    dispatch,
):
    calls, replies = dispatch
    op = operation()
    with pytest.raises(GatewayError, match="discovery_profile") as error:
        op.discover_signing_keys(profile="other")
    assert error.value.code == CasdoorErrorCode.CONFIG_CONFLICT
    assert calls == []

    op = operation()
    bad = _signing_discovery(op, jwks_uri="https://attacker.example/jwks")
    replies.append(response(bad))
    with pytest.raises(GatewayError, match="discovery_endpoint"):
        op.discover_signing_keys()
    assert len(calls) == 1


def test_deadline_is_shared_and_expired_before_next_signing_request(dispatch):
    calls, replies = dispatch
    op = operation()
    replies.append(response(_signing_discovery(op)))
    op.deadline = 0

    with pytest.raises(GatewayError, match="deadline"):
        op.discover_signing_keys()

    assert calls == []


def test_v2_uses_automatic_app_profile_without_user_key_urls(dispatch):
    calls, replies = dispatch
    config = configuration().model_copy(
        update={
            "schema_version": 2,
            "signing_key_mode": "automatic",
            "certificates": (),
        }
    )
    op = operation(config=config)
    replies.extend([response(_signing_discovery(op)), response({"keys": []})])

    source, keys, profile = op.discover_signing_keys()

    assert (profile, source, keys) == (
        "application",
        f"{op._backend}/.well-known/synthetic-app/jwks",
        {"keys": []},
    )
    assert len(calls) == 2


def test_v2_oauth_discovery_checks_app_profile_metadata_without_fetching_jwks(dispatch):
    calls, replies = dispatch
    config = configuration().model_copy(
        update={
            "schema_version": 2,
            "signing_key_mode": "automatic",
            "certificates": (),
        }
    )
    op = operation(config=config)
    replies.append(response(_signing_discovery(op)))

    endpoints = op.discover()

    assert endpoints.jwks_uri == f"{op._backend}/.well-known/synthetic-app/jwks"
    assert [call["url"] for call in calls] == [
        f"{op._backend}/.well-known/synthetic-app/openid-configuration"
    ]
