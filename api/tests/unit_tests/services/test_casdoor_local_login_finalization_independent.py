"""Independent adversarial checks for the private LOCAL finalization tail."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from enums import DeploymentEdition
from models.account import Account
from models.casdoor_extend import CasdoorFinalizationState as Finalization
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend
from sqlalchemy.orm import Session

pytest_plugins = ["test_casdoor_local_login_finalization_service_extend"]


def test_intent_arriving_during_cache_invalidation_blocks_finalization_and_issue(finalized_chain, monkeypatch):
    """The second fresh barrier must see work committed after the first read."""
    from services.billing_service import BillingService

    f = finalized_chain
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.CLOUD)
    inserted = []

    def insert_intent(workspace_id):
        # This is the actual gap between the finalizer's first read root and
        # its write root.  Commit a late non-avatar obligation on a different
        # historical/NULL-workspace scope while C1's full leases remain held.
        assert not f.chain.opened and f.chain.redis.data
        with Session(f.chain.local.engine) as session, session.begin():
            history = session.scalar(sa.select(History).limit(1))
            intent = CasdoorSyncIntentExtend(
                namespace_id=history.namespace_id,
                identity_id=history.identity_id,
                account_id=history.account_id,
                workspace_id=None,
                membership_id=None,
                revision_id=history.revision_id,
                generation=0,
                ownership_epoch=0,
                fence_epoch=0,
                kind="resource_grant",
                scope_digest="c" * 64,
                idempotency_key=uuid4().hex * 2,
                desired_json="{}",
                operation_state="unknown",
                termination_state="confirmed",
            )
            session.add(intent)
            session.flush()
            inserted.append(intent.id)

    monkeypatch.setattr(BillingService, "clean_billing_info_cache", insert_intent)
    with pytest.raises(ValueError) as error:
        f.chain.invoke(ip_address="192.0.2.7")

    assert inserted
    assert error.value.local_outcome == "committed"
    assert error.value.finalization_outcome == "not_started"
    assert error.value.token_outcome == "not_started"
    assert error.value.cleanup_released is True
    assert not f.calls
    with Session(f.chain.local.engine) as session:
        assert set(session.scalars(sa.select(History.finalization))) == {Finalization.PENDING}
        assert session.scalar(sa.select(Account.last_login_at)) is None
        intent = session.get(CasdoorSyncIntentExtend, inserted[0])
        assert intent is not None and intent.operation_state == "unknown"
