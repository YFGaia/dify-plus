"""Registered manager-to-worker retry, real owners with offline bottom transports."""

import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from configs import dify_config
from controllers.console import bp, wraps
from extensions import ext_application_services as composition
from extensions.ext_login import DifyLoginManager, _load_user_from_request
from libs import datetime_utils
from libs.passport import PassportService
from libs.token import _real_cookie_name, generate_csrf_token
from models.account import TenantAccountJoin
from models.casdoor_extend import CasdoorAvatarDispatchCursorExtend as Cursor
from services.entities.feature_entities import LicenseStatus
from services.system_feature_service import SystemFeatureService
from test_casdoor_avatar_consumer_extend import (
    avatar_fixture as original_avatar_fixture,
)
from test_casdoor_avatar_consumer_extend import consumer as original_consumer
from test_casdoor_avatar_consumer_extend import row
from test_casdoor_avatar_consumer_extend import (
    storage_fixture as original_storage_fixture,
)
from test_casdoor_avatar_pre_storage_recovery_extend import produce

from tests.unit_tests.extensions.test_casdoor_avatar_task_composition_extend import (
    registered as original_registered,
)

consumer = original_consumer
avatar_fixture = original_avatar_fixture
storage_fixture = original_storage_fixture
registered = original_registered
BASE = "/console/api/system-manage-extend/integration/casdoor/sync"


@pytest.fixture
def mounted(consumer, registered, monkeypatch):
    s = consumer
    app, celery, task, _ = registered
    for key, value in dict(
        CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS=s.account.id,
        COOKIE_DOMAIN="",
        CONSOLE_API_URL="https://console.example.test",
        CONSOLE_WEB_URL="https://console.example.test",
        ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK=True,
    ).items():
        monkeypatch.setattr(dify_config, key, value)
    monkeypatch.setattr(datetime_utils, "_now_func", lambda tz: s.utc)
    Cursor.__table__.create(s.session.get_bind())
    with s.session.begin():
        s.session.execute(sa.update(TenantAccountJoin).values(current=True))
    composition.init_app(app)
    with app.app_context():
        services = composition.application_services()
    services.casdoor_avatar_retry._now = lambda: s.utc
    services.casdoor_self_identity._now = lambda: s.utc
    refresh = "7" * 128

    class Redis:
        def get(self, key):
            return s.account.id.encode() if key == "refresh_token:" + refresh else None

        def zremrangebyscore(self, *args):
            return 0

        def zcard(self, *args):
            return 0

        def zadd(self, *args):
            return 1

        def expire(self, *args):
            return True

    redis = Redis()
    services.casdoor_avatar_retry._redis_runtime_factory = SimpleNamespace(
        open=lambda **kw: SimpleNamespace(client=redis, finish=lambda: True)
    )
    app.config.update(
        TESTING=True,
        SERVER_NAME="console.example.test",
        SECRET_KEY="synthetic-flask-only",
    )
    manager = DifyLoginManager()
    manager.init_app(app)

    @manager.request_loader
    def load(req):
        with s.maker() as session:
            return _load_user_from_request(req, session)

    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    monkeypatch.setattr(
        SystemFeatureService, "get_license_status", lambda: LicenseStatus.ACTIVE
    )
    app.register_blueprint(bp)
    client = app.test_client()
    access = PassportService().issue(
        dict(
            user_id=s.account.id,
            exp=int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            iss="COMMUNITY",
            sub="Console API Passport",
        )
    )
    csrf = generate_csrf_token(s.account.id)
    for name, value in {
        "access_token": access,
        "refresh_token": refresh,
        "csrf_token": csrf,
    }.items():
        client.set_cookie(_real_cookie_name(name), value, domain="console.example.test")

    def send(path, **kwargs):
        return client.open(
            path,
            headers={"X-CSRF-Token": csrf},
            base_url=dify_config.CONSOLE_API_URL,
            environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
            **kwargs,
        )

    published = []

    def publish(*, args, **kwargs):
        s.no_sql()
        published.append(args[0])

    monkeypatch.setattr(task, "apply_async", publish)
    importlib.import_module("tasks.casdoor_avatar_recovery_task_extend")
    s.app, s.services, s.client, s.send, s.task, s.published = (
        app,
        services,
        client,
        send,
        task,
        published,
    )
    s.recovery = celery.tasks[
        "tasks.casdoor_avatar_recovery_task_extend.dispatch_casdoor_avatar_initial_pending"
    ]
    return s


def test_registered_manager_targets_and_explicit_retry(mounted):
    s = mounted
    produce(s)
    original = row(s)
    response = s.send(BASE + "/retry-targets")
    assert response.status_code == 200, response.json
    assert response.json["targets"][0]["intent_id"] == str(s.intent_id)
    response = s.send(
        BASE + "/retry", method="POST", json={"intent_id": str(s.intent_id)}
    )
    assert response.status_code == 200, response.json
    assert response.json == {"status": "pending", "intent_id": str(s.intent_id)}
    assert s.recovery() == {"code": "scanned"}
    assert s.published == [str(s.intent_id)]
    result = s.task(s.published[0])
    assert result["code"] == "applied", result
    value = row(s)
    assert value["attempt_count"] == 2
    assert (
        json.loads(value["desired_json"])["reservations"][0]
        == json.loads(original["desired_json"])["reservations"][0]
    )
    assert s.calls.count("save") == 1
    response = s.send("/console/api/account/casdoor-identity")
    assert response.status_code == 200, response.json
    assert (
        response.json["identities"][0]["avatar_status"] == "local_attachment_recorded"
    )


@pytest.mark.parametrize(
    "query",
    [
        "?limit=0",
        "?limit=101",
        "?limit=1&limit=2",
        "?limit=1.0",
        "?after=bad",
        "?foreign=1",
    ],
)
def test_registered_query_is_closed_and_bounded(mounted, query):
    assert mounted.send(BASE + "/retry-targets" + query).status_code == 400
    assert not mounted.calls


def test_actual_schema_declares_query_and_manager_response_sanitizes_ids(mounted):
    from controllers.console import api

    s = mounted
    produce(s)
    with s.app.test_request_context():
        operation = api.__schema__["paths"][
            "/system-manage-extend/integration/casdoor/sync/retry-targets"
        ]["get"]
        assert {item["name"] for item in operation["parameters"]} == {"after", "limit"}
        assert all(item["in"] == "query" for item in operation["parameters"])
    response = s.send(BASE + "/retry-targets")
    target = response.json["targets"][0]
    assert set(target) == {
        "account_id",
        "identity_id",
        "intent_id",
        "reason",
        "retry_eligible",
    }
    assert "url_ciphertext" not in response.get_data(as_text=True)
    assert not s.published and not s.calls


def test_duplicate_manager_request_and_duplicate_delivery_do_not_allocate_third_attempt(
    mounted,
):
    s = mounted
    produce(s)
    for _ in range(2):
        assert (
            s.send(
                BASE + "/retry", method="POST", json={"intent_id": str(s.intent_id)}
            ).status_code
            == 200
        )
    assert s.recovery() == {"code": "scanned"}
    first = s.task(s.published[0])
    assert first["code"] == "applied"
    assert s.task(s.published[0]) == first
    assert row(s)["attempt_count"] == 2 and s.calls.count("save") == 1
    assert (
        s.send(
            BASE + "/retry", method="POST", json={"intent_id": str(s.intent_id)}
        ).status_code
        != 200
    )


def test_native_anonymous_and_missing_csrf_are_denied_before_mutation(mounted):
    s = mounted
    produce(s)
    before = row(s)
    s.client.delete_cookie(
        _real_cookie_name("csrf_token"), domain="console.example.test"
    )
    assert (
        s.send(
            BASE + "/retry", method="POST", json={"intent_id": str(s.intent_id)}
        ).status_code
        == 401
    )
    anonymous = s.app.test_client()
    assert (
        anonymous.get(
            BASE + "/retry-targets", base_url=dify_config.CONSOLE_API_URL
        ).status_code
        == 401
    )
    assert row(s) == before and not s.calls


def test_same_uuid_retry_is_revisited_by_original_singleton_cursor(mounted):
    s = mounted
    assert s.recovery() == {"code": "scanned"}
    assert s.published == [str(s.intent_id)]
    s.published.clear()
    produce(s)
    assert (
        s.send(
            BASE + "/retry", method="POST", json={"intent_id": str(s.intent_id)}
        ).status_code
        == 200
    )
    # The original empty tail closes one sweep; the following bounded invocation revisits the same UUID.
    assert s.recovery() == {"code": "scanned"}
    assert s.published == []
    assert s.recovery() == {"code": "scanned"}
    assert s.published == [str(s.intent_id)]
    assert s.task(s.published[0])["code"] == "applied"
