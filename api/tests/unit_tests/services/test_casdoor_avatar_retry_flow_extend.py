"""Actual manager/source admission and uncertain commit/second-attempt boundaries."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.permissions import CasdoorManagementPolicy
from models.casdoor_extend import CasdoorAuditExtend as Audit
from sqlalchemy.orm import Session, SessionTransaction
from test_casdoor_avatar_consumer_extend import row
from test_casdoor_avatar_pre_storage_recovery_extend import produce

from tests.unit_tests.controllers.test_casdoor_avatar_retry_mounted_extend import BASE
from tests.unit_tests.controllers.test_casdoor_avatar_retry_mounted_extend import (
    avatar_fixture as original_avatar_fixture,
)
from tests.unit_tests.controllers.test_casdoor_avatar_retry_mounted_extend import (
    consumer as original_consumer,
)
from tests.unit_tests.controllers.test_casdoor_avatar_retry_mounted_extend import (
    mounted as original_mounted,
)
from tests.unit_tests.controllers.test_casdoor_avatar_retry_mounted_extend import (
    registered as original_registered,
)
from tests.unit_tests.controllers.test_casdoor_avatar_retry_mounted_extend import (
    storage_fixture as original_storage_fixture,
)

avatar_fixture = original_avatar_fixture
consumer = original_consumer
mounted = original_mounted
registered = original_registered
storage_fixture = original_storage_fixture


def post(s):
    return s.send(BASE + "/retry", method="POST", json={"intent_id": str(s.intent_id)})


def test_normal_authenticated_account_is_not_a_manager(mounted):
    s = mounted
    produce(s)
    s.services.casdoor_configuration._management_policy = (
        CasdoorManagementPolicy()
    )
    before = row(s)
    assert s.send(BASE + "/retry-targets").status_code == 403
    assert post(s).status_code == 403
    assert row(s) == before and not s.data and not s.published


def test_expired_original_url_is_not_extended_or_renewed(mounted):
    s = mounted
    produce(s)
    old = row(s)
    s.utc += timedelta(seconds=301)
    assert s.send(BASE + "/retry-targets").json["targets"] == []
    assert post(s).status_code != 200
    assert row(s) == old and not s.calls


def test_actual_audit_insert_failure_rolls_back_original_whole_row(mounted):
    s = mounted
    produce(s)
    before = row(s)
    with s.session.get_bind().begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER retry_abort AFTER INSERT ON casdoor_audit_extend WHEN NEW.action='avatar_retry' BEGIN SELECT RAISE(ABORT, 'retry_abort'); END"
        )
    assert post(s).status_code != 200
    assert row(s) == before
    with Session(s.session.get_bind()) as session:
        assert not session.scalars(
            sa.select(Audit.id).where(Audit.action == "avatar_retry")
        ).all()
    assert not s.data and not s.published


def test_real_commit_ack_loss_is_reconciled_only_by_next_manager_request(
    mounted, monkeypatch
):
    s = mounted
    produce(s)
    fired = []
    original_commit = SessionTransaction.commit

    def lost_ack(transaction, *args, **kwargs):
        root = transaction.parent is None
        result = original_commit(transaction, *args, **kwargs)
        if (
            root
            and row(s)["termination_proof_kind"] == "avatar_retry_pre_storage"
            and not fired
        ):
            fired.append(True)
            raise RuntimeError("synthetic_commit_ack_lost")
        return result

    monkeypatch.setattr(SessionTransaction, "commit", lost_ack)
    assert post(s).status_code != 200
    assert fired and row(s)["operation_state"] == "pending"
    monkeypatch.setattr(SessionTransaction, "commit", original_commit)
    assert post(s).status_code == 200
    assert s.recovery() == {"code": "scanned"}
    assert s.task(s.published[0])["code"] == "applied"
    with Session(s.session.get_bind()) as session:
        assert (
            len(
                session.scalars(
                    sa.select(Audit.id).where(Audit.action == "avatar_retry")
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("failure", ["transport", "storage"])
def test_second_failure_never_becomes_another_confirmed_retry(mounted, failure):
    s = mounted
    produce(s)
    assert post(s).status_code == 200
    if failure == "transport":
        s.http(mode="error")
    else:
        s.storage_fault = "save"
    assert s.recovery() == {"code": "scanned"}
    assert s.task(s.published[0]) == {"code": "unknown"}
    value = row(s)
    assert (
        value["attempt_count"] == 2 and value["termination_state"] == "manual_recovery"
    )
    assert (
        json.loads(value["desired_json"])["reservations"][0]["cleanup_state"]
        == "complete"
    )
    assert s.send(BASE + "/retry-targets").json["targets"] == []
    assert post(s).status_code != 200
    assert row(s)["attempt_count"] == 2
    assert (
        s.send("/console/api/account/casdoor-identity").json["identities"][0][
            "avatar_status"
        ]
        == "unknown"
    )


@pytest.mark.parametrize("stage", ["pending", "claim", "applied"])
def test_registered_self_requires_the_same_unique_transition_chain(mounted, stage):
    from tests.unit_tests.repositories.test_casdoor_avatar_retry_lineage_extend import (
        repo,
    )

    s = mounted
    produce(s)
    assert post(s).status_code == 200
    if stage == "claim":
        with s.maker() as session, session.begin():
            assert (
                repo(s, session).claim_and_reserve(s.intent_id, now=s.utc).code
                == "reserved"
            )
    elif stage == "applied":
        assert s.recovery() == {"code": "scanned"}
        assert s.task(s.published[0])["code"] == "applied"
    with Session(s.session.get_bind()) as session, session.begin():
        action = "avatar_retry" if stage == "pending" else "avatar_retry_claim"
        session.execute(sa.delete(Audit).where(Audit.action == action))
    result = s.send("/console/api/account/casdoor-identity")
    assert result.status_code == 200
    assert result.json["identities"][0]["avatar_status"] == "unknown"


def test_new_affected_scope_after_producer_cas_rolls_back_whole_root(mounted):
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History

    s = mounted
    produce(s)
    before = row(s)
    values = dict(
        id=str(uuid4()),
        namespace_id=before["namespace_id"],
        identity_id=before["identity_id"],
        account_id=s.account.id,
        workspace_id=s.revision.default_workspace_id,
        revision_id=s.revision.id,
        join_id=None,
        ownership="managed",
        ownership_epoch=0,
        source="mapping",
        desired_generation=1,
        last_applied_roles_json="[]",
        last_applied_fingerprint=None,
        desired_roles_json="[]",
        baseline_json="{}",
        finalization="finalized",
        tombstone=False,
        created_at=s.utc.replace(tzinfo=None),
        updated_at=s.utc.replace(tzinfo=None),
    )
    engine = s.session.get_bind()
    insert = str(
        sa.insert(History.__table__)
        .values(**values)
        .compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER retry_scope_expand AFTER INSERT ON casdoor_audit_extend WHEN NEW.action='avatar_retry' BEGIN "
            + insert
            + "; END"
        )
    assert post(s).status_code != 200
    assert row(s) == before
    with Session(engine) as session:
        assert not session.scalars(sa.select(History.id)).all()
        assert not session.scalars(
            sa.select(Audit.id).where(Audit.action == "avatar_retry")
        ).all()
