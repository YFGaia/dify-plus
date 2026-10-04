"""Actual finite B3 effects in one SQLite root with real leases over fake Redis Lua.

These trusted test callers explicitly acquire canonical leases and original parent
locks. They do not exercise C1/L3 admission or claim live SQL/Redis lock coverage.
"""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.leases import CasdoorLeases, CasdoorLeaseScope, WorkspaceMemberScope
from core.casdoor.local_roles import LocalRoleOutcome
from core.casdoor.mapping import (
    DesiredWorkspacePlan,
    DesiredWorkspaceTarget,
    MappingIdentityContext,
)
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    OwnershipDecision,
    parse_local_withdrawal_json,
    role_baseline_json,
)
from enums import DeploymentEdition
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
    TenantStatus,
)
from models.account_money_extend import AccountMoneyExtend as Quota
from models.agent import Agent, AgentScope, AgentSource
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
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
from models.dataset import Dataset
from models.model import App
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_membership_repository_extend import (
    CasdoorMembershipRepository,
)
from services.account_service import TenantService
from services.casdoor_local_membership_service_extend import (
    CasdoorLocalMembershipService,
)

from tests.unit_tests.core.casdoor.test_leases import Clock, FakeRedisLua


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@pytest.fixture
def state(sqlite_session_factory, config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    with sqlite_session_factory() as session:
        with session.begin():
            integration = Integration(enabled=True)
            account = Account(
                name="Original",
                email="withdraw@example.test",
                status=AccountStatus.ACTIVE,
                initialized_at=datetime(2026, 1, 1),
            )
            owner = Account(name="Recipient", email="recipient@example.test")
            spaces = [Tenant(name="Workspace") for _ in range(3)]
            for space, i in zip(spaces, (100, 200, 300), strict=True):
                space.id = str(UUID(int=i))
            session.add_all([integration, account, owner, *spaces])
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
                default_workspace_id=spaces[0].id,
                certificates_json="[]",
                policy_json="{}",
                mappings_json="[]",
            )
            identity = Identity(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization="Org",
                subject="Synthetic",
                sync_generation=0,
                last_applied_json="{}",
                profile_sync_json="{}",
            )
            quota = Quota(account_id=account.id, total_quota=Decimal("71.25"), used_quota=Decimal("4.50"))
            session.add_all([revision, identity, quota])
            session.flush()
            integration.active_revision_id = revision.id
            for space in spaces[1:]:
                session.add(TenantAccountJoin(tenant_id=space.id, account_id=owner.id, role=TenantAccountRole.OWNER))
                session.add(
                    App(
                        tenant_id=space.id,
                        name="Backing",
                        mode="chat",
                        enable_site=False,
                        enable_api=False,
                        created_by=account.id,
                        maintainer=account.id,
                    )
                )
                session.add(Dataset(tenant_id=space.id, name="Dataset", created_by=account.id, maintainer=account.id))
            session.flush()
            backing = session.scalar(sa.select(App).order_by(App.tenant_id))
            session.add(
                Agent(
                    tenant_id=spaces[1].id,
                    name="Lineage",
                    scope=AgentScope.WORKFLOW_ONLY,
                    source=AgentSource.WORKFLOW,
                    app_id=backing.id,
                    backing_app_id=backing.id,
                    workflow_id=str(uuid4()),
                    workflow_node_id="node",
                    active_config_snapshot_id=str(uuid4()),
                    created_by=account.id,
                    updated_by=account.id,
                )
            )
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
            "Synthetic",
        )
        targets = tuple(
            DesiredWorkspaceTarget(UUID(space.id), role, role, reason, ())
            for space, role, reason in (
                (spaces[0], "normal", CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK),
                (spaces[1], "editor", CasdoorDecisionReason.ROLE_MAPPING),
                (spaces[2], "admin", CasdoorDecisionReason.ROLE_MAPPING),
            )
        )
        s = SimpleNamespace(
            session=session,
            factory=sqlite_session_factory,
            spaces=spaces,
            account=account,
            owner=owner,
            integration=integration,
            namespace=namespace,
            revision=revision,
            identity=identity,
            plan=DesiredWorkspacePlan(context, targets),
            correlation=uuid4(),
        )
        s.space_ids = tuple(space.id for space in spaces)
        s.email = account.email
        with caller(s):
            result = invoke(s, generation=0)
            assert result.generation == 1 and all(x.membership_created for x in result.workspaces)
        yield s


@contextmanager
def caller(s):
    """Original full parent order and canonical key owner, before any caller DML."""
    c = s.plan.context
    clock = Clock()
    redis = FakeRedisLua(clock)
    scope = CasdoorLeaseScope(
        c.namespace_id,
        c.subject,
        (s.email,),
        (c.account_id,),
        tuple(WorkspaceMemberScope(UUID(space_id), c.account_id) for space_id in s.space_ids),
    )
    leases = CasdoorLeases(redis, scope, deadline=clock() + 45, ttl_seconds=45, monotonic=clock)
    leases.acquire()
    try:
        assert leases.canonical_keys == scope.canonical_keys
        leases.ensure_owned()
        with s.session.begin():
            session = s.session
            session.execute(sa.select(Integration.id).where(Integration.id == str(c.integration_id)).with_for_update())
            session.execute(sa.select(Namespace.id).order_by(Namespace.id).with_for_update())
            session.execute(sa.select(Account.id).where(Account.id == str(c.account_id)).with_for_update())
            session.execute(sa.select(Identity.id).where(Identity.account_id == str(c.account_id)).with_for_update())
            session.execute(
                sa.select(Tenant.id).where(Tenant.id.in_(s.space_ids)).order_by(Tenant.id).with_for_update()
            )
            session.execute(
                sa.select(TenantAccountJoin.id)
                .where(TenantAccountJoin.tenant_id.in_(s.space_ids))
                .order_by(TenantAccountJoin.tenant_id, TenantAccountJoin.id)
                .with_for_update()
            )
            session.execute(
                sa.select(History.id)
                .where(History.account_id == str(c.account_id))
                .order_by(History.namespace_id, History.workspace_id, History.id)
                .with_for_update()
            )
            session.execute(
                sa.select(Intent.id).order_by(Intent.namespace_id, Intent.scope_digest, Intent.id).with_for_update()
            )
            yield leases
            leases.ensure_owned()
        assert len(redis.sets) == len(scope.canonical_keys)
    finally:
        assert leases.release()


def invoke(s, *, generation=1, withdraw=(), plan=None, correlation=True):
    return CasdoorLocalMembershipService(s.session).persist_local_memberships(
        plan or s.plan,
        expected_fence_epoch=0,
        expected_generation=generation,
        withdrawal_workspace_ids=withdraw,
        correlation_id=s.correlation if correlation else None,
    )


def withdrawal(s, *, generation=1, both=False, correlation=True):
    ids = tuple(UUID(space.id) for space in s.spaces[1:] if both or space is s.spaces[1])
    plan = replace(s.plan, targets=tuple(t for t in s.plan.targets if t.workspace_id not in ids))
    return invoke(s, generation=generation, withdraw=ids, plan=plan, correlation=correlation)


MODELS = (Account, Quota, Identity, Agent, App, Dataset, TenantAccountJoin, History, Audit, Intent)


def snapshot(s):
    with s.factory() as observer:
        return {
            model: tuple(observer.execute(sa.select(model.__table__).order_by(model.__table__.c.id)).all())
            for model in MODELS
        }


def history(s, index=1):
    return s.session.execute(
        sa.select(*History.__table__.columns).where(History.workspace_id == s.spaces[index].id)
    ).one()


def test_actual_withdraw_preserved_absence_and_same_history_regrant(state):
    s = state
    before = snapshot(s)
    with caller(s):
        old = history(s)
        result = withdrawal(s)
        row = history(s)
        assert result.generation == 2 and len(result.withdrawals) == 1 and len(result.workspaces) == 2
        assert row.id == old.id and row.join_id == old.join_id and row.ownership_epoch == old.ownership_epoch + 1
        assert (row.source, row.baseline_json) == (old.source, old.baseline_json)
        assert row.finalization is CasdoorFinalizationState.PENDING and not row.tombstone
        marker = parse_local_withdrawal_json(row.desired_roles_json)
        assert marker["withdrawal_epoch"] == row.ownership_epoch
        assert s.session.get(TenantAccountJoin, old.join_id) is None
    after = snapshot(s)
    for model in (Account, Quota, Agent):
        assert after[model] == before[model]
    for model in (App, Dataset):
        for row in after[model]:
            assert row.created_by == s.account.id
            assert row.maintainer == (s.owner.id if row.tenant_id == s.spaces[1].id else s.account.id)
    with caller(s):
        before_absence = history(s)
        result = withdrawal(s, generation=2, correlation=False)
        assert not result.withdrawals and history(s) == before_absence
        assert s.session.scalar(sa.select(sa.func.count()).select_from(Audit)) == 1
    with caller(s):
        result = invoke(s, generation=3)
        row = history(s)
        target = next(x for x in result.workspaces if x.workspace_id == UUID(s.spaces[1].id))
        assert target.membership_regranted and not target.membership_created
        assert target.outcome is LocalRoleOutcome.NOOP and not target.metadata_changed and not target.role_changed
        assert row.id == old.id and row.join_id != old.join_id and row.ownership_epoch == old.ownership_epoch + 2
        assert (row.source, row.baseline_json) == (old.source, old.baseline_json)
        assert row.finalization is CasdoorFinalizationState.PENDING
        audits = s.session.scalars(sa.select(Audit).order_by(Audit.created_at, Audit.action)).all()
        assert {a.action for a in audits} == {"local_member_withdraw", "local_member_regrant"}
        for audit in audits:
            summary = json.loads(audit.summary_json)
            assert audit.actor_account_id is None and audit.correlation_id == str(s.correlation)
            assert summary["count"] == 1 and summary["epoch_after"] == summary["epoch_before"] + 1
            assert summary["references"]["old_join_id"] == old.join_id
            assert not any(value in audit.summary_json for value in (s.account.email, s.account.name, "Synthetic"))
    final = snapshot(s)
    for model in (Account, Quota, Agent, App, Dataset):
        assert final[model] == after[model]


@pytest.mark.parametrize("source", list(CasdoorMembershipSource))
@pytest.mark.parametrize("initial_role", [None, "normal", "editor", "admin"])
def test_original_source_and_canonical_local_initial_baseline_preserved(state, source, initial_role):
    s = state
    with s.session.begin():
        baseline = role_baseline_json(
            MembershipObservation(
                UUID(s.spaces[1].id),
                UUID(s.account.id),
                uuid4() if initial_role else None,
                TenantAccountRole(initial_role) if initial_role else None,
                MembershipBackend.LOCAL,
            )
        )
        s.session.execute(
            sa.update(History)
            .where(History.workspace_id == s.spaces[1].id)
            .values(source=source, baseline_json=baseline)
        )
    with caller(s):
        withdrawal(s)
    with caller(s):
        invoke(s, generation=2)
        assert history(s).source is source and history(s).baseline_json == baseline


@pytest.mark.parametrize("failure", ["trigger", "cas", "audit", "residual", "false_receipt"])
def test_second_effect_failure_rolls_back_all_caller_and_business_writes(state, monkeypatch, failure):
    s = state
    if failure == "false_receipt":
        with caller(s):
            withdrawal(s, both=True)
    before = snapshot(s)
    if failure == "trigger":
        with s.session.begin():
            s.session.execute(
                sa.text(
                    f"CREATE TRIGGER deny_second BEFORE DELETE ON tenant_account_joins "
                    f"WHEN OLD.tenant_id = '{s.spaces[2].id}' AND OLD.account_id = '{s.account.id}' "
                    "BEGIN SELECT RAISE(ABORT, 'second_effect'); END"
                )
            )
    elif failure == "cas":
        with s.session.begin():
            s.session.execute(
                sa.text(
                    f"CREATE TRIGGER skip_cas BEFORE UPDATE ON casdoor_managed_membership_extend "
                    f"WHEN OLD.workspace_id = '{s.spaces[2].id}' BEGIN SELECT RAISE(IGNORE); END"
                )
            )
    elif failure == "audit":
        with s.session.begin():
            s.session.execute(
                sa.text(
                    "CREATE TRIGGER fail_audit BEFORE INSERT ON casdoor_audit_extend "
                    f"WHEN instr(NEW.summary_json, '{s.spaces[2].id}') > 0 "
                    "BEGIN SELECT RAISE(ABORT, 'audit_flush'); END"
                )
            )
    elif failure == "residual":
        with s.session.begin():
            s.session.execute(
                sa.text(
                    f"CREATE TRIGGER residual AFTER UPDATE OF maintainer ON apps "
                    f"WHEN OLD.tenant_id = '{s.spaces[2].id}' "
                    "BEGIN UPDATE apps SET maintainer=OLD.maintainer WHERE id=OLD.id; END"
                )
            )
    else:
        original = TenantService.persist_tenant_member

        def raced(tenant, account, session, role):
            if tenant.id == s.spaces[2].id:
                session.add(
                    TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole.NORMAL)
                )
                session.flush()
            result = original(tenant, account, session, role)
            if tenant.id == s.spaces[2].id:
                assert result.membership_created is False and result.join.role is TenantAccountRole.ADMIN
            return result

        monkeypatch.setattr(TenantService, "persist_tenant_member", raced)
    with pytest.raises((ValueError, sa.exc.IntegrityError)), caller(s):
        s.session.execute(sa.update(Account).where(Account.id == s.account.id).values(name="Earlier profile"))
        s.session.execute(sa.update(Quota).where(Quota.account_id == s.account.id).values(total_quota=99))
        s.session.execute(
            sa.update(Identity).where(Identity.id == s.identity.id).values(last_applied_json='{"earlier":true}')
        )
        s.session.flush()
        if failure == "false_receipt":
            invoke(s, generation=2)
        else:
            withdrawal(s, both=True)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "kind",
    [
        "owner",
        "override",
        "released",
        "tombstone",
        "missing",
        "remote_initial",
        "owner_initial",
        "manual_recovery",
        "remote_desired",
        "bad_desired",
        "fence",
        "fingerprint",
        "epoch_max",
        "bad_generation",
        "archived",
        "banned",
        "uninitialized",
        "no_owner",
        "two_owners",
        "foreign_history",
    ],
)
def test_present_withdrawal_guards_block_without_effect(state, kind):
    s = state
    with s.session.begin():
        row = history(s)
        updates = {}
        if kind == "owner":
            s.session.execute(
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.id == row.join_id)
                .values(role=TenantAccountRole.OWNER)
            )
        elif kind in ("override", "released"):
            updates["ownership"] = (
                CasdoorMembershipOwnership.LOCAL_OVERRIDE if kind == "override" else CasdoorMembershipOwnership.RELEASED
            )
        elif kind == "tombstone":
            updates["tombstone"] = True
        elif kind == "missing":
            s.session.execute(sa.delete(TenantAccountJoin).where(TenantAccountJoin.id == row.join_id))
        elif kind in ("remote_initial", "owner_initial"):
            value = json.loads(row.baseline_json)
            value["backend" if kind == "remote_initial" else "join_role"] = (
                "remote" if kind == "remote_initial" else "owner"
            )
            updates["baseline_json"] = canonical(value)
        elif kind == "manual_recovery":
            updates["finalization"] = CasdoorFinalizationState.MANUAL_RECOVERY
        elif kind in ("remote_desired", "bad_desired", "fence"):
            value = json.loads(row.desired_roles_json)
            value["backend" if kind == "remote_desired" else "fence_epoch"] = (
                "remote" if kind == "remote_desired" else 2
            )
            updates["desired_roles_json"] = canonical(value) if kind != "bad_desired" else "{}"
        elif kind == "fingerprint":
            updates["last_applied_fingerprint"] = "f" * 64
        elif kind == "epoch_max":
            updates["ownership_epoch"] = 2**63 - 1
        elif kind == "bad_generation":
            updates["desired_generation"] = 99
        elif kind == "archived":
            s.session.execute(sa.update(Tenant).where(Tenant.id == s.spaces[1].id).values(status=TenantStatus.ARCHIVE))
        elif kind == "banned":
            s.session.execute(sa.update(Account).where(Account.id == s.account.id).values(status=AccountStatus.BANNED))
        elif kind == "uninitialized":
            s.session.execute(sa.update(Account).where(Account.id == s.account.id).values(initialized_at=None))
        elif kind == "no_owner":
            s.session.execute(
                sa.delete(TenantAccountJoin).where(
                    TenantAccountJoin.tenant_id == s.spaces[1].id, TenantAccountJoin.role == TenantAccountRole.OWNER
                )
            )
        elif kind == "two_owners":
            s.session.add(
                TenantAccountJoin(tenant_id=s.spaces[1].id, account_id=str(uuid4()), role=TenantAccountRole.OWNER)
            )
        elif kind == "foreign_history":
            other = Namespace(
                integration_id=s.integration.id,
                expected_issuer=s.namespace.expected_issuer,
                organization="Other",
                application="Other",
                client_id="Other",
                core_fingerprint="c" * 64,
            )
            s.session.add(other)
            s.session.flush()
            s.session.execute(sa.insert(History).values(**dict(row._mapping, id=str(uuid4()), namespace_id=other.id)))
        if updates:
            s.session.execute(sa.update(History).where(History.id == row.id).values(**updates))
    before = snapshot(s)
    with pytest.raises((ValueError, sa.exc.IntegrityError)), caller(s):
        withdrawal(s)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "kind",
    [
        "epoch",
        "generation",
        "fence",
        "bool",
        "float",
        "extra",
        "duplicate",
        "uppercase",
        "spacing",
        "oversized",
        "malformed",
        "missing_key",
        "schema",
        "negative",
        "overflow",
        "last_applied",
        "fingerprint",
        "retained_global",
        "foreign_join",
        "manual_recovery",
        "override",
        "tombstone",
    ],
)
def test_controlled_marker_and_actual_absence_fail_closed(state, kind):
    s = state
    with caller(s):
        withdrawal(s)
    with s.session.begin():
        row = history(s)
        value = json.loads(row.desired_roles_json)
        updates = {}
        if kind in ("epoch", "generation", "fence"):
            key = {"epoch": "withdrawal_epoch", "generation": "withdrawal_generation", "fence": "fence_epoch"}[kind]
            value[key] += 1
        elif kind == "bool":
            value["withdrawal_epoch"] = True
        elif kind == "float":
            value["withdrawal_epoch"] = 1.0
        elif kind == "extra":
            value["secret"] = "extra"
        elif kind == "uppercase":
            value["removed_join_id"] = value["removed_join_id"].upper()
        elif kind == "missing_key":
            del value["withdrawal_epoch"]
        elif kind == "schema":
            value["schema_version"] = True
        elif kind == "negative":
            value["fence_epoch"] = -1
        elif kind == "overflow":
            value["withdrawal_epoch"] = 2**63
        elif kind == "last_applied":
            updates["last_applied_roles_json"] = row.baseline_json
        elif kind == "fingerprint":
            updates["last_applied_fingerprint"] = "0" * 64
        elif kind == "retained_global":
            foreign = TenantAccountJoin(
                tenant_id=s.spaces[2].id, account_id=str(uuid4()), role=TenantAccountRole.NORMAL
            )
            foreign.id = row.join_id
            s.session.add(foreign)
        elif kind == "foreign_join":
            s.session.add(
                TenantAccountJoin(tenant_id=s.spaces[1].id, account_id=s.account.id, role=TenantAccountRole.NORMAL)
            )
        elif kind == "manual_recovery":
            updates["finalization"] = CasdoorFinalizationState.MANUAL_RECOVERY
        elif kind == "override":
            updates["ownership"] = CasdoorMembershipOwnership.LOCAL_OVERRIDE
        elif kind == "tombstone":
            updates["tombstone"] = True
        text = canonical(value)
        if kind == "duplicate":
            text = text[:-1] + ',"schema_version":2}'
        if kind == "spacing":
            text += " "
        if kind == "oversized":
            text = "汉" * 30000
        if kind == "malformed":
            text = "{"
        updates["desired_roles_json"] = text
        s.session.execute(sa.update(History).where(History.id == row.id).values(**updates))
    before = snapshot(s)
    with pytest.raises(ValueError), caller(s):
        withdrawal(s, generation=2)
    assert snapshot(s) == before
    # Oversized TEXT is rejected by the bounded DB reader before JSON parsing.
    if kind == "oversized":
        with pytest.raises(ValueError), caller(s):
            invoke(s, generation=2)
        assert snapshot(s) == before
        return
    # Target regrant may preserve manual override or return pending, never create.
    with caller(s):
        result = invoke(s, generation=2)
        target = next(x for x in result.workspaces if x.workspace_id == UUID(s.spaces[1].id))
        assert not target.membership_created and not target.membership_regranted
        assert target.outcome in (LocalRoleOutcome.PENDING, LocalRoleOutcome.PRESERVED)


@pytest.mark.parametrize("operation", list(CasdoorOperationState))
@pytest.mark.parametrize("termination", list(CasdoorTerminationState))
@pytest.mark.parametrize("association", ["scope", "null", "linked"])
def test_every_nonavatar_intent_state_retained_and_blocks_withdrawal(state, operation, termination, association):
    s = state
    with s.session.begin():
        row = history(s)
        values = dict(
            namespace_id=str(uuid4()),
            identity_id=str(uuid4()),
            account_id=s.account.id,
            workspace_id=s.spaces[1].id,
            revision_id=s.revision.id,
            generation=93,
            ownership_epoch=1,
            fence_epoch=0,
            kind=CasdoorIntentKind.ROLE_REPLACE,
            scope_digest="a" * 64,
            idempotency_key=uuid4().hex * 2,
            desired_json="{}",
            operation_state=operation,
            termination_state=termination,
        )
        if association == "null":
            values["workspace_id"] = None
        if association == "linked":
            values.update(account_id=str(uuid4()), workspace_id=str(uuid4()), membership_id=row.id)
        s.session.add(Intent(**values))
    before = snapshot(s)
    with pytest.raises(ValueError), caller(s):
        withdrawal(s)
    assert snapshot(s) == before


@pytest.mark.parametrize("phase", ["withdraw", "regrant"])
def test_avatar_only_exclusion_and_no_external_effects(state, monkeypatch, phase):
    s = state
    if phase == "regrant":
        with caller(s):
            withdrawal(s)
    with s.session.begin():
        s.session.add(
            Intent(
                namespace_id=s.namespace.id,
                identity_id=s.identity.id,
                account_id=s.account.id,
                workspace_id=None,
                revision_id=s.revision.id,
                generation=93,
                ownership_epoch=1,
                fence_epoch=0,
                kind=CasdoorIntentKind.PROFILE_AVATAR,
                scope_digest="a" * 64,
                idempotency_key=uuid4().hex * 2,
                desired_json="{}",
                operation_state=CasdoorOperationState.APPLIED,
                termination_state=CasdoorTerminationState.CONFIRMED,
            )
        )
    import services.account_service as owner
    import services.enterprise.account_deletion_sync as deletion
    import tasks.initialize_created_app_rbac_access_task as tasks

    def forbidden(*args, **kwargs):
        raise AssertionError("No external effect in B3 root")

    monkeypatch.setattr(deletion, "sync_workspace_member_removal", forbidden)
    monkeypatch.setattr(owner.RBACService.MemberRoles, "delete_rbac_bindings", forbidden)
    monkeypatch.setattr(tasks.sync_joined_workspace_member_rbac_access_task, "delay", forbidden)
    with caller(s):
        monkeypatch.setattr(s.session, "commit", forbidden)
        monkeypatch.setattr(s.session, "begin_nested", forbidden)
        if phase == "regrant":
            invoke(s, generation=2)
        else:
            withdrawal(s)
        assert s.session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 1


@pytest.mark.parametrize("kind", ["enterprise", "default", "overlap", "too_many", "string_id", "correlation"])
def test_batch_shape_and_enterprise_denied(state, config_overrides, kind):
    s = state
    if kind == "enterprise":
        config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.ENTERPRISE)
    before = snapshot(s)
    writes = []

    def sql(_c, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(" ", 1)[0].upper() in ("UPDATE", "DELETE", "INSERT"):
            writes.append(statement)

    sa.event.listen(s.session.get_bind(), "before_cursor_execute", sql)
    try:
        with pytest.raises(ValueError), caller(s):
            if kind == "default":
                invoke(s, withdraw=(UUID(s.spaces[0].id),), plan=replace(s.plan, targets=s.plan.targets[1:]))
            elif kind == "overlap":
                invoke(s, withdraw=(UUID(s.spaces[1].id),))
            elif kind == "too_many":
                invoke(s, withdraw=tuple(uuid4() for _ in range(101)))
            elif kind == "string_id":
                invoke(s, withdraw=(s.spaces[1].id,))
            else:
                withdrawal(s, correlation=kind != "correlation")
    finally:
        sa.event.remove(s.session.get_bind(), "before_cursor_execute", sql)
    assert snapshot(s) == before
    if kind != "correlation":
        assert not writes


@pytest.mark.parametrize("field", ["baseline_json", "source", "finalization", "desired_roles_json", "ownership_epoch"])
@pytest.mark.parametrize("phase", ["withdraw", "regrant"])
def test_post_helper_full_state_recheck_rolls_back_drift(state, monkeypatch, field, phase):
    s = state
    if phase == "regrant":
        with caller(s):
            withdrawal(s)
    before = snapshot(s)
    original = (
        TenantService._persist_member_removal_effect if phase == "withdraw" else TenantService.persist_tenant_member
    )

    def drift(*args, **kwargs):
        result = original(*args, **kwargs)
        session = kwargs["session"] if phase == "withdraw" else args[2]
        # L1 helper schedules a delete; caller flushes before the row drift SQL.
        session.flush()
        value = {
            "baseline_json": "{}",
            "source": CasdoorMembershipSource.ADOPT,
            "finalization": CasdoorFinalizationState.FINALIZED,
            "desired_roles_json": "{}",
            "ownership_epoch": 9,
        }[field]
        session.execute(sa.update(History).where(History.workspace_id == s.spaces[1].id).values(**{field: value}))
        return result

    monkeypatch.setattr(
        TenantService, "_persist_member_removal_effect" if phase == "withdraw" else "persist_tenant_member", drift
    )
    with pytest.raises(ValueError), caller(s):
        if phase == "withdraw":
            withdrawal(s)
        else:
            invoke(s, generation=2)
    assert snapshot(s) == before


def test_old_new_preparation_never_accepts_controlled_history_or_forged_receipt(state):
    s = state
    with caller(s):
        withdrawal(s)
    with caller(s):
        repo = CasdoorMembershipRepository(s.session)
        version = GenerationPlanVersion(s.plan, 0, 2)
        view = repo.inspect(version, UUID(s.spaces[1].id), backend=MembershipBackend.LOCAL)
        assert view.decision is OwnershipDecision.CONTROLLED_WITHDRAWN
        with pytest.raises(ValueError):
            repo.prepare_new(version, s.plan.targets[1], backend=MembershipBackend.LOCAL)
        # A fabricated token cannot bypass issuer identity even with a real Join.
        from core.casdoor.ownership import UNKNOWN_MEMBER_ROLES
        from repositories.casdoor_membership_repository_extend import (
            NewMembershipPreparation,
        )
        from services.account_service import _PersistedTenantMember

        token = NewMembershipPreparation(
            version, s.plan.targets[1], MembershipBackend.LOCAL, UNKNOWN_MEMBER_ROLES, s.session.get_transaction()
        )
        join = s.session.scalar(sa.select(TenantAccountJoin).where(TenantAccountJoin.tenant_id == s.spaces[0].id))
        with pytest.raises(ValueError):
            repo.register_new(token, _PersistedTenantMember(join, True))


def test_marker_parser_never_authenticates_absence():
    from core.casdoor.ownership import decide_ownership

    marker = canonical(
        dict(
            backend="local",
            fence_epoch=0,
            operation="controlled_withdrawal",
            removed_join_id=str(uuid4()),
            schema_version=2,
            withdrawal_epoch=1,
            withdrawal_generation=1,
        )
    )
    assert parse_local_withdrawal_json(marker)["withdrawal_epoch"] == 1
    observation = MembershipObservation(uuid4(), uuid4(), None, None, MembershipBackend.LOCAL)
    assert decide_ownership(observation, None) is OwnershipDecision.NEW_JOIN_REQUIRED
    for raw in (marker + " ", marker.replace('"schema_version":2', '"schema_version":2.0'), "[]", "汉" * 30000):
        with pytest.raises(ValueError):
            parse_local_withdrawal_json(raw)


@pytest.mark.parametrize("phase", ["withdraw", "regrant"])
def test_private_effect_captures_cas_expected_state_before_audit_drift(state, monkeypatch, phase):
    s = state
    if phase == "regrant":
        with caller(s):
            withdrawal(s)
    original = CasdoorAuditRepository.append_local_membership

    def drift(repo, **kwargs):
        result = original(repo, **kwargs)
        repo._session.execute(
            sa.update(History)
            .where(History.workspace_id == str(kwargs["workspace_id"]))
            .values(source=CasdoorMembershipSource.ADOPT)
        )
        return result

    monkeypatch.setattr(CasdoorAuditRepository, "append_local_membership", drift)
    # Caller rolls back because exact L3-style comparison detects post-audit drift.
    before = snapshot(s)
    with pytest.raises(ValueError, match="exact_delta"), caller(s):
        result = withdrawal(s) if phase == "withdraw" else invoke(s, generation=2)
        (effect,) = result._controlled_effects
        assert effect.before.source is CasdoorMembershipSource.MAPPING
        assert effect.after.source is CasdoorMembershipSource.MAPPING
        assert effect.after.baseline_json == effect.before.baseline_json
        assert effect.after.ownership_epoch == effect.before.ownership_epoch + 1
        assert effect.after.id == effect.before.id
        assert history(s).source is CasdoorMembershipSource.ADOPT
        if tuple(history(s)) != effect.after:
            raise ValueError("exact_delta")
    assert snapshot(s) == before


@pytest.mark.parametrize("phase", ["withdraw", "regrant"])
def test_after_cas_trigger_cannot_rewrite_immutable_source(state, phase):
    s = state
    if phase == "regrant":
        with caller(s):
            withdrawal(s)
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER rewrite_source AFTER UPDATE ON casdoor_managed_membership_extend "
                f"WHEN OLD.workspace_id = '{s.spaces[1].id}' BEGIN UPDATE casdoor_managed_membership_extend "
                "SET source='adopt' WHERE id=OLD.id; END"
            )
        )
    before = snapshot(s)
    with pytest.raises(ValueError), caller(s):
        if phase == "withdraw":
            withdrawal(s)
        else:
            invoke(s, generation=2)
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["inviter", "retained_id", "owner_role", "detached", "wrong_join"])
def test_actual_regrant_receipt_mutations_rollback(state, monkeypatch, kind):
    s = state
    with caller(s):
        withdrawal(s)
    before = snapshot(s)
    original = TenantService.persist_tenant_member

    def wrong_receipt(tenant, account, session, role):
        result = original(tenant, account, session, role)
        if tenant.id == s.spaces[1].id:
            assert result.membership_created is True
            if kind == "inviter":
                result.join.invited_by = s.owner.id
            elif kind == "retained_id":
                result.join.id = history(s).join_id
            elif kind == "owner_role":
                result.join.role = TenantAccountRole.OWNER
            session.flush()
            if kind == "detached":
                session.expunge(result.join)
            if kind == "wrong_join":
                from services.account_service import _PersistedTenantMember

                other = session.scalar(
                    sa.select(TenantAccountJoin).where(TenantAccountJoin.tenant_id == s.spaces[0].id)
                )
                return _PersistedTenantMember(other, True)
        return result

    monkeypatch.setattr(TenantService, "persist_tenant_member", wrong_receipt)
    with pytest.raises(ValueError), caller(s):
        invoke(s, generation=2)
    assert snapshot(s) == before


@pytest.mark.parametrize("phase", ["withdraw", "regrant"])
def test_original_private_preparation_is_single_use_and_same_root(state, phase):
    s = state
    if phase == "regrant":
        with caller(s):
            withdrawal(s)
    before = snapshot(s)
    with pytest.raises(ValueError), caller(s):
        repo = CasdoorMembershipRepository(s.session)
        version = GenerationPlanVersion(s.plan, 0, 1 if phase == "withdraw" else 2)
        if phase == "withdraw":
            token = repo.prepare_local_withdrawal(version, UUID(s.spaces[1].id))
            with pytest.raises(ValueError):
                repo.register_local_withdrawal(replace(token))
            join = s.session.get(TenantAccountJoin, history(s).join_id)
            TenantService._persist_member_removal_effect(s.spaces[1], s.account.id, join, s.owner.id, session=s.session)
            s.session.flush()
            repo.register_local_withdrawal(token)
            repo.register_local_withdrawal(token)
        else:
            token = repo.prepare_local_regrant(version, s.plan.targets[1])
            receipt = TenantService.persist_tenant_member(s.spaces[1], s.account, s.session, "editor")
            with pytest.raises(ValueError):
                repo.register_local_regrant(replace(token), receipt)
            repo.register_local_regrant(token, receipt)
            repo.register_local_regrant(token, receipt)
    assert snapshot(s) == before


@pytest.mark.parametrize("phase", ["withdraw", "regrant"])
def test_preparation_from_earlier_root_cannot_register_actual_effect(state, phase):
    s = state
    if phase == "regrant":
        with caller(s):
            withdrawal(s)
    repo = CasdoorMembershipRepository(s.session)
    with caller(s):
        version = GenerationPlanVersion(s.plan, 0, 1 if phase == "withdraw" else 2)
        token = (
            repo.prepare_local_withdrawal(version, UUID(s.spaces[1].id))
            if phase == "withdraw"
            else repo.prepare_local_regrant(version, s.plan.targets[1])
        )
    before = snapshot(s)
    with pytest.raises(ValueError), caller(s):
        if phase == "withdraw":
            join = s.session.get(TenantAccountJoin, history(s).join_id)
            TenantService._persist_member_removal_effect(s.spaces[1], s.account.id, join, s.owner.id, session=s.session)
            s.session.flush()
            repo.register_local_withdrawal(token)
        else:
            receipt = TenantService.persist_tenant_member(s.spaces[1], s.account, s.session, "editor")
            repo.register_local_regrant(token, receipt)
    assert snapshot(s) == before


@pytest.mark.parametrize("phase", ["withdraw", "regrant"])
def test_late_confirmed_linked_intent_after_helper_rolls_back_actual_effect(state, monkeypatch, phase):
    s = state
    if phase == "regrant":
        with caller(s):
            withdrawal(s)
    before = snapshot(s)
    original = (
        TenantService._persist_member_removal_effect if phase == "withdraw" else TenantService.persist_tenant_member
    )

    def late_intent(*args, **kwargs):
        result = original(*args, **kwargs)
        session = kwargs["session"] if phase == "withdraw" else args[2]
        session.flush()
        row = history(s)
        session.add(
            Intent(
                namespace_id=str(uuid4()),
                identity_id=str(uuid4()),
                account_id=str(uuid4()),
                workspace_id=str(uuid4()),
                membership_id=row.id,
                revision_id=s.revision.id,
                generation=1,
                ownership_epoch=0,
                fence_epoch=0,
                kind=CasdoorIntentKind.RESOURCE_REVOKE,
                scope_digest="f" * 64,
                idempotency_key=uuid4().hex * 2,
                desired_json="{}",
                operation_state=CasdoorOperationState.APPLIED,
                termination_state=CasdoorTerminationState.CONFIRMED,
            )
        )
        session.flush()
        return result

    monkeypatch.setattr(
        TenantService, "_persist_member_removal_effect" if phase == "withdraw" else "persist_tenant_member", late_intent
    )
    with pytest.raises(ValueError), caller(s):
        if phase == "withdraw":
            withdrawal(s)
        else:
            invoke(s, generation=2)
    assert snapshot(s) == before
