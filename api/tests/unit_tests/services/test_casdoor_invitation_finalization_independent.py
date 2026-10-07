"""Independent boundary checks for P3L's SQL/P1 ownership split."""

from unittest.mock import Mock

import pytest
from repositories.casdoor_invitation_operation_repository_extend import InvitationOperationConflict
from services import account_adapters as adapters

from tests.unit_tests.repositories.test_casdoor_invitation_finalization_repository_extend import (
    TOKEN,
)

pytest_plugins = ("tests.unit_tests.services.test_casdoor_invitation_finalization_service_extend",)


def test_p1_calls_happen_only_after_every_sql_session_is_closed(finalizer_case, monkeypatch):
    case = finalizer_case(absent=True)
    observed = case.store.observe_versioned_invitation
    readback = case.store.read_invitation_consumption
    consume = case.store.consume_versioned_invitation

    def assert_sql_closed():
        assert case.sessions
        assert all(not session.in_transaction() for session in case.sessions)

    def observe(token):
        assert_sql_closed()
        return observed(token)

    def read(observation, *, operation_id):
        assert_sql_closed()
        return readback(observation, operation_id=operation_id)

    def consume_once(observation, *, operation_id, receipt_ttl_seconds):
        assert_sql_closed()
        return consume(
            observation,
            operation_id=operation_id,
            receipt_ttl_seconds=receipt_ttl_seconds,
        )

    monkeypatch.setattr(case.store, "observe_versioned_invitation", observe)
    monkeypatch.setattr(case.store, "read_invitation_consumption", read)
    monkeypatch.setattr(case.store, "consume_versioned_invitation", consume_once)

    result = case.finalizer.finalize(case.attempt, token=TOKEN)

    assert result.facts.completed
    assert sum(call[0] == adapters._INVITATION_CONSUME for call in case.redis.calls) == 1


def test_completed_replay_bypasses_p3k_inspection_and_all_redis(finalizer_case, monkeypatch):
    case = finalizer_case()
    completed = case.finalizer.finalize(case.attempt, token=TOKEN)
    case.redis.calls.clear()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("completed P3L replay must use its independent SQL reader")

    monkeypatch.setattr(
        "repositories.casdoor_invitation_operation_repository_extend.CasdoorInvitationOperationRepository.inspect",
        forbidden,
    )
    assert case.finalizer.finalize(case.attempt) == completed
    assert case.redis.calls == []


def test_invalid_pending_attempt_is_rejected_before_observation(finalizer_case, monkeypatch):
    case = finalizer_case()
    observe = Mock(side_effect=AssertionError("invalid attempt reached P1"))
    monkeypatch.setattr(case.store, "observe_versioned_invitation", observe)
    invalid = object()

    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(invalid, token=TOKEN)

    observe.assert_not_called()
    assert case.redis.calls == []
