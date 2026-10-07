"""Real SQLite owners; no claim of caller admission/lease or live DB lock proof."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.local_roles import LocalRoleOutcome
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingIdentityContext
from core.casdoor.ownership import OwnershipDecision
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.account_money_extend import AccountMoneyExtend as Quota
from models.casdoor_extend import CasdoorFinalizationState, CasdoorIntentKind, CasdoorMembershipOwnership
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_generation_repository_extend import CasdoorGenerationConflict
from repositories.casdoor_local_role_repository_extend import CasdoorLocalRoleConflict, CasdoorLocalRoleRepository
from repositories.casdoor_membership_repository_extend import CasdoorMembershipConflict, CasdoorMembershipRepository
from services.account_service import TenantService
from services.casdoor_local_membership_service_extend import (
    CasdoorLocalMembershipConflict,
    CasdoorLocalMembershipService,
    RequiredIntentBarrier,
)
from sqlalchemy.exc import IntegrityError
from test_casdoor_local_role_repository_extend import storage as local_storage_fixture
from test_casdoor_login_account_service_extend import counts, persist, prepare
from test_casdoor_login_account_service_extend import env as login_env_fixture
from test_casdoor_required_intent_repository_extend import intent, namespace

login_env = login_env_fixture
local_storage = local_storage_fixture


@pytest.fixture
def joined_env(login_env, monkeypatch):
    session = login_env[0]
    engine = session.get_bind()
    for model in (Tenant, TenantAccountJoin, History, Intent):
        model.__table__.create(engine)

    # Explicit SQLite root BEGIN is essential: B2A identity bind uses savepoint.
    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    with session.begin():
        spaces = [Tenant(name="First"), Tenant(name="Second")]
        spaces[0].id = str(UUID(int=100))
        spaces[1].id = str(UUID(int=200))
        session.add_all(spaces)
        from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision

        session.execute(sa.update(Revision).values(default_workspace_id=spaces[0].id))
    yield login_env, spaces


def new_plan(env, result, spaces):
    c = env[1]
    context = MappingIdentityContext(
        c.integration_id,
        c.revision_id,
        c.namespace_id,
        result.identity_id,
        result.account_id,
        c.config_digest,
        c.issuer,
        c.organization,
        c.application,
        c.client_id,
        c.subject,
    )
    return DesiredWorkspacePlan(
        context,
        tuple(
            DesiredWorkspaceTarget(UUID(space.id), role, role, CasdoorDecisionReason.ROLE_MAPPING, ())
            for space, role in zip(spaces, ("admin", "editor"), strict=True)
        ),
    )


def run(session, plan, generation=0):
    return CasdoorLocalMembershipService(session).persist_local_memberships(
        plan, expected_fence_epoch=0, expected_generation=generation
    )


def prelock(env, spaces, prepared):
    """Fixture exercises DB ordering; this is not a distributed lease fixture."""
    from models.casdoor_extend import CasdoorIntegrationExtend as Integration

    session, c, *_ = env
    session.execute(sa.select(Integration.id).where(Integration.id == str(c.integration_id)).with_for_update())
    session.execute(sa.select(Namespace.id).order_by(Namespace.id).with_for_update())
    session.execute(sa.select(Account.id).where(Account.id == str(prepared.account_id)).with_for_update())
    session.execute(sa.select(Identity.id).where(Identity.account_id == str(prepared.account_id)).with_for_update())
    session.execute(
        sa.select(Tenant.id).where(Tenant.id.in_([x.id for x in spaces])).order_by(Tenant.id).with_for_update()
    )


def test_real_b2a_two_sorted_spaces_one_generation_and_uncommitted_result(joined_env):
    env, spaces = joined_env
    session = env[0]
    prepared = prepare(env)
    with session.begin():
        root = session.get_transaction()
        prelock(env, spaces, prepared)
        account = persist(env, prepared)
        session.flush()
        plan = new_plan(env, account, spaces)
        result = run(session, replace(plan, targets=tuple(reversed(plan.targets))))
        assert session.get_transaction() is root
        assert result.generation == 1
        assert [x.workspace_id for x in result.workspaces] == [UUID(x.id) for x in spaces]
        assert [x.current_role for x in result.workspaces] == [TenantAccountRole.ADMIN, TenantAccountRole.EDITOR]
        assert all(
            x.membership_created and x.finalization is CasdoorFinalizationState.PENDING for x in result.workspaces
        )
        assert session.scalar(sa.select(Identity.sync_generation)) == 1
        assert session.scalar(sa.select(Quota.total_quota)) == 15
        assert counts(session.get_bind()) == (0, 0, 0)
        rows = session.scalars(sa.select(History)).all()
        assert len(rows) == 2 and all(row.desired_generation == 1 for row in rows)
        assert all('"reason":"role_mapping"' in row.desired_roles_json for row in rows)
        with pytest.raises(FrozenInstanceError):
            result.generation = 42
    assert counts(session.get_bind()) == (1, 1, 1)


@pytest.mark.parametrize("failure", ["trigger", "false_receipt"])
def test_second_space_failure_rolls_back_earlier_b2a_and_first_space(joined_env, monkeypatch, failure):
    env, spaces = joined_env
    session = env[0]
    prepared = prepare(env)
    if failure == "trigger":
        with session.begin():
            session.execute(
                sa.text(
                    f"CREATE TRIGGER fail_second BEFORE INSERT ON tenant_account_joins "
                    f"WHEN NEW.tenant_id = '{spaces[1].id}' BEGIN SELECT RAISE(ABORT, 'second'); END"
                )
            )
    else:
        original = TenantService.persist_tenant_member

        def race(tenant, account, owned_session, role):
            if tenant.id == spaces[1].id:
                owned_session.add(
                    TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.NORMAL)
                )
                owned_session.flush()
            return original(tenant, account, owned_session, role)

        monkeypatch.setattr(TenantService, "persist_tenant_member", race)
    expected = IntegrityError if failure == "trigger" else CasdoorMembershipConflict
    with pytest.raises(expected), session.begin():
        prelock(env, spaces, prepared)
        account = persist(env, prepared)
        session.flush()
        run(session, new_plan(env, account, spaces))
    assert counts(session.get_bind()) == (0, 0, 0)
    with session.begin():
        assert session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(History)) == 0


@pytest.fixture
def ready(local_storage):
    s = local_storage
    with s.session.begin():
        s.account.initialized_at = datetime(2026, 1, 1)
    return s


def local_run(s, plan=None):
    return run(s.session, plan or s.version.plan, generation=1)


def fail_mutator(*args, **kwargs):
    raise AssertionError("Preserved/pending scope entered a mutator")


@pytest.mark.parametrize(
    "kind", ["unmanaged", "owner", "override", "released", "tombstone", "missing", "old", "two_old", "ambiguous"]
)
def test_preservation_and_pending_history_never_enter_local_mutators(ready, monkeypatch, kind):
    s = ready
    with s.session.begin():
        if kind in ("unmanaged", "owner"):
            s.session.delete(s.history)
            if kind == "owner":
                s.join.role = TenantAccountRole.OWNER
        elif kind == "override":
            s.history.ownership = CasdoorMembershipOwnership.LOCAL_OVERRIDE
        elif kind == "released":
            s.history.ownership = CasdoorMembershipOwnership.RELEASED
        elif kind == "tombstone":
            s.history.tombstone = True
        elif kind == "missing":
            s.session.delete(s.join)
        else:
            base = dict(s.session.execute(sa.select(*History.__table__.columns)).one()._mapping)
            old = namespace(s)
            if kind != "ambiguous":
                s.session.execute(sa.update(History).values(namespace_id=old))
            if kind in ("two_old", "ambiguous"):
                s.session.execute(sa.insert(History).values(**dict(base, id=str(uuid4()), namespace_id=namespace(s))))
    monkeypatch.setattr(TenantService, "persist_tenant_member", fail_mutator)
    monkeypatch.setattr(CasdoorLocalRoleRepository, "prepare", fail_mutator)
    with s.session.begin():
        result = local_run(s).workspaces[0]
        assert not result.membership_created and not result.role_changed
        assert result.outcome in (LocalRoleOutcome.PRESERVED, LocalRoleOutcome.PENDING)
        assert result.intent_barrier is RequiredIntentBarrier.CLEAR
        if kind == "missing":
            assert result.join_id is None
        if kind == "ambiguous":
            assert result.ownership_decision is OwnershipDecision.AUTHORIZATION_PENDING
        if kind == "owner":
            assert result.ownership_decision is OwnershipDecision.OWNER_PROTECTED


@pytest.mark.parametrize("association", ["scope", "global", "third_history", "new", "avatar"])
def test_all_history_intents_barrier_retains_observation(ready, monkeypatch, association):
    s = ready
    with s.session.begin():
        values = {}
        if association == "global":
            values["workspace_id"] = None
        if association == "third_history":
            base = dict(s.session.execute(sa.select(*History.__table__.columns)).one()._mapping)
            s.session.execute(sa.update(History).values(namespace_id=namespace(s, str(UUID(int=2**128 - 1)))))
            for number in (1, 2):
                s.session.execute(
                    sa.insert(History).values(
                        **dict(base, id=str(uuid4()), namespace_id=namespace(s, str(UUID(int=number))))
                    )
                )
            values.update(account_id=str(uuid4()), workspace_id=str(uuid4()), membership_id=s.history.id)
        if association == "new":
            s.session.delete(s.history)
            s.session.delete(s.join)
        if association == "avatar":
            values["kind"] = CasdoorIntentKind.PROFILE_AVATAR
        intent(s, **values)
    if association != "avatar":
        monkeypatch.setattr(TenantService, "persist_tenant_member", fail_mutator)
        monkeypatch.setattr(CasdoorLocalRoleRepository, "prepare", fail_mutator)
    with s.session.begin():
        result = local_run(s).workspaces[0]
        if association == "avatar":
            assert result.outcome is LocalRoleOutcome.APPLIED
            assert result.intent_barrier is RequiredIntentBarrier.CLEAR
        else:
            assert result.outcome is LocalRoleOutcome.PENDING
            assert result.intent_barrier is RequiredIntentBarrier.PENDING
            if association == "new":
                assert result.ownership_decision is OwnershipDecision.NEW_JOIN_REQUIRED
                assert result.join_id is None


@pytest.mark.parametrize("field,value", [("status", AccountStatus.PENDING), ("initialized_at", None)])
def test_stale_cached_account_rechecked_before_generation_dml(ready, field, value):
    s = ready
    with s.session.begin():
        s.session.execute(sa.update(Account).values({field: value}).execution_options(synchronize_session=False))
    assert s.account.status is AccountStatus.ACTIVE and s.account.initialized_at is not None
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(s.engine, "before_cursor_execute", capture)
    with pytest.raises(CasdoorLocalMembershipConflict), s.session.begin():
        local_run(s)
    assert not any(x.lstrip().upper().startswith("UPDATE") for x in statements)


@pytest.mark.parametrize(
    "kind", ["empty", "duplicate", "too_many", "owner", "wrong_builtin", "nested", "dirty", "no_root", "remote"]
)
def test_shape_and_root_reject_before_sql(ready, monkeypatch, kind):
    s = ready
    plan = s.version.plan
    if kind == "empty":
        plan = replace(plan, targets=())
    if kind == "duplicate":
        plan = replace(plan, targets=plan.targets * 2)
    if kind == "too_many":
        plan = replace(plan, targets=tuple(replace(s.target, workspace_id=uuid4()) for _ in range(101)))
    if kind in ("owner", "wrong_builtin"):
        plan = replace(
            plan, targets=(replace(s.target, **({"target_role": "owner"} if kind == "owner" else {"builtin_id": "x"})),)
        )
    if kind == "remote":
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    if kind != "no_root":
        s.session.begin()
    if kind == "nested":
        s.session.begin_nested()
    if kind == "dirty":
        s.account.name = "Dirty"
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    with pytest.raises(CasdoorLocalMembershipConflict):
        local_run(s, plan)
    assert statements == []
    s.session.rollback()


@pytest.mark.parametrize("kind", ["fence", "revision", "default", "workspace"])
def test_actual_owner_rejections_rollback_generation(ready, kind):
    s = ready
    plan = s.version.plan
    with s.session.begin():
        if kind == "fence":
            s.namespace.fence_epoch = 1
        elif kind == "revision":
            s.integration.active_revision_id = str(uuid4())
        elif kind == "default":
            plan = replace(
                plan,
                targets=(
                    replace(
                        s.target,
                        target_role="normal",
                        builtin_id="normal",
                        reason=CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK,
                    ),
                ),
            )
            s.session.execute(sa.update(type(s.revision)).values(default_workspace_id=str(uuid4())))
        else:
            s.session.delete(s.workspace)
    with pytest.raises(
        (CasdoorGenerationConflict, CasdoorMembershipConflict, CasdoorLocalRoleConflict)
    ), s.session.begin():
        local_run(s, plan)
    with s.session.begin():
        assert s.session.scalar(sa.select(Identity.sync_generation)) == 1


def test_existing_managed_second_workspace_trigger_restores_exact_metadata(ready):
    s = ready
    with s.session.begin():
        second = Tenant(name="Second")
        second.id = str(UUID(int=2**128 - 1))
        s.session.add(second)
        s.session.flush()
        s.session.execute(
            sa.text(
                f"CREATE TRIGGER fail_second BEFORE INSERT ON tenant_account_joins "
                f"WHEN NEW.tenant_id = '{second.id}' BEGIN SELECT RAISE(ABORT, 'second'); END"
            )
        )
        before = tuple(s.session.execute(sa.select(*History.__table__.columns)).one())
        plan = replace(s.version.plan, targets=(s.target, replace(s.target, workspace_id=UUID(second.id))))
    with pytest.raises(IntegrityError), s.session.begin():
        local_run(s, plan)
    with s.session.begin():
        assert tuple(s.session.execute(sa.select(*History.__table__.columns)).one()) == before
        assert s.session.scalar(sa.select(TenantAccountJoin.role)) is TenantAccountRole.NORMAL
        assert s.session.scalar(sa.select(Identity.sync_generation)) == 1


@pytest.mark.parametrize("branch", ["managed", "unmanaged", "old", "intent", "new_intent"])
@pytest.mark.parametrize("fault", ["unavailable", "wrong_fallback"])
def test_parent_default_guard_covers_every_branch_and_rolls_back_generation(ready, monkeypatch, branch, fault):
    from models.account import TenantStatus

    s = ready
    with s.session.begin():
        default = Tenant(name="Fixed configured default")
        s.session.add(default)
        s.session.flush()
        s.session.execute(sa.update(type(s.revision)).values(default_workspace_id=default.id))
        if fault == "unavailable":
            default.status = TenantStatus.ARCHIVE
        if branch in ("unmanaged", "new_intent"):
            s.session.delete(s.history)
        if branch == "new_intent":
            s.session.delete(s.join)
        if branch == "old":
            s.session.execute(sa.update(History).values(namespace_id=namespace(s)))
        if branch in ("intent", "new_intent"):
            intent(s)
    plan = s.version.plan
    if fault == "wrong_fallback":
        plan = replace(
            plan,
            targets=(
                replace(
                    s.target,
                    target_role="normal",
                    builtin_id="normal",
                    reason=CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK,
                ),
            ),
        )
    monkeypatch.setattr(TenantService, "persist_tenant_member", fail_mutator)
    monkeypatch.setattr(CasdoorMembershipRepository, "inspect", fail_mutator)
    monkeypatch.setattr(CasdoorLocalRoleRepository, "prepare", fail_mutator)
    with pytest.raises(CasdoorLocalRoleConflict), s.session.begin():
        local_run(s, plan)
    with s.session.begin():
        assert s.session.scalar(sa.select(Identity.sync_generation)) == 1


def test_actual_false_receipt_role_update_restores_committed_admin_on_caller_rollback(ready, monkeypatch):
    s = ready
    with s.session.begin():
        s.session.delete(s.history)
        s.join.role = TenantAccountRole.ADMIN
        original_join = dict(s.session.execute(sa.select(*TenantAccountJoin.__table__.columns)).one()._mapping)
        original_join["role"] = TenantAccountRole.ADMIN
    helper = TenantService.persist_tenant_member
    seen = []

    def race(tenant, account, session, role):
        # Simulated legacy writer restores a real join after actual prepare_new.
        session.execute(sa.insert(TenantAccountJoin).values(**original_join))
        receipt = helper(tenant, account, session, role)
        seen.append((receipt.membership_created, receipt.join.role))
        return receipt

    monkeypatch.setattr(TenantService, "persist_tenant_member", race)
    with pytest.raises(CasdoorMembershipConflict), s.session.begin():
        # Make absence observable inside this root, without mocking owner reads.
        s.session.execute(sa.delete(TenantAccountJoin))
        plan = replace(s.version.plan, targets=(replace(s.target, target_role="editor", builtin_id="editor"),))
        local_run(s, plan)
    assert seen == [(False, TenantAccountRole.EDITOR)]
    with s.session.begin():
        assert s.session.scalar(sa.select(TenantAccountJoin.role)) is TenantAccountRole.ADMIN
        assert s.session.scalar(sa.select(Identity.sync_generation)) == 1


@pytest.mark.parametrize("role,fallback", [("normal", False), ("admin", False), ("normal", True)])
def test_existing_local_apply_uses_owner_metadata_cas_and_retains_baseline(ready, role, fallback):
    s = ready
    with s.session.begin():
        s.history.finalization = CasdoorFinalizationState.FINALIZED
        before = (s.history.baseline_json, s.history.source, s.history.ownership_epoch)
    desired = replace(
        s.target,
        target_role=role,
        builtin_id=role,
        reason=(CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK if fallback else CasdoorDecisionReason.ROLE_MAPPING),
    )
    with s.session.begin():
        result = local_run(s, replace(s.version.plan, targets=(desired,))).workspaces[0]
        assert result.metadata_changed
        assert result.finalization is CasdoorFinalizationState.FINALIZED
        assert not result.membership_created
        s.session.refresh(s.history)
        assert (s.history.baseline_json, s.history.source, s.history.ownership_epoch) == before
        assert s.history.desired_generation == 2
        assert f'"target_role":"{role}"' in s.history.desired_roles_json


@pytest.mark.parametrize("new", [False, True])
def test_late_intent_before_local_prepare_never_reports_clear_pending(ready, monkeypatch, new):
    s = ready
    if new:
        with s.session.begin():
            s.session.delete(s.history)
            s.session.delete(s.join)
    original = CasdoorLocalRoleRepository.prepare

    def later_intent(repo, version, target):
        intent(s)
        s.session.flush()
        return original(repo, version, target)

    monkeypatch.setattr(CasdoorLocalRoleRepository, "prepare", later_intent)
    if new:
        with pytest.raises(CasdoorLocalMembershipConflict), s.session.begin():
            local_run(s)
        with s.session.begin():
            assert s.session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0
            assert s.session.scalar(sa.select(sa.func.count()).select_from(History)) == 0
            assert s.session.scalar(sa.select(Identity.sync_generation)) == 1
    else:
        with s.session.begin():
            result = local_run(s).workspaces[0]
            assert result.outcome is LocalRoleOutcome.PENDING
            assert result.intent_barrier is RequiredIntentBarrier.PENDING
            assert s.session.scalar(sa.select(TenantAccountJoin.role)) is TenantAccountRole.NORMAL


@pytest.mark.parametrize("field,value", [("status", AccountStatus.PENDING), ("initialized_at", None)])
def test_post_b2a_account_gate_failure_rolls_back_all_earlier_writes(joined_env, field, value):
    env, spaces = joined_env
    session = env[0]
    prepared = prepare(env)
    with pytest.raises(CasdoorLocalMembershipConflict), session.begin():
        prelock(env, spaces, prepared)
        account = persist(env, prepared)
        session.flush()
        session.execute(sa.update(Account).values({field: value}).execution_options(synchronize_session=False))
        run(session, new_plan(env, account, spaces))
    assert counts(session.get_bind()) == (0, 0, 0)
