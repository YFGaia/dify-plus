"""Independent offline checks for pending invitation operation production."""

import json
from hashlib import sha256
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from models.account import Account, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from repositories.casdoor_invitation_operation_repository_extend import (
    InvitationOperationConflict,
)
from repositories.casdoor_required_intent_repository_extend import (
    CasdoorRequiredIntentRepository,
)
from services.casdoor_invitation_operation_service_extend import (
    CommittedInvitationOperation,
)
from test_casdoor_invitation_operation_repository_extend import TOKEN, prepare
from test_casdoor_invitation_operation_repository_extend import (
    operation_case as base_operation_case,  # noqa: F401
)


@pytest.fixture
def operation_case(base_operation_case):  # noqa: F811
    return base_operation_case


def produce_new(case):
    return case.service.produce(case.attempt, token=TOKEN, prepared=prepare(case))


def test_producer_returns_only_pending_snapshot_and_preserves_other_effects(operation_case, monkeypatch):
    case = operation_case
    observed = []
    original_observe = case.store.observe_versioned_invitation

    def capture(token):
        result = original_observe(token)
        observed.append(result.observation)
        return result

    monkeypatch.setattr(case.store, "observe_versioned_invitation", capture)
    before = case.protected
    handle = produce_new(case)

    assert type(handle) is CommittedInvitationOperation
    assert len(observed) == 1
    data = json.loads(handle.snapshot.desired_json)
    assert data["observed_join_role"] == "editor"
    assert (
        data["expected_receipt_json"]
        == case.store._consumption_arguments(observed[0], str(handle.snapshot.operation_id))[1].decode()
    )
    assert handle.snapshot.scope_digest == sha256(handle.snapshot.desired_json.encode()).hexdigest()
    assert data["operation_id"] == str(handle.snapshot.operation_id)
    assert data["kind"] == "invitation_finalize"
    assert TOKEN not in handle.snapshot.desired_json
    assert case.redis.token_key.decode() not in handle.snapshot.desired_json
    with case.db() as session:
        row = session.get(Intent, str(handle.snapshot.operation_id))
        issuance = session.get(Issuance, case.ids["issuance"])
        assert row is not None and row.membership_id is None and row.ownership_epoch == 0
        assert row.attempt_count == 0 and row.operation_state.value == "pending"
        assert row.termination_state.value == "not_started"
        assert issuance.state == "issued" and issuance.consumption_receipt_json is None
        assert CasdoorRequiredIntentRepository(session).read_locked(case.attempt.account_id, case.attempt.workspace_id)
        assert case.ids["join"] == session.get(TenantAccountJoin, case.ids["join"]).id
        assert case.ids["account"] == session.get(Account, case.ids["account"]).id
        assert session.get(Identity, next(iter(session.scalars(sa.select(Identity)))).id) is not None
        assert session.get(Lifecycle, case.ids["lifecycle"]).state == "active"
        join = session.get(TenantAccountJoin, case.ids["join"])
        quota = session.get(AccountMoneyExtend, case.ids["quota"])
        assert (join.role, join.current, join.last_opened_at, quota.total_quota, quota.used_quota) == before
    assert len(case.redis.calls) == 1
    assert not any(effect.mock_calls for effect in case.effects[2:])


def test_exact_retry_is_sql_only_after_token_disappears(operation_case, monkeypatch):
    case = operation_case
    first = produce_new(case)
    case.redis.entries.clear()
    monkeypatch.setattr(
        case.store,
        "observe_versioned_invitation",
        Mock(side_effect=AssertionError("an exact operation retry must not consult P1")),
    )

    second = case.service.produce(case.attempt)

    assert second == first
    assert len(case.redis.calls) == 1
    with case.db() as session:
        assert len(list(session.scalars(sa.select(Intent)))) == 1


@pytest.mark.parametrize("changed", ["identity", "revision", "generation", "lifecycle", "join_role"])
def test_retry_revalidates_live_identity_revision_lifecycle_and_join(operation_case, monkeypatch, changed):
    case = operation_case
    produce_new(case)
    with case.db() as session:
        if changed == "identity":
            session.execute(sa.update(Identity).values(issuer="https://changed.example.invalid"))
        elif changed == "revision":
            session.get(
                Integration, case.ids["integration"]
            ).active_revision_id = "00000000-0000-4000-8000-000000000000"
        elif changed == "generation":
            session.execute(sa.update(Identity).values(sync_generation=1))
        elif changed == "lifecycle":
            session.get(Lifecycle, case.ids["lifecycle"]).epoch += 1
        else:
            session.get(TenantAccountJoin, case.ids["join"]).role = "normal"
        session.commit()
    monkeypatch.setattr(
        case.store,
        "observe_versioned_invitation",
        Mock(side_effect=AssertionError("SQL-only retry must reject changed live facts before P1")),
    )

    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)

    assert len(case.redis.calls) == 1
