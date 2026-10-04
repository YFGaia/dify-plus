"""Initial navigation is bounded, durable and separate from original authority."""

import ast
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.account import Account, TenantAccountJoin
from models.base import Base
from models.casdoor_extend import CasdoorAvatarDispatchCursorExtend as Cursor
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import Session
from test_casdoor_avatar_attempt_extend import avatar_fixture, snapshot, storage_fixture
from test_casdoor_avatar_attempt_extend import worker as original_worker
from test_casdoor_profile_repository_extend import NOW

from tests.unit_tests.extensions.test_casdoor_avatar_task_composition_extend import (
    composed as original_composed,
)
from tests.unit_tests.extensions.test_casdoor_avatar_task_composition_extend import (
    consumer as original_consumer,
)
from tests.unit_tests.extensions.test_casdoor_avatar_task_composition_extend import (
    registered as original_registered,
)

avatar_fixture = avatar_fixture
storage_fixture = storage_fixture
worker = original_worker
consumer = original_consumer
composed = original_composed
registered = original_registered


@pytest.fixture
def navigation(worker):
    Cursor.__table__.create(worker.session.get_bind())
    return worker


def page(s, limit=100):
    with s.session.begin():
        return s.avatar_owner._scan_initial_dispatch_page(limit=limit, now=NOW)


def candidate(s, intent_id=None, now=NOW):
    with s.session.begin():
        return s.avatar_owner._initial_dispatch_candidate(intent_id or s.intent_id, now=now)


def cursor(s):
    with Session(s.session.get_bind()) as reader:
        row = reader.execute(sa.select(*Cursor.__table__.columns)).mappings().one_or_none()
        return dict(row) if row else None


def clone(s, key, **changes):
    """Synthetic navigation-only backlog; no clone can grant original authority."""
    values = dict(
        s.session.execute(sa.select(*Intent.__table__.columns).where(Intent.id == str(s.intent_id))).mappings().one()
    )
    values.update(id=str(key), identity_id=str(uuid4()), idempotency_key=uuid4().hex, **changes)
    s.session.execute(sa.insert(Intent).values(**values))


def test_committed_producer_page_candidate_and_original_consumer_claim(composed):
    s = composed
    Cursor.__table__.create(s.session.get_bind())
    before = snapshot(s)
    result = page(s)
    assert result.intent_ids == (s.intent_id,) and result.sweep_complete is False
    assert candidate(s) == s.intent_id
    assert snapshot(s) == before
    assert cursor(s)["version"] == 1
    with pytest.raises(FrozenInstanceError):
        result.intent_ids = ()
    claim = s.task(str(result.intent_ids[0]))
    assert claim["code"] == "applied"
    before_calls = list(s.calls)
    assert s.task(str(result.intent_ids[0])) == claim
    assert s.calls == before_calls
    assert s.calls.count("save") == 1
    assert candidate(s) is None
    assert page(s).intent_ids == ()
    assert page(s).intent_ids == ()


def test_structural_initial_exclusion_is_readonly(navigation):
    s = navigation
    fields = {
        "attempt_id": str(uuid4()),
        "lease_owner": "a" * 64,
        "lease_expires_at": NOW.replace(tzinfo=None),
        "resource_type": "avatar",
        "resource_id": str(uuid4()),
        "sent_at": NOW.replace(tzinfo=None),
        "acknowledged_at": NOW.replace(tzinfo=None),
        "readback_at": NOW.replace(tzinfo=None),
        "terminated_at": NOW.replace(tzinfo=None),
        "termination_proof_kind": "unknown",
        "proof_ref": "unknown",
        "retry_at": NOW.replace(tzinfo=None),
        "error_code": "unknown",
        "attempt_count": 1,
        "kind": "role_replace",
        "operation_state": "unknown",
        "termination_state": "manual_recovery",
    }
    for name, value in fields.items():
        with s.session.begin():
            s.session.execute(sa.update(Intent).values(**{name: value}))
            before = list(s.session.execute(sa.select(*Intent.__table__.columns)))
            assert s.avatar_owner._scan_initial_dispatch_page(now=NOW).intent_ids == ()
            assert s.avatar_owner._initial_dispatch_candidate(s.intent_id, now=NOW) is None
            assert list(s.session.execute(sa.select(*Intent.__table__.columns))) == before
            s.session.rollback()
    assert cursor(s) is None


def test_bounded_indexed_scan_passes_more_than_100_stale_records(navigation):
    s = navigation
    # Keep the real producer's UUID, and choose lower canonical keys for stale rows.
    stale_ids = tuple(UUID(int=index) for index in range(1, 102))
    assert s.intent_id.int > 101
    with s.session.begin():
        for key in stale_ids:
            clone(s, key, desired_json="{}")
    statements = []
    engine = s.session.get_bind()

    def observed(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", observed)
    try:
        first = page(s)
    finally:
        sa.event.remove(engine, "before_cursor_execute", observed)
    assert first.intent_ids == stale_ids[:100]
    assert all("desired_json" not in statement for statement in statements)
    assert all(not statement.lstrip().upper().startswith("UPDATE CASDOOR_SYNC") for statement in statements)
    assert all(candidate(s, key) is None for key in first.intent_ids)
    second = page(s)
    assert second.intent_ids == (stale_ids[-1], s.intent_id)
    assert candidate(s, second.intent_ids[0]) is None
    assert candidate(s, second.intent_ids[1]) == s.intent_id
    assert page(s).sweep_complete
    index = next(index for index in Intent.__table__.indexes if index.name == "casdoor_avatar_initial_scan_idx")
    assert tuple(column.name for column in index.columns) == (
        "kind",
        "operation_state",
        "termination_state",
        "attempt_count",
        "id",
    )
    with s.session.begin():
        query = sa.select(Intent.id).where(*s.avatar_owner._dispatch_initial_filters()).order_by(Intent.id).limit(100)
        compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
        plan = str(s.session.execute(sa.text("EXPLAIN QUERY PLAN " + compiled)).all())
        assert "casdoor_avatar_initial_scan_idx" in plan and "TEMP B-TREE" not in plan


def test_finite_sweep_wraps_and_later_accepts_new_arrivals(navigation):
    s = navigation
    low, high = UUID(int=1), UUID(int=(1 << 128) - 1)
    assert page(s, 1).intent_ids == (s.intent_id,)
    with s.session.begin():
        clone(s, low, desired_json="{}")
        clone(s, high, desired_json="{}")
    assert page(s).intent_ids == ()
    assert cursor(s)["last_id"] is cursor(s)["sweep_upper_id"] is None
    assert page(s).intent_ids == (low, s.intent_id, high)
    assert page(s).sweep_complete
    assert page(s, 1).intent_ids == (low,)


def test_cursor_rollback_cas_and_corruption_fail_closed(navigation):
    s = navigation
    with s.session.begin():
        assert s.avatar_owner._scan_initial_dispatch_page(now=NOW).intent_ids == (s.intent_id,)
        s.session.rollback()
    assert cursor(s) is None
    page(s)
    before = cursor(s)
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER corrupt_cursor AFTER UPDATE ON casdoor_avatar_dispatch_cursor_extend "
                "BEGIN UPDATE casdoor_avatar_dispatch_cursor_extend SET version = NEW.version + 1 WHERE slot = 1; END"
            )
        )
    with pytest.raises(CasdoorAvatarConflict), s.session.begin():
        s.avatar_owner._scan_initial_dispatch_page(now=NOW)
    assert cursor(s) == before
    with s.session.begin():
        s.session.execute(sa.text("DROP TRIGGER corrupt_cursor"))
    # CAS failure after the real locked read is injected at the SQL boundary.
    engine = s.session.get_bind()
    fired = []

    def race(connection, _cursor, statement, _params, _context, _many):
        if statement.startswith("UPDATE casdoor_avatar_dispatch_cursor_extend") and not fired:
            fired.append(True)
            connection.exec_driver_sql("UPDATE casdoor_avatar_dispatch_cursor_extend SET version = version + 1")

    sa.event.listen(engine, "before_cursor_execute", race)
    try:
        with pytest.raises(CasdoorAvatarConflict), s.session.begin():
            s.avatar_owner._scan_initial_dispatch_page(now=NOW)
    finally:
        sa.event.remove(engine, "before_cursor_execute", race)
    assert fired and cursor(s) == before
    with s.session.begin():
        s.session.execute(sa.text("PRAGMA ignore_check_constraints=ON"))
        s.session.execute(
            sa.text("UPDATE casdoor_avatar_dispatch_cursor_extend SET last_id = :bad"), {"bad": "z" * 10000}
        )
    with pytest.raises(CasdoorAvatarConflict), s.session.begin():
        s.avatar_owner._scan_initial_dispatch_page(now=NOW)
    with s.session.begin():
        assert s.session.scalar(sa.text("SELECT length(last_id) FROM casdoor_avatar_dispatch_cursor_extend")) == 10000


def test_original_candidate_owner_denies_malformed_expired_and_authority_drift(navigation):
    s = navigation
    assert candidate(s, now=NOW + timedelta(seconds=300)) is None
    changes = (
        (Intent, {"desired_json": "{}"}),
        (Intent, {"desired_json": "x" * 17000}),
        (Integration, {"enabled": False}),
        (Revision, {"policy_json": "{}"}),
        (Revision, {"config_digest": "a" * 64}),
        (Identity, {"sync_generation": 2}),
        (Identity, {"profile_sync_json": "{}"}),
        (Namespace, {"fence_epoch": 1}),
        (Account, {"avatar": "local-change"}),
    )
    for model, values in changes:
        with s.session.begin():
            s.session.execute(sa.update(model).values(**values))
            assert s.avatar_owner._initial_dispatch_candidate(s.intent_id, now=NOW) is None
            s.session.rollback()
    with s.session.begin():
        s.session.execute(sa.delete(TenantAccountJoin))
        assert s.avatar_owner._initial_dispatch_candidate(s.intent_id, now=NOW) is None
        s.session.rollback()
    assert candidate(s, uuid4()) is None
    assert candidate(s) == s.intent_id


def test_clean_explicit_root_limits_and_no_unrelated_writes(navigation):
    s = navigation
    engine = s.session.get_bind()
    sql = []

    def observed(_conn, _cursor, statement, _params, _context, _many):
        sql.append(statement)

    sa.event.listen(engine, "before_cursor_execute", observed)
    try:
        for bad in (True, False, 0, -1, 101, 1.0, "1", None):
            with pytest.raises(CasdoorAvatarConflict):
                s.avatar_owner._scan_initial_dispatch_page(limit=bad, now=NOW)
        assert sql == []
        for method in (
            lambda: s.avatar_owner._scan_initial_dispatch_page(now=NOW),
            lambda: s.avatar_owner._initial_dispatch_candidate(s.intent_id, now=NOW),
        ):
            with pytest.raises(CasdoorAvatarConflict):
                method()
            s.session.execute(sa.select(1))
            with pytest.raises(CasdoorAvatarConflict):
                method()
            s.session.rollback()
            with s.session.begin():
                with s.session.begin_nested(), pytest.raises(CasdoorAvatarConflict):
                    method()
                s.account.name = "dirty"
                with pytest.raises(CasdoorAvatarConflict):
                    method()
                s.session.rollback()
        before = snapshot(s)
        sql.clear()
        page(s)
        assert candidate(s) == s.intent_id
        writes = [item for item in sql if item.split()[0].upper() in ("UPDATE", "INSERT", "DELETE", "REPLACE")]
        assert writes and all("casdoor_avatar_dispatch_cursor_extend" in item for item in writes)
        assert snapshot(s) == before
    finally:
        sa.event.remove(engine, "before_cursor_execute", observed)


def test_model_migration_registration_and_portable_ddl(navigation):
    import importlib.util
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).resolve().parents[3]
        / "migrations_extend/versions/2026_10_02_0001-022_casdoor_avatar_dispatch_cursor.py"
    )
    spec = importlib.util.spec_from_file_location("navigation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "022_casdoor_avatar_cursor" and module.down_revision == "021_casdoor_integration"
    assert Base.metadata.tables[Cursor.__tablename__] is Cursor.__table__
    assert set(Cursor.__table__.columns.keys()) == {"slot", "last_id", "sweep_upper_id", "version"}
    for dialect in (postgresql.dialect(), mysql.dialect()):
        ddl = str(sa.schema.CreateTable(Cursor.__table__).compile(dialect=dialect))
        assert "version_range" in ddl and "sweep_shape" in ddl and "singleton_slot" in ddl
        output = StringIO()
        context = MigrationContext.configure(dialect_name=dialect.name, opts={"as_sql": True, "output_buffer": output})
        with Operations.context(context):
            module.upgrade()
            module.downgrade()
        rendered = output.getvalue()
        assert "casdoor_avatar_initial_scan_idx" in rendered
        assert "DROP TABLE casdoor_avatar_dispatch_cursor_extend" in rendered
        assert "INSERT" not in rendered and "DROP TABLE casdoor_sync_intent_extend" not in rendered
    tree = ast.parse(path.read_text())
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
        for node in ast.walk(tree)
    )
