"""Synthetic Flask signal regression cases for the bounded Casdoor body exclusion."""

import logging

import flask
import pytest
from extensions import ext_request_logging
from flask import Flask, Request, Response
from tests.unit_tests.config_override import apply_config_overrides

MANAGEMENT = "/console/api/system-manage-extend/integration/casdoor"
AUTH = "/console/api/auth/casdoor"
SENTINEL = "F13_SYNTHETIC_SECRET_NOT_A_CREDENTIAL"
JSON_BODY = '{"client_secret":"' + SENTINEL + '"}'
PROTECTED_PATHS = (MANAGEMENT, MANAGEMENT + "/", MANAGEMENT + "/validate", AUTH, AUTH + "/", AUTH + "/callback")


@pytest.fixture
def configured_logging(monkeypatch, caplog):
    apply_config_overrides(monkeypatch, ENABLE_REQUEST_LOGGING=True)
    caplog.set_level(logging.DEBUG, logger=ext_request_logging.logger.name)
    monkeypatch.setattr(ext_request_logging, "get_trace_id_from_otel_context", lambda: "f13-trace")


def _app(path, *, body=JSON_BODY, content_type="application/json", status=200):
    app = Flask(__name__)
    app.config["TESTING"] = True

    def handler():
        return Response(body, status=status, content_type=content_type)

    app.add_url_rule(path, view_func=handler, methods=["POST", "PUT"])
    ext_request_logging.init_app(app)
    return app


def _records(caplog):
    return [record for record in caplog.records if record.name == ext_request_logging.logger.name]


def _assert_private(caplog, path, status):
    records = _records(caplog)
    assert len(records) == 3
    for record in records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in repr(record.args)
        assert record.exc_info is None
        assert record.exc_text is None
    started, access, finished = records
    assert started.levelno == finished.levelno == logging.DEBUG
    assert started.msg == "Received Request %s -> %s"
    assert started.args == ("POST", path)
    assert access.levelno == logging.INFO
    assert access.args[:3] == ("POST", path, status)
    assert isinstance(access.args[3], float)
    assert access.args[3] >= 0
    assert access.args[4] == "f13-trace"
    assert finished.msg == "Response %s %s"
    assert finished.args[0].startswith(str(status))


@pytest.mark.usefixtures("configured_logging")
@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_configured_signals_never_log_casdoor_bodies(path, caplog):
    app = _app(path)
    controller_calls = []

    @app.before_request
    def observe_before_controller():
        # request_started has already run: this catches the actual pre-controller leak.
        controller_calls.append(True)
        assert len(_records(caplog)) == 1
        assert SENTINEL not in _records(caplog)[0].getMessage()
        assert SENTINEL not in repr(_records(caplog)[0].args)

    response = app.test_client().post(path, data=JSON_BODY, content_type="application/json")
    assert response.status_code == 200
    assert response.text == JSON_BODY
    assert controller_calls == [True]
    _assert_private(caplog, path, 200)


@pytest.mark.usefixtures("configured_logging")
@pytest.mark.parametrize("path", [MANAGEMENT, AUTH + "/callback"])
@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        (JSON_BODY, "application/json; charset=utf-8", 400),
        ('{"invalid":' + SENTINEL, "application/json", 500),
        (SENTINEL, "text/plain", 401),
        ("", "application/json", 204),
    ],
)
def test_payload_types_errors_query_headers_and_cookies_stay_private(path, body, content_type, status, caplog):
    app = _app(path, body=body, content_type=content_type, status=status)
    client = app.test_client()
    client.set_cookie("session", SENTINEL)
    response = client.post(
        path + "?code=" + SENTINEL + "&state=" + SENTINEL,
        data=body,
        content_type=content_type,
        headers={"Authorization": "Bearer " + SENTINEL, "X-Input": SENTINEL},
    )
    assert response.status_code == status
    _assert_private(caplog, path, status)


@pytest.mark.usefixtures("configured_logging")
@pytest.mark.parametrize("prefix", [MANAGEMENT, AUTH])
@pytest.mark.parametrize("status", [404, 405])
def test_unmatched_and_wrong_method_descendants_stay_private(prefix, status, caplog):
    app = _app(prefix)
    path = prefix + "/missing"
    if status == 405:
        app.add_url_rule(path, "get_only", lambda: "unused", methods=["GET"])
    response = app.test_client().post(path, data=JSON_BODY, content_type="application/json")
    assert response.status_code == status
    _assert_private(caplog, path, status)


@pytest.mark.usefixtures("configured_logging")
@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_signals_do_not_access_or_serialize_bodies(path, monkeypatch, caplog):
    class UnreadableRequest(Request):
        @property
        def data(self):
            pytest.fail("request body was read")

    class UnreadableResponse(Response):
        def get_data(self, *args, **kwargs):
            pytest.fail("response body was read")

    def forbidden_json(*args, **kwargs):
        pytest.fail("JSON parser or serializer was called")

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.request_class = UnreadableRequest
    app.add_url_rule(
        path, view_func=lambda: UnreadableResponse(JSON_BODY, mimetype="application/json"), methods=["POST"]
    )
    ext_request_logging.init_app(app)
    monkeypatch.setattr(ext_request_logging.json, "loads", forbidden_json)
    monkeypatch.setattr(ext_request_logging.json, "dumps", forbidden_json)
    assert app.test_client().post(path, data=JSON_BODY, content_type="application/json").status_code == 200
    _assert_private(caplog, path, 200)


@pytest.mark.usefixtures("configured_logging")
@pytest.mark.parametrize(
    "path",
    [
        "/ordinary",
        MANAGEMENT + "-other",
        MANAGEMENT + "evil/child",
        AUTH + "-other",
        AUTH + "evil/child",
        "/other" + AUTH,
    ],
)
def test_other_routes_and_prefix_lookalikes_keep_body_logs(path, caplog):
    app = _app(path)
    response = app.test_client().post(path, data=JSON_BODY, content_type="application/json")
    assert response.status_code == 200
    started, access, finished = _records(caplog)
    assert "Request Body" in started.getMessage()
    assert SENTINEL in started.getMessage()
    assert SENTINEL in started.args[2]
    assert access.args[:3] == ("POST", path, 200)
    assert "Response Body" in finished.getMessage()
    assert SENTINEL in finished.getMessage()
    assert SENTINEL in finished.args[2]


@pytest.mark.usefixtures("configured_logging")
def test_finished_without_any_active_context_keeps_original_body_and_trace_fallback(monkeypatch, caplog):
    assert not flask.has_request_context()
    assert not flask.has_app_context()
    monkeypatch.setattr(ext_request_logging, "get_trace_id_from_otel_context", lambda: None)
    response = Response(JSON_BODY, mimetype="application/json", headers={"X-Trace-Id": "fallback-trace"})
    ext_request_logging._log_request_finished(None, response)
    access, finished = _records(caplog)
    assert access.args == ("-", "-", 200, "-", "fallback-trace")
    assert SENTINEL in finished.getMessage()
    assert SENTINEL in finished.args[2]


@pytest.mark.usefixtures("configured_logging")
def test_management_put_keeps_payload_for_controller_without_logging_it(caplog):
    app = _app(MANAGEMENT)
    received = []

    @app.before_request
    def consume_payload():
        received.append(flask.request.get_json())

    body = '{"secret":{"action":"replace","value":"' + SENTINEL + '"}}'
    assert app.test_client().put(MANAGEMENT, data=body, content_type="application/json").status_code == 200
    assert received == [{"secret": {"action": "replace", "value": SENTINEL}}]
    records = _records(caplog)
    assert len(records) == 3
    assert records[0].args == ("PUT", MANAGEMENT)
    assert records[1].args[:3] == ("PUT", MANAGEMENT, 200)
    for record in records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in repr(record.args)
        assert record.exc_info is None


@pytest.mark.usefixtures("configured_logging")
def test_none_response_without_any_active_context_does_not_log(caplog):
    assert not flask.has_request_context()
    assert not flask.has_app_context()
    ext_request_logging._log_request_finished(None, None)
    assert not _records(caplog)
