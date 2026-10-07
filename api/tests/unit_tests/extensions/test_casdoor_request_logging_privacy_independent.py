"""Independent HTTP checks for body privacy on Casdoor paths."""

import logging

from extensions import ext_request_logging
from flask import Flask, Response, request
from tests.unit_tests.config_override import apply_config_overrides
from werkzeug.exceptions import BadRequest

SENTINEL = "F13_INDEPENDENT_SYNTHETIC_MARKER"


def test_malformed_config_body_and_error_response_are_omitted_from_signal_logs(monkeypatch, caplog):
    path = "/console/api/system-manage-extend/integration/casdoor/validate"
    apply_config_overrides(monkeypatch, ENABLE_REQUEST_LOGGING=True)
    caplog.set_level(logging.DEBUG, logger=ext_request_logging.logger.name)
    monkeypatch.setattr(ext_request_logging, "get_trace_id_from_otel_context", lambda: "independent-trace")

    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.post(path)
    def validate():
        request.get_json()  # Exercise real application parsing after request_started.
        return {"ok": True}

    @app.errorhandler(BadRequest)
    def malformed(_error):
        return Response('{"error":"' + SENTINEL + '"}', status=400, mimetype="application/json")

    ext_request_logging.init_app(app)
    response = app.test_client().post(path, data='{"client_secret":"' + SENTINEL, content_type="application/json")

    assert response.status_code == 400
    assert SENTINEL in response.get_data(as_text=True)
    records = [r for r in caplog.records if r.name == ext_request_logging.logger.name]
    assert [record.levelno for record in records] == [logging.DEBUG, logging.INFO, logging.DEBUG]
    assert records[0].msg == "Received Request %s -> %s"
    assert records[0].args == ("POST", path)
    assert records[1].args[:3] == ("POST", path, 400)
    assert isinstance(records[1].args[3], float)
    assert records[1].args[3] >= 0
    assert records[1].args[4] == "independent-trace"
    assert records[2].msg == "Response %s %s"
    assert records[2].args[0].startswith("400")
    for record in records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in repr(record.args)
        assert record.exc_info is None
        assert record.exc_text is None


def test_malformed_auth_callback_body_and_error_response_are_omitted_from_signal_logs(monkeypatch, caplog):
    path = "/console/api/auth/casdoor/callback"
    apply_config_overrides(monkeypatch, ENABLE_REQUEST_LOGGING=True)
    caplog.set_level(logging.DEBUG, logger=ext_request_logging.logger.name)

    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.post(path)
    def callback():
        request.get_json()
        return {"authenticated": True}

    @app.errorhandler(BadRequest)
    def malformed(_error):
        return Response('{"message":"' + SENTINEL + '"}', status=400, mimetype="application/json")

    ext_request_logging.init_app(app)
    response = app.test_client().post(path, data='{"code":"' + SENTINEL, content_type="application/json")

    assert response.status_code == 400
    assert SENTINEL in response.get_data(as_text=True)
    records = [r for r in caplog.records if r.name == ext_request_logging.logger.name]
    assert len(records) == 3
    assert records[0].args == ("POST", path)
    assert records[1].args[:3] == ("POST", path, 400)
    assert records[2].msg == "Response %s %s"
    for record in records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in repr(record.args)
        assert record.exc_info is None
        assert record.exc_text is None
