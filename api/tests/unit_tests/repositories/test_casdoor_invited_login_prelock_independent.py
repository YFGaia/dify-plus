"""Independent stop-boundary checks for the P3M-B parent prelock."""

import pytest
import sqlalchemy as sa
from models.account import Account
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
from sqlalchemy.exc import OperationalError
from test_casdoor_invited_login_prelock_repository_extend import PARENTS, capture, discover, locks, table
from test_casdoor_invited_login_scope_repository_extend import configuration_factory

pytest_plugins = ("test_casdoor_invited_login_scope_repository_extend",)


def test_final_unlocked_projection_rejects_post_p3l_account_drift_without_late_locks(invited_scope_case, monkeypatch):
    case = invited_scope_case()
    original_name = discover(case).scope.account.name
    original = CasdoorInvitationFinalizationRepository.inspect
    inspected = []

    def inspect_then_change_account(repository, attempt):
        completion = original(repository, attempt)
        inspected.append((completion, len(locks(queries))))
        changed = repository._session.execute(
            sa.update(Account).where(Account.id == case.ids["account"]).values(name="changed-after-p3l")
        )
        assert changed.rowcount == 1
        return completion

    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", inspect_then_change_account)
    with capture(case) as queries, pytest.raises(CasdoorLoginScopeConflict):
        with case.db() as session, session.begin():
            CasdoorInvitedLoginScopeRepository(session, configuration_factory).prelock_and_recheck_completed_invitation(
                case.attempt
            )

    locked = locks(queries)
    assert len(inspected) == 1 and inspected[0][0].completed is True
    assert [table(query) for query in locked[:6]] == PARENTS
    assert len(locked) == inspected[0][1]
    with case.db() as session:
        assert session.get(Account, case.ids["account"]).name == original_name
    assert not case.redis.calls


def test_p3l_lock_error_stops_after_all_candidate_parents_without_retry_or_final_projection(
    invited_scope_case, monkeypatch
):
    case = invited_scope_case()
    inspect_calls = []
    projection_calls = []
    original_projection = CasdoorLoginScopeRepository._project_scope

    def record_projection(owner, *args, **kwargs):
        projection_calls.append(kwargs.get("_lock", False))
        return original_projection(owner, *args, **kwargs)

    def fail_inspect(owner, attempt):
        inspect_calls.append(attempt)
        assert [table(query) for query in locks(queries)[:6]] == PARENTS
        raise OperationalError("synthetic P3L NOWAIT", {}, RuntimeError("busy"))

    monkeypatch.setattr(CasdoorLoginScopeRepository, "_project_scope", record_projection)
    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", fail_inspect)
    with capture(case) as queries, pytest.raises(CasdoorLoginScopeConflict):
        with case.db() as session, session.begin():
            CasdoorInvitedLoginScopeRepository(session, configuration_factory).prelock_and_recheck_completed_invitation(
                case.attempt
            )

    assert inspect_calls == [case.attempt]
    assert projection_calls == [False, False]
    assert [table(query) for query in locks(queries)] == PARENTS
    assert not case.redis.calls
