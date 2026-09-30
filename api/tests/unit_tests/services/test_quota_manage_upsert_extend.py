"""Quota writes must use the deployment engine's atomic upsert syntax."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import mysql, postgresql

from services import system_manage_extend
from services.system_manage_extend import QuotaManageService


@pytest.mark.parametrize("dialect_name", ["postgresql", "mysql", "mariadb"])
def test_quota_upsert_compiles_for_engine_without_resetting_usage(monkeypatch, dialect_name):
    session = MagicMock()
    session.get_bind.return_value.dialect.name = dialect_name
    monkeypatch.setattr(system_manage_extend, "db", SimpleNamespace(session=session))
    account_id = str(uuid4())
    amount = 12.3456789

    QuotaManageService.set_user_quota(account_id, amount)

    statement = session.execute.call_args.args[0]
    compiler = postgresql.dialect() if dialect_name == "postgresql" else mysql.dialect()
    compiled = statement.compile(dialect=compiler)
    sql = str(compiled)
    assert compiled.params["account_id"] == account_id
    assert compiled.params["total_quota"] == amount
    assert compiled.params["used_quota"] == 0
    conflict = (
        sql.split("DO UPDATE SET", 1)[-1] if dialect_name == "postgresql" else sql.split("DUPLICATE KEY UPDATE", 1)[-1]
    )
    assert "total_quota" in conflict
    assert "used_quota" not in conflict
    assert "account_id" not in conflict
    assert "ON CONFLICT (account_id)" in sql if dialect_name == "postgresql" else "ON DUPLICATE KEY UPDATE" in sql
    session.execute.assert_called_once()
    session.commit.assert_called_once_with()


def test_unsupported_database_is_rejected_before_writing(monkeypatch):
    session = MagicMock()
    session.get_bind.return_value.dialect.name = "sqlite"
    monkeypatch.setattr(system_manage_extend, "db", SimpleNamespace(session=session))

    with pytest.raises(ValueError, match="Unsupported quota database dialect: sqlite"):
        QuotaManageService.set_user_quota(str(uuid4()), 1)

    session.execute.assert_not_called()
    session.commit.assert_not_called()


def test_negative_quota_is_rejected_before_writing(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(system_manage_extend, "db", SimpleNamespace(session=session))

    with pytest.raises(ValueError, match="quota"):
        QuotaManageService.set_user_quota(str(uuid4()), -1)

    session.execute.assert_not_called()
    session.commit.assert_not_called()
