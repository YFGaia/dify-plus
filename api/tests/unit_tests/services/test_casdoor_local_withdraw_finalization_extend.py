"""Committed controlled absence recovery and exact finalization freeze gates."""

from datetime import datetime
from uuid import UUID

import pytest
import sqlalchemy as sa
from configs import dify_config
from enums import DeploymentEdition
from models.account import Tenant, TenantAccountRole
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorFinalizationState as Finalization
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorMembershipSource as Source
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
from services.account_adapters import BillingWorkspaceMembershipCache
from services.casdoor_local_login_finalization_service_extend import CasdoorLocalLoginFinalizationService
from sqlalchemy.orm import Session
from test_casdoor_local_withdraw_login_extend import SPACE, history, login, withdrawal_chain

pytest_plugins = ["test_casdoor_local_login_coordinator_service_extend"]
withdrawal_fixture = withdrawal_chain


@pytest.mark.parametrize("multiple", [False, True])
def test_cloud_cache_failure_commits_absence_then_fresh_nohit_recovers(withdrawal_fixture, monkeypatch, multiple):
    f = withdrawal_fixture
    if multiple:
        with Session(f.chain.local.engine) as s, s.begin():
            space = Tenant(name="Prior workspace")
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
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.CLOUD)
    original = BillingWorkspaceMembershipCache.invalidate
    cached = []

    def fail(cache, workspace_id):
        assert not f.chain.opened and f.chain.redis.data
        cached.append(workspace_id)
        raise TimeoutError("offline cache failed after C1 commit")

    monkeypatch.setattr(BillingWorkspaceMembershipCache, "invalidate", fail)
    with pytest.raises(TimeoutError) as error:
        login(f)
    assert error.value.local_outcome == "committed" and error.value.token_outcome == "not_started"
    pending = history(f)
    assert pending.finalization is Finalization.PENDING and not f.calls
    with Session(f.chain.local.engine) as s:
        assert s.get(Join, pending.join_id) is None
    monkeypatch.setattr(BillingWorkspaceMembershipCache, "invalidate", original)
    recovered = login(f)
    assert recovered.tokens and not recovered.persistence.withdrawals
    assert recovered.persistence.generation > pending.desired_generation
    done = history(f)
    assert done.finalization is Finalization.FINALIZED
    for field in pending._mapping:
        if field not in ("finalization", "updated_at"):
            assert getattr(done, field) == getattr(pending, field)
    assert len(f.cache_calls) == (2 if multiple else 1)
    caches = len(f.cache_calls)
    login(f)
    assert len(f.cache_calls) == caches


@pytest.mark.parametrize(
    "field", ["baseline_json", "desired_roles_json", "source", "created_at", "updated_at", "finalization"]
)
def test_cache_to_write_fullrow_drift_denies_tokens(withdrawal_fixture, monkeypatch, field):
    f = withdrawal_fixture
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.CLOUD)

    def tamper(cache, workspace_id):
        assert not f.chain.opened and f.chain.redis.data
        with Session(f.chain.local.engine) as s, s.begin():
            value = (
                "{}"
                if field.endswith("json")
                else Source.ADOPT
                if field == "source"
                else Finalization.FINALIZED
                if field == "finalization"
                else datetime(2000, 1, 1)
            )
            s.execute(sa.update(History).where(History.workspace_id == SPACE).values(**{field: value}))

    monkeypatch.setattr(BillingWorkspaceMembershipCache, "invalidate", tamper)
    with pytest.raises(ValueError) as error:
        login(f)
    assert error.value.local_outcome == "committed" and error.value.token_outcome == "not_started"
    assert not f.calls


@pytest.mark.parametrize("stage", ["cas", "metadata", "final_read"])
@pytest.mark.parametrize("field", ["source", "created_at", "updated_at", "baseline_json"])
def test_i19_late_fullrow_drift_rejects_original_issuer(withdrawal_fixture, monkeypatch, stage, field):
    f = withdrawal_fixture
    original_finalize = CasdoorLocalLoginFinalizationService._finalize
    original_metadata = CasdoorLocalLoginFinalizationService._login_metadata
    original_discover = CasdoorLoginScopeRepository.discover
    fired = False

    def change(session):
        nonlocal fired
        fired = True
        value = Source.ADOPT if field == "source" else "{}" if field == "baseline_json" else datetime(2000, 1, 1)
        session.execute(sa.update(History).where(History.workspace_id == SPACE).values(**{field: value}))

    if stage == "cas":

        def finalize(session, row):
            original_finalize(session, row)
            change(session)

        monkeypatch.setattr(CasdoorLocalLoginFinalizationService, "_finalize", staticmethod(finalize))
    elif stage == "metadata":

        def metadata(session, *args):
            result = original_metadata(session, *args)
            change(session)
            return result

        monkeypatch.setattr(CasdoorLocalLoginFinalizationService, "_login_metadata", staticmethod(metadata))
    else:
        # Change after the I19 write commit, before final clean read discovers it.
        commits = 0

        def discover(owner, *args, **kwargs):
            nonlocal commits
            if history_phase[0] and not fired:
                change(owner.session)
            return original_discover(owner, *args, **kwargs)

        history_phase = [False]

        def committed(session):
            nonlocal commits
            commits += 1
            # Detect write-root finalization using its actual flushed history state.
            if stage == "final_read" and finalized_write[0]:
                history_phase[0] = True

        finalized_write = [False]

        def finalize(session, row):
            original_finalize(session, row)
            finalized_write[0] = True

        monkeypatch.setattr(CasdoorLocalLoginFinalizationService, "_finalize", staticmethod(finalize))
        monkeypatch.setattr(CasdoorLoginScopeRepository, "discover", discover)
        sa.event.listen(Session, "after_commit", committed)
    try:
        # model-owned updated_at directly after CAS is the explicitly allowed delta.
        if stage == "cas" and field == "updated_at":
            assert login(f).tokens
        else:
            with pytest.raises(ValueError) as error:
                login(f)
            assert error.value.local_outcome == "committed" and error.value.token_outcome == "not_started"
            assert not f.calls
    finally:
        if stage == "final_read":
            sa.event.remove(Session, "after_commit", committed)
    assert fired


@pytest.mark.parametrize("failure", ["zero_rows", "abort"])
def test_second_absence_finalization_rolls_back_only_i19(withdrawal_fixture, failure):
    from sqlalchemy.exc import IntegrityError
    from test_casdoor_local_withdraw_login_extend import add_prior

    f = withdrawal_fixture
    second = add_prior(f)
    with Session(f.chain.local.engine) as s, s.begin():
        ending = "SELECT RAISE(IGNORE);" if failure == "zero_rows" else "SELECT RAISE(ABORT, 'offline I19');"
        s.execute(
            sa.text(
                "CREATE TRIGGER second_finalization BEFORE UPDATE ON casdoor_managed_membership_extend "
                "WHEN NEW.workspace_id = '" + second + "' AND NEW.finalization = 'finalized' "
                "AND OLD.finalization = 'pending' BEGIN " + ending + " END"
            )
        )
    with pytest.raises((ValueError, IntegrityError)) as error:
        login(f)
    assert error.value.local_outcome == "committed"
    assert error.value.finalization_outcome == "not_committed"
    assert error.value.token_outcome == "not_started" and not f.calls
    with Session(f.chain.local.engine) as s:
        rows = tuple(
            s.execute(sa.select(History.join_id, History.finalization).where(History.workspace_id.in_((SPACE, second))))
        )
        assert len(rows) == 2
        assert all(r.finalization is Finalization.PENDING and s.get(Join, r.join_id) is None for r in rows)


@pytest.mark.parametrize("epoch", [0, 1, 2])
@pytest.mark.parametrize("workspace", [100, 200])
def test_persistent_initial_nojoin_only_supported_nondefault_epoch(withdrawal_fixture, epoch, workspace):
    from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json

    f = withdrawal_fixture
    workspace_id = str(UUID(int=workspace))
    with Session(f.chain.local.engine) as s, s.begin():
        baseline = role_baseline_json(
            MembershipObservation(UUID(workspace_id), UUID(f.account_id), None, None, MembershipBackend.LOCAL)
        )
        s.execute(
            sa.update(History)
            .where(History.workspace_id == workspace_id)
            .values(baseline_json=baseline, ownership_epoch=epoch)
        )
    if epoch >= 2 and workspace == 200:
        assert login(f, hit=True).tokens
    else:
        with pytest.raises(ValueError) as error:
            login(f, hit=True)
        assert error.value.local_outcome == "committed" and error.value.token_outcome == "not_started"
        assert not f.calls


@pytest.mark.parametrize("field", ["created_at", "updated_at", "source", "baseline_json", "finalization"])
def test_original_fullrow_cas_rejects_drift_immediately_before_update(withdrawal_fixture, monkeypatch, field):
    f = withdrawal_fixture
    original = CasdoorLocalLoginFinalizationService._finalize

    def drift(session, row):
        value = (
            Source.ADOPT
            if field == "source"
            else "{}"
            if field == "baseline_json"
            else Finalization.FINALIZED
            if field == "finalization"
            else datetime(2000, 1, 1)
        )
        session.execute(sa.update(History).where(History.id == row.id).values(**{field: value}))
        original(session, row)

    monkeypatch.setattr(CasdoorLocalLoginFinalizationService, "_finalize", staticmethod(drift))
    with pytest.raises(ValueError) as error:
        login(f)
    assert error.value.local_outcome == "committed" and error.value.finalization_outcome == "not_committed"
    assert history(f).finalization is Finalization.PENDING and not f.calls
