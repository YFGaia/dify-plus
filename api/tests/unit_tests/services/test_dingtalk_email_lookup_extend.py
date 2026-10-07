"""Focused tests for the DingTalk enterprise email lookup transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from services import ding_talk_extend
from services.dingtalk_email_lookup_extend import extract_email, lookup_email
from services.system_manage_extend import SystemIntegrationManageService


def _config(**overrides):
    config = {
        "enabled": True,
        "url": "https://directory.example.test/email",
        "method": "GET",
        "request_param_field": "userId",
        "response_email_field": "data[0].email",
    }
    config.update(overrides)
    return config


def test_get_sends_configured_user_id_query_parameter(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"data": [{"email": "user@example.test"}]})
    request = Mock(return_value=response)
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)

    result = lookup_email("ding-123", _config())

    assert result["email"] == "user@example.test"
    args, kwargs = request.call_args
    assert args == ("GET", "https://directory.example.test/email")
    assert dict(kwargs["params"]) == {"userId": "ding-123"}
    assert kwargs["max_retries"] == 0


def test_post_raw_json_merges_user_id_over_existing_value(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"email": "user@example.test"})
    request = Mock(return_value=response)
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)

    result = lookup_email(
        "ding-123",
        _config(
            method="POST",
            response_email_field="email",
            body_type="raw",
            body_data={"raw": '{"tenant":"acme","userId":"stale"}'},
        ),
    )

    assert result["result"] == "success"
    assert request.call_args.kwargs["json"] == {"tenant": "acme", "userId": "ding-123"}


@pytest.mark.parametrize(
    ("body_type", "body_key"),
    [("form-data", "form_data"), ("x-www-form-urlencoded", "urlencoded")],
)
def test_form_body_merges_user_id_and_preserves_fields(monkeypatch, body_type, body_key):
    response = SimpleNamespace(status_code=200, json=lambda: {"email": "user@example.test"})
    request = Mock(return_value=response)
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)
    config = _config(
        method="POST",
        body_type=body_type,
        response_email_field="email",
        body_data={body_key: [{"key": "tenant", "value": "acme"}, {"key": "userId", "value": "stale"}]},
    )

    lookup_email("ding-456", config)

    assert request.call_args.kwargs["data"] == {"tenant": "acme", "userId": "ding-456"}


def test_bearer_auth_and_custom_headers_are_sent(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"email": "user@example.test"})
    request = Mock(return_value=response)
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)

    lookup_email(
        "ding-123",
        _config(
            authorization={"type": "bearer", "token": "synthetic-token"},
            headers={"X-Directory": "enterprise"},
        ),
    )

    kwargs = request.call_args.kwargs
    assert kwargs["headers"] == {
        "X-Directory": "enterprise",
        "Authorization": "Bearer synthetic-token",
    }
    assert kwargs["auth"] is None


def test_basic_auth_is_passed_to_http_client(monkeypatch):
    import httpx

    response = SimpleNamespace(status_code=200, json=lambda: {"email": "user@example.test"})
    request = Mock(return_value=response)
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)

    lookup_email(
        "ding-123",
        _config(authorization={"type": "basic", "username": "svc", "password": "synthetic-secret"}),
    )

    auth = request.call_args.kwargs["auth"]
    assert isinstance(auth, httpx.BasicAuth)
    assert auth._auth_header == httpx.BasicAuth("svc", "synthetic-secret")._auth_header


@pytest.mark.parametrize(
    ("response", "expected_message"),
    [
        (SimpleNamespace(status_code=503, json=lambda: {}), "Email API returned a non-200 response."),
        (
            SimpleNamespace(status_code=200, json=lambda: {"data": [{"email": "not-an-email"}]}),
            "No valid email found at the configured response path.",
        ),
        (
            SimpleNamespace(status_code=200, json=Mock(side_effect=ValueError("private response body"))),
            "Email API request failed or returned invalid JSON.",
        ),
    ],
)
def test_lookup_returns_sanitized_failures(monkeypatch, response, expected_message):
    request = Mock(return_value=response)
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)

    result = lookup_email("ding-123", _config())

    assert result["result"] == "failed"
    assert result["message"] == expected_message
    assert "private response body" not in json.dumps(result)


def test_extract_email_supports_nested_dicts_and_array_indices():
    assert (
        extract_email({"result": {"users": [{"mail": "a@example.test"}]}}, "result.users[0].mail") == "a@example.test"
    )
    assert extract_email({"result": []}, "result[0].mail") == ""
    assert extract_email({"mail": "not-an-email"}, "mail") == ""


def test_disabled_config_returns_fallback_without_request(monkeypatch):
    request = Mock(side_effect=AssertionError("disabled integration must not make a request"))
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)
    integration = SimpleNamespace(config=json.dumps({"email_api": {"enabled": False, "url": "https://example.test"}}))

    assert ding_talk_extend.DingTalkService.get_email_from_third_party_api("ding-123", integration) == ""
    request.assert_not_called()


def test_persisted_dingtalk_config_keeps_forward_compatible_email_fields(monkeypatch):
    record = SimpleNamespace(
        status=False,
        corp_id="",
        agent_id="",
        app_key="",
        app_id="",
        app_secret="",
        config="{}",
    )
    monkeypatch.setattr(SystemIntegrationManageService, "_get_or_create_record", lambda classify: record)
    commit = Mock()
    monkeypatch.setattr("services.system_manage_extend.db.session.commit", commit)
    config = {
        "email_api": {
            "enabled": True,
            "url": "https://directory.example.test/email",
            "method": "GET",
            "request_param_field": "unionId",
            "response_email_field": "data[0].email",
            "provider_extension": {"tenant": "acme"},
        },
        "unrelated_extension": {"future": True},
    }

    SystemIntegrationManageService.set_config(1, {"config": config})

    assert json.loads(record.config) == config
    commit.assert_called_once_with()


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "false"},
        {"authorization": {"type": "bearer", "token": 123}},
        {"body_type": "x-www-form-urlencoded", "body_data": {"urlencoded": {"bad": "shape"}}},
        {"body_type": "raw", "body_data": {"raw": "[]"}},
    ],
)
def test_invalid_settings_are_rejected_before_any_request(monkeypatch, patch):
    from services.dingtalk_email_lookup_extend import validate_email_lookup

    request = Mock()
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)
    with pytest.raises(ValueError):
        validate_email_lookup(_config(**patch))
    request.assert_not_called()


def test_get_retains_existing_query_fields_and_overrides_stale_user_id(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"data": [{"email": "user@example.test"}]})
    request = Mock(return_value=response)
    monkeypatch.setattr("services.dingtalk_email_lookup_extend.ssrf_proxy.make_request", request)
    lookup_email("ding-123", _config(url="https://directory.example.test/email?tenant=acme&userId=old&tag=a&tag=b"))
    params = request.call_args.kwargs["params"]
    assert params["tenant"] == "acme"
    assert params["userId"] == "ding-123"
    assert params.get_list("tag") == ["a", "b"]
