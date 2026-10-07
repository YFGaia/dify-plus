"""Real SQLite constraints/rollback and real helper receipts; synthetic owners."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingIdentityContext
from core.casdoor.ownership import (
    UNKNOWN_MEMBER_ROLES,
    ExternalMemberRolesProjection,
    MemberRole,
    MembershipBackend,
    OwnershipDecision,
    RolesKnowledge,
)
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorManagedMembershipExtend,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceExtend,
)
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_membership_repository_extend import CasdoorMembershipConflict, CasdoorMembershipRepository
from services.account_service import TenantService
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

    for model in (
        Account,
        Tenant,
        TenantAccountJoin,
        CasdoorIntegrationExtend,
        CasdoorNamespaceExtend,
        CasdoorConfigRevisionExtend,
        CasdoorIdentityExtend,
        CasdoorManagedMembershipExtend,
    ):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        integration = CasdoorIntegrationExtend(enabled=True)
        account = Account(name="Synthetic", email="synthetic@example.test", status=AccountStatus.ACTIVE)
        workspace = Tenant(name="Existing synthetic workspace")
        session.add_all([integration, account, workspace])
        session.flush()
        namespace = CasdoorNamespaceExtend(
            integration_id=integration.id,
            expected_issuer="https://synthetic.example.test",
            organization="Org",
            application="App",
            client_id="Client",
            core_fingerprint="b" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = CasdoorConfigRevisionExtend(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url="https://synthetic.example.test",
            backend_api_url="https://synthetic.example.test",
            expected_issuer="https://synthetic.example.test",
            organization="Org",
            application="App",
            client_id="Client",
            button_text="Casdoor",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        identity = CasdoorIdentityExtend(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=namespace.expected_issuer,
            organization="Org",
            subject="ExactCaseSubject",
            sync_generation=1,
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
            namespace.expected_issuer,
            "Org",
            "App",
            "Client",
            "ExactCaseSubject",
        )
        target = DesiredWorkspaceTarget(
            UUID(workspace.id), "normal", "normal", CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK, ()
        )
        version = GenerationPlanVersion(DesiredWorkspacePlan(context, (target,)), 0, 1)
        session.commit()
        yield SimpleNamespace(
            session=session,
            engine=engine,
            integration=integration,
            account=account,
            workspace=workspace,
            namespace=namespace,
            revision=revision,
            identity=identity,
            version=version,
            target=target,
            repo=CasdoorMembershipRepository(session),
        )
    engine.dispose()


def inspect(s, *, version=None, backend=MembershipBackend.LOCAL, remote=UNKNOWN_MEMBER_ROLES):
    return s.repo.inspect(version or s.version, UUID(s.workspace.id), backend=backend, remote=remote)


def new(s, *, backend=MembershipBackend.LOCAL, remote=UNKNOWN_MEMBER_ROLES, role="normal"):
    token = s.repo.prepare_new(s.version, s.target, backend=backend, remote=remote)
    receipt = TenantService.persist_tenant_member(s.workspace, s.account, s.session, role)
    return token, receipt


def register(s, **kwargs):
    return s.repo.register_new(*new(s, **kwargs))


def count(s, model):
    return s.session.scalar(sa.select(sa.func.count()).select_from(model))


def test_new_fallback_baseline_is_pending_and_entire_uow_rolls_back(storage):
    s = storage
    with pytest.raises(RuntimeError, match="later failure"), s.session.begin():
        snapshot = register(s)
        assert snapshot.source is CasdoorMembershipSource.FALLBACK
        assert snapshot.namespace_id == s.version.plan.context.namespace_id
        assert snapshot.identity_id == s.version.identity_id
        assert snapshot.account_id == UUID(s.account.id) and snapshot.workspace_id == UUID(s.workspace.id)
        assert snapshot.desired_generation == 1 and snapshot.ownership_epoch == 0
        assert snapshot.baseline_json == snapshot.last_applied_roles_json
        assert json.loads(snapshot.baseline_json)["join_role"] == "normal"
        assert inspect(s).decision is OwnershipDecision.MANAGED_CURRENT
        row = s.session.get(CasdoorManagedMembershipExtend, str(snapshot.membership_id))
        assert row.finalization is CasdoorFinalizationState.PENDING and not row.tombstone
        assert count(s, TenantAccountJoin) == count(s, CasdoorManagedMembershipExtend) == 1
        raise RuntimeError("later failure")
    with s.session.begin():
        assert count(s, TenantAccountJoin) == count(s, CasdoorManagedMembershipExtend) == 0
        assert count(s, Account) == count(s, Tenant) == 1


@pytest.mark.parametrize("role", ["admin", "editor", "normal"])
def test_actual_new_mapped_join_registration_records_mapping_source(storage, role):
    s = storage
    target = replace(s.target, target_role=role, builtin_id=role, reason=CasdoorDecisionReason.ROLE_MAPPING)
    s.target = target
    s.version = replace(s.version, plan=replace(s.version.plan, targets=(target,)))
    with s.session.begin():
        record = register(s, role=role)
        assert record.source is CasdoorMembershipSource.MAPPING
        assert json.loads(record.baseline_json)["join_role"] == role
        assert inspect(s).decision is OwnershipDecision.MANAGED_CURRENT


@pytest.mark.parametrize("role", list(TenantAccountRole))
@pytest.mark.parametrize("invited", [False, True])
def test_existing_members_owner_invite_or_other_provider_preserved(storage, role, invited):
    s = storage
    with s.session.begin():
        join = TenantAccountJoin(
            tenant_id=s.workspace.id, account_id=s.account.id, role=role, invited_by=s.account.id if invited else None
        )
        s.session.add(join)
        s.session.flush()
        view = inspect(s)
        assert view.decision is (
            OwnershipDecision.OWNER_PROTECTED
            if role is TenantAccountRole.OWNER
            else OwnershipDecision.PRESERVE_UNMANAGED
        )
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        assert join.role is role and count(s, CasdoorManagedMembershipExtend) == 0


def test_prepare_then_legacy_insert_helper_existing_receipt_cannot_be_managed(storage):
    s = storage
    with s.session.begin():
        token = s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        # Deterministic legacy insert interleaving, not multi-client DB concurrency.
        join = TenantAccountJoin(tenant_id=s.workspace.id, account_id=s.account.id, role=TenantAccountRole.NORMAL)
        s.session.add(join)
        s.session.flush()
        receipt = TenantService.persist_tenant_member(s.workspace, s.account, s.session)
        assert receipt.membership_created is False
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)
        assert inspect(s).decision is OwnershipDecision.PRESERVE_UNMANAGED
        assert count(s, CasdoorManagedMembershipExtend) == 0


@pytest.mark.parametrize("kind", ["naked_id", "boolean", "fake_receipt", "other_repo", "unflushed"])
def test_receipt_or_token_alone_never_adopts(storage, kind):
    s = storage
    with s.session.begin():
        token = s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        if kind == "unflushed":
            join = TenantAccountJoin(tenant_id=s.workspace.id, account_id=s.account.id)
            s.session.add(join)
            from services.account_service import _PersistedTenantMember

            receipt = _PersistedTenantMember(join, True)
        else:
            actual = TenantService.persist_tenant_member(s.workspace, s.account, s.session)
            receipt = {
                "naked_id": actual.join.id,
                "boolean": True,
                "fake_receipt": SimpleNamespace(join=actual.join, membership_created=True),
            }.get(kind, actual)
        repository = CasdoorMembershipRepository(s.session) if kind == "other_repo" else s.repo
        with pytest.raises(CasdoorMembershipConflict):
            repository.register_new(token, receipt)
        with s.session.no_autoflush:
            assert count(s, CasdoorManagedMembershipExtend) == 0


def test_failed_registration_rolls_back_helper_existing_role_update_with_entire_uow(storage):
    s = storage
    with s.session.begin():
        other_workspace = Tenant(name="Existing manual membership workspace")
        s.session.add(other_workspace)
        s.session.flush()
        existing = TenantAccountJoin(
            tenant_id=other_workspace.id, account_id=s.account.id, role=TenantAccountRole.ADMIN
        )
        s.session.add(existing)
    with pytest.raises(CasdoorMembershipConflict), s.session.begin():
        token = s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        receipt = TenantService.persist_tenant_member(other_workspace, s.account, s.session, "normal")
        assert receipt.membership_created is False and existing.role is TenantAccountRole.NORMAL
        s.repo.register_new(token, receipt)
    with s.session.begin():
        assert s.session.get(TenantAccountJoin, existing.id).role is TenantAccountRole.ADMIN
        assert count(s, CasdoorManagedMembershipExtend) == 0


def test_token_one_use_and_exact_transaction(storage):
    s = storage
    with s.session.begin():
        token, receipt = new(s)
        s.repo.register_new(token, receipt)
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)


def test_unused_preparation_cannot_cross_transaction_boundary(storage):
    s = storage
    with s.session.begin():
        token = s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
    with s.session.begin():
        receipt = TenantService.persist_tenant_member(s.workspace, s.account, s.session)
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)
        assert count(s, CasdoorManagedMembershipExtend) == 0
    with s.session.begin():
        # A token cannot cross transaction boundaries even with an actual receipt.
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)


@pytest.mark.parametrize(
    "ownership,tombstone",
    [
        (CasdoorMembershipOwnership.MANAGED, False),
        (CasdoorMembershipOwnership.LOCAL_OVERRIDE, False),
        (CasdoorMembershipOwnership.RELEASED, False),
        (CasdoorMembershipOwnership.LOCAL_OVERRIDE, True),
    ],
)
def test_historical_record_blocks_rejoin_after_deletion(storage, ownership, tombstone):
    s = storage
    with s.session.begin():
        record = register(s)
        row = s.session.get(CasdoorManagedMembershipExtend, str(record.membership_id))
        row.ownership, row.tombstone = ownership, tombstone
        s.session.delete(s.session.get(TenantAccountJoin, str(record.join_id)))
        s.session.flush()
        decision = inspect(s).decision
        assert decision is (
            OwnershipDecision.MARK_OVERRIDE_REQUIRED
            if ownership is CasdoorMembershipOwnership.MANAGED
            else OwnershipDecision.PRESERVE_UNMANAGED
            if ownership is CasdoorMembershipOwnership.RELEASED
            else OwnershipDecision.PRESERVE_OVERRIDE
        )
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)


def test_same_role_recreated_join_and_manual_change_yield_override_without_role_writes(storage):
    s = storage
    with s.session.begin():
        record = register(s)
        join = s.session.get(TenantAccountJoin, str(record.join_id))
        join.role = TenantAccountRole.ADMIN
        s.session.flush()
        assert inspect(s).decision is OwnershipDecision.MARK_OVERRIDE_REQUIRED
        assert join.role is TenantAccountRole.ADMIN
        s.session.delete(join)
        s.session.flush()
        recreated = TenantAccountJoin(tenant_id=s.workspace.id, account_id=s.account.id, role=TenantAccountRole.NORMAL)
        s.session.add(recreated)
        s.session.flush()
        assert inspect(s).decision is OwnershipDecision.MARK_OVERRIDE_REQUIRED
        assert (
            s.session.get(CasdoorManagedMembershipExtend, str(record.membership_id)).ownership
            is CasdoorMembershipOwnership.MANAGED
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("integration.enabled", False),
        ("integration.active_revision_id", None),
        ("namespace.fence_epoch", 1),
        ("namespace.lifecycle", "fencing"),
        ("namespace.organization", "Other"),
        ("revision.config_digest", "c" * 64),
        ("identity.sync_generation", 2),
        ("identity.account_id", str(uuid4())),
        ("account.status", AccountStatus.BANNED),
        ("account.status", AccountStatus.CLOSED),
        ("workspace.status", "archive"),
    ],
)
def test_fresh_db_guards_reject_cached_projection_and_leave_owner_unchanged(storage, field, value):
    s = storage
    obj, attr = field.split(".")
    model = type(getattr(s, obj))
    with s.session.begin():
        s.session.execute(
            sa.update(model)
            .where(model.id == getattr(s, obj).id)
            .values({attr: value})
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(CasdoorMembershipConflict):
            inspect(s)
        assert count(s, CasdoorManagedMembershipExtend) == 0


@pytest.mark.parametrize("pending", ["dirty_account", "dirty_namespace", "new_join", "deleted_account"])
def test_pending_work_is_rejected_before_any_sql_and_retained(storage, pending):
    s = storage
    statements = []
    with s.session.begin():
        if pending == "dirty_account":
            s.account.name = "Pending"
        elif pending == "dirty_namespace":
            s.namespace.fence_epoch = 1
        elif pending == "new_join":
            s.session.add(TenantAccountJoin(tenant_id=s.workspace.id, account_id=s.account.id))
        else:
            s.session.delete(s.account)
        sa.event.listen(s.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        with pytest.raises(CasdoorMembershipConflict):
            inspect(s)
        assert statements == [] and (s.session.dirty or s.session.new or s.session.deleted)
        s.session.rollback()


def test_full_owner_chain_rejects_cross_account_subject_identity_and_managed_owner(storage):
    s = storage
    with s.session.begin():
        for field, value in (
            ("account_id", uuid4()),
            ("identity_id", uuid4()),
            ("namespace_id", uuid4()),
            ("subject", "exactcasesubject"),
        ):
            c = replace(s.version.plan.context, **{field: value})
            version = replace(s.version, plan=replace(s.version.plan, context=c))
            with pytest.raises(CasdoorMembershipConflict):
                inspect(s, version=version)
        record = register(s)
        s.session.execute(
            sa.update(CasdoorManagedMembershipExtend)
            .where(CasdoorManagedMembershipExtend.id == str(record.membership_id))
            .values(identity_id=str(uuid4()))
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(CasdoorMembershipConflict):
            inspect(s)


def test_remote_unknown_no_grant_and_owner_requires_fence_without_release(storage):
    s = storage
    projection = ExternalMemberRolesProjection(UUID(s.workspace.id), UUID(s.account.id), RolesKnowledge.COMPLETE, ())
    with s.session.begin():
        assert inspect(s, backend=MembershipBackend.REMOTE).decision is OwnershipDecision.AUTHORIZATION_PENDING
        with pytest.raises(CasdoorMembershipConflict):
            new(s, backend=MembershipBackend.REMOTE)
        record = register(s, backend=MembershipBackend.REMOTE, remote=projection)
        owner = replace(projection, roles=(MemberRole("owner", True, "global_system_default", "owner"),))
        assert (
            inspect(s, backend=MembershipBackend.REMOTE, remote=owner).decision is OwnershipDecision.REQUEST_OWNER_FENCE
        )
        row = s.session.get(CasdoorManagedMembershipExtend, str(record.membership_id))
        assert row.ownership is CasdoorMembershipOwnership.MANAGED and row.ownership_epoch == 0
        assert s.session.get(TenantAccountJoin, str(record.join_id)).role is TenantAccountRole.NORMAL


def test_remote_snapshot_over_text_budget_has_no_membership_or_metadata_writes(storage):
    s = storage
    projection = ExternalMemberRolesProjection(
        UUID(s.workspace.id),
        UUID(s.account.id),
        RolesKnowledge.COMPLETE,
        (MemberRole("r", False, "", "", tuple(f"权限-{i}-" + "中" * 20 for i in range(500))),),
    )
    statements = []
    with s.session.begin():
        sa.event.listen(s.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        assert (
            inspect(s, backend=MembershipBackend.REMOTE, remote=projection).decision
            is OwnershipDecision.AUTHORIZATION_PENDING
        )
        with pytest.raises(CasdoorMembershipConflict):
            new(s, backend=MembershipBackend.REMOTE, remote=projection)
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
        assert count(s, TenantAccountJoin) == count(s, CasdoorManagedMembershipExtend) == 0


def test_actual_sql_constraints_and_outer_rollback(storage):
    s = storage
    with pytest.raises(IntegrityError), s.session.begin():
        record = register(s)
        row = s.session.get(CasdoorManagedMembershipExtend, str(record.membership_id))
        duplicate = CasdoorManagedMembershipExtend(
            **{
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
                if column.name not in ("id", "created_at", "updated_at")
            }
        )
        s.session.add(duplicate)
        s.session.flush()
    with s.session.begin():
        assert count(s, CasdoorManagedMembershipExtend) == count(s, TenantAccountJoin) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace_id", str(uuid4())),
        ("revision_id", str(uuid4())),
        ("desired_generation", -1),
        ("ownership_epoch", -1),
        ("source", "unrecognized"),
        ("ownership", "unrecognized"),
    ],
)
def test_actual_fk_enum_and_nonnegative_constraints_rollback_join_and_metadata(storage, field, value):
    s = storage
    with pytest.raises(IntegrityError), s.session.begin():
        record = register(s)
        row = s.session.get(CasdoorManagedMembershipExtend, str(record.membership_id))
        if field in ("source", "ownership"):
            # Bypass only ORM EnumText in this negative fixture to exercise the
            # actual SQLite CHECK constraint, not its earlier bind-time guard.
            s.session.execute(
                sa.text(f"UPDATE casdoor_managed_membership_extend SET {field}=:value WHERE id=:membership_id"),
                {"value": value, "membership_id": str(record.membership_id)},
            )
        else:
            setattr(row, field, value)
            s.session.flush()
    with s.session.begin():
        assert count(s, CasdoorManagedMembershipExtend) == count(s, TenantAccountJoin) == 0


def test_old_namespace_tombstone_never_creates_new_namespace_management(storage):
    s = storage
    with s.session.begin():
        old_namespace = CasdoorNamespaceExtend(
            integration_id=s.integration.id,
            expected_issuer=s.namespace.expected_issuer,
            organization="Previous",
            application="App",
            client_id="Client",
            core_fingerprint="c" * 64,
            lifecycle="archived",
        )
        s.session.add(old_namespace)
        s.session.flush()
        row = CasdoorManagedMembershipExtend(
            namespace_id=old_namespace.id,
            identity_id=str(uuid4()),
            account_id=s.account.id,
            workspace_id=s.workspace.id,
            join_id=None,
            ownership=CasdoorMembershipOwnership.RELEASED,
            source=CasdoorMembershipSource.MAPPING,
            revision_id=s.revision.id,
            desired_generation=0,
            last_applied_roles_json="{}",
            desired_roles_json="{}",
            baseline_json="{}",
            tombstone=True,
        )
        s.session.add(row)
        s.session.flush()
        assert inspect(s).decision is OwnershipDecision.PRESERVE_UNMANAGED
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        assert count(s, TenantAccountJoin) == 0


def test_revalidation_after_helper_flush_rejects_late_generation(storage):
    s = storage
    with s.session.begin():
        token, receipt = new(s)
        s.session.execute(
            sa.update(CasdoorIdentityExtend)
            .where(CasdoorIdentityExtend.id == s.identity.id)
            .values(sync_generation=2)
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)
        assert count(s, CasdoorManagedMembershipExtend) == 0


def test_registration_rechecks_invitation_marker_from_database_not_cached_join(storage):
    s = storage
    with s.session.begin():
        token, receipt = new(s)
        assert receipt.join.invited_by is None
        s.session.execute(
            sa.update(TenantAccountJoin)
            .where(TenantAccountJoin.id == receipt.join.id)
            .values(invited_by=s.account.id)
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)
        assert count(s, CasdoorManagedMembershipExtend) == 0


@pytest.mark.parametrize("role", ["owner", "admin", "editor"])
def test_registration_rejects_owner_or_unexpected_helper_role(storage, role):
    s = storage
    with s.session.begin():
        token, receipt = new(s, role=role)
        with pytest.raises(CasdoorMembershipConflict):
            s.repo.register_new(token, receipt)
        assert receipt.join.role == role and count(s, CasdoorManagedMembershipExtend) == 0


def test_clean_lock_order_and_no_inspection_dml_compile_both_dialects(storage):
    s = storage
    statements = []
    with s.session.begin():
        sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
        inspect(s)
        tables = [str(statement.get_final_froms()[0]) for statement in statements]
        assert tables == [
            "casdoor_integration_extend",
            "casdoor_namespace_extend",
            "casdoor_config_revision_extend",
            "accounts",
            "casdoor_identity_extend",
            "tenants",
            "tenant_account_joins",
            "casdoor_managed_membership_extend",
        ]
        for dialect in (postgresql.dialect(), mysql.dialect()):
            for statement in statements:
                sql = str(statement.compile(dialect=dialect))
                assert sql.startswith("SELECT")
                if "FROM casdoor_config_revision_extend" not in sql:
                    assert "FOR UPDATE" in sql
