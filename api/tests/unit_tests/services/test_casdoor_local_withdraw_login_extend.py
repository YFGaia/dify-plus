"""Actual signed C2 -> original C1/B2/B3/I19 chain with offline bottom wires."""

from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json
from enums import DeploymentEdition
from models.account import Account, AccountStatus, TenantAccountRole
from models.account import TenantAccountJoin as Join
from models.account_money_extend import AccountMoneyExtend as Quota
from models.agent import Agent, AgentScope, AgentSource
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
from models.casdoor_extend import (
    CasdoorFinalizationState as Finalization,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorMembershipSource as Source,
)
from models.dataset import Dataset
from models.model import App
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from sqlalchemy.orm import Session
from test_casdoor_local_login_finalization_service_extend import attach_finalizer, new_authorization

pytest_plugins = ["test_casdoor_local_login_coordinator_service_extend"]
SPACE = str(UUID(int=200))


@pytest.fixture
def withdrawal_chain(chain, monkeypatch):
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    f = attach_finalizer(chain, monkeypatch)
    first = chain.invoke(ip_address="192.0.2.7")
    f.account_id = str(first.persistence.account_id)
    for model in (App, Dataset, Agent):
        model.__table__.create(chain.local.engine)
    with Session(chain.local.engine) as s, s.begin():
        recipient = Account(
            name="Owner", email="owner@example.test", status=AccountStatus.ACTIVE, initialized_at=datetime(2026, 1, 1)
        )
        s.add(recipient)
        s.flush()
        f.owner_id = recipient.id
        s.add(Join(tenant_id=SPACE, account_id=recipient.id, role=TenantAccountRole.OWNER))
        app = App(
            tenant_id=SPACE,
            name="Original",
            mode="chat",
            enable_api=False,
            enable_site=False,
            created_by=f.account_id,
            maintainer=f.account_id,
        )
        s.add(app)
        s.add(Dataset(tenant_id=SPACE, name="Original", created_by=f.account_id, maintainer=f.account_id))
        s.flush()
        s.add(
            Agent(
                tenant_id=SPACE,
                name="Lineage",
                scope=AgentScope.WORKFLOW_ONLY,
                source=AgentSource.WORKFLOW,
                app_id=app.id,
                backing_app_id=app.id,
                workflow_id=str(uuid4()),
                workflow_node_id="node",
                active_config_snapshot_id=str(uuid4()),
                created_by=f.account_id,
                updated_by=f.account_id,
            )
        )
        s.execute(sa.update(Join).where(Join.account_id == f.account_id).values(current=False))
        s.execute(sa.update(Join).where(Join.account_id == f.account_id, Join.tenant_id == SPACE).values(current=True))
    f.calls.clear()
    f.cache_calls.clear()
    return f


def history(f):
    with Session(f.chain.local.engine) as s:
        return s.execute(sa.select(*History.__table__.columns).where(History.workspace_id == SPACE)).one()


def snapshot(f):
    with Session(f.chain.local.engine) as s:
        return {
            model.__tablename__: tuple(
                tuple(r) for r in s.execute(sa.select(*model.__table__.columns).order_by(model.id))
            )
            for model in (Account, Quota, Identity, Join, History, Audit, App, Dataset, Agent)
        }


def login(f, *, hit=False):
    f.chain.roles[0]["users"] = ["Org/person"] if hit else []
    return new_authorization(f.chain)


@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize("initial_none", [False, True])
def test_signed_withdraw_nohit_regrant_and_later_ordinary(withdrawal_chain, source, initial_none):
    f = withdrawal_chain
    with Session(f.chain.local.engine) as s, s.begin():
        changes = {"source": source}
        if initial_none:
            changes["baseline_json"] = role_baseline_json(
                MembershipObservation(UUID(SPACE), UUID(f.account_id), None, None, MembershipBackend.LOCAL)
            )
        s.execute(sa.update(History).where(History.workspace_id == SPACE).values(**changes))
    before = history(f)
    stable = snapshot(f)
    withdrawn = login(f)
    absent = history(f)
    assert withdrawn.tokens and len(withdrawn.persistence.withdrawals) == 1
    assert absent.join_id == before.join_id and absent.ownership_epoch == before.ownership_epoch + 1
    assert absent.source == source and absent.baseline_json == before.baseline_json
    assert absent.finalization is Finalization.FINALIZED and not absent.tombstone
    with Session(f.chain.local.engine) as s:
        assert s.get(Join, before.join_id) is None
        assert s.scalar(sa.select(App.maintainer)) == f.owner_id
        assert s.scalar(sa.select(Dataset.maintainer)) == f.owner_id
        assert (
            s.scalar(sa.select(Join.tenant_id).where(Join.account_id == f.account_id, Join.current.is_(True))) != SPACE
        )
    nohit = login(f)
    assert nohit.tokens and not nohit.persistence.withdrawals
    assert history(f) == absent
    returned = login(f, hit=True)
    rejoined = history(f)
    assert returned.tokens and returned.persistence.workspaces[1].membership_regranted
    assert rejoined.id == before.id and rejoined.join_id != before.join_id
    assert rejoined.ownership_epoch == before.ownership_epoch + 2
    ordinary = login(f, hit=True)
    assert ordinary.tokens and not ordinary.persistence.workspaces[1].membership_regranted
    assert history(f).baseline_json == before.baseline_json
    after = snapshot(f)
    for model in (Quota, Agent):
        assert after[model.__tablename__] == stable[model.__tablename__]
    assert len(after[Account.__tablename__]) == 2 and len(after[Identity.__tablename__]) == 1
    with Session(f.chain.local.engine) as s:
        assert s.scalar(sa.select(App.maintainer)) == f.owner_id
        assert s.scalar(sa.select(Dataset.maintainer)) == f.owner_id
    assert not f.cache_calls


@pytest.mark.parametrize("stage", ["withdraw", "regrant", "ordinary"])
@pytest.mark.parametrize("field", ["baseline_json", "desired_roles_json", "source", "created_at", "finalization"])
def test_b2_fullrow_drift_rolls_back_before_b3(withdrawal_chain, monkeypatch, stage, field):
    f = withdrawal_chain
    if stage == "regrant":
        login(f)
        f.calls.clear()
    before = snapshot(f)
    original = CasdoorLoginAccountService.persist_login_account

    def drift(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        s = kwargs["session"]
        if field == "baseline_json":
            value = role_baseline_json(
                MembershipObservation(UUID(SPACE), UUID(f.account_id), None, None, MembershipBackend.LOCAL)
            )
        elif field == "desired_roles_json":
            value = "{}"
        elif field == "source":
            value = Source.ADOPT
        elif field == "created_at":
            value = datetime(2000, 1, 1)
        else:
            value = Finalization.PENDING
        s.execute(sa.update(History).where(History.workspace_id == SPACE).values(**{field: value}))
        return result

    monkeypatch.setattr(CasdoorLoginAccountService, "persist_login_account", drift)
    with pytest.raises(ValueError) as error:
        login(f, hit=stage != "withdraw")
    assert error.value.local_outcome == "unknown" and error.value.cleanup_released
    assert snapshot(f) == before and not f.calls


@pytest.mark.parametrize("field", ["baseline_json", "source", "created_at", "updated_at", "finalization"])
def test_post_audit_fullrow_drift_rolls_back_whole_c1(withdrawal_chain, monkeypatch, field):
    f = withdrawal_chain
    before = snapshot(f)
    original = CasdoorAuditRepository.append_local_membership

    def drift(owner, **kwargs):
        result = original(owner, **kwargs)
        value = (
            "{}"
            if field == "baseline_json"
            else Source.ADOPT
            if field == "source"
            else Finalization.FINALIZED
            if field == "finalization"
            else datetime(2000, 1, 1)
        )
        owner._session.execute(sa.update(History).where(History.workspace_id == SPACE).values(**{field: value}))
        return result

    monkeypatch.setattr(CasdoorAuditRepository, "append_local_membership", drift)
    with pytest.raises(ValueError):
        login(f)
    assert snapshot(f) == before and not f.calls


def test_enterprise_controlled_denied_before_account_consume(withdrawal_chain, monkeypatch):
    f = withdrawal_chain
    before = snapshot(f)
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.ENTERPRISE)
    with pytest.raises(ValueError):
        login(f)
    assert not f.chain.prepared[-1]._consumed
    assert snapshot(f) == before and not f.calls


def add_prior(f):
    from models.account import Tenant

    with Session(f.chain.local.engine) as s, s.begin():
        space = Tenant(name="Earlier managed workspace")
        space.id = str(UUID(int=300))
        s.add(space)
        s.flush()
        s.add(Join(tenant_id=space.id, account_id=f.owner_id, role=TenantAccountRole.OWNER))
        joined = Join(tenant_id=space.id, account_id=f.account_id, role=TenantAccountRole.ADMIN)
        s.add(joined)
        s.flush()
        old = s.scalar(sa.select(History).where(History.workspace_id == SPACE))
        fields = {
            c.name: getattr(old, c.name)
            for c in History.__table__.columns
            if c.name not in ("id", "created_at", "updated_at")
        }
        fields.update(workspace_id=space.id, join_id=joined.id)
        s.add(History(**fields))
    return str(UUID(int=300))


@pytest.mark.parametrize("stage", ["withdraw", "regrant", "ordinary"])
@pytest.mark.parametrize("field", ["baseline_json", "desired_roles_json"])
def test_real_sql_trigger_at_b2_boundary_cannot_be_overwritten(withdrawal_chain, monkeypatch, stage, field):
    import json

    f = withdrawal_chain
    if stage == "regrant":
        login(f)
        f.calls.clear()
    row = history(f)
    value = role_baseline_json(
        MembershipObservation(UUID(SPACE), UUID(f.account_id), None, None, MembershipBackend.LOCAL)
    )
    if field == "desired_roles_json":
        desired = json.loads(row.desired_roles_json)
        # Still valid canonical schema; a later ordinary role owner could overwrite it.
        desired["fence_epoch"] = 1
        value = json.dumps(desired, sort_keys=True, separators=(",", ":"))
    with Session(f.chain.local.engine) as s, s.begin():
        s.connection().exec_driver_sql(
            "CREATE TRIGGER b2_fullrow_drift AFTER UPDATE ON casdoor_identity_extend "
            "WHEN NEW.sync_generation = OLD.sync_generation BEGIN UPDATE casdoor_managed_membership_extend SET "
            + field
            + " = '"
            + value.replace("'", "''")
            + "' WHERE workspace_id = '"
            + SPACE
            + "'; END"
        )
    original = CasdoorLoginAccountService.persist_login_account
    fired = []

    def b2_statement(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        # Actual USE_BOUND is read-only. Inject one SQL statement at its exit;
        # the database trigger performs the history mutation before C1 rereads.
        kwargs["session"].execute(sa.update(Identity).values(sync_generation=Identity.sync_generation))
        fired.append(True)
        return result

    monkeypatch.setattr(CasdoorLoginAccountService, "persist_login_account", b2_statement)
    before = snapshot(f)
    with pytest.raises(ValueError):
        login(f, hit=stage != "withdraw")
    assert fired == [True]
    assert snapshot(f) == before and not f.calls


@pytest.mark.parametrize("failure", ["delete", "cas", "audit"])
def test_second_workspace_sql_failure_restores_first_effect(withdrawal_chain, failure):
    from sqlalchemy.exc import IntegrityError

    f = withdrawal_chain
    second = add_prior(f)
    clauses = {
        "delete": "BEFORE DELETE ON tenant_account_joins WHEN OLD.tenant_id = '"
        + second
        + "' AND OLD.account_id = '"
        + f.account_id
        + "'",
        "cas": "BEFORE UPDATE ON casdoor_managed_membership_extend WHEN NEW.workspace_id = '"
        + second
        + "' AND NEW.ownership_epoch > OLD.ownership_epoch",
        "audit": (
            "BEFORE INSERT ON casdoor_audit_extend WHEN NEW.action = 'local_member_withdraw' "
            "AND (SELECT COUNT(*) FROM casdoor_audit_extend WHERE action = 'local_member_withdraw') = 1"
        ),
    }
    with Session(f.chain.local.engine) as s, s.begin():
        s.execute(
            sa.text(
                "CREATE TRIGGER second_effect_failure "
                + clauses[failure]
                + " BEGIN SELECT RAISE(ABORT, 'offline second effect'); END"
            )
        )
    before = snapshot(f)
    with pytest.raises(IntegrityError):
        login(f)
    assert snapshot(f) == before and not f.calls


@pytest.mark.parametrize("stage", ["withdraw", "regrant"])
@pytest.mark.parametrize("association", ["scope", "null", "linked"])
def test_late_confirmed_intent_after_audit_rolls_back_c1(withdrawal_chain, monkeypatch, stage, association):
    from models.casdoor_extend import CasdoorIntentKind, CasdoorOperationState, CasdoorTerminationState
    from models.casdoor_extend import CasdoorSyncIntentExtend as Intent

    f = withdrawal_chain
    if stage == "regrant":
        login(f)
        f.calls.clear()
    before = snapshot(f)
    original = CasdoorAuditRepository.append_local_membership

    def late(owner, **kwargs):
        original(owner, **kwargs)
        values = dict(
            namespace_id=str(kwargs["namespace_id"]),
            identity_id=str(kwargs["identity_id"]),
            account_id=f.account_id,
            workspace_id=SPACE,
            revision_id=str(kwargs["revision_id"]),
            generation=1,
            ownership_epoch=0,
            fence_epoch=0,
            kind=CasdoorIntentKind.ROLE_REPLACE,
            scope_digest="a" * 64,
            idempotency_key=uuid4().hex * 2,
            desired_json="{}",
            operation_state=CasdoorOperationState.APPLIED,
            termination_state=CasdoorTerminationState.CONFIRMED,
        )
        if association == "null":
            values["workspace_id"] = None
        elif association == "linked":
            values.update(
                account_id=str(uuid4()), workspace_id=str(uuid4()), membership_id=str(kwargs["membership_id"])
            )
        owner._session.add(Intent(**values))
        owner._session.flush()

    monkeypatch.setattr(CasdoorAuditRepository, "append_local_membership", late)
    with pytest.raises(ValueError):
        login(f, hit=stage == "regrant")
    assert snapshot(f) == before and not f.calls
    with Session(f.chain.local.engine) as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(Intent)) == 0


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "extra_join", "old_id_foreign", "old_id_preserved", "immutable_after"]
)
def test_final_scope_requires_exact_original_effects(withdrawal_chain, monkeypatch, mutation):
    from dataclasses import replace

    from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService

    f = withdrawal_chain
    if mutation in ("old_id_foreign", "old_id_preserved"):
        login(f)
        f.calls.clear()
    before = snapshot(f)
    retained_id = history(f).join_id
    original = CasdoorLocalMembershipService.persist_local_memberships

    def tamper(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        effects = result._controlled_effects
        if mutation == "missing":
            return replace(result, _controlled_effects=())
        if mutation == "duplicate":
            return replace(result, _controlled_effects=effects + effects)
        if mutation == "immutable_after":
            row = effects[0].after._replace(source=Source.ADOPT)
            owner._session.execute(sa.update(History).where(History.id == row.id).values(source=Source.ADOPT))
            from repositories.casdoor_membership_repository_extend import CasdoorMembershipRepository

            return replace(
                result,
                _controlled_effects=(
                    replace(effects[0], after=row, managed=CasdoorMembershipRepository._snapshot(row)),
                ),
            )
        join = Join(tenant_id=str(UUID(int=100)), account_id=f.owner_id, role=TenantAccountRole.NORMAL)
        if mutation in ("old_id_foreign", "old_id_preserved"):
            join.id = retained_id
        else:
            join.tenant_id = SPACE
            join.account_id = f.account_id
        owner._session.add(join)
        owner._session.flush()
        return result

    monkeypatch.setattr(CasdoorLocalMembershipService, "persist_local_memberships", tamper)
    with pytest.raises(ValueError):
        login(f, hit=mutation == "old_id_foreign")
    assert snapshot(f) == before and not f.calls


@pytest.mark.parametrize("protection", ["owner", "override", "tombstone", "unmanaged"])
def test_lost_role_preserves_original_uncontrolled_membership(withdrawal_chain, protection):
    from models.casdoor_extend import CasdoorMembershipOwnership

    f = withdrawal_chain
    with Session(f.chain.local.engine) as s, s.begin():
        if protection == "owner":
            s.execute(
                sa.update(Join)
                .where(Join.tenant_id == SPACE, Join.account_id == f.account_id)
                .values(role=TenantAccountRole.OWNER)
            )
        elif protection == "unmanaged":
            s.execute(sa.delete(History).where(History.workspace_id == SPACE))
        else:
            s.execute(
                sa.update(History)
                .where(History.workspace_id == SPACE)
                .values(ownership=CasdoorMembershipOwnership.LOCAL_OVERRIDE, tombstone=protection == "tombstone")
            )
    before = snapshot(f)
    if protection == "owner":
        with pytest.raises(ValueError):
            login(f)
        assert snapshot(f) == before
    else:
        assert login(f).tokens
        with Session(f.chain.local.engine) as s:
            assert (
                s.scalar(sa.select(Join.role).where(Join.tenant_id == SPACE, Join.account_id == f.account_id))
                is TenantAccountRole.ADMIN
            )
            assert s.scalar(sa.select(App.maintainer)) == f.account_id


def test_withdrawn_current_uses_original_join_id_fallback_not_fixed_default(withdrawal_chain):
    from models.account import Tenant

    f = withdrawal_chain
    other = str(UUID(int=300))
    with Session(f.chain.local.engine) as s, s.begin():
        space = Tenant(name="Existing normal membership")
        space.id = other
        s.add(space)
        s.flush()
        join = Join(tenant_id=other, account_id=f.account_id, role=TenantAccountRole.NORMAL)
        join.id = str(UUID(int=1))
        s.add(join)
    assert login(f).tokens
    with Session(f.chain.local.engine) as s:
        assert (
            s.scalar(sa.select(Join.tenant_id).where(Join.account_id == f.account_id, Join.current.is_(True))) == other
        )
