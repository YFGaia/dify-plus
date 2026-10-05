"""Authority and drift failures through registered ordinary callbacks, offline."""

from copy import copy
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_casdoor_invited_local_http_extend import (
    test_registered_invite_auto_session_original_init_native_and_sql as original_invite_success,
)
from test_casdoor_local_http_extend import begin, send
from test_casdoor_managed_local_login_extend import historical_rows, ordinary

from libs.token import _real_cookie_name
from models.account import Account, AccountStatus, TenantAccountJoin
from models.casdoor_extend import CasdoorAuditExtend, CasdoorIdentityExtend, CasdoorSyncIntentExtend
from repositories import casdoor_terminal_local_invitation_repository_extend as terminal
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from repositories.casdoor_required_intent_repository_extend import (
    CasdoorRequiredIntentConflict,
    CasdoorRequiredIntentRepository,
)
from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService

pytest_plugins = ("test_casdoor_invited_local_http_extend",)


@pytest.fixture
def terminal_case(invited_mounted):
    original_invite_success(invited_mounted, AccountStatus.PENDING, False, False)
    return invited_mounted


def full_snapshot(case):
    with Session(case.m.f.local.engine) as session:
        return tuple(
            tuple(
                tuple(row)
                for row in session.execute(
                    sa.select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns)
                )
            )
            for model in (
                Account,
                TenantAccountJoin,
                CasdoorIdentityExtend,
                CasdoorSyncIntentExtend,
                CasdoorAuditExtend,
            )
        )


@pytest.mark.parametrize("value", [True, False, "skip-id", {"confirmed": True}, object()])
def test_unregistered_flags_observations_and_session_info_do_not_authorize(terminal_case, value):
    case = terminal_case
    before = full_snapshot(case)
    with Session(case.m.f.local.engine) as session, session.begin():
        session.info["terminal_invitation_confirmed"] = True
        owner = CasdoorRequiredIntentRepository(session)
        assert owner.read_locked(UUID(case.account), UUID(int=100))
        with pytest.raises((CasdoorLoginScopeConflict, CasdoorRequiredIntentConflict)):
            owner.read_locked(UUID(case.account), UUID(int=100), _ordinary_guard=value)
        with pytest.raises(CasdoorRequiredIntentConflict):
            owner.read_locked(UUID(case.account), UUID(int=100), invitation_guard=object(), _ordinary_guard=value)
    assert full_snapshot(case) == before


def test_arbitrary_callbacks_cannot_mint_attempt_or_bind_root():
    with pytest.raises(CasdoorLoginScopeConflict):
        terminal._begin_ordinary_terminal_attempt(
            prepared=object(), roles=object(), leases=object(), configuration=object()
        )
    with pytest.raises(CasdoorLoginScopeConflict):
        terminal._bind_ordinary_terminal_root(object(), object(), object())


def test_real_guards_cannot_be_copied_reused_after_callback_or_across_roots(terminal_case, monkeypatch):
    case = terminal_case
    captured = []
    original = terminal._lock_ordinary_terminal_parents

    def capture(guard, *args):
        result = original(guard, *args)
        captured.append(guard)
        with pytest.raises(CasdoorLoginScopeConflict):
            terminal._ordinary_terminal_exclusion(copy(guard), guard.session, guard.attempt.prepared.account_id)
        return result

    monkeypatch.setattr(terminal, "_lock_ordinary_terminal_parents", capture)
    ordinary(case, 1)
    assert len({guard.transaction for guard in captured}) == 4
    assert len({guard.attempt for guard in captured}) == 1
    for guard in captured:
        with pytest.raises(CasdoorLoginScopeConflict):
            terminal._ordinary_terminal_exclusion(guard, guard.session, guard.attempt.prepared.account_id)
        with Session(case.m.f.local.engine) as session, session.begin():
            with pytest.raises(CasdoorLoginScopeConflict):
                terminal._ordinary_terminal_exclusion(guard, session, guard.attempt.prepared.account_id)


def test_historical_evidence_drift_after_actual_b3_rolls_back_whole_c1(terminal_case, monkeypatch):
    case = terminal_case
    before = full_snapshot(case)
    token_count = len(case.m.f.control.tokens)
    original = CasdoorLocalMembershipService.persist_local_memberships
    touched = []

    def drift(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        owner._session.execute(
            sa.update(CasdoorAuditExtend)
            .where(CasdoorAuditExtend.action == "invited_local_membership_write")
            .values(summary_json="{}")
        )
        touched.append(result.generation)
        return result

    monkeypatch.setattr(CasdoorLocalMembershipService, "persist_local_memberships", drift)
    state = begin(case.m)
    result = send(case.m, "/callback", query_string={"state": state, "code": "synthetic-evidence-drift-code"})
    assert result.status_code == 302
    assert "/signin/casdoor-result?" in result.location
    assert touched == [2]
    assert full_snapshot(case) == before
    assert len(case.m.f.control.tokens) == token_count
    assert case.m.f.control.completed[-1].phases.local_outcome == "unknown"
    assert case.m.f.control.completed[-1].phases.cleanup_released is True


def test_terminal_drift_between_tail_read_and_write_roots_retains_committed_c1(terminal_case, monkeypatch):
    case = terminal_case
    before_tokens = len(case.m.f.control.tokens)
    target = []
    seen = set()
    injected = []
    original = terminal._ordinary_terminal_last

    def capture(guard):
        result = original(guard)
        if guard is not None:
            seen.add(guard.transaction)
            if len(seen) == 2 and not target:
                target.append(guard.transaction)
        return result

    def after_root(session, transaction):
        if target and transaction is target[0] and not injected:
            injected.append(True)
            with Session(case.m.f.local.engine) as writer, writer.begin():
                writer.execute(
                    sa.update(CasdoorAuditExtend)
                    .where(CasdoorAuditExtend.action == "invited_local_membership_finalization")
                    .values(summary_json="{}")
                )

    monkeypatch.setattr(terminal, "_ordinary_terminal_last", capture)
    sa.event.listen(Session, "after_transaction_end", after_root)
    try:
        state = begin(case.m)
        result = send(case.m, "/callback", query_string={"state": state, "code": "synthetic-tail-gap-code"})
    finally:
        sa.event.remove(Session, "after_transaction_end", after_root)
    assert injected == [True]
    assert result.status_code == 302
    assert "/signin/casdoor-result?" in result.location
    assert len(case.m.f.control.tokens) == before_tokens
    phases = case.m.f.control.completed[-1].phases
    assert phases.local_outcome == "committed"
    assert phases.token_outcome == "not_started"
    assert phases.finalization_outcome == "not_started"
    assert phases.cleanup_released is True
    with Session(case.m.f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 2


@pytest.mark.parametrize("failure", ["token", "cleanup"])
def test_uncertain_token_or_cleanup_never_delivers_an_ordinary_pair(terminal_case, failure):
    case = terminal_case
    old_cookies = dict(case.m.client._cookies)
    if failure == "token":
        case.m.f.control.fail_token = len(case.m.f.control.tokens) + 1
    else:
        case.m.f.control.fail_release = True
    state = begin(case.m)
    result = send(case.m, "/callback", query_string={"state": state, "code": "synthetic-tail-failure-code"})
    assert result.status_code != 302 or "/apps/" not in (result.location or "")
    checked = 0
    for key, old in old_cookies.items():
        if key[2] in {_real_cookie_name(name) for name in ("access_token", "refresh_token", "csrf_token")}:
            assert case.m.client._cookies[key].value == old.value
            checked += 1
    assert checked == 3
    completed = case.m.f.control.completed[-1]
    assert getattr(completed, "tokens", None) is None
    assert completed.phases.local_outcome == "committed"
    assert completed.phases.finalization_outcome == "committed"
    if failure == "token":
        assert completed.phases.token_outcome == "unknown"
        assert completed.phases.cleanup_released is True
    else:
        assert completed.phases.cleanup_released is False


@pytest.mark.parametrize("field", ["operation_state", "termination_state", "summary_json"])
def test_nonterminal_or_corrupt_history_still_blocks_before_b2(terminal_case, monkeypatch, field):
    case = terminal_case
    from models.casdoor_extend import CasdoorOperationState, CasdoorTerminationState

    with Session(case.m.f.local.engine) as session, session.begin():
        if field == "summary_json":
            session.execute(
                sa.update(CasdoorAuditExtend)
                .where(CasdoorAuditExtend.action == "invited_local_membership_finalization")
                .values(summary_json="{}")
            )
        else:
            value = CasdoorOperationState.UNKNOWN if field == "operation_state" else CasdoorTerminationState.UNCONFIRMED
            session.execute(sa.update(CasdoorSyncIntentExtend).values(**{field: value}))
    before = full_snapshot(case)
    token_count = len(case.m.f.control.tokens)
    state = begin(case.m)
    result = send(case.m, "/callback", query_string={"state": state, "code": "synthetic-corrupt-history-code"})
    assert result.status_code == 302
    assert "/signin/casdoor-result?" in result.location
    assert full_snapshot(case) == before
    assert len(case.m.f.control.tokens) == token_count
    assert historical_rows(case)[0]


@pytest.mark.parametrize("kind", ["role_replace", "resource_grant", "invitation_finalize"])
def test_extra_confirmed_intent_is_not_hidden_by_terminal_exclusion(terminal_case, kind):
    case = terminal_case
    from models.casdoor_extend import CasdoorIntentKind

    with Session(case.m.f.local.engine) as session, session.begin():
        old = session.execute(sa.select(*CasdoorSyncIntentExtend.__table__.columns)).one()
        values = dict(old._mapping)
        values.update(id=str(uuid4()), idempotency_key="d20-extra-" + kind, kind=CasdoorIntentKind(kind))
        session.execute(sa.insert(CasdoorSyncIntentExtend).values(**values))
    before = full_snapshot(case)
    token_count = len(case.m.f.control.tokens)
    state = begin(case.m)
    result = send(case.m, "/callback", query_string={"state": state, "code": "synthetic-extra-intent-code"})
    assert result.status_code == 302
    assert "/signin/casdoor-result?" in result.location
    assert full_snapshot(case) == before
    assert len(case.m.f.control.tokens) == token_count
