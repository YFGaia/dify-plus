"""Synthetic actual SQLite UoW behavior; no writer hook or PG/MySQL race proof."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.manual_ownership import ManualMemberScope, ManualMutationKind, ManualOwnershipDecision
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingIdentityContext
from core.casdoor.ownership import MembershipBackend, MembershipObservation, OwnershipDecision, decide_ownership
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from repositories.casdoor_generation_repository_extend import MAX_GENERATION, GenerationPlanVersion
from repositories.casdoor_manual_ownership_repository_extend import (
    CasdoorManualOwnershipConflict,
    CasdoorManualOwnershipRepository,
)
from repositories.casdoor_membership_repository_extend import CasdoorMembershipConflict, CasdoorMembershipRepository
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def connect(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (Account, Tenant, TenantAccountJoin, Integration, Namespace, Revision, Identity, History, Intent):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        integration = Integration(enabled=False)
        account = Account(name="Synthetic", email="manual@example.test", status=AccountStatus.ACTIVE)
        workspace = Tenant(name="Synthetic")
        session.add_all([integration, account, workspace])
        session.flush()
        namespace = Namespace(
            integration_id=integration.id,
            expected_issuer="https://example.test",
            organization="Org",
            application="App",
            client_id="Client",
            core_fingerprint="a" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url="https://example.test",
            backend_api_url="https://example.test",
            expected_issuer="https://example.test",
            organization="Org",
            application="App",
            client_id="Client",
            button_text="Casdoor",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        identity = Identity(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=namespace.expected_issuer,
            organization="Org",
            subject="Subject",
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        join = TenantAccountJoin(account_id=account.id, tenant_id=workspace.id, role=TenantAccountRole.NORMAL)
        session.add_all([revision, identity, join])
        session.flush()
        history = History(
            namespace_id=namespace.id,
            identity_id=identity.id,
            account_id=account.id,
            workspace_id=workspace.id,
            join_id=join.id,
            revision_id=revision.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=3,
            source=CasdoorMembershipSource.MAPPING,
            desired_generation=5,
            last_applied_roles_json="original-applied",
            last_applied_fingerprint="b" * 64,
            desired_roles_json="original-desired",
            baseline_json='"' + "x" * 65533 + '"',
        )
        session.add(history)
        session.commit()
        yield SimpleNamespace(
            engine=engine,
            session=session,
            integration=integration,
            namespace=namespace,
            revision=revision,
            identity=identity,
            account=account,
            workspace=workspace,
            join=join,
            history=history,
            scope=ManualMemberScope(UUID(account.id), UUID(workspace.id)),
            repo=CasdoorManualOwnershipRepository(session),
        )
    engine.dispose()


def prepare(s, kind=ManualMutationKind.ROLE_CHANGE, backend=MembershipBackend.LOCAL, scopes=None):
    return s.repo.prepare(scopes or (s.scope,), kind=kind, backend=backend)


def fresh_history(s):
    s.session.expire_all()
    return s.session.get(History, s.history.id)


def add_intent(s, **changes):
    values = dict(
        namespace_id=s.namespace.id,
        identity_id=s.identity.id,
        account_id=s.account.id,
        workspace_id=s.workspace.id,
        membership_id=None,
        revision_id=s.revision.id,
        generation=1,
        ownership_epoch=0,
        fence_epoch=0,
        kind=CasdoorIntentKind.ROLE_REPLACE,
        scope_digest="c" * 64,
        idempotency_key=uuid4().hex * 2,
        desired_json="{}",
    )
    values.update(changes)
    row = Intent(**values)
    s.session.add(row)
    s.session.commit()
    return row


def test_metadata_only_and_exact_preservation(storage):
    s = storage
    before = (
        s.history.baseline_json,
        s.history.last_applied_roles_json,
        s.history.last_applied_fingerprint,
        s.history.desired_roles_json,
        s.history.finalization,
    )
    with s.session.begin():
        token = prepare(s)
        assert token.inspections[0].decision is ManualOwnershipDecision.MANAGED_READY
        result = s.repo.mark_local_override(token)
        assert result[0].ownership_epoch == 4
        assert result[0].retained_join_id == UUID(s.join.id)
    row = fresh_history(s)
    assert row.ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
    assert before == (
        row.baseline_json,
        row.last_applied_roles_json,
        row.last_applied_fingerprint,
        row.desired_roles_json,
        row.finalization,
    )
    assert len(row.baseline_json.encode()) == 65535
    assert s.session.get(TenantAccountJoin, s.join.id).role is TenantAccountRole.NORMAL
    assert s.account.status is AccountStatus.ACTIVE


def test_real_delete_tombstone_history_reader_and_outer_rollback(storage):
    s = storage
    historical_join = s.join.id
    baseline = s.history.baseline_json
    with pytest.raises(RuntimeError, match="writer failed"):
        with s.session.begin():
            token = prepare(s, ManualMutationKind.MEMBER_REMOVE)
            s.repo.mark_local_override(token)
            s.session.delete(s.join)
            s.session.flush()
            raise RuntimeError("writer failed")
    assert s.session.get(TenantAccountJoin, historical_join) is not None
    assert fresh_history(s).ownership is CasdoorMembershipOwnership.MANAGED
    s.session.rollback()
    with s.session.begin():
        s.repo.mark_local_override(prepare(s, ManualMutationKind.MEMBER_REMOVE))
        s.session.delete(s.session.get(TenantAccountJoin, historical_join))
    row = fresh_history(s)
    assert s.session.get(TenantAccountJoin, historical_join) is None
    assert row.tombstone and row.join_id == historical_join and row.baseline_json == baseline
    snapshot = CasdoorMembershipRepository._snapshot(row)
    observation = MembershipObservation(s.scope.workspace_id, s.scope.account_id, None, None, MembershipBackend.LOCAL)
    assert decide_ownership(observation, snapshot) is OwnershipDecision.PRESERVE_OVERRIDE
    assert (
        CasdoorMembershipRepository(s.session)._history(s.scope.account_id, s.scope.workspace_id)[0].join_id
        == historical_join
    )
    s.session.rollback()
    with s.session.begin():
        assert prepare(s).inspections[0].decision is ManualOwnershipDecision.ALREADY_LOCAL
        assert s.repo.mark_local_override(prepare(s)) == ()


@pytest.mark.parametrize("state", ["new", "dirty", "deleted", "nested", "inactive", "no_transaction"])
@pytest.mark.parametrize("entrypoint", ["prepare", "mark"])
def test_reject_unclean_root_before_any_sql(storage, state, entrypoint):
    s = storage
    token = None
    if entrypoint == "mark":
        s.session.begin()
        token = prepare(s)
        if state == "no_transaction":
            s.session.commit()
    if state != "no_transaction":
        if not s.session.in_transaction():
            s.session.begin()
    if state == "new":
        s.session.add(Account(name="Pending", email="pending@example.test"))
    elif state == "dirty":
        s.account.name = "Pending"
    elif state == "deleted":
        s.session.delete(s.join)
    elif state == "nested":
        s.session.begin_nested()
    elif state == "inactive":
        duplicate = Account(name="Duplicate", email="duplicate@example.test")
        duplicate.id = s.account.id
        s.session.expunge(s.account)
        s.session.add(duplicate)
        with pytest.raises(IntegrityError):
            s.session.flush()
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    with pytest.raises((CasdoorManualOwnershipConflict, RuntimeError)):
        if entrypoint == "mark":
            s.repo.mark_local_override(token)
        else:
            prepare(s)
    assert statements == []
    if state == "new":
        assert s.session.new
    if state == "dirty":
        assert s.account.name == "Pending" and s.session.dirty
    if state == "deleted":
        assert s.join in s.session.deleted


@pytest.mark.parametrize(
    "axis",
    [
        (CasdoorOperationState.UNKNOWN, CasdoorTerminationState.CONFIRMED),
        (CasdoorOperationState.IN_FLIGHT, CasdoorTerminationState.UNCONFIRMED),
        (CasdoorOperationState.APPLIED, CasdoorTerminationState.CONFIRMED),
        (CasdoorOperationState.CANCELLED, CasdoorTerminationState.NOT_STARTED),
        (CasdoorOperationState.FAILED, CasdoorTerminationState.MANUAL_RECOVERY),
        (CasdoorOperationState.PENDING, CasdoorTerminationState.NOT_STARTED),
    ],
)
def test_any_required_intent_needs_real_i16_even_confirmed(storage, axis):
    s = storage
    add_intent(
        s,
        operation_state=axis[0],
        termination_state=axis[1],
        termination_proof_kind="fabricated",
        proof_ref="fake-confirmed",
    )
    with s.session.begin():
        token = prepare(s)
        assert token.inspections[0].decision is ManualOwnershipDecision.TERMINATION_REQUIRED
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(token)
    assert fresh_history(s).ownership_epoch == 3


@pytest.mark.parametrize(
    "kind",
    [
        CasdoorIntentKind.RESOURCE_GRANT,
        CasdoorIntentKind.RESOURCE_REVOKE,
        CasdoorIntentKind.MEMBER_REMOVE,
        CasdoorIntentKind.INVITATION_FINALIZE,
    ],
)
def test_null_membership_or_workspace_intents_are_included(storage, kind):
    s = storage
    add_intent(s, kind=kind, workspace_id=None)
    with s.session.begin():
        assert prepare(s).inspections[0].decision is ManualOwnershipDecision.TERMINATION_REQUIRED


def test_avatar_does_not_authorize_or_block_membership(storage):
    s = storage
    add_intent(s, kind=CasdoorIntentKind.PROFILE_AVATAR, operation_state=CasdoorOperationState.UNKNOWN)
    with s.session.begin():
        assert s.repo.mark_local_override(prepare(s))[0].ownership_epoch == 4


@pytest.mark.parametrize(
    "ownership", [None, CasdoorMembershipOwnership.RELEASED, CasdoorMembershipOwnership.LOCAL_OVERRIDE]
)
def test_no_implicit_ownership_even_equal_role(storage, ownership):
    s = storage
    if ownership is None:
        s.session.delete(s.history)
    else:
        s.history.ownership = ownership
    s.session.commit()
    with s.session.begin():
        assert s.repo.mark_local_override(prepare(s)) == ()
    assert s.session.scalar(sa.select(sa.func.count()).select_from(History)) == (0 if ownership is None else 1)


@pytest.mark.parametrize("mode", ["local_owner", "remote", "join_recreated", "managed_absent", "managed_tombstone"])
def test_owner_remote_and_contradictory_history_fail_closed(storage, mode):
    s = storage
    if mode == "local_owner":
        s.join.role = TenantAccountRole.OWNER
    elif mode == "join_recreated":
        s.history.join_id = str(uuid4())
    elif mode == "managed_absent":
        s.session.delete(s.join)
    elif mode == "managed_tombstone":
        s.history.tombstone = True
    s.session.commit()
    with s.session.begin():
        token = prepare(s, backend=MembershipBackend.REMOTE if mode == "remote" else MembershipBackend.LOCAL)
        assert token.inspections[0].decision is (
            ManualOwnershipDecision.AUTHORIZATION_PENDING
            if mode == "remote"
            else ManualOwnershipDecision.OWNER_FENCE_REQUIRED
        )
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(token)
    assert fresh_history(s).ownership_epoch == 3


def test_archived_namespace_unlinked_history_still_protected(storage):
    s = storage
    s.namespace.lifecycle = CasdoorNamespaceLifecycle.ARCHIVED
    s.session.delete(s.identity)
    s.session.commit()
    with s.session.begin():
        assert s.repo.mark_local_override(prepare(s))[0].ownership_epoch == 4


@pytest.mark.parametrize("change", ["epoch", "join", "fence", "intent", "new_history"])
def test_fresh_recheck_detects_interleaving_not_real_concurrency(storage, change):
    s = storage
    with s.session.begin():
        token = prepare(s)
        if change == "epoch":
            s.session.execute(sa.update(History).values(ownership_epoch=4).execution_options(synchronize_session=False))
        elif change == "join":
            s.session.execute(
                sa.update(TenantAccountJoin)
                .values(role=TenantAccountRole.ADMIN)
                .execution_options(synchronize_session=False)
            )
        elif change == "fence":
            s.session.execute(sa.update(Namespace).values(fence_epoch=1).execution_options(synchronize_session=False))
        elif change == "intent":
            s.session.execute(
                sa.insert(Intent).values(
                    namespace_id=s.namespace.id,
                    identity_id=s.identity.id,
                    account_id=s.account.id,
                    workspace_id=s.workspace.id,
                    revision_id=s.revision.id,
                    generation=0,
                    ownership_epoch=0,
                    fence_epoch=0,
                    kind=CasdoorIntentKind.RESOURCE_GRANT,
                    scope_digest="d" * 64,
                    idempotency_key="e" * 64,
                    desired_json="{}",
                )
            )
        else:
            s.session.execute(sa.delete(History).execution_options(synchronize_session=False))
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(token)
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(token)


def test_token_issuer_object_and_uow_binding_no_bool_proof(storage):
    s = storage
    with s.session.begin():
        token = prepare(s)
        for fake in (True, replace(token), token):
            with pytest.raises(CasdoorManualOwnershipConflict):
                CasdoorManualOwnershipRepository(s.session).mark_local_override(fake)
        with pytest.raises(TypeError):
            s.repo.mark_local_override(token, termination_confirmed=True)
        assert s.repo.mark_local_override(token)
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(token)
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(token)


def test_epoch_maximum_cannot_overflow(storage):
    s = storage
    s.history.ownership_epoch = MAX_GENERATION
    s.session.commit()
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(prepare(s))
    assert fresh_history(s).ownership_epoch == MAX_GENERATION


@pytest.mark.parametrize("status", [AccountStatus.BANNED, AccountStatus.CLOSED])
def test_banned_account_metadata_does_not_replace_legacy_policy(storage, status):
    s = storage
    s.account.status = status
    s.session.commit()
    with s.session.begin():
        s.repo.mark_local_override(prepare(s, ManualMutationKind.MEMBER_REMOVE))
    assert s.account.status is status


def test_column_lock_order_and_cas_shape_only(storage):
    s = storage
    statements = []
    sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
    with s.session.begin():
        s.repo.mark_local_override(prepare(s))
    lock_tables = []
    for statement in statements:
        if isinstance(statement, sa.sql.Select) and statement._for_update_arg is not None:
            for dialect in (postgresql.dialect(), mysql.dialect()):
                assert "FOR UPDATE" in str(statement.compile(dialect=dialect))
            lock_tables.append(statement.get_final_froms()[0].name)
        if isinstance(statement, sa.sql.Select):
            assert "baseline_json" not in str(statement) and "last_applied_roles_json" not in str(statement)
    assert lock_tables[:8] == [
        Integration.__tablename__,
        Namespace.__tablename__,
        Account.__tablename__,
        Identity.__tablename__,
        Tenant.__tablename__,
        TenantAccountJoin.__tablename__,
        History.__tablename__,
        Intent.__tablename__,
    ]
    update = next(statement for statement in statements if isinstance(statement, sa.sql.Update))
    sql = str(update.compile(dialect=postgresql.dialect()))
    assert "ownership_epoch =" in sql and "ownership =" in sql and "join_id =" in sql
    assert "baseline_json" not in sql and "last_applied_roles_json" not in sql


def add_namespace(s):
    namespace = Namespace(
        integration_id=s.integration.id,
        expected_issuer=s.namespace.expected_issuer,
        organization="Org",
        application="App",
        client_id="Client",
        core_fingerprint="f" * 64,
        lifecycle=CasdoorNamespaceLifecycle.ARCHIVED,
    )
    s.session.add(namespace)
    s.session.flush()
    revision = Revision(
        integration_id=s.integration.id,
        namespace_id=namespace.id,
        revision_number=2,
        config_digest="f" * 64,
        browser_frontend_url="https://example.test",
        backend_api_url="https://example.test",
        expected_issuer=namespace.expected_issuer,
        organization="Org",
        application="App",
        client_id="Client",
        button_text="Casdoor",
        default_workspace_id=s.workspace.id,
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    s.session.add(revision)
    s.session.flush()
    # Missing historical identity is legitimate after properly fenced unlink.
    history = History(
        namespace_id=namespace.id,
        identity_id=str(uuid4()),
        account_id=s.account.id,
        workspace_id=s.workspace.id,
        join_id=s.join.id,
        revision_id=revision.id,
        ownership=CasdoorMembershipOwnership.MANAGED,
        ownership_epoch=8,
        source=CasdoorMembershipSource.MAPPING,
        desired_generation=9,
        last_applied_roles_json="{}",
        desired_roles_json="{}",
        baseline_json="{}",
    )
    s.session.add(history)
    s.session.commit()
    return namespace, revision, history


def test_all_namespace_managed_histories_are_one_batch(storage):
    s = storage
    _namespace, _revision, historical = add_namespace(s)
    with s.session.begin():
        receipts = s.repo.mark_local_override(prepare(s))
        assert {row.membership_id for row in receipts} == {UUID(s.history.id), UUID(historical.id)}
        assert {row.ownership_epoch for row in receipts} == {4, 9}
    assert (
        s.session.scalar(
            sa.select(sa.func.count())
            .select_from(History)
            .where(History.ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE)
        )
        == 2
    )


def test_multirow_cas_failure_rolls_back_whole_uow(storage):
    s = storage
    add_namespace(s)
    original = dict(s.session.execute(sa.select(History.id, History.ownership_epoch)).all())
    s.session.rollback()
    cas_updates = []
    injected = False

    def race(connection, _cursor, statement, _parameters, _context, _many):
        nonlocal injected
        if statement.startswith("UPDATE casdoor_managed_membership_extend") and "ownership =" in statement:
            cas_updates.append(statement)
            if len(cas_updates) == 2 and not injected:
                injected = True
                connection.execute(sa.update(History).where(History.id == raced_id).values(ownership_epoch=100))

    sa.event.listen(s.engine, "before_cursor_execute", race)
    with pytest.raises(CasdoorManualOwnershipConflict):
        with s.session.begin():
            token = prepare(s)
            raced_id = token._state.histories[1].id
            s.repo.mark_local_override(token)
    assert injected and len(cas_updates) == 2
    actual = s.session.execute(sa.select(History.id, History.ownership_epoch, History.ownership)).all()
    assert {row.id: row.ownership_epoch for row in actual} == original
    assert all(row.ownership is CasdoorMembershipOwnership.MANAGED for row in actual)


def test_actual_prepare_new_rejects_retained_deleted_join_history(storage):
    s = storage
    s.integration.enabled = True
    s.integration.active_revision_id = s.revision.id
    s.identity.sync_generation = 1
    s.session.commit()
    with s.session.begin():
        s.repo.mark_local_override(prepare(s, ManualMutationKind.MEMBER_REMOVE))
        s.session.delete(s.join)
    context = MappingIdentityContext(
        UUID(s.integration.id),
        UUID(s.revision.id),
        UUID(s.namespace.id),
        UUID(s.identity.id),
        s.scope.account_id,
        "a" * 64,
        s.namespace.expected_issuer,
        "Org",
        "App",
        "Client",
        s.identity.subject,
    )
    target = DesiredWorkspaceTarget(
        s.scope.workspace_id, "normal", "normal", CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK, ()
    )
    version = GenerationPlanVersion(DesiredWorkspacePlan(context, (target,)), 0, 1)
    with s.session.begin():
        with pytest.raises(CasdoorMembershipConflict):
            CasdoorMembershipRepository(s.session).prepare_new(version, target, backend=MembershipBackend.LOCAL)


def test_already_absent_managed_join_only_remove_can_tombstone(storage):
    s = storage
    join_id = s.join.id
    s.session.delete(s.join)
    s.session.commit()
    with s.session.begin():
        assert prepare(s).inspections[0].decision is ManualOwnershipDecision.OWNER_FENCE_REQUIRED
        receipt = s.repo.mark_local_override(prepare(s, ManualMutationKind.MEMBER_REMOVE))[0]
        assert receipt.tombstone and receipt.retained_join_id == UUID(join_id)


@pytest.mark.parametrize("managed_owner", [False, True])
def test_transfer_preserves_unmanaged_owner_and_fences_managed_owner(storage, managed_owner):
    s = storage
    owner = Account(name="Owner", email="owner@example.test")
    s.session.add(owner)
    s.session.flush()
    join = TenantAccountJoin(account_id=owner.id, tenant_id=s.workspace.id, role=TenantAccountRole.OWNER)
    s.session.add(join)
    s.session.flush()
    if managed_owner:
        s.session.add(
            History(
                namespace_id=s.namespace.id,
                identity_id=str(uuid4()),
                account_id=owner.id,
                workspace_id=s.workspace.id,
                join_id=join.id,
                revision_id=s.revision.id,
                ownership=CasdoorMembershipOwnership.MANAGED,
                ownership_epoch=0,
                source=CasdoorMembershipSource.MAPPING,
                desired_generation=0,
                last_applied_roles_json="{}",
                desired_roles_json="{}",
                baseline_json="{}",
            )
        )
    s.session.commit()
    scopes = (s.scope, ManualMemberScope(UUID(owner.id), s.scope.workspace_id))
    with s.session.begin():
        token = prepare(s, kind=ManualMutationKind.OWNER_TRANSFER, scopes=scopes)
        if managed_owner:
            assert any(view.decision is ManualOwnershipDecision.OWNER_FENCE_REQUIRED for view in token.inspections)
            with pytest.raises(CasdoorManualOwnershipConflict):
                s.repo.mark_local_override(token)
        else:
            assert len(s.repo.mark_local_override(token)) == 1
    assert join.role is TenantAccountRole.OWNER and s.join.role is TenantAccountRole.NORMAL


def test_fresh_same_type_token_from_old_root_transaction_fails(storage):
    s = storage
    with s.session.begin():
        token = prepare(s)
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.mark_local_override(token)


@pytest.mark.parametrize(
    "bad",
    [(), (ManualMemberScope("invalid", uuid4()),), tuple(ManualMemberScope(uuid4(), uuid4()) for _ in range(101))],
)
def test_scope_scalar_and_count_bounds_reject_before_sql(storage, bad):
    s = storage
    statements = []
    with s.session.begin():
        sa.event.listen(s.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        with pytest.raises(CasdoorManualOwnershipConflict):
            s.repo.prepare(bad, kind=ManualMutationKind.ROLE_CHANGE, backend=MembershipBackend.LOCAL)
        assert statements == []


def namespace_values(s, identifier):
    return dict(
        id=identifier,
        integration_id=s.integration.id,
        expected_issuer="https://example.test",
        organization="Org",
        application="App",
        client_id="Client",
        core_fingerprint="a" * 64,
    )


def seed_large_parent_set(s):
    accounts = [s.account.id] + [str(uuid4()) for _ in range(99)]
    namespaces = [s.namespace.id] + [str(uuid4()) for _ in range(20)]
    s.session.execute(
        sa.insert(Account),
        [dict(id=value, name="Synthetic", email=f"{i}@example.test") for i, value in enumerate(accounts[1:])],
    )
    s.session.execute(sa.insert(Namespace), [namespace_values(s, value) for value in namespaces[1:]])
    s.session.commit()
    return accounts, namespaces, tuple(ManualMemberScope(UUID(account), s.scope.workspace_id) for account in accounts)


def test_actual_namespace_2001_overflow_is_rejected_not_truncated(storage):
    s = storage
    s.session.execute(sa.insert(Namespace), [namespace_values(s, str(uuid4())) for _ in range(2000)])
    s.session.commit()
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            prepare(s)
    assert fresh_history(s).ownership_epoch == 3


def test_actual_identity_2001_overflow_is_rejected_not_truncated(storage):
    s = storage
    accounts, namespaces, scopes = seed_large_parent_set(s)
    s.session.execute(
        sa.insert(Identity),
        [
            dict(
                id=str(uuid4()),
                namespace_id=namespace,
                account_id=account,
                issuer="https://example.test",
                organization="Org",
                subject=account,
                subject_digest=hashlib.sha256(account.encode()).hexdigest(),
                last_applied_json="{}",
                profile_sync_json="{}",
            )
            for namespace in namespaces
            for account in accounts
            if (namespace, account) != (s.namespace.id, s.account.id)
        ],
    )
    s.session.commit()
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            prepare(s, scopes=scopes)
    assert fresh_history(s).ownership_epoch == 3


def test_actual_history_2001_overflow_is_rejected_not_truncated(storage):
    s = storage
    accounts, namespaces, scopes = seed_large_parent_set(s)
    s.session.execute(
        sa.insert(History),
        [
            dict(
                id=str(uuid4()),
                namespace_id=namespace,
                identity_id=str(uuid4()),
                account_id=account,
                workspace_id=s.workspace.id,
                revision_id=s.revision.id,
                join_id=s.join.id,
                ownership=CasdoorMembershipOwnership.MANAGED,
                source=CasdoorMembershipSource.MAPPING,
                last_applied_roles_json="{}",
                desired_roles_json="{}",
                baseline_json="{}",
            )
            for namespace in namespaces
            for account in accounts
            if (namespace, account) != (s.namespace.id, s.account.id)
        ],
    )
    s.session.commit()
    # Synthetic cross-namespace revision references never pass the chain check;
    # the earlier bounded discovery rejects the over-limit set before that check.
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            prepare(s, scopes=scopes)
    assert fresh_history(s).ownership_epoch == 3


def test_actual_intent_20001_overflow_is_rejected_not_truncated(storage):
    s = storage
    s.session.execute(
        sa.insert(Intent),
        [
            dict(
                id=str(uuid4()),
                namespace_id=s.namespace.id,
                identity_id=s.identity.id,
                account_id=s.account.id,
                workspace_id=s.workspace.id,
                revision_id=s.revision.id,
                generation=0,
                ownership_epoch=0,
                fence_epoch=0,
                kind=CasdoorIntentKind.RESOURCE_GRANT,
                scope_digest="a" * 64,
                idempotency_key=uuid4().hex * 2,
                desired_json="{}",
            )
            for _ in range(20001)
        ],
    )
    s.session.commit()
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            prepare(s)
    assert fresh_history(s).ownership_epoch == 3


@pytest.mark.parametrize("association", ["identity", "revision", "namespace"])
def test_strict_historical_parent_association(storage, association):
    s = storage
    namespace, revision, _history = add_namespace(s)
    if association == "identity":
        s.identity.namespace_id = namespace.id
    elif association == "revision":
        s.history.revision_id = revision.id
    else:
        s.namespace.organization = "OtherOrg"
    s.session.commit()
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            prepare(s)
    assert fresh_history(s).ownership_epoch == 3


def test_largest_legal_increment_and_db_out_of_range_epoch(storage):
    s = storage
    s.history.ownership_epoch = MAX_GENERATION - 1
    s.session.commit()
    with s.session.begin():
        assert s.repo.mark_local_override(prepare(s))[0].ownership_epoch == MAX_GENERATION
    s.session.execute(
        sa.update(History)
        .values(ownership=CasdoorMembershipOwnership.MANAGED, ownership_epoch=sa.literal_column("9223372036854775808"))
        .execution_options(synchronize_session=False)
    )
    s.session.commit()
    with s.session.begin():
        with pytest.raises(CasdoorManualOwnershipConflict):
            prepare(s)
