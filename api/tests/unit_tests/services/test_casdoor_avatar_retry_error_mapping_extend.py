"""Registered manager transport rejection with the original native source fixture."""

from uuid import uuid4

import sqlalchemy as sa
from models.casdoor_extend import CasdoorAuditExtend as Audit
from sqlalchemy.orm import Session
from test_casdoor_avatar_consumer_extend import row
from test_casdoor_avatar_pre_storage_recovery_extend import produce
from test_casdoor_avatar_retry_flow_extend import avatar_fixture as original_avatar_fixture
from test_casdoor_avatar_retry_flow_extend import consumer as original_consumer
from test_casdoor_avatar_retry_flow_extend import storage_fixture as original_storage_fixture
from test_casdoor_avatar_retry_mounted_extend import mounted as original_mounted
from test_casdoor_avatar_retry_mounted_extend import registered as original_registered

avatar_fixture = original_avatar_fixture
consumer = original_consumer
storage_fixture = original_storage_fixture
mounted = original_mounted
registered = original_registered


def test_registered_missing_retry_intent_is_expected_rejection_without_effects(mounted):
    fixture = mounted
    produce(fixture)
    before = row(fixture)
    response = fixture.send(
        "/console/api/system-manage-extend/integration/casdoor/sync/retry",
        method="POST",
        json={"intent_id": str(uuid4())},
    )
    assert row(fixture) == before
    assert not fixture.calls and not fixture.published
    with Session(fixture.session.get_bind()) as session:
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(Audit)
                .where(Audit.action.in_(("avatar_retry", "avatar_retry_claim")))
            )
            == 0
        )
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.json["code"] == "invalid_transaction"
