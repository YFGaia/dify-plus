"""extend: AppRunner.add_messages_context 的 NULL retention_number 回归单测。

背景：app_extend 行可能仅由 WebApp 认证开关创建（webapp_auth_enabled 有值、
retention_number 为 NULL）。历史实现对该行执行 int(None) 抛 TypeError，
导致 chat 生成 500。本用例锁定「NULL 与行不存在同义：直接跳过」的语义。

conftest 的 autouse fixture 会把该方法整体桩掉，这里在 import 期先保存原实现。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.app.apps.base_app_runner import AppRunner

# import 期抓取原实现，绕过 conftest autouse bypass fixture 的 monkeypatch
_original_add_messages_context = AppRunner.add_messages_context


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    from extensions import ext_database

    db = MagicMock()
    monkeypatch.setattr(ext_database, "db", db)
    return db


@pytest.fixture
def mock_redis(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    from extensions import ext_redis

    redis = MagicMock()
    redis.get.return_value = None
    monkeypatch.setattr(ext_redis, "redis_client", redis)
    return redis


def _call(prompt_messages: list) -> None:
    _original_add_messages_context(
        SimpleNamespace(), prompt_messages, app_id="app-1", conversation_id="c-1", message_id="m-1"
    )


class TestAddMessagesContextExtend:
    def test_null_retention_number_row_is_skipped(self, mock_db: MagicMock, mock_redis: MagicMock):
        """行存在但 retention_number 为 NULL（如仅配置过 WebApp 认证开关）时直接跳过。"""
        mock_db.session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            retention_number=None
        )

        _call(prompt_messages=[object()] * 10)

        mock_redis.set.assert_not_called()
        mock_db.session.add.assert_not_called()

    def test_missing_row_is_skipped(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_db.session.query.return_value.filter.return_value.first.return_value = None

        _call(prompt_messages=[object()] * 10)

        mock_redis.set.assert_not_called()
        mock_db.session.add.assert_not_called()

    def test_configured_retention_registers_context_split(self, mock_db: MagicMock, mock_redis: MagicMock):
        mock_db.session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(retention_number=2)

        _call(prompt_messages=[object()] * 10)

        mock_redis.set.assert_called_once_with("retention_number_app-1", 2)
        mock_db.session.add.assert_called_once()
        mock_db.session.commit.assert_called_once()
