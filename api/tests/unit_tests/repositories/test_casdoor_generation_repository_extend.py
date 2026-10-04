"""Real SQLite rollback/CAS plus offline SQL shape, not DB concurrency proof."""

from dataclasses import FrozenInstanceError, replace
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
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import Session


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def configure(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (
        Account,
        CasdoorIntegrationExtend,
        CasdoorNamespaceExtend,
        CasdoorConfigRevisionExtend,
        CasdoorIdentityExtend,
    ):
        model.__table__.create(engine)
    with Session(engine) as session:
        config = CasdoorConfiguration(
            browser_frontend_url="https://synthetic.example.test",
            backend_api_url="https://synthetic.example.test",
            expected_issuer="https://synthetic.example.test",
            organization="Org",
            application="App",
            client_id="Client",
            default_workspace_id=uuid4(),
        )
        integration = CasdoorIntegrationExtend(enabled=True)
        account = Account(name="Existing", email="existing@example.test", status=AccountStatus.PENDING)
        other_account = Account(name="Other", email="other@example.test")
        session.add_all([integration, account, other_account])
        session.flush()
        namespace = CasdoorNamespaceExtend(
            integration_id=integration.id,
            expected_issuer=config.expected_issuer,
            organization=config.organization,
            application=config.application,
            client_id=config.client_id,
            core_fingerprint="b" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = CasdoorConfigRevisionExtend(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url=config.browser_frontend_url,
            backend_api_url=config.backend_api_url,
            expected_issuer=config.expected_issuer,
            organization=config.organization,
            application=config.application,
            client_id=config.client_id,
            button_text="Casdoor",
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
            subject="CaseSubject",
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        session.add_all([revision, identity])
        session.flush()
        integration.active_revision_id = revision.id
        context = MappingIdentityContext(
            UUID(integration.id),
            UUID(revision.id),
            UUID(namespace.id),
            UUID(identity.id),
            UUID(account.id),
            "a" * 64,
            config.expected_issuer,
            config.organization,
            config.application,
            config.client_id,
            "CaseSubject",
        )
        plan = resolve_workspace_plan(
            configuration=config,
            snapshot=EffectiveRoleSnapshot("CaseSubject", StructuredUserRef("Org", "u"), ()),
            context=context,
            availability=ServerWorkspaceAvailability(
                (
                    WorkspaceAvailability(
                        config.default_workspace_id,
                        WorkspaceState.NORMAL,
                        (BuiltinResolution("normal", "local-normal"),),
                    ),
                )
            ),
        )
        other_id = UUID(other_account.id)
        session.commit()
        yield session, plan, other_id
    engine.dispose()


def allocate(storage, *, plan=None, generation=0, epoch=0):
    session, original, _ = storage
    return CasdoorGenerationRepository(session).allocate(
        original if plan is None else plan,
        expected_fence_epoch=epoch,
        expected_generation=generation,
    )


def current(storage):
    session, plan, _ = storage
    return session.scalar(
        sa.select(CasdoorIdentityExtend.sync_generation).where(
            CasdoorIdentityExtend.id == str(plan.context.identity_id)
        )
    )


def test_increments_only_generation_and_freezes_plan_version(storage):
    session, plan, _ = storage
    with session.begin():
        first = allocate(storage)
        assert first.plan is plan
        assert first.identity_id == plan.context.identity_id
        assert first.generation == 1 and first.fence_epoch == 0
        assert allocate(storage, generation=1).generation == 2
        account = session.get(Account, str(plan.context.account_id))
        assert account.status == AccountStatus.PENDING
        assert account.email == "existing@example.test"
        assert account.initialized_at is None
        identity = session.get(CasdoorIdentityExtend, str(plan.context.identity_id))
        assert identity.subject == "CaseSubject"
        assert identity.last_applied_json == identity.profile_sync_json == "{}"
        with pytest.raises(FrozenInstanceError):
            first.generation = 10
    assert current(storage) == 2


def test_outer_rollback_undoes_generation_and_caller_change(storage):
    session, plan, _ = storage
    session.begin()
    account = session.get(Account, str(plan.context.account_id))
    assert allocate(storage).generation == 1
    account.name = "Pending caller update"
    session.flush()
    session.rollback()
    assert current(storage) == 0
    assert session.get(Account, str(plan.context.account_id)).name == "Existing"


def test_stale_generation_and_replayed_plan_cannot_overwrite_new(storage):
    session, _, _ = storage
    with session.begin():
        allocate(storage)
    session.begin()
    with pytest.raises(CasdoorGenerationConflict, match="^config_conflict$"):
        allocate(storage)
    assert session.is_active
    assert current(storage) == 1


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (CasdoorIntegrationExtend, "enabled", False),
        (CasdoorIntegrationExtend, "active_revision_id", None),
        (CasdoorIntegrationExtend, "active_revision_id", str(uuid4())),
        (CasdoorNamespaceExtend, "lifecycle", "fencing"),
        (CasdoorNamespaceExtend, "lifecycle", "archived"),
        (CasdoorNamespaceExtend, "fence_epoch", 1),
        (CasdoorNamespaceExtend, "organization", "org"),
        (CasdoorNamespaceExtend, "expected_issuer", "https://wrong.example.test"),
        (CasdoorNamespaceExtend, "application", "WrongApp"),
        (CasdoorNamespaceExtend, "client_id", "WrongClient"),
        (CasdoorConfigRevisionExtend, "config_digest", "c" * 64),
        (CasdoorConfigRevisionExtend, "application", "WrongApp"),
        (CasdoorConfigRevisionExtend, "client_id", "WrongClient"),
        (CasdoorIdentityExtend, "subject", "CollisionSubject"),
        (CasdoorIdentityExtend, "issuer", "https://wrong.test"),
        (CasdoorIdentityExtend, "organization", "org"),
        (CasdoorIdentityExtend, "subject_digest", "c" * 64),
    ],
)
def test_fresh_database_guards_reject_despite_cached_models(storage, model, field, value):
    session, plan, _ = storage
    ids = {
        CasdoorIntegrationExtend: plan.context.integration_id,
        CasdoorNamespaceExtend: plan.context.namespace_id,
        CasdoorConfigRevisionExtend: plan.context.revision_id,
        CasdoorIdentityExtend: plan.context.identity_id,
    }
    cached = session.get(model, str(ids[model]))
    before = getattr(cached, field)
    session.execute(sa.update(model.__table__).where(model.id == str(ids[model])).values(**{field: value}))
    assert getattr(cached, field) == before
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage)
    assert current(storage) == 0


@pytest.mark.parametrize("field", ["integration_id", "revision_id", "namespace_id", "identity_id", "account_id"])
def test_exact_owner_scope_rejects_other_or_missing_ids(storage, field):
    session, plan, other_account = storage
    changed = other_account if field == "account_id" else uuid4()
    session.begin()
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage, plan=replace(plan, context=replace(plan.context, **{field: changed})))
    assert current(storage) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Account)) == 2


def test_changed_exact_subject_does_not_match_digest_or_casefold(storage):
    session, plan, _ = storage
    session.begin()
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage, plan=replace(plan, context=replace(plan.context, subject="casesubject")))
    assert current(storage) == 0


def test_actual_cross_namespace_revision_and_identity_scopes_reject(storage):
    session, plan, _ = storage
    session.begin()
    namespace = CasdoorNamespaceExtend(
        integration_id=str(plan.context.integration_id),
        expected_issuer=plan.context.issuer,
        organization=plan.context.organization,
        application=plan.context.application,
        client_id=plan.context.client_id,
        core_fingerprint="d" * 64,
    )
    session.add(namespace)
    session.flush()
    revision_id = str(plan.context.revision_id)
    session.execute(
        sa.update(CasdoorConfigRevisionExtend.__table__)
        .where(CasdoorConfigRevisionExtend.id == revision_id)
        .values(namespace_id=namespace.id)
    )
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage)
    session.execute(
        sa.update(CasdoorConfigRevisionExtend.__table__)
        .where(CasdoorConfigRevisionExtend.id == revision_id)
        .values(namespace_id=str(plan.context.namespace_id))
    )
    session.execute(
        sa.update(CasdoorIdentityExtend.__table__)
        .where(CasdoorIdentityExtend.id == str(plan.context.identity_id))
        .values(namespace_id=namespace.id)
    )
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage)
    assert current(storage) == 0


def test_identity_account_mismatch_is_not_reassigned(storage):
    session, plan, other = storage
    session.begin()
    session.execute(
        sa.update(CasdoorIdentityExtend.__table__)
        .where(CasdoorIdentityExtend.id == str(plan.context.identity_id))
        .values(account_id=str(other))
    )
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage)
    assert current(storage) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "https://wrong.test"),
        ("organization", "org"),
        ("application", "app"),
        ("client_id", "client"),
        ("config_digest", "c" * 64),
        ("subject", "\ud800"),
    ],
)
def test_complete_context_tuple_is_rechecked_against_current_storage(storage, field, value):
    session, plan, _ = storage
    session.begin()
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage, plan=replace(plan, context=replace(plan.context, **{field: value})))
    assert current(storage) == 0


@pytest.mark.parametrize(
    "pending",
    [
        "dirty_account",
        "dirty_integration",
        "dirty_namespace",
        "dirty_identity",
        "new_account",
        "new_identity",
        "deleted_account",
    ],
)
def test_pending_caller_work_is_rejected_without_any_sql(storage, pending):
    session, plan, _ = storage
    session.begin()
    if pending == "dirty_account":
        session.get(Account, str(plan.context.account_id)).name = "Unflushed"
    elif pending == "dirty_integration":
        session.get(CasdoorIntegrationExtend, str(plan.context.integration_id)).enabled = False
    elif pending == "dirty_namespace":
        session.get(CasdoorNamespaceExtend, str(plan.context.namespace_id)).fence_epoch = 1
    elif pending == "dirty_identity":
        session.get(CasdoorIdentityExtend, str(plan.context.identity_id)).remote_email = "changed@example.test"
    elif pending == "new_account":
        session.add(Account(name="New", email="new@example.test"))
    elif pending == "new_identity":
        session.add(
            CasdoorIdentityExtend(
                namespace_id=str(plan.context.namespace_id),
                account_id=str(storage[2]),
                issuer=plan.context.issuer,
                organization=plan.context.organization,
                subject="NewSubject",
                last_applied_json="{}",
                profile_sync_json="{}",
            )
        )
    else:
        session.delete(session.get(Account, str(plan.context.account_id)))
    before = (tuple(session.new), tuple(session.dirty), tuple(session.deleted))
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    sa.event.listen(session.bind, "before_cursor_execute", capture)
    try:
        with pytest.raises(CasdoorGenerationConflict, match="^config_conflict$"):
            allocate(storage)
        assert statements == []
        assert (tuple(session.new), tuple(session.dirty), tuple(session.deleted)) == before
        assert session.is_active and session.in_transaction()
    finally:
        sa.event.remove(session.bind, "before_cursor_execute", capture)
    session.rollback()
    assert current(storage) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(Account)) == 2


def test_draft_save_and_etag_do_not_invalidate_unchanged_active(storage):
    session, plan, _ = storage
    with session.begin():
        # Simulate an already-persisted draft change, leaving a clean Session.
        session.execute(
            sa.update(CasdoorIntegrationExtend.__table__)
            .where(CasdoorIntegrationExtend.id == str(plan.context.integration_id))
            .values(draft_revision_id=str(uuid4()), etag=7)
        )
        assert allocate(storage).generation == 1


@pytest.mark.parametrize("generation", [-1, True, 1.0, MAX_GENERATION, MAX_GENERATION + 1])
def test_invalid_or_overflow_expected_generation_does_not_write(storage, generation):
    session, _, _ = storage
    session.begin()
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage, generation=generation)
    assert current(storage) == 0


@pytest.mark.parametrize("epoch", [-1, True, 1.0, MAX_GENERATION + 1])
def test_invalid_fence_epoch_does_not_write(storage, epoch):
    session, _, _ = storage
    session.begin()
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage, epoch=epoch)
    assert current(storage) == 0


def test_bigint_last_safe_increment_then_reject_overflow(storage):
    session, plan, _ = storage
    with session.begin():
        session.execute(
            sa.update(CasdoorIdentityExtend.__table__)
            .where(CasdoorIdentityExtend.id == str(plan.context.identity_id))
            .values(sync_generation=MAX_GENERATION - 1)
        )
        assert allocate(storage, generation=MAX_GENERATION - 1).generation == MAX_GENERATION
        with pytest.raises(CasdoorGenerationConflict):
            allocate(storage, generation=MAX_GENERATION)
        assert current(storage) == MAX_GENERATION


def test_sql_cas_rowcount_loser_is_not_reported_success(storage, monkeypatch):
    session, plan, _ = storage
    session.begin()
    original = session.execute
    changed = False

    def competing_write(statement, *args, **kwargs):
        nonlocal changed
        if isinstance(statement, sa.sql.dml.Update) and not changed:
            changed = True
            original(
                sa.update(CasdoorIdentityExtend.__table__)
                .where(CasdoorIdentityExtend.id == str(plan.context.identity_id))
                .values(sync_generation=1)
            )
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", competing_write)
    with pytest.raises(CasdoorGenerationConflict):
        allocate(storage)
    assert current(storage) == 1
    session.rollback()
    assert current(storage) == 0


def test_actual_lock_order_and_update_scope_compile_for_supported_dialects(storage, monkeypatch):
    session, _, _ = storage
    session.begin()
    original = session.execute
    original_scalar = session.scalar
    statements = []

    def capture(statement, *args, **kwargs):
        statements.append(statement)
        return original(statement, *args, **kwargs)

    def capture_scalar(statement, *args, **kwargs):
        statements.append(statement)
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", capture)
    monkeypatch.setattr(session, "scalar", capture_scalar)
    actual_sql = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        actual_sql.append(statement)

    sa.event.listen(session.bind, "before_cursor_execute", capture_sql)
    try:
        allocate(storage)
    finally:
        sa.event.remove(session.bind, "before_cursor_execute", capture_sql)
    # The fixture's explicit transaction BEGIN is not a row-locking DML/read.
    actual_sql = [sql for sql in actual_sql if sql != "BEGIN"]
    assert actual_sql[0].startswith("SELECT casdoor_integration_extend.")
    assert [sql for sql in actual_sql if sql.startswith(("UPDATE", "INSERT", "DELETE"))] == [actual_sql[-1]]
    assert actual_sql[-1].startswith("UPDATE casdoor_identity_extend SET")
    assert statements[0].get_final_froms()[0].name == "casdoor_integration_extend"
    locks = [s for s in statements if isinstance(s, sa.sql.Select) and s._for_update_arg is not None]
    assert [s.get_final_froms()[0].name for s in locks] == [
        "casdoor_integration_extend",
        "casdoor_namespace_extend",
        "accounts",
        "casdoor_identity_extend",
    ]
    for dialect in (postgresql.dialect(), mysql.dialect()):
        assert all("FOR UPDATE" in str(s.compile(dialect=dialect)) for s in locks)
        update = next(s for s in statements if isinstance(s, sa.sql.dml.Update))
        sql = str(update.compile(dialect=dialect))
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
            assert f"casdoor_identity_extend.{field}" in sql
        assert "sync_generation +" in sql


def test_requires_existing_explicit_transaction(storage):
    session, _, _ = storage
    with pytest.raises(RuntimeError, match="active caller-owned transaction"):
        allocate(storage)
    assert not session.in_transaction()
