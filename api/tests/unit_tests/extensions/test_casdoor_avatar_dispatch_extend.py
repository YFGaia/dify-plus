"""Actual producer/navigation/consumer with only broker transport substituted."""

import importlib
import json
from datetime import timedelta
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from celery import _state
from configs.feature import CeleryScheduleTasksConfig
from extensions import ext_application_services as composition
from extensions import ext_celery
from flask import Flask, current_app, has_app_context
from libs import datetime_utils
from models.casdoor_extend import CasdoorAvatarDispatchCursorExtend as Cursor
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from pydantic import ValidationError
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from services.casdoor_avatar_dispatch_service_extend import CasdoorAvatarDispatchService
from sqlalchemy.orm import Session
from test_casdoor_avatar_consumer_extend import row
from test_casdoor_avatar_intent_extend import URL
from test_casdoor_avatar_intent_extend import avatar as original_avatar
from test_casdoor_profile_repository_extend import storage as original_storage
from tests.unit_tests.config_override import apply_config_overrides
from tests.unit_tests.extensions.test_casdoor_avatar_task_composition_extend import (
    composed as original_composed,
)
from tests.unit_tests.extensions.test_casdoor_avatar_task_composition_extend import (
    consumer as original_consumer,
)
from tests.unit_tests.extensions.test_casdoor_avatar_task_composition_extend import (
    registered as original_registered,
)

TASK = "tasks.casdoor_avatar_recovery_task_extend.dispatch_casdoor_avatar_initial_pending"
MODULE = "tasks.casdoor_avatar_recovery_task_extend"
avatar_fixture = original_avatar
storage_fixture = original_storage
consumer = original_consumer
registered = original_registered
composed = original_composed


@pytest.fixture
def dispatch(composed, registered, monkeypatch):
    s = composed
    app, celery, initial, _ = registered
    Cursor.__table__.create(s.session.get_bind())
    apply_config_overrides(monkeypatch, ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK=True)
    importlib.import_module(MODULE)
    s.recovery = celery.tasks[TASK]
    with app.app_context():
        s.dispatch = composition.application_services().casdoor_avatar_dispatch
    s.published = []

    def publish(*, args, **kwargs):
        s.no_sql()
        assert has_app_context() and current_app._get_current_object() is app
        assert kwargs == {"queue": "extend_low", "retry": False, "ignore_result": True}
        assert len(args) == 1 and str(UUID(args[0])) == args[0]
        s.published.append(args[0])

    s.publish = publish
    monkeypatch.setattr(initial, "apply_async", publish)
    return s


def reset_navigation(s):
    # Fixture isolation between faults; production never repairs/deletes its cursor.
    with Session(s.session.get_bind()) as session, session.begin():
        session.execute(sa.delete(Cursor))


def test_disabled_registered_task_skips_services_sql_and_publisher(consumer, registered, monkeypatch):
    _, celery, initial, _ = registered
    module = importlib.import_module(MODULE)
    apply_config_overrides(monkeypatch, ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK=False)
    lookup = Mock(side_effect=AssertionError("service access forbidden"))
    monkeypatch.setattr(composition, "application_services", lookup)
    monkeypatch.setattr(type(consumer.maker), "__call__", Mock(side_effect=AssertionError("SQL forbidden")))
    publisher = Mock(side_effect=AssertionError("publisher forbidden"))
    monkeypatch.setattr(initial, "apply_async", publisher)
    assert celery.tasks[TASK]() == {"code": "disabled"}
    assert "application_services" not in vars(module)
    assert not consumer.sessions and not consumer.calls
    lookup.assert_not_called()
    publisher.assert_not_called()


def test_positive_interval_default_off_and_exact_celery_registration(registered, monkeypatch):
    _, before, _, _ = registered
    assert CeleryScheduleTasksConfig().ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK is False
    assert CeleryScheduleTasksConfig().CASDOOR_AVATAR_INITIAL_RECOVERY_INTERVAL_SECONDS == 30
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            CeleryScheduleTasksConfig(CASDOOR_AVATAR_INITIAL_RECOVERY_INTERVAL_SECONDS=invalid)
    assert MODULE not in before.conf.imports
    assert "casdoor_avatar_initial_recovery" not in before.conf.beat_schedule
    apply_config_overrides(
        monkeypatch,
        ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK=True,
        CASDOOR_AVATAR_INITIAL_RECOVERY_INTERVAL_SECONDS=17,
    )
    monkeypatch.setattr(_state, "default_app", _state.default_app)
    app = Flask("recovery-options")
    celery = ext_celery.init_app(app)
    try:
        importlib.import_module(MODULE)
        task = celery.tasks[TASK]
        assert celery.conf.imports.count(MODULE) == 1
        assert [name for name in celery.conf.imports if name != MODULE] == list(before.conf.imports)
        schedule = dict(celery.conf.beat_schedule)
        assert schedule.pop("casdoor_avatar_initial_recovery") == {
            "task": TASK,
            "schedule": timedelta(seconds=17),
            "options": {"queue": "extend_low", "retry": False, "ignore_result": True},
        }
        assert schedule == before.conf.beat_schedule
        assert task.app is celery and task.queue == "extend_low" and task.ignore_result is True
        assert task.autoretry_for == () and task.max_retries == 0
        assert task.acks_late is False and task.reject_on_worker_lost is False
    finally:
        celery.close()


def test_committed_navigation_and_each_fresh_candidate_close_before_publish(dispatch, monkeypatch):
    s = dispatch
    before = row(s)
    # A lower malformed row tests that every examined ID gets its own actual root.
    with s.session.begin():
        values = dict(before, id=str(UUID(int=1)), identity_id=str(UUID(int=2)), idempotency_key="navigation-only")
        s.session.execute(sa.insert(Intent).values(**values))
    commits = []
    s.commit_hook = commits.append
    candidates = []
    original = CasdoorAvatarRepository._initial_dispatch_candidate

    def candidate(repository, key, **kwargs):
        candidates.append((key, repository._session))
        assert len(s.sessions) >= 2
        assert all(x.consumer_closed for x in s.sessions[:-1])
        return original(repository, key, **kwargs)

    monkeypatch.setattr(CasdoorAvatarRepository, "_initial_dispatch_candidate", candidate)
    assert s.recovery() == {"code": "scanned"}
    assert s.published == [str(s.intent_id)] and len(s.sessions) == 3
    assert commits == [1, 2, 3]
    assert [key for key, _ in candidates] == [UUID(int=1), s.intent_id]
    assert candidates[0][1] is not candidates[1][1]
    s.no_sql()
    with Session(s.session.get_bind()) as reader:
        assert (
            dict(reader.execute(sa.select(Intent.__table__).where(Intent.id == str(s.intent_id))).mappings().one())
            == before
        )


def test_navigation_and_candidate_commit_close_faults_publish_nothing(dispatch):
    s = dispatch
    before = row(s)
    for stage in (1, 2):
        for fault in ("commit", "close"):
            reset_navigation(s)
            target = len(s.sessions) + stage

            def fail(index, boundary=target):
                if index == boundary:
                    raise RuntimeError(URL)

            s.commit_hook = fail if fault == "commit" else None
            s.close_hook = fail if fault == "close" else None
            assert s.recovery() == {"code": "unknown"}
            assert not s.published and row(s) == before
            s.no_sql()
    s.commit_hook = s.close_hook = None
    # A damaged durable cursor is also closed, never reset by dispatch.
    with Session(s.session.get_bind()) as session, session.begin():
        session.execute(sa.text("PRAGMA ignore_check_constraints=ON"))
        session.execute(sa.update(Cursor).values(last_id="bad"))
    assert s.recovery() == {"code": "unknown"}
    assert not s.published and row(s) == before


def test_original_authority_denies_config_policy_generation_and_expiry(dispatch):
    s = dispatch
    before = row(s)
    changes = ((Integration, "enabled", False), (Identity, "sync_generation", 2))
    for model, field, value in changes:
        reset_navigation(s)
        with Session(s.session.get_bind()) as session, session.begin():
            old = session.scalar(sa.select(getattr(model, field)))
            session.execute(sa.update(model).values(**{field: value}))
        assert s.recovery() == {"code": "scanned"} and not s.published
        with Session(s.session.get_bind()) as session, session.begin():
            session.execute(sa.update(model).values(**{field: old}))
    reset_navigation(s)
    with s.session.begin():
        old_policy, old_digest = s.revision.policy_json, s.revision.config_digest
        policy = json.loads(old_policy)
        policy["avatar_sync"] = False
        s.session.execute(sa.update(Revision).values(policy_json=json.dumps(policy)))
        s.session.expire(s.revision)
        config = s.config_owner._configuration(s.revision)
        digest = s.config_owner._validation_digest(config, s.revision)
        s.session.execute(sa.update(Revision).values(config_digest=digest))
    assert s.recovery() == {"code": "scanned"} and not s.published
    with s.session.begin():
        s.session.execute(sa.update(Revision).values(policy_json=old_policy, config_digest=old_digest))
    reset_navigation(s)
    s.utc += timedelta(seconds=300)
    assert s.recovery() == {"code": "scanned"} and not s.published
    assert row(s) == before and not s.calls


def test_broker_exception_ambiguous_ack_and_shutdown_preserve_intent(dispatch, registered, monkeypatch, caplog):
    s = dispatch
    _, _, initial, _ = registered
    before = row(s)
    sent = []

    def unavailable(**kwargs):
        s.no_sql()
        raise ConnectionError(URL)

    monkeypatch.setattr(initial, "apply_async", unavailable)
    assert s.recovery() == {"code": "unknown"}
    assert not s.published and row(s) == before
    assert s.recovery() == {"code": "scanned"}  # Next bounded scan wraps after failure.

    def fail(*, args, **kwargs):
        s.publish(args=args, **kwargs)
        sent.append(args[0])  # May already be enqueued before lost acknowledgment.
        raise RuntimeError(URL)

    monkeypatch.setattr(initial, "apply_async", fail)
    assert s.recovery() == {"code": "unknown"}
    assert row(s) == before and not s.calls
    assert s.recovery() == {"code": "scanned"}  # Empty tail wraps.
    assert s.recovery() == {"code": "unknown"}
    assert sent == [str(s.intent_id)] * 2 and row(s) == before
    for signal in (KeyboardInterrupt, SystemExit):
        reset_navigation(s)

        def stop(shutdown=signal, **kwargs):
            s.no_sql()
            raise shutdown(URL)

        monkeypatch.setattr(initial, "apply_async", stop)
        with pytest.raises(signal) as caught:
            s.recovery()
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert str(caught.value) in ("avatar dispatch interrupted", "1")
        assert row(s) == before
    assert URL not in caplog.text


def test_duplicate_transport_delivery_uses_actual_registered_consumer(dispatch):
    s = dispatch
    before = row(s)
    assert s.recovery() == {"code": "scanned"} and row(s) == before
    assert s.recovery() == {"code": "scanned"}
    assert s.recovery() == {"code": "scanned"}
    assert s.published == [str(s.intent_id)] * 2
    first = s.task(s.published[0])
    calls = s.calls.copy()
    assert first["code"] == "applied"
    assert s.task(s.published[1]) == first and s.calls == calls
    assert s.calls.count("save") == s.calls.count("decrypt") == 1
    assert s.recovery() == {"code": "scanned"}
    assert len(s.published) == 2


def test_factory_and_task_import_are_lazy_and_reuse_shared_services(consumer, registered, monkeypatch):
    s = consumer
    app, celery, initial, _ = registered
    monkeypatch.setattr(type(s.maker), "__call__", Mock(side_effect=AssertionError("SQL forbidden")))
    publisher = Mock(side_effect=AssertionError("broker forbidden"))
    monkeypatch.setattr(initial, "apply_async", publisher)
    monkeypatch.setattr(celery, "connection_for_write", publisher)
    importlib.reload(importlib.import_module(MODULE))
    composition.init_app(app)
    with app.app_context():
        services = composition.application_services()
        assert composition.application_services() is services
    actual = services.casdoor_avatar_dispatch
    assert type(actual) is CasdoorAvatarDispatchService
    assert actual._session_factory is s.maker
    assert actual._configuration_service is services.casdoor_configuration
    assert actual._now is datetime_utils.utc_now
    assert actual._publish is composition._publish_casdoor_avatar_initial
    assert not s.sessions and not s.calls
    publisher.assert_not_called()
