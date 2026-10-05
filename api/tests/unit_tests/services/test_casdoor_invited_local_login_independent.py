"""Independent actual-private-caller faults for invited LOCAL initialization."""

import pytest
import sqlalchemy as sa
from models.account_money_extend import AccountMoneyExtend
from sqlalchemy.orm import Session

from test_casdoor_invited_local_login_coordinator_extend import snapshot

pytest_plugins = (
    "test_casdoor_local_login_service_extend",
    "test_casdoor_local_login_coordinator_service_extend",
    "test_casdoor_invited_local_login_coordinator_extend",
)


def _remove_quota(case):
    with Session(case.chain.local.engine) as session, session.begin():
        session.execute(
            sa.delete(AccountMoneyExtend).where(
                AccountMoneyExtend.account_id == case.account
            )
        )


def test_missing_quota_owner_failure_rolls_back_actual_account_initialization(
    invited, monkeypatch
):
    case = invited
    _remove_quota(case)
    before = snapshot(case)
    import services.account_quota_service_extend as quota_owner

    original = quota_owner.ensure_account_quota_extend
    calls = []

    def fail_after_real_quota_write(account_id, *, session):
        calls.append(account_id)
        original(account_id, session=session)
        raise RuntimeError("private quota owner failure")

    monkeypatch.setattr(
        quota_owner, "ensure_account_quota_extend", fail_after_real_quota_write
    )
    with pytest.raises(Exception) as failure:
        case.invoke()

    assert failure.value.local_outcome == "unknown"
    assert calls == [str(case.account)]
    assert snapshot(case) == before
    assert not case.chain.redis.data
    assert case.redis.token_key in case.redis.entries


def test_lease_loss_after_missing_quota_creation_rolls_back_same_sql_root(
    invited, monkeypatch
):
    case = invited
    _remove_quota(case)
    before = snapshot(case)
    import services.account_quota_service_extend as quota_owner

    original = quota_owner.ensure_account_quota_extend
    calls = []

    def lose_lease_after_real_quota_write(account_id, *, session):
        calls.append(account_id)
        original(account_id, session=session)
        case.chain.redis.data.clear()

    monkeypatch.setattr(
        quota_owner, "ensure_account_quota_extend", lose_lease_after_real_quota_write
    )
    with pytest.raises(Exception) as failure:
        case.invoke()

    assert failure.value.local_outcome == "unknown"
    assert calls == [str(case.account)]
    assert snapshot(case) == before
    assert case.redis.token_key in case.redis.entries
