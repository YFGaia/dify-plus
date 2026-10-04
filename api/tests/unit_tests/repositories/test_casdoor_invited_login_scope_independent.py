"""Adversarial checks for completed-invitation scope binding and barriers."""

import pytest
import sqlalchemy as sa
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_invitation_finalization_repository_extend import (
    CasdoorInvitationFinalizationRepository,
)
from repositories.casdoor_invited_login_scope_repository_extend import (
    CasdoorInvitedLoginScopeRepository,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
)
from test_casdoor_invited_login_scope_repository_extend import (
    add_history_scope,
    configuration_factory,
    discover,
)

pytest_plugins = ("test_casdoor_invited_login_scope_repository_extend",)


def test_old_generation_applied_intent_is_retained_in_invited_scope(invited_scope_case):
    case = invited_scope_case()
    ids = add_history_scope(case)
    with case.db() as session, session.begin():
        stale = session.get(Intent, ids["intent"])
        stale.generation = 0
        stale.operation_state = "applied"
        stale.termination_state = "confirmed"

    result = discover(case)

    retained = [intent for intent in result.scope.intents if intent.id == ids["intent"]]
    assert len(retained) == 1
    assert (retained[0].operation_state, retained[0].termination_state, retained[0].generation) == (
        "applied",
        "confirmed",
        0,
    )
    historical_identity = next(identity for identity in result.scope.identities if identity.id == ids["identity"])
    assert historical_identity.sync_generation == 7
    with case.db() as session, session.begin(), pytest.raises(CasdoorLoginScopeConflict):
        CasdoorLoginScopeRepository(session, configuration_factory)._intent_barrier(
            case.attempt.account_id, result.scope
        )
    assert not case.redis.calls


def test_projection_rejects_p3l_state_change_after_same_session_inspection(invited_scope_case, monkeypatch):
    case = invited_scope_case()
    original = CasdoorInvitationFinalizationRepository.inspect
    inspected = []

    def inspect_then_drift(repository, attempt):
        completion = original(repository, attempt)
        inspected.append((repository._session, completion))
        changed = repository._session.execute(
            sa.update(Intent)
            .where(Intent.id == str(completion.snapshot.operation_id))
            .values(termination_state="not_started")
        )
        assert changed.rowcount == 1
        return completion

    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", inspect_then_drift)

    with pytest.raises(CasdoorLoginScopeConflict):
        with case.db() as session, session.begin():
            result = CasdoorInvitedLoginScopeRepository(session, configuration_factory).discover_completed_invitation(
                case.attempt
            )
            pytest.fail(f"drifted P3L projection unexpectedly returned {result!r}")

    assert len(inspected) == 1
    assert inspected[0][1].completed is True
    with case.db() as session:
        current = session.get(Intent, str(case.pending.snapshot.operation_id))
        assert current.termination_state.value == "confirmed"
    assert not case.redis.calls
