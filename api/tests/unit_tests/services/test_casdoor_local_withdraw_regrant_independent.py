"""Independent actual-path adversarial check for a regrant race."""

import pytest
import sqlalchemy as sa
import test_casdoor_local_withdraw_regrant_extend as author
from models.account import TenantAccountJoin, TenantAccountRole
from services.account_service import TenantService


@pytest.fixture
def state(sqlite_session_factory, config_overrides):
    yield from author.state.__wrapped__(sqlite_session_factory, config_overrides)


def test_regrant_rejects_retained_join_id_resurrected_in_another_workspace(
    state, monkeypatch
):
    s = state
    with author.caller(s):
        author.withdrawal(s)
        retained_join_id = author.history(s).join_id
    before = author.snapshot(s)
    original = TenantService.persist_tenant_member

    def regrant_then_resurrect(tenant, account, session, role):
        receipt = original(tenant, account, session, role)
        if tenant.id == s.spaces[1].id:
            assert receipt.membership_created is True
            # Simulate a concurrent stale mapping inserted after preparation and
            # after the real helper, in another namespace/scope.
            stale = TenantAccountJoin(
                tenant_id=s.spaces[0].id,
                account_id=s.owner.id,
                role=TenantAccountRole.NORMAL,
            )
            stale.id = retained_join_id
            session.add(stale)
            session.flush()
        return receipt

    monkeypatch.setattr(TenantService, "persist_tenant_member", regrant_then_resurrect)
    with pytest.raises(ValueError), author.caller(s):
        author.invoke(s, generation=2)

    # The entire caller root restores the old absence, generation, and all
    # account/resource/history/audit rows after the real helper had inserted a
    # new scoped join and the race inserted the retained ID elsewhere.
    assert author.snapshot(s) == before
    with s.factory() as observer:
        assert (
            observer.scalar(
                sa.select(TenantAccountJoin.id).where(
                    TenantAccountJoin.id == retained_join_id
                )
            )
            is None
        )
