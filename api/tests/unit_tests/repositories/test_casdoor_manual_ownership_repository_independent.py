"""Independent real-SQLite checks for ownership metadata and rollback boundaries."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.manual_ownership import ManualMemberScope, ManualMutationKind, ManualOwnershipDecision
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingIdentityContext
from core.casdoor.ownership import MembershipBackend
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
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
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_manual_ownership_repository_extend import (
    CasdoorManualOwnershipConflict,
    CasdoorManualOwnershipRepository,
)
from repositories.casdoor_membership_repository_extend import CasdoorMembershipConflict, CasdoorMembershipRepository
from sqlalchemy.orm import Session


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def configure(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (Account, Tenant, TenantAccountJoin, Integration, Namespace, Revision, Identity, History, Intent):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        integration = Integration(enabled=True)
        account = Account(name="Independent", email="independent@example.test", status=AccountStatus.ACTIVE)
        workspace = Tenant(name="Independent")
        session.add_all((integration, account, workspace))
        session.flush()
        namespace = Namespace(
            integration_id=integration.id,
            expected_issuer="https://independent.example.test",
            organization="IndependentOrg",
            application="IndependentApp",
            client_id="IndependentClient",
            core_fingerprint="a" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="b" * 64,
            browser_frontend_url="https://independent.example.test",
            backend_api_url="https://independent.example.test",
            expected_issuer=namespace.expected_issuer,
            organization=namespace.organization,
            application=namespace.application,
            client_id=namespace.client_id,
            button_text="Independent",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        identity = Identity(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=namespace.expected_issuer,
            organization=namespace.organization,
            subject="independent-subject",
            last_applied_json="identity-state",
            profile_sync_json="profile-state",
        )
        join = TenantAccountJoin(account_id=account.id, tenant_id=workspace.id, role=TenantAccountRole.NORMAL)
        session.add_all((revision, identity, join))
        session.flush()
        history = History(
            namespace_id=namespace.id,
            identity_id=identity.id,
            account_id=account.id,
            workspace_id=workspace.id,
            join_id=join.id,
            revision_id=revision.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=7,
            desired_generation=11,
            source=CasdoorMembershipSource.MAPPING,
            last_applied_roles_json='["old-applied"]',
            last_applied_fingerprint="c" * 64,
            desired_roles_json='["desired"]',
            baseline_json='"' + "z" * 65533 + '"',
            finalization=CasdoorFinalizationState.PENDING,
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


def _prepare(db, kind=ManualMutationKind.ROLE_CHANGE, backend=MembershipBackend.LOCAL):
    return db.repo.prepare((db.scope,), kind=kind, backend=backend)


def _refresh_history(db):
    db.session.expire_all()
    return db.session.get(History, db.history.id)


def _add_intent(db, *, kind, workspace_id, membership_id=None, operation_state=CasdoorOperationState.APPLIED):
    item = Intent(
        namespace_id=db.namespace.id,
        identity_id=db.identity.id,
        account_id=db.account.id,
        workspace_id=workspace_id,
        membership_id=membership_id,
        revision_id=db.revision.id,
        generation=5,
        ownership_epoch=7,
        fence_epoch=0,
        kind=kind,
        scope_digest="d" * 64,
        idempotency_key=uuid4().hex * 2,
        desired_json="{}",
        operation_state=operation_state,
        termination_state=CasdoorTerminationState.CONFIRMED,
        termination_proof_kind="untrusted-fixture-claim",
        proof_ref="untrusted-fixture-reference",
    )
    db.session.add(item)
    db.session.commit()
    return item


def test_metadata_update_preserves_snapshot_text_and_does_not_touch_join_or_account(db):
    before = tuple(
        getattr(db.history, field)
        for field in ("baseline_json", "last_applied_roles_json", "last_applied_fingerprint", "desired_roles_json")
    )
    emitted = []
    sa.event.listen(db.engine, "before_cursor_execute", lambda _c, _cu, sql, _p, _ctx, _many: emitted.append(sql))
    with db.session.begin():
        receipt = db.repo.mark_local_override(_prepare(db))[0]
        assert receipt.ownership_epoch == 8
        assert receipt.retained_join_id == UUID(db.join.id)
    row = _refresh_history(db)
    assert (
        tuple(
            getattr(row, field)
            for field in ("baseline_json", "last_applied_roles_json", "last_applied_fingerprint", "desired_roles_json")
        )
        == before
    )
    assert len(row.baseline_json.encode("utf-8")) == 65535
    assert row.finalization is CasdoorFinalizationState.PENDING
    assert db.session.get(TenantAccountJoin, db.join.id).role is TenantAccountRole.NORMAL
    assert db.account.status is AccountStatus.ACTIVE
    update_sql = next(
        sql for sql in emitted if sql.lstrip().upper().startswith("UPDATE CASDOOR_MANAGED_MEMBERSHIP_EXTEND")
    )
    assert "BASELINE_JSON" not in update_sql.upper()
    assert "LAST_APPLIED_ROLES_JSON" not in update_sql.upper()
    assert "DESIRED_ROLES_JSON" not in update_sql.upper()


def test_dirty_root_guard_runs_zero_sql_and_leaves_pending_state_for_caller(db):
    statements = []
    sa.event.listen(db.engine, "before_cursor_execute", lambda _c, _cu, sql, _p, _ctx, _many: statements.append(sql))
    with db.session.begin():
        db.account.name = "pending caller value"
        with pytest.raises(CasdoorManualOwnershipConflict):
            _prepare(db)
        assert statements == []
        assert db.account in db.session.dirty
        assert db.account.name == "pending caller value"


def test_retained_deleted_join_history_blocks_the_actual_new_management_gate(db):
    db.integration.active_revision_id = db.revision.id
    db.identity.sync_generation = 3
    db.session.commit()
    retained_join = db.join.id
    with db.session.begin():
        receipt = db.repo.mark_local_override(_prepare(db, ManualMutationKind.MEMBER_REMOVE))[0]
        assert receipt.tombstone and receipt.retained_join_id == UUID(retained_join)
        db.session.delete(db.join)
    assert db.session.get(TenantAccountJoin, retained_join) is None
    tombstone = _refresh_history(db)
    assert tombstone.join_id == retained_join and tombstone.tombstone

    context = MappingIdentityContext(
        UUID(db.integration.id),
        UUID(db.revision.id),
        UUID(db.namespace.id),
        UUID(db.identity.id),
        db.scope.account_id,
        "e" * 64,
        db.namespace.expected_issuer,
        db.namespace.organization,
        db.namespace.application,
        db.namespace.client_id,
        db.identity.subject,
    )
    target = DesiredWorkspaceTarget(
        db.scope.workspace_id,
        "normal",
        "normal",
        CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK,
        (),
    )
    version = GenerationPlanVersion(DesiredWorkspacePlan(context, (target,)), 2, 3)
    db.session.rollback()
    with db.session.begin():
        with pytest.raises(CasdoorMembershipConflict):
            CasdoorMembershipRepository(db.session).prepare_new(version, target, backend=MembershipBackend.LOCAL)


@pytest.mark.parametrize(
    ("kind", "workspace_id", "membership_id", "operation_state"),
    [
        (CasdoorIntentKind.RESOURCE_REVOKE, None, None, CasdoorOperationState.APPLIED),
        (CasdoorIntentKind.INVITATION_FINALIZE, None, None, CasdoorOperationState.CANCELLED),
        (CasdoorIntentKind.ROLE_REPLACE, None, "membership", CasdoorOperationState.PENDING),
    ],
)
def test_pending_or_claimed_workspace_intents_block_even_when_not_inflight(
    db, kind, workspace_id, membership_id, operation_state
):
    associated_membership = db.history.id if membership_id == "membership" else membership_id
    _add_intent(
        db,
        kind=kind,
        workspace_id=workspace_id,
        membership_id=associated_membership,
        operation_state=operation_state,
    )
    with db.session.begin():
        token = _prepare(db)
        assert token.inspections[0].decision is ManualOwnershipDecision.TERMINATION_REQUIRED
        with pytest.raises(CasdoorManualOwnershipConflict):
            db.repo.mark_local_override(token)


def test_avatar_only_intent_is_outside_the_required_member_barrier(db):
    _add_intent(
        db,
        kind=CasdoorIntentKind.PROFILE_AVATAR,
        workspace_id=db.workspace.id,
        operation_state=CasdoorOperationState.UNKNOWN,
    )
    with db.session.begin():
        assert db.repo.mark_local_override(_prepare(db))[0].ownership_epoch == 8


def test_archived_namespace_and_unlinked_identity_history_are_still_updated(db):
    second_namespace = Namespace(
        integration_id=db.integration.id,
        expected_issuer="https://old.example.test",
        organization="OldOrg",
        application="OldApp",
        client_id="OldClient",
        core_fingerprint="f" * 64,
        lifecycle=CasdoorNamespaceLifecycle.ARCHIVED,
    )
    db.session.add(second_namespace)
    db.session.flush()
    second_revision = Revision(
        integration_id=db.integration.id,
        namespace_id=second_namespace.id,
        revision_number=2,
        config_digest="1" * 64,
        browser_frontend_url="https://old.example.test",
        backend_api_url="https://old.example.test",
        expected_issuer=second_namespace.expected_issuer,
        organization=second_namespace.organization,
        application=second_namespace.application,
        client_id=second_namespace.client_id,
        button_text="Old",
        default_workspace_id=db.workspace.id,
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    db.session.add(second_revision)
    db.session.flush()
    historical = History(
        namespace_id=second_namespace.id,
        identity_id=str(uuid4()),
        account_id=db.account.id,
        workspace_id=db.workspace.id,
        join_id=db.join.id,
        revision_id=second_revision.id,
        ownership=CasdoorMembershipOwnership.MANAGED,
        ownership_epoch=2,
        desired_generation=4,
        source=CasdoorMembershipSource.MAPPING,
        last_applied_roles_json="legacy-applied",
        desired_roles_json="legacy-desired",
        baseline_json="legacy-baseline",
    )
    db.session.add(historical)
    db.session.commit()
    with db.session.begin():
        receipts = db.repo.mark_local_override(_prepare(db))
        assert {receipt.membership_id for receipt in receipts} == {UUID(db.history.id), UUID(historical.id)}
    db.session.expire_all()
    assert db.session.get(History, db.history.id).ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
    assert db.session.get(History, historical.id).ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
