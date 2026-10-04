"""Independent full-row gate check at the original CLOUD cache boundary."""

from datetime import datetime

import pytest
import sqlalchemy as sa
from configs import dify_config
from enums import DeploymentEdition
from models.account import Account
from models.account import TenantAccountJoin as Join
from models.account_money_extend import AccountMoneyExtend as Quota
from models.casdoor_extend import CasdoorFinalizationState as Finalization
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from services.account_adapters import BillingWorkspaceMembershipCache
from sqlalchemy.orm import Session
from test_casdoor_local_withdraw_login_extend import (
    SPACE,
    history,
    login,
    snapshot,
    withdrawal_chain,
)

pytest_plugins = ["test_casdoor_local_login_coordinator_service_extend"]
withdrawal_fixture = withdrawal_chain


def test_cloud_cache_created_at_only_commit_denies_issuance_and_preserves_withdrawal(
    withdrawal_fixture, monkeypatch
):
    f = withdrawal_fixture
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.CLOUD)
    changed_created_at = datetime(2000, 1, 1)
    cache_calls = []
    gap_rows = []
    original_state = snapshot(f)
    with Session(f.chain.local.engine) as s:
        original_identity = s.scalar(
            sa.select(Identity).where(Identity.account_id == f.account_id)
        )
        original_generation = original_identity.sync_generation

    def commit_created_at_drift(cache, workspace_id):
        assert workspace_id == SPACE
        assert not f.chain.opened and f.chain.redis.data
        cache_calls.append(workspace_id)
        # This second root commits while the original C2 leases remain held.
        with Session(f.chain.local.engine) as other_root, other_root.begin():
            before = other_root.execute(
                sa.select(*History.__table__.columns).where(
                    History.workspace_id == workspace_id
                )
            ).one()
            gap_rows.append(before)
            other_root.execute(
                sa.update(History)
                .where(History.workspace_id == workspace_id)
                .values(created_at=changed_created_at, updated_at=before.updated_at)
            )
            after = other_root.execute(
                sa.select(*History.__table__.columns).where(
                    History.workspace_id == workspace_id
                )
            ).one()
            assert after.created_at == changed_created_at
            for column in History.__table__.columns:
                if column.name != "created_at":
                    assert after._mapping[column.name] == before._mapping[column.name]

    monkeypatch.setattr(
        BillingWorkspaceMembershipCache, "invalidate", commit_created_at_drift
    )
    with pytest.raises(ValueError) as error:
        login(f)

    assert (
        error.value.local_outcome == "committed"
        and error.value.token_outcome == "not_started"
    )
    assert cache_calls == [SPACE]
    assert len(gap_rows) == 1
    assert not f.calls
    committed = history(f)
    assert committed.created_at == changed_created_at
    gap_row = gap_rows[0]
    assert committed.updated_at == gap_row.updated_at
    for column in History.__table__.columns:
        if column.name != "created_at":
            assert committed._mapping[column.name] == gap_row._mapping[column.name]
    assert committed.finalization is Finalization.PENDING
    final_state = snapshot(f)
    assert final_state[Account.__tablename__] == original_state[Account.__tablename__]
    assert final_state[Quota.__tablename__] == original_state[Quota.__tablename__]
    with Session(f.chain.local.engine) as s:
        identity = s.get(Identity, original_identity.id)
        assert identity.sync_generation == original_generation + 1
    with Session(f.chain.local.engine) as s:
        assert s.get(Join, committed.join_id) is None
