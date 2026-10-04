"""Independent counterexample checks for the I01 storage contract.

These tests exercise boundary and schema invariants from the frozen interface;
they do not exercise configuration, repository, unlink, or remote-sync behavior.
"""

import hashlib
import importlib.util
import io
from pathlib import Path
from uuid import uuid4

import models
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorManagedMembershipExtend,
    CasdoorNamespaceExtend,
    CasdoorSyncIntentExtend,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

API_DIR = Path(__file__).resolve().parents[3]
MIGRATION_PATH = API_DIR / "migrations_extend/versions/2026_09_30_0001-021_casdoor_integration.py"
spec = importlib.util.spec_from_file_location("casdoor_i01_independent_migration", MIGRATION_PATH)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def _id():
    return str(uuid4())


@pytest.fixture
def db_session():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with Session(connection) as session:
            yield session
    engine.dispose()


def _base_rows(session):
    integration = CasdoorIntegrationExtend()
    session.add(integration)
    session.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer="https://idp.invalid",
        organization="Org",
        application="App",
        client_id="Client",
        core_fingerprint="a" * 64,
    )
    session.add(namespace)
    session.flush()
    revision = CasdoorConfigRevisionExtend(
        integration_id=integration.id,
        namespace_id=namespace.id,
        revision_number=1,
        config_digest="b" * 64,
        browser_frontend_url="https://idp.invalid",
        backend_api_url="https://idp.invalid",
        expected_issuer=namespace.expected_issuer,
        organization=namespace.organization,
        application=namespace.application,
        client_id=namespace.client_id,
        button_text="Login",
        default_workspace_id=_id(),
        certificates_json="[]",
        policy_json='{"schema_version":1}',
        mappings_json="[]",
    )
    session.add(revision)
    session.flush()
    return integration, namespace, revision


def _identity(session, namespace, subject, account_id=None):
    identity = CasdoorIdentityExtend(
        namespace_id=namespace.id,
        account_id=account_id or _id(),
        issuer=namespace.expected_issuer,
        organization=namespace.organization,
        subject=subject,
        last_applied_json="{}",
        profile_sync_json="{}",
    )
    session.add(identity)
    session.flush()
    return identity


def test_subject_digest_preserves_exact_case_and_unicode_and_rejects_byte_overflow(db_session):
    _, namespace, _ = _base_rows(db_session)
    values = ("User", "user", "é", "e\u0301")
    identities = [_identity(db_session, namespace, value) for value in values]
    assert [row.subject_digest for row in identities] == [
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in values
    ]
    assert len({row.subject_digest for row in identities}) == len(values)

    boundary = "é" * 127 + "a"  # exactly 255 UTF-8 bytes
    accepted = CasdoorIdentityExtend(subject=boundary)
    assert accepted.subject == boundary
    assert len(accepted.subject.encode("utf-8")) == 255
    assert accepted.subject_digest == hashlib.sha256(boundary.encode("utf-8")).hexdigest()
    with pytest.raises(ValueError):
        CasdoorIdentityExtend(subject=boundary + "b")


def test_identity_uniqueness_is_both_directions_but_namespace_scoped(db_session):
    _, first_ns, _ = _base_rows(db_session)
    first = _identity(db_session, first_ns, "CaseSensitiveSub")
    with pytest.raises(IntegrityError), db_session.begin_nested():
        _identity(db_session, first_ns, "CaseSensitiveSub", _id())
    with pytest.raises(IntegrityError), db_session.begin_nested():
        _identity(db_session, first_ns, "different-sub", first.account_id)

    # The same external subject/account pair may be bound in a new namespace.
    other_ns = CasdoorNamespaceExtend(
        integration_id=first_ns.integration_id,
        expected_issuer=first_ns.expected_issuer,
        organization=first_ns.organization,
        application="ReplacementApp",
        client_id="ReplacementClient",
        core_fingerprint="c" * 64,
    )
    db_session.add(other_ns)
    db_session.flush()
    assert _identity(db_session, other_ns, first.subject, first.account_id).id != first.id


def test_lazy_singleton_and_snapshot_sequence_constraints_survive_direct_sql(db_session):
    assert db_session.query(CasdoorIntegrationExtend).count() == 0
    integration, namespace, _ = _base_rows(db_session)
    assert integration.enabled is False
    assert integration.active_revision_id is None and integration.draft_revision_id is None
    assert integration.etag == 0
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(CasdoorIntegrationExtend())
        db_session.flush()
    revision = db_session.query(CasdoorConfigRevisionExtend).one()
    duplicate = CasdoorConfigRevisionExtend(
        integration_id=revision.integration_id,
        namespace_id=namespace.id,
        revision_number=revision.revision_number,
        config_digest="f" * 64,
        browser_frontend_url=revision.browser_frontend_url,
        backend_api_url=revision.backend_api_url,
        expected_issuer=revision.expected_issuer,
        organization=revision.organization,
        application=revision.application,
        client_id=revision.client_id,
        button_text=revision.button_text,
        default_workspace_id=revision.default_workspace_id,
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(duplicate)
        db_session.flush()


def test_tombstone_and_historical_identity_refs_have_no_identity_fk(db_session):
    _, namespace, revision = _base_rows(db_session)
    identity = _identity(db_session, namespace, "retained-subject")
    membership = CasdoorManagedMembershipExtend(
        namespace_id=namespace.id,
        identity_id=identity.id,
        account_id=identity.account_id,
        workspace_id=revision.default_workspace_id,
        join_id=None,
        ownership="released",
        source="fallback",
        revision_id=revision.id,
        last_applied_roles_json="[]",
        desired_roles_json="[]",
        baseline_json="{}",
        tombstone=True,
    )
    session_intent = CasdoorSyncIntentExtend(
        namespace_id=namespace.id,
        identity_id=identity.id,
        account_id=identity.account_id,
        workspace_id=revision.default_workspace_id,
        revision_id=revision.id,
        generation=3,
        ownership_epoch=4,
        fence_epoch=5,
        kind="member_remove",
        scope_digest="d" * 64,
        idempotency_key="e" * 64,
        desired_json="{}",
    )
    db_session.add_all([membership, session_intent])
    db_session.flush()
    assert membership.join_id is None and membership.tombstone
    assert membership.ownership == "released"
    assert session_intent.identity_id == identity.id
    # The frozen schema intentionally preserves these history references without
    # an identity FK. Lifecycle code must fence and reconcile before deletion.
    assert not any(
        fk.column.table.name == "casdoor_identity_extend"
        for table in (CasdoorManagedMembershipExtend.__table__, CasdoorSyncIntentExtend.__table__)
        for fk in table.foreign_keys
    )


def test_outbox_keeps_generation_epochs_idempotency_and_termination_independent(db_session):
    _, namespace, revision = _base_rows(db_session)
    identity = _identity(db_session, namespace, "outbox-subject")
    intent = CasdoorSyncIntentExtend(
        namespace_id=namespace.id,
        identity_id=identity.id,
        account_id=identity.account_id,
        revision_id=revision.id,
        generation=0,
        ownership_epoch=0,
        fence_epoch=0,
        kind="role_replace",
        scope_digest="1" * 64,
        idempotency_key="2" * 64,
        desired_json="{}",
    )
    db_session.add(intent)
    db_session.flush()
    intent.generation, intent.ownership_epoch, intent.fence_epoch = 8, 9, 10
    intent.operation_state, intent.termination_state = "applied", "unconfirmed"
    intent.sent_at = sa.func.current_timestamp()
    db_session.flush()
    assert (intent.generation, intent.ownership_epoch, intent.fence_epoch) == (8, 9, 10)
    assert intent.operation_state == "applied" and intent.termination_state == "unconfirmed"
    with pytest.raises(IntegrityError), db_session.begin_nested():
        duplicate = CasdoorSyncIntentExtend(
            namespace_id=namespace.id,
            identity_id=identity.id,
            account_id=identity.account_id,
            revision_id=revision.id,
            generation=8,
            ownership_epoch=9,
            fence_epoch=10,
            kind="role_replace",
            scope_digest="3" * 64,
            idempotency_key=intent.idempotency_key,
            desired_json="{}",
        )
        db_session.add(duplicate)
        db_session.flush()


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_migration_offline_ddl_contains_only_new_storage_and_expected_types(dialect):
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.upgrade()
    ddl = output.getvalue().upper()
    assert ddl.count("CREATE TABLE CASDOOR_") == 8
    assert "ALTER TABLE" not in ddl and "INSERT INTO" not in ddl and "UPDATE " not in ddl
    assert "UUID_GENERATE_V4" not in ddl
    assert "CASDOOR_IDENTITY_EXTEND" in ddl and "CASDOOR_SYNC_INTENT_EXTEND" in ddl
    if dialect == "mysql":
        assert "CHARACTER SET ASCII COLLATE ASCII_BIN" in ddl
        assert "MAPPINGS_JSON LONGTEXT" in ddl and "ENCRYPTED_SECRET LONGTEXT" in ddl
    else:
        assert "MAPPINGS_JSON TEXT" in ddl and "ENCRYPTED_SECRET TEXT" in ddl
    assert set(models.db.Model.metadata.tables).issuperset(
        {
            "casdoor_integration_extend",
            "casdoor_namespace_extend",
            "casdoor_config_revision_extend",
            "casdoor_validation_extend",
            "casdoor_identity_extend",
            "casdoor_managed_membership_extend",
            "casdoor_sync_intent_extend",
            "casdoor_audit_extend",
        }
    )


def test_migrated_columns_constraints_and_indexes_match_registered_metadata(db_session):
    inspector = sa.inspect(db_session.connection())
    for table in models.db.Model.metadata.sorted_tables:
        if not table.name.startswith("casdoor_"):
            continue
        assert all(constraint.name is None or len(constraint.name) <= 63 for constraint in table.constraints)
        assert all(len(index.name) <= 63 for index in table.indexes)
        actual_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        assert set(actual_columns) == set(table.columns.keys())
        for column in table.columns:
            assert actual_columns[column.name]["nullable"] is column.nullable
        actual_uniques = {
            tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table.name)
        }
        expected_uniques = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert actual_uniques == expected_uniques
        actual_indexes = {
            (index["name"], tuple(index["column_names"])) for index in inspector.get_indexes(table.name)
        }
        expected_indexes = {
            (index.name, tuple(column.name for column in index.columns)) for index in table.indexes
        }
        assert actual_indexes == expected_indexes
        actual_fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys(table.name)
        }
        expected_fks = {
            (
                tuple(column.name for column in constraint.columns),
                constraint.elements[0].column.table.name,
                constraint.ondelete,
            )
            for constraint in table.constraints
            if isinstance(constraint, sa.ForeignKeyConstraint)
        }
        assert actual_fks == expected_fks


def test_upgrade_and_downgrade_leave_legacy_provider_tables_and_rows_untouched():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE system_integration_extend (classify INTEGER, config TEXT)")
        connection.exec_driver_sql("INSERT INTO system_integration_extend VALUES (4, 'legacy-marker')")
        connection.exec_driver_sql("CREATE TABLE account_integrates (account_id TEXT, provider TEXT)")
        connection.exec_driver_sql("INSERT INTO account_integrates VALUES ('synthetic-id', 'legacy-oauth')")
        before = {
            table: (
                [(column["name"], str(column["type"])) for column in sa.inspect(connection).get_columns(table)],
                connection.exec_driver_sql(f"SELECT * FROM {table}").all(),
            )
            for table in ("system_integration_extend", "account_integrates")
        }
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
        for table, (columns_before, rows_before) in before.items():
            columns_after = [
                (column["name"], str(column["type"])) for column in sa.inspect(connection).get_columns(table)
            ]
            assert columns_after == columns_before
            assert connection.exec_driver_sql(f"SELECT * FROM {table}").all() == rows_before
    engine.dispose()
