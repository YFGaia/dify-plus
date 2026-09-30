"""Compile extension DDL and exercise dialect-specific SQL without database services.

These checks use synthetic rows and do not establish legacy database compatibility.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql

VERSIONS_DIR = Path(__file__).resolve().parents[3] / "migrations_extend" / "versions"
UUID_MIGRATIONS = sorted(path for path in VERSIONS_DIR.glob("*.py") if "uuid_default =" in path.read_text())


def _load(name):
    path = next(VERSIONS_DIR.glob(f"*{name}*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind(dialect):
    bind = MagicMock()
    bind.dialect = dialect
    return bind


@pytest.mark.parametrize("path", UUID_MIGRATIONS, ids=lambda path: path.name)
@pytest.mark.parametrize("dialect", [mysql.dialect(), postgresql.dialect()], ids=["mysql", "postgresql"])
def test_uuid_and_text_defaults_compile_for_target_dialect(path, dialect):
    migration = _load(path.stem)
    bind = _bind(dialect)
    tables = []

    def capture_table(name, *columns):
        tables.append(sa.Table(name, sa.MetaData(), *columns))

    with patch.object(migration, "op") as fake_op, patch.object(migration.Inspector, "from_engine") as inspect:
        fake_op.get_bind.return_value = bind
        fake_op.create_table.side_effect = capture_table
        inspect.return_value.get_table_names.return_value = []
        migration.upgrade()

    assert tables
    for table in tables:
        ddl = str(sa.schema.CreateTable(table).compile(dialect=dialect))
        if dialect.name == "mysql":
            assert "id CHAR(36) NOT NULL DEFAULT (UUID())" in ddl
            assert "uuid_generate_v4" not in ddl
            assert "::" not in ddl
            for column in table.c:
                if isinstance(column.type, sa.Text) and column.server_default is not None:
                    assert str(column.server_default.arg) in {"('')", "('[]')"}
        else:
            assert "id UUID DEFAULT uuid_generate_v4() NOT NULL" in ddl
            assert str(table.c.id.server_default.arg) == "uuid_generate_v4()"


@pytest.mark.parametrize("dialect", [mysql.dialect(), postgresql.dialect()], ids=["mysql", "postgresql"])
def test_drop_statements_quote_identifiers_for_dialect(dialect):
    for name in ("drop_gva_admin_tables", "drop_recommended_category_tables"):
        migration = _load(name)
        bind = _bind(dialect)
        with patch.object(migration, "op") as fake_op:
            fake_op.get_bind.return_value = bind
            migration.upgrade()
        statements = [str(call.args[0]) for call in bind.execute.call_args_list]
        quote = "`" if dialect.name == "mysql" else '"'
        assert statements
        assert all(f"DROP TABLE IF EXISTS {quote}" in sql for sql in statements)
        if name == "drop_gva_admin_tables":
            assert all(sql.endswith(" CASCADE") == (dialect.name == "postgresql") for sql in statements)


@pytest.mark.parametrize("dialect", [mysql.dialect(), postgresql.dialect()], ids=["mysql", "postgresql"])
def test_category_merge_preserves_existing_order_and_quotes_reserved_column(dialect):
    migration = _load("migrate_recommended_categories_to_native")
    bind = _bind(dialect)
    bind.execute.return_value.fetchall.return_value = [
        SimpleNamespace(recommended_id="app", category="B"),
        SimpleNamespace(recommended_id="app", category="A"),
        SimpleNamespace(recommended_id="app", category="B"),
    ]
    bind.execute.return_value.scalar.return_value = '["B", "existing"]'
    with patch.object(migration, "op") as fake_op, patch.object(migration.sa, "inspect") as inspect:
        fake_op.get_bind.return_value = bind
        inspect.return_value.get_table_names.return_value = [
            "recommended_category_extend",
            "recommended_apps_category_join_extend",
        ]
        migration.upgrade()
    calls = bind.execute.call_args_list
    quote = "`" if dialect.name == "mysql" else '"'
    assert f"c.{quote}table{quote} AS category" in str(calls[0].args[0])
    assert "CAST(:categories AS json)" in str(calls[-1].args[0])
    assert json.loads(calls[-1].args[1]["categories"]) == ["B", "existing", "A"]


def test_mysql_deduplication_retains_latest_row_per_account_with_stable_ties():
    migration = _load("add_account_money_extend_unique_constraint")
    bind = _bind(mysql.dialect())
    with patch.object(migration, "op") as fake_op, patch.object(migration.Inspector, "from_engine") as inspect:
        fake_op.get_bind.return_value = bind
        inspect.return_value.get_table_names.return_value = ["account_money_extend"]
        migration.upgrade()
    statement = bind.execute.call_args.args[0]
    assert "DISTINCT ON" not in str(statement)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE account_money_extend (id TEXT, account_id TEXT, updated_at TEXT)"))
        conn.execute(
            sa.text("INSERT INTO account_money_extend VALUES (:id, :account_id, :updated_at)"),
            [
                {"id": "a-old", "account_id": "a", "updated_at": "2024-01-01"},
                {"id": "a-new", "account_id": "a", "updated_at": "2024-02-01"},
                {"id": "b-1", "account_id": "b", "updated_at": "2024-01-01"},
                {"id": "b-2", "account_id": "b", "updated_at": "2024-01-01"},
                {"id": "c-only", "account_id": "c", "updated_at": "2024-01-01"},
                {"id": "d-null", "account_id": "d", "updated_at": None},
                {"id": "d-dated", "account_id": "d", "updated_at": "2024-01-01"},
            ],
        )
        conn.execute(statement)
        assert set(conn.execute(sa.text("SELECT id FROM account_money_extend")).scalars()) == {
            "a-new",
            "b-2",
            "c-only",
            "d-null",
        }
        conn.execute(statement)
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM account_money_extend")) == 4
    engine.dispose()


def test_postgres_deduplication_sql_remains_unchanged():
    migration = _load("add_account_money_extend_unique_constraint")
    bind = _bind(postgresql.dialect())
    with patch.object(migration, "op") as fake_op, patch.object(migration.Inspector, "from_engine") as inspect:
        fake_op.get_bind.return_value = bind
        inspect.return_value.get_table_names.return_value = ["account_money_extend"]
        migration.upgrade()
    sql = " ".join(str(bind.execute.call_args.args[0]).split())
    assert sql == (
        "DELETE FROM account_money_extend WHERE id NOT IN ( SELECT DISTINCT ON (account_id) id "
        "FROM account_money_extend ORDER BY account_id, updated_at DESC )"
    )


def test_mysql_category_downgrade_matches_original_schema():
    migration = _load("drop_recommended_category_tables")
    statements = []
    engine = sa.create_mock_engine(
        "mysql://", lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect)))
    )
    with patch.object(migration, "op") as fake_op:
        fake_op.get_bind.return_value = engine
        migration.downgrade()
    creates = [sql for sql in statements if "CREATE TABLE" in sql]
    indexes = [sql for sql in statements if "CREATE INDEX" in sql]
    assert len(creates) == 2
    assert all("id CHAR(36) NOT NULL DEFAULT (UUID())" in sql for sql in creates)
    assert any("`table` VARCHAR(255) NOT NULL" in sql for sql in creates)
    assert len(indexes) == 4
    assert all("IF NOT EXISTS" not in sql for sql in indexes)
