"""D24-A2 actual LOCAL alternate writer and canonical permission owner."""

from datetime import datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from werkzeug.exceptions import Forbidden

from models.account import Account, AccountIntegrate, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorMembershipOwnership, CasdoorOperationState
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from repositories.casdoor_manual_ownership_repository_extend import (
    CasdoorManualOwnershipRepository,
)
from services.enterprise.rbac_service import RBACService
from tests.unit_tests.services.test_casdoor_manual_member_mutation_service_extend import (
    add_intent,
    local_mode as local_mode_fixture,
    rows,
    storage as storage_fixture,
)

local_mode = local_mode_fixture
storage = storage_fixture


def replace_role(s, role="admin", *, actor=None):
    return RBACService.MemberRoles.replace(
        s.workspace.id,
        s.operator.id if actor is None else actor,
        s.account.id,
        [role],
        session=s.session,
    )


def test_actual_normal_actor_denied_before_real_metadata_or_role_change(storage):
    s = storage
    s.owner_join.role = "normal"
    s.session.commit()
    before = rows(s, History)
    with pytest.raises(Forbidden):
        replace_role(s)
    assert rows(s, History) == before
    assert s.join.role == "normal"


def test_actual_owner_changes_shared_role_and_marks_override(storage):
    s = storage
    response = replace_role(s)
    assert response.account_id == s.account.id
    assert s.join.role == "admin"
    assert rows(s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE


@pytest.mark.parametrize("actor", ["missing", "none", "nonmember", "self"])
def test_direct_caller_cannot_supply_absent_or_nonmember_or_self_actor(storage, actor):
    s = storage
    actor_id = (
        str(uuid4())
        if actor == "missing"
        else None
        if actor == "none"
        else s.account.id
    )
    if actor == "nonmember":
        account = Account(name="Unrelated", email="unrelated@example.test")
        s.session.add(account)
        s.session.commit()
        actor_id = account.id
    before = rows(s, History), rows(s, TenantAccountJoin)
    with pytest.raises(Forbidden, match="No permission to update member role"):
        RBACService.MemberRoles.replace(
            s.workspace.id, actor_id, s.account.id, ["admin"], session=s.session
        )
    assert before == (rows(s, History), rows(s, TenantAccountJoin))


@pytest.mark.parametrize(
    "old,new,allowed",
    [
        ("normal", "admin", True),
        ("admin", "normal", True),
        ("normal", "editor", True),
        ("normal", "owner", False),
        ("owner", "normal", False),
        ("owner", "owner", False),
    ],
)
def test_actual_admin_canonical_target_and_requested_owner_rules(
    storage, old, new, allowed
):
    s = storage
    s.owner_join.role = "admin"
    s.join.role = old
    s.session.commit()
    before = rows(s, History), rows(s, TenantAccountJoin)
    if allowed:
        result = replace_role(s, new)
        assert result.roles[0].id == new and s.join.role == new
        assert (
            rows(s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
        )
    else:
        with pytest.raises(Forbidden):
            replace_role(s, new)
        assert before == (rows(s, History), rows(s, TenantAccountJoin))


@pytest.mark.parametrize("actor_role", ["admin", "owner", "normal", "editor"])
def test_noop_has_original_response_only_after_actual_authorization(
    storage, monkeypatch, actor_role
):
    s = storage
    s.owner_join.role = actor_role
    s.session.commit()
    before = rows(s, History), rows(s, TenantAccountJoin)

    def forbidden_prepare(*args, **kwargs):
        raise AssertionError("no-op must not mark")

    monkeypatch.setattr(CasdoorManualOwnershipRepository, "prepare", forbidden_prepare)
    if actor_role in ("admin", "owner"):
        result = replace_role(s, "normal")
        assert result.account_id == s.account.id and result.roles[0].id == "normal"
    else:
        with pytest.raises(Forbidden):
            replace_role(s, "normal")
    assert before == (rows(s, History), rows(s, TenantAccountJoin))


@pytest.mark.parametrize("which", ["actor", "target"])
def test_clean_but_stale_identity_map_cannot_grant_permission(storage, which):
    s = storage
    s.owner_join.role = "admin"
    s.session.commit()
    old = s.owner_join.role, s.join.role
    join = s.owner_join if which == "actor" else s.join
    # Actual SQL drift, deliberately leave clean identity-map objects stale.
    s.session.execute(
        sa.update(TenantAccountJoin)
        .where(TenantAccountJoin.id == join.id)
        .values(role="normal" if which == "actor" else "owner")
        .execution_options(synchronize_session=False)
    )
    s.session.commit()
    assert old == (s.owner_join.role, s.join.role) and not s.session.dirty
    before = rows(s, History), rows(s, TenantAccountJoin)
    with pytest.raises(Forbidden):
        replace_role(s)
    assert before == (rows(s, History), rows(s, TenantAccountJoin))


@pytest.mark.parametrize("state", list(CasdoorOperationState))
def test_original_required_intent_states_still_deny_real_change(storage, state):
    s = storage
    add_intent(s, state)
    before = rows(s, History), rows(s, TenantAccountJoin)
    with pytest.raises(Forbidden):
        replace_role(s)
    assert before == (rows(s, History), rows(s, TenantAccountJoin))


@pytest.mark.parametrize("managed", [False, True])
def test_actual_transfer_prepares_complete_change_set_and_original_epoch_effects(
    storage, monkeypatch, managed
):
    s = storage
    if not managed:
        s.session.delete(s.history)
        s.session.commit()
    seen = []
    original = CasdoorManualOwnershipRepository.prepare

    def prepare(repo, scopes, **kwargs):
        assert (
            not s.session.dirty
            and s.owner_join.role == "owner"
            and s.join.role == "normal"
        )
        seen.append({str(scope.account_id) for scope in scopes})
        return original(repo, scopes, **kwargs)

    monkeypatch.setattr(CasdoorManualOwnershipRepository, "prepare", prepare)
    result = replace_role(s, "owner")
    assert seen == [{s.operator.id, s.account.id}]
    assert (
        result.roles[0].id == "owner"
        and s.owner_join.role == "normal"
        and s.join.role == "owner"
    )
    assert len(rows(s, Lifecycle)) == 2
    assert all(row.epoch == 1 for row in rows(s, Lifecycle))
    assert (len(rows(s, History)) == 1) == managed


def operator_history(s):
    identity = Identity(
        namespace_id=s.namespace.id,
        account_id=s.operator.id,
        issuer=s.namespace.expected_issuer,
        organization="Org",
        subject="Operator",
        last_applied_json="{}",
        profile_sync_json="{}",
    )
    s.session.add(identity)
    s.session.flush()
    history = History(
        **{
            column.name: getattr(s.history, column.name)
            for column in History.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
    )
    history.id = str(uuid4())
    history.identity_id = identity.id
    history.account_id = s.operator.id
    history.join_id = s.owner_join.id
    s.session.add(history)
    s.session.commit()
    return history.id


def test_authorization_actor_is_not_an_affected_override_and_original_fullrows_preserved(
    storage,
):
    s = storage
    history_id = operator_history(s)
    s.session.add(
        AccountIntegrate(
            account_id=s.account.id,
            provider="github",
            open_id="synthetic-old",
            encrypted_token="synthetic-encrypted-fixture",
        )
    )
    s.session.execute(
        sa.insert(AccountMoneyExtend).values(
            id=str(uuid4()),
            account_id=s.account.id,
            total_quota=123.456789,
            used_quota=8.7654321,
            created_at=datetime(2020, 1, 1),
            updated_at=datetime(2021, 1, 1),
        )
    )
    s.session.commit()
    before = {
        model: rows(s, model)
        for model in (Account, AccountIntegrate, AccountMoneyExtend)
    }
    original_operator_history = next(
        row for row in rows(s, History) if row.id == history_id
    )
    replace_role(s)
    assert before == {model: rows(s, model) for model in before}
    assert original_operator_history == next(
        row for row in rows(s, History) if row.id == history_id
    )


def test_managed_standing_owner_denies_transfer_without_partial_marker(storage):
    s = storage
    operator_history(s)
    before = rows(s, History), rows(s, TenantAccountJoin)
    with pytest.raises(Forbidden):
        replace_role(s, "owner")
    assert before == (rows(s, History), rows(s, TenantAccountJoin))


@pytest.mark.parametrize(
    "drift", ["actor-role", "target-role", "target-join", "owner-set"]
)
def test_fresh_postparent_admission_and_affected_set_detect_actual_sql_drift_and_roll_back(
    storage, monkeypatch, drift
):
    s = storage
    before = rows(s, History), rows(s, TenantAccountJoin), rows(s, Lifecycle)
    original = CasdoorManualOwnershipRepository.mark_local_override

    def mark(repo, token):
        result = original(repo, token)
        assert not s.session.dirty
        if drift == "actor-role":
            statement = (
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.id == s.owner_join.id)
                .values(role="normal")
            )
        elif drift == "target-role":
            statement = (
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.id == s.join.id)
                .values(role="editor")
            )
        elif drift == "target-join":
            statement = (
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.id == s.join.id)
                .values(id=str(uuid4()))
            )
        else:
            statement = (
                sa.update(TenantAccountJoin)
                .where(TenantAccountJoin.id == s.owner_join.id)
                .values(id=str(uuid4()))
            )
        s.session.execute(statement.execution_options(synchronize_session=False))
        return result

    monkeypatch.setattr(CasdoorManualOwnershipRepository, "mark_local_override", mark)
    with pytest.raises(Forbidden):
        replace_role(s, "owner" if drift == "owner-set" else "admin")
    s.session.rollback()
    assert before == (rows(s, History), rows(s, TenantAccountJoin), rows(s, Lifecycle))


@pytest.mark.parametrize("failure", ["authority", "commit-before", "commit-after"])
def test_actual_writer_failure_and_unknown_ack_preserve_genuine_sql_boundary(
    storage, monkeypatch, failure
):
    s = storage
    before = rows(s, History), rows(s, TenantAccountJoin), rows(s, Lifecycle)
    commit = s.session.commit
    if failure == "authority":

        def fail(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith(
                "INSERT INTO invitation_authority_lifecycle_extend"
            ):
                raise RuntimeError("actual authority SQL failure")

        sa.event.listen(s.engine, "before_cursor_execute", fail)
    else:

        def fail_commit():
            if failure == "commit-after":
                commit()
            raise RuntimeError("commit acknowledgement unavailable")

        monkeypatch.setattr(s.session, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError):
            replace_role(s)
    finally:
        if failure == "authority":
            sa.event.remove(s.engine, "before_cursor_execute", fail)
        s.session.rollback()
    if failure == "commit-after":
        assert s.session.get(TenantAccountJoin, s.join.id).role == "admin"
        assert (
            rows(s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
        )
        assert rows(s, Lifecycle)[0].epoch == 1
    else:
        assert before == (
            rows(s, History),
            rows(s, TenantAccountJoin),
            rows(s, Lifecycle),
        )
