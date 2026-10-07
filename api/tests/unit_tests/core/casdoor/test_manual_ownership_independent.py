"""Independent policy checks for the bounded manual-ownership foundation."""

from uuid import uuid4

from core.casdoor.manual_ownership import (
    ManualHistoryObservation,
    ManualMemberScope,
    ManualMutationKind,
    ManualOwnershipDecision,
    decide_manual_ownership,
    validate_manual_scopes,
)
from core.casdoor.ownership import MembershipBackend, MembershipObservation
from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorMembershipOwnership


def test_role_equality_does_not_turn_unmanaged_history_into_owned_metadata():
    scope = ManualMemberScope(uuid4(), uuid4())
    observation = MembershipObservation(
        scope.workspace_id,
        scope.account_id,
        uuid4(),
        TenantAccountRole.NORMAL,
        MembershipBackend.LOCAL,
    )
    decision = decide_manual_ownership(observation, (), required_intent_ids=())
    assert decision is ManualOwnershipDecision.PRESERVE_UNMANAGED


def test_remote_managed_history_stays_closed_even_with_complete_projection_elsewhere():
    scope = ManualMemberScope(uuid4(), uuid4())
    history = ManualHistoryObservation(uuid4(), CasdoorMembershipOwnership.MANAGED, 2, uuid4(), False)
    observation = MembershipObservation(
        scope.workspace_id,
        scope.account_id,
        history.join_id,
        TenantAccountRole.NORMAL,
        MembershipBackend.REMOTE,
    )
    assert decide_manual_ownership(observation, (history,), required_intent_ids=()) is (
        ManualOwnershipDecision.AUTHORIZATION_PENDING
    )


def test_mixed_history_and_any_required_intent_block_the_whole_scope():
    scope = ManualMemberScope(uuid4(), uuid4())
    join_id = uuid4()
    observation = MembershipObservation(
        scope.workspace_id,
        scope.account_id,
        join_id,
        TenantAccountRole.NORMAL,
        MembershipBackend.LOCAL,
    )
    histories = (
        ManualHistoryObservation(uuid4(), CasdoorMembershipOwnership.MANAGED, 4, join_id, False),
        ManualHistoryObservation(uuid4(), CasdoorMembershipOwnership.RELEASED, 9, join_id, False),
    )
    assert decide_manual_ownership(observation, histories, required_intent_ids=()) is (
        ManualOwnershipDecision.OWNER_FENCE_REQUIRED
    )
    assert decide_manual_ownership(observation, histories[:1], required_intent_ids=(uuid4(),)) is (
        ManualOwnershipDecision.TERMINATION_REQUIRED
    )


def test_transfer_shape_is_exactly_two_distinct_accounts_in_one_workspace():
    workspace_id = uuid4()
    first, second = ManualMemberScope(uuid4(), workspace_id), ManualMemberScope(uuid4(), workspace_id)
    validate_manual_scopes((first, second), ManualMutationKind.OWNER_TRANSFER)
