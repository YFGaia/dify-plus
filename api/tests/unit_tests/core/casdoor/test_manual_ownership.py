from uuid import uuid4

import pytest
from core.casdoor.manual_ownership import (
    MAX_MANUAL_SCOPES,
    ManualHistoryObservation,
    ManualMemberScope,
    ManualMutationKind,
    ManualOwnershipDecision,
    decide_manual_ownership,
    validate_manual_scopes,
)
from core.casdoor.ownership import (
    ExternalMemberRolesProjection,
    MembershipBackend,
    MembershipObservation,
    RolesKnowledge,
)
from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorMembershipOwnership


@pytest.mark.parametrize(
    "bad", [(), [], (True,), tuple(ManualMemberScope(uuid4(), uuid4()) for _ in range(MAX_MANUAL_SCOPES + 1))]
)
def test_invalid_scope_sets(bad):
    with pytest.raises(ValueError):
        validate_manual_scopes(bad, ManualMutationKind.ROLE_CHANGE)


def test_transfer_needs_exact_two_distinct_accounts_same_workspace():
    one = ManualMemberScope(uuid4(), uuid4())
    two = ManualMemberScope(uuid4(), one.workspace_id)
    validate_manual_scopes((one, two), ManualMutationKind.OWNER_TRANSFER)
    for scopes in ((one,), (one, one), (one, ManualMemberScope(uuid4(), uuid4()))):
        with pytest.raises(ValueError):
            validate_manual_scopes(scopes, ManualMutationKind.OWNER_TRANSFER)


def test_complete_remote_projection_is_not_provenance():
    account, workspace, join = uuid4(), uuid4(), uuid4()
    observation = MembershipObservation(
        workspace,
        account,
        join,
        TenantAccountRole.NORMAL,
        MembershipBackend.REMOTE,
        ExternalMemberRolesProjection(workspace, account, RolesKnowledge.COMPLETE, ()),
    )
    managed = ManualHistoryObservation(uuid4(), CasdoorMembershipOwnership.MANAGED, 0, join, False)
    assert (
        decide_manual_ownership(observation, (managed,), required_intent_ids=())
        is ManualOwnershipDecision.AUTHORIZATION_PENDING
    )


def test_same_role_does_not_create_ownership_and_intents_always_block():
    observation = MembershipObservation(uuid4(), uuid4(), uuid4(), TenantAccountRole.NORMAL, MembershipBackend.LOCAL)
    assert (
        decide_manual_ownership(observation, (), required_intent_ids=()) is ManualOwnershipDecision.PRESERVE_UNMANAGED
    )
    assert (
        decide_manual_ownership(observation, (), required_intent_ids=(uuid4(),))
        is ManualOwnershipDecision.TERMINATION_REQUIRED
    )
