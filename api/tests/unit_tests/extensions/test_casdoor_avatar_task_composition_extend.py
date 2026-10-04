"""Actual FlaskTask and factory composition over the accepted initial consumer."""

import hashlib
import importlib
import json
import time
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from celery import _state
from flask import Flask, current_app, has_app_context
from graphon.file import helpers as file_helpers
from sqlalchemy.orm import Session
from test_casdoor_avatar_consumer_extend import consumer as original_consumer
from test_casdoor_avatar_consumer_extend import row, unknown
from test_casdoor_avatar_intent_extend import URL
from test_casdoor_avatar_intent_extend import avatar as original_avatar
from test_casdoor_profile_repository_extend import storage as original_storage

from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto
from core.logging import context as log_context
from extensions import ext_application_services as composition
from extensions import ext_celery
from libs import datetime_utils
from models.account import Account, TenantAccountJoin
from models.enums import CreatorUserRole
from models.model import UploadFile
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationRepository,
)
from services.account_avatar_file_gateway import SQLAlchemyAccountAvatarFileGateway
from services.casdoor_avatar_consumer_service_extend import CasdoorAvatarConsumerService
from tests.unit_tests.config_override import apply_config_overrides

TASK = "tasks.casdoor_avatar_initial_task_extend.consume_casdoor_avatar_initial_task"
INTENT = "01234567-89ab-4cde-8fab-0123456789ab"
storage_fixture = original_storage
avatar_fixture = original_avatar
consumer = original_consumer


@pytest.fixture
def registered(monkeypatch):
    # Restore Celery globals as well as Dify settings after each isolated app.
    monkeypatch.setattr(_state, "default_app", _state.default_app)
    monkeypatch.setattr(_state._tls, "current_app", getattr(_state._tls, "current_app", None))
    apply_config_overrides(
        monkeypatch,
        CELERY_BROKER_URL="memory://",
        CELERY_BACKEND="cache",
        CELERY_TASK_ANNOTATIONS={},
        CELERY_USE_SENTINEL=False,
        DISABLE_TELEMETRY=True,
        SECRET_KEY="synthetic-profile-key",
        RBAC_ENABLED=False,
    )
    app = Flask(__name__)
    celery = ext_celery.init_app(app)
    module = importlib.import_module("tasks.casdoor_avatar_initial_task_extend")
    task = celery.tasks[TASK]
    assert task.app is celery
    yield app, celery, task, module
    celery.close()
    log_context.clear_request_context()


@pytest.fixture
def composed(consumer, registered, monkeypatch):
    s = consumer
    app, _celery, task, _module = registered
    monkeypatch.setattr(datetime_utils, "_now_func", lambda tz: s.utc)
    # The original fixture already configured the real shared sessionmaker.
    composition.init_app(app)
    with app.app_context():
        services = composition.application_services()
    s.actual = services.casdoor_avatar_consumer
    assert s.actual is not s.consumer
    assert s.actual._session_factory is s.maker
    assert s.actual._configuration_service is services.casdoor_configuration
    s.task = task
    return s


def test_actual_factory_is_lazy_and_uses_shared_owners(consumer, registered, monkeypatch):
    s = consumer
    app, _, _, _ = registered
    monkeypatch.setattr(CasdoorCrypto, "__init__", Mock(side_effect=AssertionError("unexpected crypto")))
    monkeypatch.setattr(type(s.maker), "__call__", Mock(side_effect=AssertionError("unexpected SQL")))
    composition.init_app(app)
    with app.app_context():
        services = composition.application_services()
    actual = services.casdoor_avatar_consumer
    assert type(actual) is CasdoorAvatarConsumerService
    assert actual._session_factory is services.casdoor_configuration._session_factory is s.maker
    assert actual._configuration_service is services.casdoor_configuration
    assert actual._now is datetime_utils.utc_now
    assert actual._monotonic is time.monotonic
    assert s.maker.kw["expire_on_commit"] is False
    assert not s.sessions and not s.calls


def test_registered_flask_task_import_lookup_context_and_options(registered, monkeypatch):
    app, celery, task, module = registered
    assert module.consume_casdoor_avatar_initial_task.name == TASK
    assert celery.conf.imports.count("tasks.casdoor_avatar_initial_task_extend") == 1
    assert all(entry["task"] != TASK for entry in celery.conf.beat_schedule.values())
    assert task.queue == "extend_low" and task.ignore_result is True
    assert task.autoretry_for == () and task.max_retries == 0
    assert task.acks_late is False and task.reject_on_worker_lost is False
    assert "application_services" not in vars(module)
    calls = []

    def lookup():
        assert has_app_context() and current_app._get_current_object() is app
        assert log_context.get_identity_context() == ("", "", "")
        assert log_context.get_request_id()
        calls.append("lookup")
        raise LookupError(URL)

    monkeypatch.setattr(composition, "application_services", lookup)
    log_context.set_identity_context(tenant_id="previous", user_id="previous", user_type="account")
    assert calls == [] and not has_app_context()
    assert task(INTENT) == {"code": "unknown"}
    assert calls == ["lookup"] and not has_app_context()
    with pytest.raises(TypeError):
        task(INTENT, "extra")
    assert calls == ["lookup"]


def test_actual_factory_consumer_complete_and_duplicate(composed, monkeypatch):
    s = composed
    result = s.task(str(s.intent_id))
    assert result["code"] == "applied" and str(UUID(result["file_id"])) == result["file_id"]
    assert len(s.sessions) == 6
    assert s.calls.count("decrypt") == s.calls.count("http") == 1
    assert s.calls.count("save") == s.calls.count("readback") == s.calls.count("storage_close") == 1
    with Session(s.session.get_bind()) as reader:
        file = reader.get(UploadFile, result["file_id"])
        assert file.tenant_id == s.revision.default_workspace_id and file.created_by == s.account.id
        assert file.created_by_role == CreatorUserRole.ACCOUNT and file.source_url == "" and file.used
        assert file.hash == hashlib.sha3_256(s.data[file.key]).hexdigest()
        assert file.size == len(s.data[file.key])
        assert reader.get(Account, s.account.id).avatar == file.id
        join = reader.scalar(sa.select(TenantAccountJoin).where(TenantAccountJoin.account_id == s.account.id))
        assert join.tenant_id == file.tenant_id
    monkeypatch.setattr(
        file_helpers,
        "get_signed_file_url",
        lambda **kw: "signed:" + kw["upload_file_id"],
    )
    gateway = SQLAlchemyAccountAvatarFileGateway(session_factory=s.maker)
    assert gateway.get_owned_signed_url(account_id=s.account.id, upload_file_id=result["file_id"]) == (
        "signed:" + result["file_id"]
    )
    before = s.calls.copy()
    sessions_before = len(s.sessions)
    assert s.task(str(s.intent_id)) == result
    assert s.calls == before and len(s.sessions) == sessions_before + 1
    s.no_sql()


@pytest.mark.parametrize(
    "value",
    [
        None,
        UUID(INTENT),
        {"id": INTENT},
        INTENT.upper(),
        INTENT.replace("-", ""),
        " " + INTENT,
        "z" * 36,
    ],
    ids=["none", "uuid-object", "mapping", "uppercase", "compact", "space", "nonhex"],
)
def test_malformed_is_rejected_before_lookup(registered, monkeypatch, value):
    _, _, task, _ = registered
    lookup = Mock(side_effect=AssertionError("lookup forbidden"))
    monkeypatch.setattr(composition, "application_services", lookup)
    assert task(value) == {"code": "unknown"}
    lookup.assert_not_called()


def test_default_off_reaches_original_eligibility_denial(composed, monkeypatch):
    s = composed
    assert CasdoorConfiguration.model_fields["avatar_sync"].default is False
    with s.session.begin():
        policy = json.loads(s.revision.policy_json)
        policy["avatar_sync"] = False
        revision_type = type(s.revision)
        s.session.execute(sa.update(revision_type).values(policy_json=json.dumps(policy)))
        s.session.expire(s.revision)
        config = s.config_owner._configuration(s.revision)
        digest = s.config_owner._validation_digest(config, s.revision)
        s.session.execute(sa.update(revision_type).values(config_digest=digest))
    observed = []
    original = CasdoorConfigurationRepository._configuration

    def configuration(repository, revision):
        result = original(repository, revision)
        observed.append(result.avatar_sync)
        return result

    monkeypatch.setattr(CasdoorConfigurationRepository, "_configuration", configuration)
    assert s.task(str(s.intent_id)) == {"code": "unknown"}
    assert observed and not any(observed)
    assert not s.calls
    value = row(s)
    assert value["operation_state"] == "pending" and value["attempt_count"] == 0
    assert json.loads(value["desired_json"])["reservations"] == []


@pytest.mark.parametrize("site", ["accessor", "field"])
def test_lookup_failure_is_fixed_unknown(registered, monkeypatch, site, caplog):
    app, _, task, _ = registered
    if site == "accessor":
        monkeypatch.setattr(composition, "application_services", Mock(side_effect=RuntimeError(URL)))
    else:
        app.extensions["application_services"] = object()
    assert task(INTENT) == {"code": "unknown"}
    assert URL not in caplog.text


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit], ids=["interrupt", "exit"])
def test_lookup_shutdown_is_sanitized_outside_handlers(registered, monkeypatch, signal):
    _, _, task, _ = registered
    monkeypatch.setattr(composition, "application_services", Mock(side_effect=signal(URL)))
    with pytest.raises(signal) as caught:
        task(INTENT)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert str(caught.value) in ("avatar task interrupted", "1")
    assert URL not in repr(caught.value)
    frames = list(caught.traceback)
    entry = next(frame for frame in frames if frame.name == "consume_casdoor_avatar_initial_task")
    assert all(entry.locals[name] is None for name in ("consumer", "parsed", "intent_id", "application_services"))


@pytest.mark.parametrize(
    "site,signal",
    [("supplier", KeyboardInterrupt), ("storage", SystemExit)],
    ids=["supplier-interrupt", "storage-exit"],
)
def test_actual_consumer_shutdown_survives_task(composed, monkeypatch, site, signal):
    s = composed

    def stop(*args, **kwargs):
        raise signal(URL)

    if site == "supplier":
        monkeypatch.setattr(CasdoorCrypto, "decrypt", stop)
    else:
        s.save_hook = stop
    with pytest.raises(signal) as caught:
        s.task(str(s.intent_id))
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert str(caught.value) in ("avatar consumption interrupted", "1")
    assert URL not in repr(caught.value)
    unknown(s, "fetch_unknown" if site == "supplier" else "storage_unknown")
    assert s.calls.count("save") == (site == "storage")


def test_failed_constructor_remains_startup_failure(consumer, registered, monkeypatch):
    app, _, _, _ = registered
    failure = RuntimeError("synthetic constructor fault")
    monkeypatch.setattr(composition, "CasdoorAvatarConsumerService", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        composition.init_app(app)
    assert caught.value is failure
    assert "application_services" not in app.extensions
    assert not consumer.calls and not consumer.sessions
