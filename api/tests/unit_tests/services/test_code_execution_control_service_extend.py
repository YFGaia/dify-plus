"""CodeExecutionControlService 写路径单测（openspec p5-admin-decommission 任务 2.7）。

db.session 与 redis_client 均为 mock：验证规范化/校验/查重逻辑与
「DB commit 后重建缓存、redis 失败不回滚只降级 cache_synced」的失败语义。
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import services.system_manage_extend as service_module
from services.system_manage_extend import CONTROL_MAIL_CACHE_KEY, CodeExecutionControlService


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


def _stub_duplicate_check(mock_db: MagicMock, existing: object | None) -> None:
    """query(...).filter(...).first() 路径（add/remove 的存在性检查）"""
    mock_db.session.query.return_value.filter.return_value.first.return_value = existing


def _stub_email_rows(mock_db: MagicMock, emails: list[str]) -> None:
    """query(...).order_by(...).all() 路径（rebuild 的全量名单查询）"""
    rows = [SimpleNamespace(email=email) for email in emails]
    mock_db.session.query.return_value.order_by.return_value.all.return_value = rows


class TestAddEmail:
    def test_add_normalizes_email_and_syncs_cache(self, mock_db: MagicMock, mock_redis: MagicMock):
        _stub_duplicate_check(mock_db, existing=None)
        _stub_email_rows(mock_db, ["user@example.com"])

        record, cache_synced = CodeExecutionControlService.add_email("  User@Example.COM ", created_by="acc-1")

        assert record.email == "user@example.com"
        assert record.created_by == "acc-1"
        assert cache_synced is True
        mock_db.session.add.assert_called_once_with(record)
        mock_db.session.commit.assert_called_once()
        mock_redis.set.assert_called_once_with(CONTROL_MAIL_CACHE_KEY, json.dumps(["user@example.com"]))

    @pytest.mark.parametrize("bad_email", ["", "   ", "not-an-email", "a@b", "a b@c.com", "@example.com"])
    def test_add_invalid_format_raises_value_error(self, mock_db: MagicMock, mock_redis: MagicMock, bad_email: str):
        with pytest.raises(ValueError, match="Invalid email format"):
            CodeExecutionControlService.add_email(bad_email, created_by="acc-1")

        mock_db.session.add.assert_not_called()
        mock_db.session.commit.assert_not_called()
        mock_redis.set.assert_not_called()

    def test_add_duplicate_raises_value_error(self, mock_db: MagicMock, mock_redis: MagicMock):
        _stub_duplicate_check(mock_db, existing=SimpleNamespace(email="user@example.com"))

        with pytest.raises(ValueError, match="already exists"):
            CodeExecutionControlService.add_email("user@example.com", created_by="acc-1")

        mock_db.session.add.assert_not_called()
        mock_db.session.commit.assert_not_called()

    def test_add_redis_failure_returns_cache_synced_false_without_rollback(
        self, mock_db: MagicMock, mock_redis: MagicMock
    ):
        _stub_duplicate_check(mock_db, existing=None)
        _stub_email_rows(mock_db, ["user@example.com"])
        mock_redis.set.side_effect = ConnectionError("redis down")

        record, cache_synced = CodeExecutionControlService.add_email("user@example.com", created_by="acc-1")

        assert record.email == "user@example.com"
        assert cache_synced is False
        # DB 写入不回滚：commit 已执行且无 rollback
        mock_db.session.commit.assert_called_once()
        mock_db.session.rollback.assert_not_called()


class TestRemoveEmail:
    def test_remove_not_found_raises_value_error(self, mock_db: MagicMock, mock_redis: MagicMock):
        _stub_duplicate_check(mock_db, existing=None)

        with pytest.raises(ValueError, match="not found"):
            CodeExecutionControlService.remove_email("missing-id")

        mock_db.session.delete.assert_not_called()
        mock_db.session.commit.assert_not_called()

    def test_remove_deletes_and_syncs_cache(self, mock_db: MagicMock, mock_redis: MagicMock):
        record = SimpleNamespace(id="rec-1", email="user@example.com")
        _stub_duplicate_check(mock_db, existing=record)
        _stub_email_rows(mock_db, [])

        cache_synced = CodeExecutionControlService.remove_email("rec-1")

        assert cache_synced is True
        mock_db.session.delete.assert_called_once_with(record)
        mock_db.session.commit.assert_called_once()
        # 名单清空后 redis 写入空数组（spec「清空名单」scenario）
        mock_redis.set.assert_called_once_with(CONTROL_MAIL_CACHE_KEY, json.dumps([]))

    def test_remove_redis_failure_returns_false(self, mock_db: MagicMock, mock_redis: MagicMock):
        record = SimpleNamespace(id="rec-1", email="user@example.com")
        _stub_duplicate_check(mock_db, existing=record)
        _stub_email_rows(mock_db, [])
        mock_redis.set.side_effect = ConnectionError("redis down")

        assert CodeExecutionControlService.remove_email("rec-1") is False
        mock_db.session.commit.assert_called_once()


class TestRebuildControlMailCache:
    def test_rebuild_is_idempotent(self, mock_db: MagicMock, mock_redis: MagicMock):
        _stub_email_rows(mock_db, ["a@example.com", "b@example.com"])

        first = CodeExecutionControlService.rebuild_control_mail_cache()
        second = CodeExecutionControlService.rebuild_control_mail_cache()

        assert first is True
        assert second is True
        assert mock_redis.set.call_count == 2
        payloads = [call.args for call in mock_redis.set.call_args_list]
        assert payloads[0] == payloads[1] == (CONTROL_MAIL_CACHE_KEY, json.dumps(["a@example.com", "b@example.com"]))

    def test_rebuild_redis_failure_returns_false_and_does_not_raise(self, mock_db: MagicMock, mock_redis: MagicMock):
        _stub_email_rows(mock_db, ["a@example.com"])
        mock_redis.set.side_effect = ConnectionError("redis down")

        assert CodeExecutionControlService.rebuild_control_mail_cache() is False
