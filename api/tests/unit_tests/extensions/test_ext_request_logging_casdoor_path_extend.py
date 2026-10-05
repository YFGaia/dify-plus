"""Exercise enabled application logs with synthetic Casdoor navigation handles."""

import logging

import pytest
from flask import Flask, Request, Response

from extensions import ext_request_logging
from tests.unit_tests.config_override import apply_config_overrides

HANDLE = "d21-synthetic-opaque-initialization-handle"
PRIVATE = "d21-synthetic-query-header-body-cookie"
MANAGEMENT = "/console/api/system-manage-extend/integration/casdoor"
AUTH = "/console/api/auth/casdoor"
IDENTITY = "/console/api/account/casdoor-identity"
BODY = '{"private":"' + PRIVATE + '"}'


@pytest.fixture
def enabled_logging(monkeypatch):
    apply_config_overrides(monkeypatch, ENABLE_REQUEST_LOGGING=True)
    monkeypatch.setattr(ext_request_logging, "get_trace_id_from_otel_context", lambda: "d21-observable-trace")


def _app(path, *, status=200, methods=("GET", "POST")):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.add_url_rule(
        path,
        view_func=lambda: Response(BODY, status=status, mimetype="application/json"),
        methods=methods,
    )
    ext_request_logging.init_app(app)
    return app


def _records(caplog):
    return [record for record in caplog.records if record.name == ext_request_logging.logger.name]


def _assert_casdoor_logs(caplog, *, category, status, level, method="GET"):
    records = _records(caplog)
    for record in records:
        assert HANDLE not in record.getMessage()
        assert PRIVATE not in record.getMessage()
        assert HANDLE not in repr(record.args)
        assert PRIVATE not in repr(record.args)
        assert record.exc_info is None
        assert record.exc_text is None
    access = [record for record in records if record.levelno == logging.INFO]
    assert len(access) == 1
    assert access[0].args[:3] == (method, category, status)
    assert isinstance(access[0].args[3], float)
    assert access[0].args[3] >= 0
    assert access[0].args[4] == "d21-observable-trace"
    debug = [record for record in records if record.levelno == logging.DEBUG]
    if level == logging.INFO:
        assert debug == []
    else:
        assert len(debug) == 2
        assert debug[0].msg == "Received Request %s -> %s"
        assert debug[0].args == (method, category)
        assert debug[1].msg == "Response %s %s"
        assert debug[1].args[0].startswith(str(status))


@pytest.mark.usefixtures("enabled_logging")
@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("status", [200, 400, 500])
@pytest.mark.parametrize(
    ("path", "category"),
    [
        (AUTH + "/diagnostic/" + HANDLE, AUTH),
        (AUTH + "/identity/" + HANDLE, AUTH),
        (AUTH, AUTH),
        (AUTH + "/callback/" + HANDLE, AUTH),
        (MANAGEMENT, MANAGEMENT),
        (MANAGEMENT + "/nested/" + HANDLE, MANAGEMENT),
        (IDENTITY, IDENTITY),
        (IDENTITY + "/nested/" + HANDLE, IDENTITY),
    ],
)
def test_enabled_logs_keep_namespace_and_access_fields_without_handles(path, category, status, level, caplog):
    caplog.set_level(level, logger=ext_request_logging.logger.name)
    app = _app(path, status=status)
    client = app.test_client()
    client.set_cookie("session", PRIVATE)
    response = client.get(
        path + "?code=" + PRIVATE + "&state=" + PRIVATE,
        data=BODY,
        content_type="application/json",
        headers={"Authorization": "Bearer " + PRIVATE, "X-Input": PRIVATE},
    )
    assert response.status_code == status
    assert response.text == BODY
    _assert_casdoor_logs(caplog, category=category, status=status, level=level)


@pytest.mark.usefixtures("enabled_logging")
@pytest.mark.parametrize("category", [MANAGEMENT, AUTH, IDENTITY])
@pytest.mark.parametrize("status", [404, 405])
def test_unmatched_and_method_error_descendants_use_namespace(category, status, caplog):
    caplog.set_level(logging.DEBUG, logger=ext_request_logging.logger.name)
    path = category + "/missing/" + HANDLE
    app = _app(category)
    if status == 405:
        app.add_url_rule(path, "post_only", lambda: "unused", methods=["POST"])
    response = app.test_client().get(path)
    assert response.status_code == status
    _assert_casdoor_logs(caplog, category=category, status=status, level=logging.DEBUG)


@pytest.mark.usefixtures("enabled_logging")
@pytest.mark.parametrize(
    "path",
    [
        "/ordinary/" + HANDLE,
        MANAGEMENT + "-other/" + HANDLE,
        AUTH + "evil/" + HANDLE,
        IDENTITY + "-other/" + HANDLE,
        "/other" + AUTH + "/" + HANDLE,
    ],
)
def test_ordinary_and_prefix_lookalike_paths_preserve_json_logging(path, caplog):
    caplog.set_level(logging.DEBUG, logger=ext_request_logging.logger.name)
    response = _app(path).test_client().post(path, data=BODY, content_type="application/json")
    assert response.status_code == 200
    started, access, finished = _records(caplog)
    assert started.args[:2] == ("POST", path)
    assert "Request Body" in started.getMessage()
    assert PRIVATE in started.args[2]
    assert access.args[:3] == ("POST", path, 200)
    assert "Response Body" in finished.getMessage()
    assert PRIVATE in finished.args[2]


@pytest.mark.usefixtures("enabled_logging")
@pytest.mark.parametrize("category", [MANAGEMENT, AUTH, IDENTITY])
def test_protected_context_does_not_read_sensitive_bodies(category, caplog, monkeypatch):
    class UnreadableRequest(Request):
        @property
        def data(self):
            pytest.fail("application logger read the sensitive request body")

    class UnreadableResponse(Response):
        def get_data(self, *args, **kwargs):
            pytest.fail("application logger read the sensitive response body")

    def forbidden_json(*args, **kwargs):
        pytest.fail("application logger parsed or serialized sensitive JSON")

    app = Flask(__name__)
    app.request_class = UnreadableRequest
    caplog.set_level(logging.DEBUG, logger=ext_request_logging.logger.name)
    with app.test_request_context(category + "/nested/" + HANDLE, method="POST", content_type="application/json"):
        monkeypatch.setattr(ext_request_logging.json, "loads", forbidden_json)
        monkeypatch.setattr(ext_request_logging.json, "dumps", forbidden_json)
        ext_request_logging._log_request_started(app)
        ext_request_logging._log_request_finished(app, UnreadableResponse(BODY, mimetype="application/json"))
    _assert_casdoor_logs(caplog, category=category, status=200, level=logging.DEBUG, method="POST")
