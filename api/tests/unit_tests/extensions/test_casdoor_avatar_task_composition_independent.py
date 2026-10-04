"""Independent actual FlaskTask composition checks using original fixtures."""

import pytest
from flask import Flask, current_app, has_app_context
from sqlalchemy.orm import Session
from test_casdoor_avatar_task_composition_extend import (
    INTENT,
    URL,
    avatar_fixture,
    composed,
    consumer,
    registered,
    storage_fixture,
)

from models.model import UploadFile

__all__ = ["avatar_fixture", "composed", "consumer", "registered", "storage_fixture"]


def test_registered_task_uses_its_app_and_restores_outer_context(composed):
    state = composed
    outer = Flask("outer-app")

    class RejectingServices:
        @property
        def casdoor_avatar_consumer(self):
            raise AssertionError("outer application services must not be used")

    outer.extensions["application_services"] = RejectingServices()
    with outer.app_context():
        assert current_app._get_current_object() is outer
        result = state.task(str(state.intent_id))
        assert result["code"] == "applied"
        assert current_app._get_current_object() is outer
        with Session(state.session.get_bind()) as reader:
            file = reader.get(UploadFile, result["file_id"])
            assert file is not None and file.key in state.data
        assert len(state.calls) > 0

        calls_before_replay = state.calls.copy()
        sessions_before_replay = len(state.sessions)
        assert state.task(str(state.intent_id)) == result
        assert current_app._get_current_object() is outer
        assert state.calls == calls_before_replay
        assert len(state.sessions) == sessions_before_replay + 1
        state.no_sql()


def test_application_services_field_shutdown_is_sanitized(registered, caplog):
    app, _, task, _ = registered
    outer = Flask("field-outer-app")

    class ShuttingDownServices:
        @property
        def casdoor_avatar_consumer(self):
            raise SystemExit(URL)

    app.extensions["application_services"] = ShuttingDownServices()
    with outer.app_context():
        with pytest.raises(SystemExit) as caught:
            task(INTENT)
        assert current_app._get_current_object() is outer
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert str(caught.value) == "1"
    assert URL not in repr(caught.value) and URL not in caplog.text
    entry = next(frame for frame in caught.traceback if frame.name == "consume_casdoor_avatar_initial_task")
    assert all(entry.locals[name] is None for name in ("consumer", "parsed", "intent_id", "application_services"))
    assert not has_app_context()
