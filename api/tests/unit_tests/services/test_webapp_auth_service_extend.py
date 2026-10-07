"""WebAppAuthExtendService 单测：redis 投影缓存读写与安全默认值语义。

db / redis_client 均为 mock：验证缓存两种状态均重查 DB、缓存未命中回填、
NULL 列按默认开启处理、写路径 get-or-create + 提交后缓存失效，以及 redis/db 异常时的降级行为。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import services.webapp_auth_service_extend as service_module
from services.webapp_auth_service_extend import WebAppAuthExtendService

APP_ID = "app-1"
CACHE_KEY = f"webapp_auth_enabled_extend:{APP_ID}"


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    db = MagicMock()
    monkeypatch.setattr(service_module, "db", db)
    return db


@pytest.fixture
def mock_redis(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    redis = MagicMock()
    monkeypatch.setattr(service_module, "redis_client", redis)
    return redis


class TestIsWebAppAuthEnabled:
    def test_cached_protected_value_rechecks_db(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_redis.get.return_value = b"1"
        mock_db.session.scalar.return_value = SimpleNamespace(webapp_auth_enabled=False)

        assert WebAppAuthExtendService.is_webapp_auth_enabled(APP_ID) is False
        mock_redis.get.assert_called_once_with(CACHE_KEY)
        mock_db.session.scalar.assert_called_once()
        mock_redis.set.assert_called_once_with(CACHE_KEY, "0", ex=60)

    def test_cached_public_value_rechecks_db(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_redis.get.return_value = b"0"
        mock_db.session.scalar.return_value = SimpleNamespace(webapp_auth_enabled=True)

        assert WebAppAuthExtendService.is_webapp_auth_enabled(APP_ID) is True
        mock_db.session.scalar.assert_called_once()
        mock_redis.set.assert_called_once_with(CACHE_KEY, "1", ex=60)

    def test_cache_miss_defaults_to_enabled_and_backfills(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_redis.get.return_value = None
        mock_db.session.scalar.return_value = None  # 无 AppExtend 记录

        assert WebAppAuthExtendService.is_webapp_auth_enabled(APP_ID) is True
        mock_redis.set.assert_called_once_with(CACHE_KEY, "1", ex=60)

    def test_cache_keys_are_isolated_per_app(self, mock_db: MagicMock, mock_redis: MagicMock):
        other_app_id = "app-2"
        mock_redis.get.return_value = None
        mock_db.session.scalar.return_value = SimpleNamespace(webapp_auth_enabled=False)

        assert WebAppAuthExtendService.is_webapp_auth_enabled(other_app_id) is False

        mock_redis.get.assert_called_once_with("webapp_auth_enabled_extend:app-2")
        mock_redis.set.assert_called_once_with("webapp_auth_enabled_extend:app-2", "0", ex=60)

    def test_null_column_means_enabled(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_redis.get.return_value = None
        mock_db.session.scalar.return_value = SimpleNamespace(webapp_auth_enabled=None)

        assert WebAppAuthExtendService.is_webapp_auth_enabled(APP_ID) is True
        mock_redis.set.assert_called_once_with(CACHE_KEY, "1", ex=60)

    def test_disabled_column_backfills_zero(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_redis.get.return_value = None
        mock_db.session.scalar.return_value = SimpleNamespace(webapp_auth_enabled=False)

        assert WebAppAuthExtendService.is_webapp_auth_enabled(APP_ID) is False
        mock_redis.set.assert_called_once_with(CACHE_KEY, "0", ex=60)

    def test_redis_down_falls_back_to_db(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_redis.get.side_effect = ConnectionError("redis down")
        mock_db.session.scalar.return_value = SimpleNamespace(webapp_auth_enabled=False)

        assert WebAppAuthExtendService.is_webapp_auth_enabled(APP_ID) is False

    def test_db_error_defaults_to_enabled(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_redis.get.return_value = None
        mock_db.session.scalar.side_effect = RuntimeError("db down")

        assert WebAppAuthExtendService.is_webapp_auth_enabled(APP_ID) is True


class TestSetWebAppAuthEnabled:
    def test_creates_row_without_invalidating_uncommitted_cache(self, mock_redis: MagicMock):
        session = MagicMock()
        session.scalar.return_value = None

        WebAppAuthExtendService.set_webapp_auth_enabled(APP_ID, False, session=session)

        added = session.add.call_args.args[0]
        assert added.app_id == APP_ID
        assert added.webapp_auth_enabled is False
        session.commit.assert_not_called()  # 由调用方（with_session）统一提交
        mock_redis.delete.assert_not_called()

    def test_updates_existing_row(self, mock_redis: MagicMock):
        existing = SimpleNamespace(app_id=APP_ID, webapp_auth_enabled=None)
        session = MagicMock()
        session.scalar.return_value = existing

        WebAppAuthExtendService.set_webapp_auth_enabled(APP_ID, True, session=session)

        assert existing.webapp_auth_enabled is True
        session.add.assert_not_called()
        mock_redis.delete.assert_not_called()

    def test_invalidate_after_commit_deletes_only_app_key(self, mock_redis: MagicMock):
        WebAppAuthExtendService.invalidate_after_commit(APP_ID)

        mock_redis.delete.assert_called_once_with(CACHE_KEY)

    def test_redis_failure_does_not_raise(self, mock_redis: MagicMock):
        session = MagicMock()
        session.scalar.return_value = None
        mock_redis.delete.side_effect = ConnectionError("redis down")

        WebAppAuthExtendService.set_webapp_auth_enabled(APP_ID, False, session=session)
        WebAppAuthExtendService.invalidate_after_commit(APP_ID)
