"""Offline storage contracts; SQLite constraint tests are not PG/MySQL acceptance."""

import importlib.util
import io
from pathlib import Path
from uuid import UUID, uuid4

import models
import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
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
spec = importlib.util.spec_from_file_location("casdoor_storage_migration", MIGRATION_PATH)
assert spec is not None
assert spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
TABLES = [table for table in models.db.Model.metadata.sorted_tables if table.name.startswith("casdoor_")]


def add_and_flush(storage, row):
    storage.add(row)
    storage.flush()


def modify_revision(storage, revision):
    revision.policy_json = '{"changed":true}'
    storage.flush()


def uid() -> str:
    return str(uuid4())


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.commit()
        with Session(connection) as session:
            yield session
    engine.dispose()


def make_namespace(storage: Session, integration: CasdoorIntegrationExtend | None = None):
    integration = integration or CasdoorIntegrationExtend()
    if integration.id is None:
        storage.add(integration)
        storage.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer="https://synthetic-idp.example",
        organization="SyntheticOrg",
        application="SyntheticApp",
        client_id="SyntheticClient",
        core_fingerprint="a" * 64,
    )
    storage.add(namespace)
    storage.flush()
    return namespace


def make_revision(storage: Session, namespace: CasdoorNamespaceExtend):
    revision = CasdoorConfigRevisionExtend(
        integration_id=namespace.integration_id,
        namespace_id=namespace.id,
        revision_number=1,
        config_digest="b" * 64,
        browser_frontend_url="https://synthetic-idp.example",
        backend_api_url="https://synthetic-idp.example",
        expected_issuer=namespace.expected_issuer,
        organization=namespace.organization,
        application=namespace.application,
        client_id=namespace.client_id,
        button_text="Synthetic SSO",
        default_workspace_id=uid(),
        certificates_json="[]",
        policy_json='{"schema_version":1}',
        mappings_json="[]",
    )
    storage.add(revision)
    storage.flush()
    return revision


def make_identity(storage: Session, namespace: CasdoorNamespaceExtend, subject="SyntheticSubject", account_id=None):
    identity = CasdoorIdentityExtend(
        namespace_id=namespace.id,
        account_id=account_id or uid(),
        issuer=namespace.expected_issuer,
        organization=namespace.organization,
        subject=subject,
        last_applied_json="{}",
        profile_sync_json="{}",
    )
    storage.add(identity)
    storage.flush()
    return identity


def make_membership(storage: Session, namespace, revision, identity):
    membership = CasdoorManagedMembershipExtend(
        namespace_id=namespace.id,
        identity_id=identity.id,
        account_id=identity.account_id,
        workspace_id=revision.default_workspace_id,
        join_id=uid(),
        ownership="managed",
        source="fallback",
        revision_id=revision.id,
        last_applied_roles_json="[]",
        desired_roles_json='["normal"]',
        baseline_json="{}",
    )
    storage.add(membership)
    storage.flush()
    return membership


def make_intent(storage: Session, namespace, revision, identity, membership):
    intent = CasdoorSyncIntentExtend(
        namespace_id=namespace.id,
        identity_id=identity.id,
        account_id=identity.account_id,
        workspace_id=membership.workspace_id,
        membership_id=membership.id,
        revision_id=revision.id,
        generation=0,
        ownership_epoch=0,
        fence_epoch=0,
        kind="role_replace",
        scope_digest="c" * 64,
        idempotency_key="d" * 64,
        desired_json='["normal"]',
    )
    storage.add(intent)
    storage.flush()
    return intent


def test_registration_lazy_singleton_and_client_uuid_defaults(storage):
    assert len(TABLES) == 8
    assert storage.query(CasdoorIntegrationExtend).count() == 0
    integration = CasdoorIntegrationExtend()
    storage.add(integration)
    storage.flush()
    assert UUID(integration.id).version == 4
    assert integration.enabled is False
    assert integration.etag == 0
    assert integration.active_revision_id is None
    with pytest.raises(IntegrityError), storage.begin_nested():
        add_and_flush(storage, CasdoorIntegrationExtend())
    with pytest.raises(IntegrityError), storage.begin_nested():
        add_and_flush(storage, CasdoorIntegrationExtend(slot=2))


def test_identity_keys_are_bidirectional_case_sensitive_and_namespace_scoped(storage):
    namespace = make_namespace(storage)
    identity = make_identity(storage, namespace)
    case_distinct = make_identity(storage, namespace, subject="syntheticsubject")
    assert identity.subject_digest != case_distinct.subject_digest
    with pytest.raises(IntegrityError), storage.begin_nested():
        make_identity(storage, namespace, account_id=identity.account_id, subject="DifferentSubject")
    with pytest.raises(IntegrityError), storage.begin_nested():
        make_identity(storage, namespace, subject=identity.subject)
    integration = storage.get(CasdoorIntegrationExtend, namespace.integration_id)
    other_namespace = make_namespace(storage, integration)
    other = make_identity(storage, other_namespace, subject=identity.subject, account_id=identity.account_id)
    assert other.id != identity.id
    assert identity.email_verified is None


def test_subject_limit_is_utf8_bytes_and_digest_cannot_be_overridden():
    assert CasdoorIdentityExtend(subject="中" * 85).subject == "中" * 85
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        CasdoorIdentityExtend(subject="中" * 86)
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        CasdoorIdentityExtend(subject="")
    identity = CasdoorIdentityExtend(subject="SyntheticSubject")
    with pytest.raises(ValueError, match="exact subject"):
        identity.subject_digest = "0" * 64
    with pytest.raises(ValueError, match="immutable"):
        identity.subject = "ChangedSubject"


def test_revision_sequence_schema_and_snapshot_guard(storage):
    namespace = make_namespace(storage)
    revision = make_revision(storage, namespace)
    with pytest.raises(IntegrityError), storage.begin_nested():
        make_revision(storage, namespace)
    with pytest.raises(IntegrityError), storage.begin_nested():
        storage.execute(sa.update(CasdoorConfigRevisionExtend.__table__).values(revision_number=0))
    with pytest.raises(IntegrityError), storage.begin_nested():
        storage.execute(sa.update(CasdoorConfigRevisionExtend.__table__).values(schema_version=2))
    with pytest.raises(ValueError, match="immutable"), storage.begin_nested():
        modify_revision(storage, revision)


def test_outbox_idempotency_and_separate_completion_axes(storage):
    namespace = make_namespace(storage)
    revision = make_revision(storage, namespace)
    identity = make_identity(storage, namespace)
    membership = make_membership(storage, namespace, revision, identity)
    intent = make_intent(storage, namespace, revision, identity, membership)
    assert intent.operation_state == "pending"
    assert intent.termination_state == "not_started"
    assert intent.attempt_id is None
    assert intent.sent_at is None
    assert membership.finalization == "pending"
    with pytest.raises(IntegrityError), storage.begin_nested():
        make_intent(storage, namespace, revision, identity, membership)
    intent.operation_state = "applied"
    intent.termination_state = "unconfirmed"
    storage.flush()
    # Desired state readback can be recorded without claiming request termination.
    storage.expire_all()
    assert storage.get(CasdoorSyncIntentExtend, intent.id).termination_state == "unconfirmed"
    assert storage.get(CasdoorManagedMembershipExtend, membership.id).finalization == "pending"


@pytest.mark.parametrize("column", ["generation", "ownership_epoch", "fence_epoch", "attempt_count"])
def test_negative_outbox_counters_rejected_by_database(storage, column):
    namespace = make_namespace(storage)
    revision = make_revision(storage, namespace)
    identity = make_identity(storage, namespace)
    membership = make_membership(storage, namespace, revision, identity)
    make_intent(storage, namespace, revision, identity, membership)
    with pytest.raises(IntegrityError), storage.begin_nested():
        storage.execute(sa.update(CasdoorSyncIntentExtend.__table__).values({column: -1}))


def test_database_enum_check_and_historical_reference_survival(storage):
    namespace = make_namespace(storage)
    revision = make_revision(storage, namespace)
    identity = make_identity(storage, namespace)
    membership = make_membership(storage, namespace, revision, identity)
    intent = make_intent(storage, namespace, revision, identity, membership)
    with pytest.raises(IntegrityError), storage.begin_nested():
        storage.connection().exec_driver_sql("UPDATE casdoor_managed_membership_extend SET ownership = 'owner'")
    membership.join_id = None
    membership.tombstone = True
    membership.ownership = "released"
    intent.operation_state = "cancelled"
    intent.termination_state = "confirmed"
    intent.termination_proof_kind = "local_commit"
    storage.flush()
    historical_id = identity.id
    storage.delete(identity)
    storage.flush()
    assert storage.get(CasdoorManagedMembershipExtend, membership.id).identity_id == historical_id
    assert storage.get(CasdoorSyncIntentExtend, intent.id).identity_id == historical_id
    # Domain owner must fence/reconcile first; this test only proves history can survive deletion.


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_explicit_migration_offline_ddl_has_no_old_table_or_seed_operations(dialect):
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        migration.upgrade()
    ddl = buffer.getvalue()
    assert ddl.count("CREATE TABLE casdoor_") == 8
    assert not any(keyword in ddl for keyword in ("ALTER TABLE", "INSERT INTO", "UPDATE ", "uuid_generate_v4"))
    assert "ON DELETE RESTRICT" in ddl
    assert "identity_id_fkey" not in ddl
    if dialect == "mysql":
        assert "CHARACTER SET ascii COLLATE ascii_bin" in ddl
        assert "id CHAR(36)" in ddl
        assert "mappings_json LONGTEXT" in ddl
        assert "encrypted_secret LONGTEXT" in ddl
    else:
        assert "id UUID" in ddl
        assert "mappings_json TEXT" in ddl
        assert "encrypted_secret TEXT" in ddl
    for table in TABLES:
        for constraint in table.constraints:
            assert constraint.name is None or len(constraint.name) <= 63
        for index in table.indexes:
            assert len(index.name) <= 63


def test_upgrade_schema_matches_registered_models_and_downgrade_removes_only_new_tables(storage):
    connection = storage.connection()
    inspector = sa.inspect(connection)
    assert set(inspector.get_table_names()) == {table.name for table in TABLES}
    for table in TABLES:
        assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
        actual_uniques = {
            tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table.name)
        }
        expected_uniques = {
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert actual_uniques == expected_uniques
        reflected = {column["name"]: column for column in inspector.get_columns(table.name)}
        for column in table.columns:
            assert reflected[column.name]["nullable"] == column.nullable
        actual_checks = {constraint["sqltext"] for constraint in inspector.get_check_constraints(table.name)}
        expected_checks = {
            str(constraint.sqltext) for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)
        }
        assert actual_checks == expected_checks
        actual_indexes = {(index["name"], tuple(index["column_names"])) for index in inspector.get_indexes(table.name)}
        expected_indexes = {(index.name, tuple(column.name for column in index.columns)) for index in table.indexes}
        assert actual_indexes == expected_indexes
        actual_fks = {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                fk["options"].get("ondelete"),
            )
            for fk in inspector.get_foreign_keys(table.name)
        }
        expected_fks = {
            (
                tuple(column.name for column in constraint.columns),
                constraint.elements[0].column.table.name,
                tuple(element.column.name for element in constraint.elements),
                constraint.ondelete,
            )
            for constraint in table.constraints
            if isinstance(constraint, sa.ForeignKeyConstraint)
        }
        assert actual_fks == expected_fks
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
    assert sa.inspect(connection).get_table_names() == []


def test_extension_revision_chain_has_one_head():
    config = Config()
    config.set_main_option("script_location", str(API_DIR / "migrations_extend"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["021_casdoor_integration"]
    assert scripts.get_revision("021_casdoor_integration").down_revision == "020_workflow_run_account"


def test_migration_preserves_existing_provider_schema_and_data():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE system_integration_extend (classify INTEGER, config TEXT)")
        connection.exec_driver_sql("INSERT INTO system_integration_extend VALUES (4, 'legacy-sentinel')")
        connection.exec_driver_sql("CREATE TABLE account_integrates (account_id TEXT, provider TEXT)")
        connection.exec_driver_sql("INSERT INTO account_integrates VALUES ('synthetic-account', 'legacy-oauth2')")
        before = {
            name: (sa.inspect(connection).get_columns(name), connection.exec_driver_sql(f"SELECT * FROM {name}").all())
            for name in ("system_integration_extend", "account_integrates")
        }
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
        for name, (columns, rows) in before.items():
            after_columns = sa.inspect(connection).get_columns(name)
            assert [(column["name"], str(column["type"])) for column in after_columns] == [
                (column["name"], str(column["type"])) for column in columns
            ]
            assert connection.exec_driver_sql(f"SELECT * FROM {name}").all() == rows
    engine.dispose()
