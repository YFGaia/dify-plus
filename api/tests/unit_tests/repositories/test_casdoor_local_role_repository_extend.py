"""Actual SQLite UoW/CAS tests; synthetic internal plans are not login proof."""

import ast
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.local_roles import LocalRoleOutcome
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingIdentityContext
from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json, roles_fingerprint
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
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
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_local_role_repository_extend import CasdoorLocalRoleConflict, CasdoorLocalRoleRepository
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
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
        Integration,
        Namespace,
        Revision,
        Identity,
        History,
        Intent,
        InvitationAuthorityLifecycleExtend,
    ):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        integration = Integration(enabled=True)
        account = Account(name="Synthetic", email="local-role@example.test", status=AccountStatus.ACTIVE)
        workspace = Tenant(name="Synthetic existing workspace")
        session.add_all([integration, account, workspace])
        session.flush()
        namespace = Namespace(
            integration_id=integration.id,
            expected_issuer="https://synthetic.example.test",
            organization="Org",
            application="App",
            client_id="Client",
            core_fingerprint="b" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url=namespace.expected_issuer,
            backend_api_url=namespace.expected_issuer,
            expected_issuer=namespace.expected_issuer,
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
            sync_generation=1,
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        join = TenantAccountJoin(account_id=account.id, tenant_id=workspace.id, role=TenantAccountRole.NORMAL)
        session.add_all([revision, identity, join])
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
            "Subject",
        )
        target = DesiredWorkspaceTarget(UUID(workspace.id), "admin", "admin", CasdoorDecisionReason.ROLE_MAPPING, ())
        version = GenerationPlanVersion(DesiredWorkspacePlan(context, (target,)), 0, 1)
        observation = MembershipObservation(
            UUID(workspace.id), UUID(account.id), UUID(join.id), join.role, MembershipBackend.LOCAL
        )
        baseline = role_baseline_json(observation)
        history = History(
            namespace_id=namespace.id,
            identity_id=identity.id,
            account_id=account.id,
            workspace_id=workspace.id,
            join_id=join.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=7,
            source=CasdoorMembershipSource.ADOPT,
            desired_generation=1,
            revision_id=revision.id,
            last_applied_roles_json=baseline,
            last_applied_fingerprint=roles_fingerprint(observation),
            desired_roles_json="{}",
            baseline_json=baseline,
            finalization=CasdoorFinalizationState.PENDING,
            tombstone=False,
        )
        session.add(history)
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
            join=join,
            history=history,
            version=version,
            target=target,
            repo=CasdoorLocalRoleRepository(session),
        )
    engine.dispose()


def target(s, role, *, fallback=False):
    s.target = replace(
        s.target,
        target_role=role,
        builtin_id=role,
        reason=CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK if fallback else CasdoorDecisionReason.ROLE_MAPPING,
    )
    s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,)))


def apply(s):
    return s.repo.apply(s.repo.prepare(s.version, s.target))


def columns(s):
    return s.session.execute(
        sa.select(
            TenantAccountJoin.role,
            History.last_applied_roles_json,
            History.last_applied_fingerprint,
            History.baseline_json,
            History.source,
            History.ownership_epoch,
            History.finalization,
            History.desired_generation,
        ).join(History, History.join_id == TenantAccountJoin.id)
    ).one()


def test_upgrade_downgrade_and_default_admin_loss_with_stable_reentry(storage):
    s = storage
    baseline = s.history.baseline_json
    for role, fallback in [("admin", False), ("editor", False), ("normal", True)]:
        target(s, role, fallback=fallback)
        with s.session.begin():
            receipt = apply(s)
            assert receipt.applied and receipt.role_changed
            assert receipt.current_role.value == role
            row = columns(s)
            assert row.role.value == json.loads(row.last_applied_roles_json)["join_role"] == role
            assert row.last_applied_fingerprint == hashlib.sha256(row.last_applied_roles_json.encode()).hexdigest()
            assert (row.baseline_json, row.source, row.ownership_epoch, row.finalization) == (
                baseline,
                CasdoorMembershipSource.ADOPT,
                7,
                CasdoorFinalizationState.PENDING,
            )
            again = apply(s)
            assert again.outcome is LocalRoleOutcome.NOOP and again.applied
            assert not again.role_changed and not again.metadata_changed


def test_outer_rollback_restores_role_and_all_metadata(storage):
    s = storage
    with s.session.begin():
        before = columns(s)
    with pytest.raises(RuntimeError, match="later"), s.session.begin():
        assert apply(s).role_changed
        raise RuntimeError("later")
    with s.session.begin():
        assert columns(s) == before


@pytest.mark.parametrize(
    "kind", ["owner", "dataset", "unmanaged", "released", "override", "tombstone", "drift", "stalejoin"]
)
def test_protected_and_drift_scopes_never_mutate(storage, kind):
    s = storage
    with s.session.begin():
        if kind in ("owner", "dataset"):
            s.join.role = TenantAccountRole.OWNER if kind == "owner" else TenantAccountRole.DATASET_OPERATOR
        elif kind == "unmanaged":
            s.session.delete(s.history)
        elif kind in ("released", "override"):
            s.history.ownership = (
                CasdoorMembershipOwnership.RELEASED if kind == "released" else CasdoorMembershipOwnership.LOCAL_OVERRIDE
            )
        elif kind == "tombstone":
            s.history.tombstone = True
        elif kind == "drift":
            s.history.last_applied_fingerprint = "f" * 64
        else:
            s.history.join_id = str(uuid4())
    with s.session.begin():
        before_role = s.session.scalar(sa.select(TenantAccountJoin.role))
        receipt = apply(s)
        assert not receipt.applied and not receipt.role_changed and not receipt.metadata_changed
        assert receipt.outcome in (LocalRoleOutcome.PRESERVED, LocalRoleOutcome.PENDING)
        assert s.session.scalar(sa.select(TenantAccountJoin.role)) == before_role


@pytest.mark.parametrize(
    "field,value",
    [
        ("sync_generation", 2),
        ("fence_epoch", 1),
        ("enabled", False),
        ("active_revision_id", None),
        ("subject", "Changed"),
        ("config_digest", "c" * 64),
        ("account_id", str(uuid4())),
        ("status", AccountStatus.BANNED),
    ],
)
def test_exact_current_owner_guard_rejects_stale_db(storage, field, value):
    s = storage
    model = (
        s.identity
        if field in ("sync_generation", "subject", "account_id")
        else s.namespace
        if field == "fence_epoch"
        else s.revision
        if field == "config_digest"
        else s.account
        if field == "status"
        else s.integration
    )
    with s.session.begin():
        s.session.execute(sa.update(type(model)).where(type(model).id == model.id).values({field: value}))
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)


@pytest.mark.parametrize("change", ["epoch", "join", "generation", "history"])
def test_one_use_preparation_rechecks_actual_locked_columns(storage, change):
    s = storage
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        token = s.repo.prepare(s.version, s.target)
        if change == "join":
            s.session.execute(sa.update(TenantAccountJoin).values(role=TenantAccountRole.EDITOR))
        elif change == "generation":
            s.session.execute(sa.update(Identity).values(sync_generation=2))
        elif change == "history":
            s.session.execute(sa.delete(History))
        else:
            s.session.execute(sa.update(History).values(ownership_epoch=8))
        s.repo.apply(token)
    with s.session.begin(), pytest.raises(CasdoorLocalRoleConflict):
        s.repo.apply(token)


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted", "nested", "no_root", "inactive", "rbac"])
def test_clean_root_preconditions_reject_before_sql(storage, monkeypatch, pending):
    s = storage
    statements = []
    if pending != "no_root":
        s.session.begin()
    if pending == "new":
        s.session.add(Account(name="Pending", email="pending@example.test"))
    elif pending == "dirty":
        s.account.name = "Pending"
    elif pending == "deleted":
        s.session.delete(s.account)
    elif pending == "nested":
        s.session.begin_nested()
    elif pending == "inactive":
        account_id = s.account.id
        s.session.expunge(s.account)
        duplicate = Account(name="Duplicate", email="duplicate@example.test")
        duplicate.id = account_id
        s.session.add(duplicate)
        with pytest.raises(IntegrityError):
            s.session.flush()
    elif pending == "rbac":
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises((CasdoorLocalRoleConflict, RuntimeError)):
        s.repo.prepare(s.version, s.target)
    assert not statements
    s.session.rollback()


@pytest.mark.parametrize("kind", list(CasdoorIntentKind))
@pytest.mark.parametrize("association", ["scope", "workspace_null", "membership"])
def test_any_related_intent_even_applied_confirmed_blocks_apply(storage, kind, association):
    s = storage
    with s.session.begin():
        s.session.add(
            Intent(
                namespace_id=s.namespace.id,
                identity_id=s.identity.id,
                account_id=s.account.id if association != "membership" else str(uuid4()),
                workspace_id=s.workspace.id if association == "scope" else None,
                membership_id=s.history.id if association == "membership" else None,
                revision_id=s.revision.id,
                generation=1,
                ownership_epoch=7,
                fence_epoch=0,
                kind=kind,
                scope_digest="a" * 64,
                idempotency_key="b" * 64,
                desired_json="{}",
                operation_state=CasdoorOperationState.APPLIED,
                termination_state=CasdoorTerminationState.CONFIRMED,
            )
        )
    with s.session.begin():
        receipt = apply(s)
        assert receipt.applied == (kind is CasdoorIntentKind.PROFILE_AVATAR)
        if kind is not CasdoorIntentKind.PROFILE_AVATAR:
            assert receipt.outcome is LocalRoleOutcome.PENDING
            assert s.session.scalar(sa.select(TenantAccountJoin.role)) is TenantAccountRole.NORMAL


@pytest.mark.parametrize("field", ["last_applied_roles_json", "desired_roles_json", "baseline_json"])
def test_actual_byte_length_guard_rejects_unicode_before_text_materialization(storage, field):
    s = storage
    with s.session.begin():
        setattr(s.history, field, "汉" * 22000)  # Characters fit; UTF-8 bytes exceed TEXT contract.
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)
    assert any("CAST(" in stmt and "BLOB" in stmt for stmt in statements)
    assert not any("SELECT casdoor_managed_membership_extend.namespace_id" in stmt for stmt in statements)
    assert not any(stmt.startswith("UPDATE") for stmt in statements)


def test_metadata_only_new_generation_preserves_source_epoch_finalization(storage):
    s = storage
    with s.session.begin():
        apply(s)
    with s.session.begin():
        s.identity.sync_generation = 2
    s.version = replace(s.version, generation=2)
    with s.session.begin():
        result = apply(s)
        assert result.applied and not result.role_changed and result.metadata_changed
        assert columns(s).desired_generation == 2
        assert apply(s).outcome is LocalRoleOutcome.NOOP


def test_cas_failure_after_actual_role_update_rolls_back_entire_uow(storage, monkeypatch):
    s = storage
    execute = s.session.execute

    def fail_metadata(statement, *args, **kwargs):
        if isinstance(statement, sa.sql.dml.Update) and statement.table.name == History.__tablename__:
            return SimpleNamespace(rowcount=0)
        return execute(statement, *args, **kwargs)

    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        token = s.repo.prepare(s.version, s.target)
        monkeypatch.setattr(s.session, "execute", fail_metadata)
        s.repo.apply(token)
    monkeypatch.setattr(s.session, "execute", execute)
    with s.session.begin():
        assert columns(s).role is TenantAccountRole.NORMAL
        assert columns(s).desired_generation == 1


def test_sql_shape_has_ordered_locks_cas_and_no_hidden_side_effect_owner(storage):
    s = storage
    statements = []
    sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
    with s.session.begin():
        apply(s)
    selects = [stmt for stmt in statements if isinstance(stmt, sa.sql.Select) and stmt._for_update_arg is not None]
    for dialect in (postgresql.dialect(), mysql.dialect()):
        assert all("FOR UPDATE" in str(stmt.compile(dialect=dialect)) for stmt in selects)
        assert "octet_length" in str(sa.select(sa.func.octet_length(History.baseline_json)).compile(dialect=dialect))
    updates = [str(stmt) for stmt in statements if isinstance(stmt, sa.sql.dml.Update)]
    assert len(updates) == 2
    assert "ownership_epoch" in updates[1] and "last_applied_fingerprint" in updates[1]
    assignments = updates[1].split(" WHERE ")[0]
    assert "baseline_json=" not in assignments and "source=" not in assignments and "finalization=" not in assignments
    repo_tree = ast.parse(
        Path(__file__).parents[3].joinpath("repositories/casdoor_local_role_repository_extend.py").read_text()
    )
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"commit", "rollback", "delay", "send_request", "replace"}
        for n in ast.walk(repo_tree)
    )


@pytest.mark.parametrize("bad", ["builtin", "owner", "duplicate", "role_bool", "generation_bool", "epoch_negative"])
def test_invalid_bounded_plan_fails_before_sql(storage, bad):
    s = storage
    if bad == "builtin":
        s.target = replace(s.target, builtin_id="caller-tag")
    elif bad == "owner":
        s.target = replace(s.target, target_role="owner", builtin_id="owner")
    elif bad == "role_bool":
        s.target = replace(s.target, target_role=True)
    s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,) * (2 if bad == "duplicate" else 1)))
    if bad == "generation_bool":
        s.version = replace(s.version, generation=True)
    elif bad == "epoch_negative":
        s.version = replace(s.version, fence_epoch=-1)
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)
    assert not statements or statements == ["BEGIN"]


def test_absent_join_is_preserved_and_never_created(storage):
    s = storage
    with s.session.begin():
        s.session.delete(s.history)
        s.session.delete(s.join)
    with s.session.begin():
        result = apply(s)
        assert result.outcome is LocalRoleOutcome.PRESERVED and not result.applied
        assert s.session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0


@pytest.mark.parametrize("multiple", [False, True])
def test_old_or_second_namespace_history_fails_closed(storage, multiple):
    s = storage
    with s.session.begin():
        namespace = Namespace(
            integration_id=s.integration.id,
            expected_issuer=s.namespace.expected_issuer,
            organization="Org",
            application="App",
            client_id="Client",
            core_fingerprint="c" * 64,
        )
        s.session.add(namespace)
        s.session.flush()
        if multiple:
            copy = History(
                namespace_id=namespace.id,
                identity_id=str(uuid4()),
                account_id=s.account.id,
                workspace_id=s.workspace.id,
                join_id=s.join.id,
                ownership=CasdoorMembershipOwnership.RELEASED,
                source=CasdoorMembershipSource.ADOPT,
                desired_generation=1,
                revision_id=s.revision.id,
                last_applied_roles_json="{}",
                last_applied_fingerprint="a" * 64,
                desired_roles_json="{}",
                baseline_json="{}",
            )
            s.session.add(copy)
        else:
            s.history.namespace_id = namespace.id
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)
    with s.session.begin():
        assert s.session.scalar(sa.select(TenantAccountJoin.role)) is TenantAccountRole.NORMAL


def test_namespace_bound_is_enforced_before_member_mutation(storage, monkeypatch):
    s = storage
    monkeypatch.setattr("repositories.casdoor_local_role_repository_extend.MAX_NAMESPACES", 0)
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)


def test_postwrite_actual_readback_mismatch_rolls_back_role(storage):
    s = storage
    with s.session.begin():
        s.session.execute(
            sa.text("""CREATE TRIGGER local_role_guard AFTER UPDATE OF role ON tenant_account_joins
            WHEN NEW.role = 'admin' BEGIN
            UPDATE tenant_account_joins SET role = 'editor' WHERE id = NEW.id;
            END""")
        )
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)
    with s.session.begin():
        assert columns(s).role is TenantAccountRole.NORMAL


def test_exact_text_boundary_remains_byte_identical(storage):
    s = storage
    original = "汉" * 21845  # Exactly 65,535 bytes, retained initial historical evidence.
    with s.session.begin():
        s.history.baseline_json = original
    with s.session.begin():
        assert apply(s).applied
        assert columns(s).baseline_json == original


def test_flushed_actual_new_registration_can_apply_without_finalizing(storage):
    from repositories.casdoor_membership_repository_extend import CasdoorMembershipRepository
    from services.account_service import TenantService

    s = storage
    with s.session.begin():
        s.session.delete(s.history)
        s.session.delete(s.join)
    with s.session.begin():
        members = CasdoorMembershipRepository(s.session)
        token = members.prepare_new(s.version, s.target, backend=MembershipBackend.LOCAL)
        member = TenantService.persist_tenant_member(s.workspace, s.account, s.session, "admin")
        managed = members.register_new(token, member)
        assert not s.session.new and not s.session.dirty
        receipt = apply(s)
        assert receipt.applied and not receipt.role_changed and receipt.membership_id == managed.membership_id
        assert s.session.scalar(sa.select(History.finalization)) is CasdoorFinalizationState.PENDING


def test_new_immutable_revision_advances_only_current_version_metadata(storage):
    s = storage
    with s.session.begin():
        apply(s)
    with s.session.begin():
        row = Revision(
            integration_id=s.integration.id,
            namespace_id=s.namespace.id,
            revision_number=2,
            config_digest="d" * 64,
            browser_frontend_url=s.namespace.expected_issuer,
            backend_api_url=s.namespace.expected_issuer,
            expected_issuer=s.namespace.expected_issuer,
            organization="Org",
            application="App",
            client_id="Client",
            button_text="Casdoor",
            default_workspace_id=s.workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        s.session.add(row)
        s.session.flush()
        s.integration.active_revision_id = row.id
        s.identity.sync_generation = 2
        new_id = row.id
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)
    context = replace(s.version.plan.context, revision_id=UUID(new_id), config_digest="d" * 64)
    s.version = replace(s.version, generation=2, plan=replace(s.version.plan, context=context))
    with s.session.begin():
        receipt = apply(s)
        assert receipt.applied and not receipt.role_changed and receipt.metadata_changed
        assert s.session.scalar(sa.select(History.revision_id)) == new_id
        assert columns(s).source is CasdoorMembershipSource.ADOPT


def test_fallback_uses_saved_fixed_default_not_another_workspace(storage):
    s = storage
    with s.session.begin():
        second = Tenant(name="Different synthetic workspace")
        s.session.add(second)
        s.session.flush()
        s.session.execute(sa.update(Revision).values(default_workspace_id=second.id))
    target(s, "normal", fallback=True)
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        apply(s)


def test_default_admin_loses_mapping_to_normal_with_actual_fingerprint(storage):
    s = storage
    with s.session.begin():
        apply(s)
    target(s, "normal", fallback=True)
    with s.session.begin():
        receipt = apply(s)
        assert receipt.prior_role is TenantAccountRole.ADMIN
        assert receipt.current_role is TenantAccountRole.NORMAL and receipt.role_changed
        assert receipt.finalization is CasdoorFinalizationState.PENDING


def test_matching_dataset_operator_fingerprint_cannot_enter_local_role_owner(storage):
    s = storage
    observation = MembershipObservation(
        UUID(s.workspace.id),
        UUID(s.account.id),
        UUID(s.join.id),
        TenantAccountRole.DATASET_OPERATOR,
        MembershipBackend.LOCAL,
    )
    with s.session.begin():
        s.join.role = TenantAccountRole.DATASET_OPERATOR
        s.history.last_applied_roles_json = role_baseline_json(observation)
        s.history.last_applied_fingerprint = roles_fingerprint(observation)
    with s.session.begin():
        receipt = apply(s)
        assert receipt.outcome is LocalRoleOutcome.PRESERVED and not receipt.applied
        assert columns(s).role is TenantAccountRole.DATASET_OPERATOR


def test_stable_noop_performs_no_dml(storage):
    s = storage
    with s.session.begin():
        apply(s)
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with s.session.begin():
        assert apply(s).outcome is LocalRoleOutcome.NOOP
    assert not any(stmt.startswith(("UPDATE", "INSERT", "DELETE")) for stmt in statements)
