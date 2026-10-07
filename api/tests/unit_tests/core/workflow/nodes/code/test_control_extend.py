"""ExecutionControl.check_code 安全回退单测（openspec p5-admin-decommission 任务 2.7 / 13.4）。

对应 spec「redis 异常安全回退」scenario：redis 键缺失、非法 JSON、DB 查询异常
三分支均须返回 False 且不抛异常（code 节点回退普通 sandbox，不中断 workflow）。
"""

import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

import core.workflow.nodes.code.control_extend as control_module
from core.workflow.nodes.code.control_extend import ExecutionControl

TENANT_ID = "tenant-123"


@pytest.fixture
def mock_redis(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    redis = MagicMock()
    monkeypatch.setattr(control_module, "redis_client", redis)
    return redis


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    db = MagicMock()
    monkeypatch.setattr(control_module, "db", db)
    return db


def test_missing_redis_key_returns_false(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.return_value = None

    assert ExecutionControl().check_code(TENANT_ID) is False
    mock_db.session.query.assert_not_called()


def test_invalid_json_returns_false(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.return_value = b"not-a-json-array"

    assert ExecutionControl().check_code(TENANT_ID) is False
    mock_db.session.query.assert_not_called()


def test_non_list_json_returns_false(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.return_value = json.dumps({"unexpected": "shape"})

    assert ExecutionControl().check_code(TENANT_ID) is False
    mock_db.session.query.assert_not_called()


def test_empty_list_returns_false_without_db_query(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.return_value = json.dumps([])

    assert ExecutionControl().check_code(TENANT_ID) is False
    mock_db.session.query.assert_not_called()


def test_db_query_error_returns_false(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.return_value = json.dumps(["owner@example.com"])
    mock_db.session.query.side_effect = SQLAlchemyError("db down")

    assert ExecutionControl().check_code(TENANT_ID) is False


def test_redis_get_error_returns_false(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.side_effect = ConnectionError("redis down")

    assert ExecutionControl().check_code(TENANT_ID) is False
    mock_db.session.query.assert_not_called()


def test_owner_in_allowlist_returns_true(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.return_value = json.dumps(["owner@example.com"])
    mock_db.session.query.return_value.join.return_value.filter.return_value.count.return_value = 1

    assert ExecutionControl().check_code(TENANT_ID) is True


def test_owner_not_in_allowlist_returns_false(mock_redis: MagicMock, mock_db: MagicMock):
    mock_redis.get.return_value = json.dumps(["someone-else@example.com"])
    mock_db.session.query.return_value.join.return_value.filter.return_value.count.return_value = 0

    assert ExecutionControl().check_code(TENANT_ID) is False
