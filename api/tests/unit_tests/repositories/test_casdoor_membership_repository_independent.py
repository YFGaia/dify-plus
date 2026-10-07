"""Independent SQLite and SQL-shape checks for I11-B managed membership."""

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingIdentityContext
from core.casdoor.ownership import (
    ExternalMemberRolesProjection,
    MemberRole,
    MembershipBackend,
    OwnershipDecision,
    RolesKnowledge,
)
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorManagedMembershipExtend,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
)
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_membership_repository_extend import (
    CasdoorMembershipConflict,
    CasdoorMembershipRepository,
    NewMembershipPreparation,
)
from services.account_service import TenantService
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import Session


@pytest.fixture
def world():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def sqlite_setup(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def sqlite_begin(connection):
        connection.exec_driver_sql("BEGIN")

    tables = (
        Account,
        Tenant,
        TenantAccountJoin,
        CasdoorIntegrationExtend,
        CasdoorNamespaceExtend,
        CasdoorConfigRevisionExtend,
        CasdoorIdentityExtend,
        CasdoorManagedMembershipExtend,
    )
    for model in tables:
        model.__table__.create(engine)
    session = Session(engine, expire_on_commit=False)
    with session.begin():
        integration = CasdoorIntegrationExtend(enabled=True)
        account = Account(name="Offline I11B", email="i11b@example.invalid", status=AccountStatus.ACTIVE)
        workspace = Tenant(name="Pre-existing workspace")
        other_workspace = Tenant(name="Legacy membership workspace")
        session.add_all((integration, account, workspace, other_workspace))
        session.flush()
        namespace = CasdoorNamespaceExtend(
            integration_id=integration.id,
            expected_issuer="https://idp.example.invalid",
            organization="SyntheticOrg",
            application="SyntheticApp",
            client_id="synthetic-client",
            core_fingerprint="c" * 64,
            lifecycle=CasdoorNamespaceLifecycle.ACTIVE,
            fence_epoch=0,
        )
        session.add(namespace)
        session.flush()
        revision = CasdoorConfigRevisionExtend(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="d" * 64,
            browser_frontend_url="https://idp.example.invalid",
            backend_api_url="https://idp.example.invalid",
            expected_issuer=namespace.expected_issuer,
            organization="SyntheticOrg",
            application="SyntheticApp",
            client_id="synthetic-client",
            button_text="Synthetic SSO",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        identity = CasdoorIdentityExtend(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=namespace.expected_issuer,
            organization="SyntheticOrg",
            subject="StableCaseSensitiveSubject",
            sync_generation=4,
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        session.add_all((revision, identity))
        session.flush()
        integration.active_revision_id = revision.id
        context = MappingIdentityContext(
            UUID(integration.id),
            UUID(revision.id),
            UUID(namespace.id),
            UUID(identity.id),
            UUID(account.id),
            "d" * 64,
            namespace.expected_issuer,
            "SyntheticOrg",
            "SyntheticApp",
            "synthetic-client",
            "StableCaseSensitiveSubject",
        )
        target = DesiredWorkspaceTarget(
            UUID(workspace.id), "normal", "builtin-normal", CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK, ()
        )
        version = GenerationPlanVersion(DesiredWorkspacePlan(context, (target,)), 0, 4)
    value = SimpleNamespace(
        engine=engine,
        session=session,
        integration=integration,
        account=account,
        workspace=workspace,
        other_workspace=other_workspace,
        namespace=namespace,
        revision=revision,
        identity=identity,
        target=target,
        version=version,
        repository=CasdoorMembershipRepository(session),
    )
    try:
        yield value
    finally:
        session.close()
        engine.dispose()


def make_join_and_receipt(world, role="normal"):
    return TenantService.persist_tenant_member(world.workspace, world.account, world.session, role)


def test_clean_local_fallback_registers_exact_pending_scope_and_rolls_back_as_unit(world):
    with pytest.raises(RuntimeError, match="abort unit"), world.session.begin():
        token = world.repository.prepare_new(world.version, world.target, backend=MembershipBackend.LOCAL)
        receipt = make_join_and_receipt(world, "normal")
        recorded = world.repository.register_new(token, receipt)
        assert recorded.ownership is CasdoorMembershipOwnership.MANAGED
        assert recorded.source is CasdoorMembershipSource.FALLBACK
        assert recorded.namespace_id == world.version.plan.context.namespace_id
        assert recorded.identity_id == world.version.identity_id
        assert recorded.account_id == world.version.plan.context.account_id
        assert recorded.workspace_id == world.target.workspace_id
        assert recorded.join_id == UUID(receipt.join.id)
        assert recorded.desired_generation == world.version.generation
        assert recorded.ownership_epoch == 0
        assert json.loads(recorded.baseline_json)["join_role"] == "normal"
        row = world.session.get(CasdoorManagedMembershipExtend, str(recorded.membership_id))
        assert row.finalization is CasdoorFinalizationState.PENDING
        assert row.baseline_json == row.last_applied_roles_json
        inspection = world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)
        assert inspection.decision is (OwnershipDecision.MANAGED_CURRENT)
        raise RuntimeError("abort unit")
    with world.session.begin():
        assert world.session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0
        assert world.session.scalar(sa.select(sa.func.count()).select_from(CasdoorManagedMembershipExtend)) == 0


def test_remote_backend_requires_scoped_complete_projection_and_records_normal_join(world):
    projection = ExternalMemberRolesProjection(
        UUID(world.workspace.id),
        UUID(world.account.id),
        RolesKnowledge.COMPLETE,
        (),
    )
    with world.session.begin():
        token = world.repository.prepare_new(
            world.version, world.target, backend=MembershipBackend.REMOTE, remote=projection
        )
        receipt = make_join_and_receipt(world, "normal")
        snapshot = world.repository.register_new(token, receipt)
        assert json.loads(snapshot.baseline_json)["roles"] == []
        assert snapshot.source is CasdoorMembershipSource.FALLBACK


@pytest.mark.parametrize("state_kind", ["new", "dirty", "deleted"])
def test_pending_session_state_fails_before_repository_sql_and_is_retained(world, state_kind):
    seen = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        seen.append(statement)

    with world.session.begin():
        if state_kind == "new":
            obj = Account(name="not flushed", email="pending@example.invalid")
            world.session.add(obj)
        elif state_kind == "dirty":
            obj = world.account
            obj.name = "unsaved caller edit"
        else:
            obj = world.account
            world.session.delete(obj)
        sa.event.listen(world.engine, "before_cursor_execute", observe)
        try:
            with pytest.raises(CasdoorMembershipConflict):
                world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)
        finally:
            sa.event.remove(world.engine, "before_cursor_execute", observe)
        assert seen == []
        if state_kind == "new":
            assert obj in world.session.new
        elif state_kind == "dirty":
            assert obj in world.session.dirty
        else:
            assert obj in world.session.deleted


def test_savepoint_and_missing_root_transaction_are_rejected(world):
    with pytest.raises(RuntimeError, match="active caller-owned root"):
        world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)
    with world.session.begin():
        with world.session.begin_nested():
            with pytest.raises(RuntimeError, match="active caller-owned root"):
                world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)


def test_parent_and_scope_reads_lock_in_contractual_order(world):
    statement_seen = []

    def observe(_conn, clause, _multiparams, _params, _options):
        if isinstance(clause, sa.sql.Select):
            statement_seen.append(clause)

    with world.session.begin():
        sa.event.listen(world.engine, "before_execute", observe)
        try:
            world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)
        finally:
            sa.event.remove(world.engine, "before_execute", observe)
    tables_seen = []
    for statement in statement_seen:
        for table in (
            "casdoor_integration_extend",
            "casdoor_namespace_extend",
            "casdoor_config_revision_extend",
            "accounts",
            "casdoor_identity_extend",
            "tenants",
            "tenant_account_joins",
            "casdoor_managed_membership_extend",
        ):
            compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
            if f"from {table}" in compiled and table not in tables_seen:
                tables_seen.append(table)
    assert tables_seen == [
        "casdoor_integration_extend",
        "casdoor_namespace_extend",
        "casdoor_config_revision_extend",
        "accounts",
        "casdoor_identity_extend",
        "tenants",
        "tenant_account_joins",
        "casdoor_managed_membership_extend",
    ]
    # SQLite executes these same clauses but omits row locks. Compile the
    # repository's captured SELECTs to confirm every mutable scope read locks.
    mutable_tables = {
        "casdoor_integration_extend",
        "casdoor_namespace_extend",
        "accounts",
        "casdoor_identity_extend",
        "tenants",
        "tenant_account_joins",
        "casdoor_managed_membership_extend",
    }
    for statement in statement_seen:
        source_sql = str(statement.compile(dialect=postgresql.dialect())).lower()
        table = next((name for name in mutable_tables if f"from {name}" in source_sql), None)
        if table is not None:
            assert "for update" in source_sql, source_sql
        for dialect in (postgresql.dialect(), mysql.dialect()):
            compiled = str(statement.compile(dialect=dialect)).lower()
            if table is not None:
                assert "for update" in compiled, compiled


def test_stale_namespace_epoch_or_active_revision_cannot_inspect(world):
    with world.session.begin():
        world.namespace.fence_epoch = 1
    world.session.expire_all()
    with world.session.begin():
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)


@pytest.mark.parametrize(
    "owner_change", ["account_banned", "account_closed", "identity_generation", "workspace_archived"]
)
def test_unavailable_account_workspace_or_stale_generation_is_rejected(world, owner_change):
    with world.session.begin():
        if owner_change == "account_banned":
            world.account.status = AccountStatus.BANNED
        elif owner_change == "account_closed":
            world.account.status = AccountStatus.CLOSED
        elif owner_change == "identity_generation":
            world.identity.sync_generation = 5
        else:
            world.workspace.status = TenantStatus.ARCHIVE
    world.session.expire_all()
    with world.session.begin():
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)
    with world.session.begin():
        world.namespace.fence_epoch = 0
        world.integration.enabled = False
    world.session.expire_all()
    with world.session.begin():
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)


def test_existing_invitation_and_existing_role_are_not_claimed(world):
    with world.session.begin():
        join = TenantAccountJoin(
            tenant_id=world.workspace.id,
            account_id=world.account.id,
            role=TenantAccountRole.ADMIN,
            invited_by=world.account.id,
        )
        world.session.add(join)
        world.session.flush()
        decision = world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)
        assert decision.decision is OwnershipDecision.PRESERVE_UNMANAGED
        assert decision.invited_by == UUID(world.account.id)
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.prepare_new(world.version, world.target, backend=MembershipBackend.LOCAL)
        assert join.role is TenantAccountRole.ADMIN
        assert world.session.scalar(sa.select(sa.func.count()).select_from(CasdoorManagedMembershipExtend)) == 0


def test_historical_tombstone_from_another_namespace_blocks_rejoin(world):
    with world.session.begin():
        old_namespace = CasdoorNamespaceExtend(
            integration_id=world.integration.id,
            expected_issuer="https://old.example.invalid",
            organization="OldOrg",
            application="OldApp",
            client_id="old-client",
            core_fingerprint="f" * 64,
        )
        world.session.add(old_namespace)
        world.session.flush()
        old_revision = CasdoorConfigRevisionExtend(
            integration_id=world.integration.id,
            namespace_id=old_namespace.id,
            revision_number=2,
            config_digest="e" * 64,
            browser_frontend_url="https://old.example.invalid",
            backend_api_url="https://old.example.invalid",
            expected_issuer=old_namespace.expected_issuer,
            organization="OldOrg",
            application="OldApp",
            client_id="old-client",
            button_text="Old SSO",
            default_workspace_id=world.workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        world.session.add(old_revision)
        world.session.flush()
        row = CasdoorManagedMembershipExtend(
            namespace_id=old_namespace.id,
            identity_id=world.identity.id,
            account_id=world.account.id,
            workspace_id=world.workspace.id,
            join_id=None,
            ownership=CasdoorMembershipOwnership.RELEASED,
            ownership_epoch=1,
            source=CasdoorMembershipSource.MAPPING,
            desired_generation=3,
            revision_id=old_revision.id,
            last_applied_roles_json="{}",
            last_applied_fingerprint=None,
            desired_roles_json="{}",
            baseline_json="{}",
            tombstone=True,
        )
        world.session.add(row)
        world.session.flush()
        inspection = world.repository.inspect(world.version, UUID(world.workspace.id), backend=MembershipBackend.LOCAL)
        assert inspection.decision is (OwnershipDecision.PRESERVE_UNMANAGED)
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.prepare_new(world.version, world.target, backend=MembershipBackend.LOCAL)


def test_legacy_join_after_prepare_false_receipt_must_escape_and_rollback_role_change(world):
    with world.session.begin():
        legacy = TenantAccountJoin(
            tenant_id=world.other_workspace.id, account_id=world.account.id, role=TenantAccountRole.ADMIN
        )
        world.session.add(legacy)
    with pytest.raises(CasdoorMembershipConflict), world.session.begin():
        token = world.repository.prepare_new(world.version, world.target, backend=MembershipBackend.LOCAL)
        receipt = TenantService.persist_tenant_member(world.other_workspace, world.account, world.session, "normal")
        assert receipt.membership_created is False
        assert legacy.role is TenantAccountRole.NORMAL
        world.repository.register_new(token, receipt)
    with world.session.begin():
        restored = world.session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.tenant_id == world.other_workspace.id,
                TenantAccountJoin.account_id == world.account.id,
            )
        )
        assert restored.role is TenantAccountRole.ADMIN
        assert world.session.scalar(sa.select(sa.func.count()).select_from(CasdoorManagedMembershipExtend)) == 0


def test_receipt_and_registration_authority_are_bound_to_registry_and_exact_transaction(world):
    with world.session.begin():
        token = world.repository.prepare_new(world.version, world.target, backend=MembershipBackend.LOCAL)
        copied = NewMembershipPreparation(token.version, token.target, token.backend, token.remote, token.transaction)
        other_repository = CasdoorMembershipRepository(world.session)
        with pytest.raises(CasdoorMembershipConflict):
            other_repository.register_new(copied, None)
        receipt = make_join_and_receipt(world)
        assert receipt.membership_created is True
        assert sa.inspect(receipt.join).session is world.session
        assert sa.inspect(receipt.join).persistent is True
        forged = SimpleNamespace(join=receipt.join, membership_created=True)
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.register_new(token, forged)
        # The failed consume burns this token; type/fields alone are not proof.
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.register_new(token, receipt)
        assert world.session.scalar(sa.select(sa.func.count()).select_from(CasdoorManagedMembershipExtend)) == 0


def test_complete_remote_owner_stops_before_register_without_claiming_unmanaged_record(world):
    owner = ExternalMemberRolesProjection(
        UUID(world.workspace.id),
        UUID(world.account.id),
        RolesKnowledge.COMPLETE,
        (MemberRole("remote-owner", True, "global_system_default", "owner"),),
    )
    with world.session.begin():
        with pytest.raises(CasdoorMembershipConflict):
            world.repository.prepare_new(world.version, world.target, backend=MembershipBackend.REMOTE, remote=owner)
        assert world.session.scalar(sa.select(sa.func.count()).select_from(CasdoorManagedMembershipExtend)) == 0
