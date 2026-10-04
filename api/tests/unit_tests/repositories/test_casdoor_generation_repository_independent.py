"""Independent SQLite checks for I11-A local generation CAS.

SQLite validates persistence and rollback shape, not multi-client lock behavior
for PostgreSQL/MySQL. A captured SQL sequence documents flush ordering too.
"""

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.claims import StructuredUserRef
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.mapping import (
    BuiltinResolution,
    MappingIdentityContext,
    ServerWorkspaceAvailability,
    WorkspaceAvailability,
    WorkspaceState,
    resolve_workspace_plan,
)
from core.casdoor.role_graph import EffectiveRoleSnapshot
from models.account import Account, AccountStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
)
from repositories.casdoor_generation_repository_extend import (
    MAX_GENERATION,
    CasdoorGenerationConflict,
    CasdoorGenerationRepository,
)
from sqlalchemy.orm import Session


@pytest.fixture
def persisted_plan():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")

    for table in (
        Account.__table__,
        CasdoorIntegrationExtend.__table__,
        CasdoorNamespaceExtend.__table__,
        CasdoorConfigRevisionExtend.__table__,
        CasdoorIdentityExtend.__table__,
    ):
        table.create(engine)

    session = Session(engine)
    config = CasdoorConfiguration(
        browser_frontend_url="https://synthetic.invalid",
        backend_api_url="https://synthetic.invalid",
        expected_issuer="https://issuer.synthetic.invalid",
        organization="IndependentOrg",
        application="IndependentApp",
        client_id="IndependentClient",
        default_workspace_id=UUID(int=7001),
    )
    integration = CasdoorIntegrationExtend(enabled=True)
    account = Account(name="Existing account", email="bound@example.invalid", status=AccountStatus.PENDING)
    session.add_all((integration, account))
    session.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer=config.expected_issuer,
        organization=config.organization,
        application=config.application,
        client_id=config.client_id,
        core_fingerprint="c" * 64,
    )
    session.add(namespace)
    session.flush()
    revision = CasdoorConfigRevisionExtend(
        integration_id=integration.id,
        namespace_id=namespace.id,
        revision_number=1,
        config_digest="d" * 64,
        browser_frontend_url=config.browser_frontend_url,
        backend_api_url=config.backend_api_url,
        expected_issuer=config.expected_issuer,
        organization=config.organization,
        application=config.application,
        client_id=config.client_id,
        button_text="Synthetic SSO",
        default_workspace_id=str(config.default_workspace_id),
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    identity = CasdoorIdentityExtend(
        namespace_id=namespace.id,
        account_id=account.id,
        issuer=config.expected_issuer,
        organization=config.organization,
        subject="Verbatim-Subject/β",
        last_applied_json="{}",
        profile_sync_json="{}",
    )
    session.add_all((revision, identity))
    session.flush()
    integration.active_revision_id = revision.id
    context = MappingIdentityContext(
        integration_id=UUID(integration.id),
        revision_id=UUID(revision.id),
        namespace_id=UUID(namespace.id),
        identity_id=UUID(identity.id),
        account_id=UUID(account.id),
        config_digest=revision.config_digest,
        issuer=config.expected_issuer,
        organization=config.organization,
        application=config.application,
        client_id=config.client_id,
        subject=identity.subject,
    )
    plan = resolve_workspace_plan(
        configuration=config,
        snapshot=EffectiveRoleSnapshot(identity.subject, StructuredUserRef(config.organization, "online-user"), ()),
        context=context,
        availability=ServerWorkspaceAvailability(
            (
                WorkspaceAvailability(
                    config.default_workspace_id,
                    WorkspaceState.NORMAL,
                    (BuiltinResolution("normal", "synthetic-normal"),),
                ),
            )
        ),
    )
    session.commit()
    yield session, plan, engine
    session.close()
    engine.dispose()


def _allocate(session, plan, generation=0, epoch=0):
    return CasdoorGenerationRepository(session).allocate(
        plan, expected_fence_epoch=epoch, expected_generation=generation
    )


def test_sqlite_cas_changes_generation_only_and_outer_rollback_restores_caller_state(persisted_plan):
    session, plan, _engine = persisted_plan
    session.begin()
    version = _allocate(session, plan)
    account = session.get(Account, str(plan.context.account_id))
    account.name = "transaction-local edit"
    assert version.generation == 1
    assert version.fence_epoch == 0
    assert (
        session.scalar(
            sa.select(CasdoorIdentityExtend.subject_digest).where(
                CasdoorIdentityExtend.id == str(plan.context.identity_id)
            )
        )
        == hashlib.sha256(plan.context.subject.encode()).hexdigest()
    )
    session.rollback()
    assert (
        session.scalar(
            sa.select(CasdoorIdentityExtend.sync_generation).where(
                CasdoorIdentityExtend.id == str(plan.context.identity_id)
            )
        )
        == 0
    )
    assert session.get(Account, str(plan.context.account_id)).name == "Existing account"


def test_independent_replay_is_rejected_and_draft_pointer_is_irrelevant(persisted_plan):
    session, plan, _engine = persisted_plan
    with session.begin():
        integration = session.get(CasdoorIntegrationExtend, str(plan.context.integration_id))
        integration.draft_revision_id = str(uuid4())
        integration.etag = 31
    with session.begin():
        assert _allocate(session, plan).generation == 1
    with session.begin():
        with pytest.raises(CasdoorGenerationConflict):
            _allocate(session, plan)
        assert (
            session.scalar(
                sa.select(CasdoorIdentityExtend.sync_generation).where(
                    CasdoorIdentityExtend.id == str(plan.context.identity_id)
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "field", ("subject", "namespace_id", "account_id", "identity_id", "revision_id", "config_digest")
)
def test_replayed_plan_cannot_cross_raw_identity_or_revision_owner_boundary(persisted_plan, field):
    session, plan, _engine = persisted_plan
    value = "verbatim-subject/β" if field == "subject" else "e" * 64 if field == "config_digest" else uuid4()
    session.begin()
    with pytest.raises(CasdoorGenerationConflict):
        _allocate(session, replace(plan, context=replace(plan.context, **{field: value})))
    assert (
        session.scalar(
            sa.select(CasdoorIdentityExtend.sync_generation).where(
                CasdoorIdentityExtend.id == str(plan.context.identity_id)
            )
        )
        == 0
    )
    session.rollback()


def test_disabled_integration_and_namespace_fence_are_checked_from_current_database(persisted_plan):
    session, plan, _engine = persisted_plan
    session.begin()
    session.execute(
        sa.update(CasdoorIntegrationExtend.__table__)
        .where(CasdoorIntegrationExtend.id == str(plan.context.integration_id))
        .values(enabled=False)
    )
    with pytest.raises(CasdoorGenerationConflict):
        _allocate(session, plan)
    session.rollback()

    session.begin()
    session.execute(
        sa.update(CasdoorNamespaceExtend.__table__)
        .where(CasdoorNamespaceExtend.id == str(plan.context.namespace_id))
        .values(fence_epoch=1)
    )
    with pytest.raises(CasdoorGenerationConflict):
        _allocate(session, plan)
    session.rollback()


@pytest.mark.parametrize("generation", (-1, True, MAX_GENERATION, MAX_GENERATION + 1))
def test_generation_rejects_invalid_expected_values_and_signed_bigint_overflow(persisted_plan, generation):
    session, plan, _engine = persisted_plan
    session.begin()
    with pytest.raises(CasdoorGenerationConflict):
        _allocate(session, plan, generation=generation)
    assert (
        session.scalar(
            sa.select(CasdoorIdentityExtend.sync_generation).where(
                CasdoorIdentityExtend.id == str(plan.context.identity_id)
            )
        )
        == 0
    )
    session.rollback()


@pytest.mark.parametrize(
    "pending_kind",
    (
        "dirty-account",
        "dirty-integration",
        "dirty-namespace",
        "dirty-identity",
        "new-account",
        "new-identity",
        "deleted-account",
    ),
)
def test_pending_orm_work_is_rejected_without_sql_and_remains_pending(persisted_plan, pending_kind):
    session, plan, engine = persisted_plan
    statements = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    session.begin()
    if pending_kind == "dirty-account":
        session.get(Account, str(plan.context.account_id)).name = "pending account edit"
    elif pending_kind == "dirty-integration":
        session.get(CasdoorIntegrationExtend, str(plan.context.integration_id)).enabled = False
    elif pending_kind == "dirty-namespace":
        session.get(CasdoorNamespaceExtend, str(plan.context.namespace_id)).fence_epoch = 1
    elif pending_kind == "dirty-identity":
        session.get(CasdoorIdentityExtend, str(plan.context.identity_id)).remote_email = "pending@example.invalid"
    elif pending_kind == "new-account":
        session.add(Account(name="Unflushed account", email="new@example.invalid"))
    elif pending_kind == "new-identity":
        session.add(
            CasdoorIdentityExtend(
                namespace_id=str(plan.context.namespace_id),
                account_id=str(uuid4()),
                issuer=plan.context.issuer,
                organization=plan.context.organization,
                subject="unflushed-subject",
                last_applied_json="{}",
                profile_sync_json="{}",
            )
        )
    else:
        session.delete(session.get(Account, str(plan.context.account_id)))

    # Do not count reads used to prepare the pending state; capture only allocate.
    statements.clear()
    with pytest.raises(CasdoorGenerationConflict):
        _allocate(session, plan)
    assert statements == []
    if pending_kind == "new-identity":
        assert any(item.subject == "unflushed-subject" for item in session.new)
    elif pending_kind == "new-account":
        assert any(item.name == "Unflushed account" for item in session.new)
    elif pending_kind == "deleted-account":
        assert any(item.id == str(plan.context.account_id) for item in session.deleted)
    else:
        assert session.dirty
    session.rollback()


def test_clean_session_locks_in_order_then_executes_only_identity_cas_dml(persisted_plan):
    session, plan, engine = persisted_plan
    statements = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    session.begin()
    _allocate(session, plan)

    table_selects = {
        table: next(
            index
            for index, sql in enumerate(statements)
            if sql.lstrip().startswith("select") and f"from {table}" in sql
        )
        for table in (
            "casdoor_integration_extend",
            "casdoor_namespace_extend",
            "accounts",
            "casdoor_identity_extend",
        )
    }
    assert [table for table, _ in sorted(table_selects.items(), key=lambda item: item[1])] == [
        "casdoor_integration_extend",
        "casdoor_namespace_extend",
        "accounts",
        "casdoor_identity_extend",
    ]
    updates = [(index, sql) for index, sql in enumerate(statements) if sql.lstrip().startswith("update")]
    assert len(updates) == 1
    update_index, update_sql = updates[0]
    assert update_index == len(statements) - 1
    assert update_sql.startswith("update casdoor_identity_extend")
    for field in (
        "id",
        "namespace_id",
        "account_id",
        "issuer",
        "organization",
        "subject",
        "subject_digest",
        "sync_generation",
    ):
        assert field in update_sql
    assert "sync_generation" in update_sql
    session.rollback()


def test_repository_requires_an_explicit_caller_transaction(persisted_plan):
    session, plan, _engine = persisted_plan
    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        _allocate(session, plan)
    assert not session.in_transaction()
