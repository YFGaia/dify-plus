"""Real account-owner/root composition, read-only modeled P1 observation, offline."""

import json
from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import SessionTransaction

from models.account import Account, AccountStatus, TenantAccountJoin
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_invitation_operation_repository_extend import (
    CasdoorInvitationOperationRepository,
    InvitationOperationConflict,
)
from repositories.casdoor_required_intent_repository_extend import CasdoorRequiredIntentRepository
from services import casdoor_invitation_operation_service_extend as service_module
from services.entities.account_activation_entities import InvitationObservationResult
from tests.unit_tests.repositories.test_casdoor_invitation_operation_repository_extend import (
    TOKEN,  # noqa: F401
    canonical,
    create,
    observe,
    prepare,
    protected,
)
from tests.unit_tests.repositories.test_casdoor_invitation_operation_repository_extend import (
    operation_case as base_operation_case,  # noqa: F401
)


@pytest.fixture
def operation_case(base_operation_case):  # noqa: F811
    return base_operation_case


def assert_unwritten(case):
    with case.db() as session:
        account = session.get(Account, case.ids["account"])
        assert account.status == AccountStatus.PENDING and account.initialized_at is None
        assert session.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 0
        assert session.get(Issuance, case.ids["issuance"]).state == "issued"
        assert protected(case, session) == case.protected


def test_first_creation_acknowledged_commit_fresh_read_and_required_intent_barrier(operation_case):
    case = operation_case
    prepared = prepare(case)
    case.sessions.clear()
    handle = case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    assert len(case.sessions) == 3 and len({id(session) for session in case.sessions}) == 3
    assert all(not session.in_transaction() for session in case.sessions)
    assert len(case.redis.calls) == 1 and case.redis.token_key in case.redis.entries
    data = json.loads(handle.snapshot.desired_json)
    assert data["kind"] == "invitation_finalize" and data["generation"] == 0
    with case.db() as session, session.begin():
        account = session.get(Account, case.ids["account"])
        assert account.status == AccountStatus.ACTIVE and account.initialized_at is not None
        assert account.name == "Initialized" and account.password == "original"
        assert protected(case, session) == case.protected
        assert CasdoorRequiredIntentRepository(session).read_locked(case.attempt.account_id, case.attempt.workspace_id)
        assert session.get(Issuance, case.ids["issuance"]).consumption_receipt_json is None
    for effect in case.effects[2:]:
        assert not effect.mock_calls


def test_exact_retry_reuses_operation_deadline_with_no_token_or_redis_io(operation_case, monkeypatch):
    case = operation_case
    first = create(case)
    case.redis.entries.clear()  # Simulate token deletion; this does not prove consume.
    monkeypatch.setattr(
        case.store, "observe_versioned_invitation", Mock(side_effect=AssertionError("retry must be SQL-only"))
    )
    second = case.service.produce(case.attempt)
    third = case.service.produce(case.attempt, token="ignored client value")
    assert first == second == third
    with case.db() as session:
        rows = list(session.scalars(sa.select(Intent)))
        assert len(rows) == 1 and rows[0].desired_json == first.snapshot.desired_json
        assert rows[0].created_at == rows[0].updated_at == first.snapshot.created_at
    assert len(case.redis.calls) == 1


@pytest.mark.parametrize(
    "state", ["absent", "expired", "unavailable", "wrong_payload", "forged", "stale", "invalid_token"]
)
def test_new_operation_requires_fresh_authentic_observation_before_any_write(operation_case, monkeypatch, state):
    case = operation_case
    prepared = prepare(case)
    if state == "absent":
        case.redis.entries.clear()
    elif state == "expired":
        case.redis.entries[case.redis.token_key] = ("string", case.payload.encode(), 0)
    elif state == "unavailable":
        case.redis.eval = Mock(side_effect=RuntimeError("private Redis detail"))
    elif state == "wrong_payload":
        data = json.loads(case.payload)
        data["role"] = "normal"
        case.redis.entries[case.redis.token_key] = ("string", canonical(data).encode(), 60000)
    elif state == "forged":
        genuine = observe(case)
        forged = replace(genuine)
        monkeypatch.setattr(
            case.store,
            "observe_versioned_invitation",
            Mock(return_value=InvitationObservationResult("observed", forged)),
        )
    elif state == "stale":
        original = case.store.observe_versioned_invitation
        clock = [100.0]
        monkeypatch.setattr(service_module, "monotonic", lambda: clock[0])

        def slow_observe(token):
            result = original(token)
            clock[0] = 200.0
            return result

        monkeypatch.setattr(case.store, "observe_versioned_invitation", slow_observe)
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token="invalid" if state == "invalid_token" else TOKEN, prepared=prepared)
    assert_unwritten(case)
    assert prepared._consumed is False


@pytest.mark.parametrize(
    "mutation",
    [
        "namespace",
        "identity",
        "account",
        "workspace",
        "revision",
        "generation",
        "fence",
        "issuance",
        "lifecycle",
        "join_role",
        "join_id",
    ],
)
def test_changed_live_state_blocks_existing_operation_sql_only(operation_case, mutation):
    case = operation_case
    first = create(case)
    with case.db() as session:
        if mutation == "namespace":
            session.get(Namespace, case.ids["namespace"]).organization = "Changed"
        elif mutation == "identity":
            session.execute(sa.update(Identity).values(issuer="https://changed.example.invalid"))
        elif mutation == "account":
            session.get(Account, case.ids["account"]).status = AccountStatus.BANNED
        elif mutation == "workspace":
            session.delete(session.get(TenantAccountJoin, case.ids["join"]))
            from models.account import Tenant

            session.delete(session.get(Tenant, case.ids["workspace"]))
        elif mutation == "revision":
            session.get(Integration, case.ids["integration"]).active_revision_id = str(uuid4())
        elif mutation == "generation":
            session.execute(sa.update(Identity).values(sync_generation=1))
        elif mutation == "fence":
            session.get(Namespace, case.ids["namespace"]).fence_epoch += 1
        elif mutation == "issuance":
            session.get(Issuance, case.ids["issuance"]).payload_digest = "0" * 64
        elif mutation == "lifecycle":
            session.get(Lifecycle, case.ids["lifecycle"]).epoch += 1
        elif mutation == "join_role":
            session.get(TenantAccountJoin, case.ids["join"]).role = "normal"
        else:
            session.get(TenantAccountJoin, case.ids["join"]).id = str(uuid4())
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)
    assert len(case.redis.calls) == 1
    with case.db() as session:
        assert session.get(Intent, str(first.snapshot.operation_id)).desired_json == first.snapshot.desired_json


@pytest.mark.parametrize("phase", ["intent_insert", "owner_after_persist", "precommit_deadline", "contention"])
def test_late_failure_rolls_back_account_identity_and_operation(operation_case, monkeypatch, phase):
    case = operation_case
    prepared = prepare(case)
    if phase == "intent_insert":
        with case.db() as session:
            session.execute(
                sa.text(
                    "CREATE TRIGGER reject_p3k_intent BEFORE INSERT ON casdoor_sync_intent_extend "
                    "BEGIN SELECT RAISE(ABORT, 'private failure'); END"
                )
            )
            session.commit()
    elif phase == "owner_after_persist":
        original = case.owner.persist_invited_login_account

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("private owner data")

        monkeypatch.setattr(case.owner, "persist_invited_login_account", fail)
    elif phase == "precommit_deadline":
        original = CasdoorInvitationOperationRepository.create_pending
        clock = [100.0]
        monkeypatch.setattr(service_module, "monotonic", lambda: clock[0])
        from repositories import casdoor_invitation_operation_repository_extend as repository_module

        monkeypatch.setattr(repository_module, "monotonic", lambda: clock[0])

        def create_then_expire(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = 200.0
            return result

        monkeypatch.setattr(CasdoorInvitationOperationRepository, "create_pending", create_then_expire)
    else:
        original = CasdoorInvitationOperationRepository.create_pending

        def lock_contention(*args, **kwargs):
            raise sa.exc.OperationalError("FOR UPDATE NOWAIT", {}, Exception("private lock unavailable"))

        monkeypatch.setattr(CasdoorInvitationOperationRepository, "create_pending", lock_contention)
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    assert_unwritten(case)
    assert len(case.redis.calls) == 1 and case.redis.token_key in case.redis.entries


@pytest.mark.parametrize("mode", ["before", "after"])
def test_commit_failure_or_lost_ack_never_returns_committed_handle(operation_case, monkeypatch, mode):
    case = operation_case
    prepared = prepare(case)
    original_create = CasdoorInvitationOperationRepository.create_pending

    def marked_create(self, *args, **kwargs):
        result = original_create(self, *args, **kwargs)
        self._session.info["p3k-created"] = True
        return result

    monkeypatch.setattr(CasdoorInvitationOperationRepository, "create_pending", marked_create)
    original_commit = SessionTransaction.commit

    def uncertain_commit(self, *args, **kwargs):
        if self._parent is None and self.session.info.get("p3k-created"):
            if mode == "after":
                original_commit(self, *args, **kwargs)
            raise RuntimeError("private commit uncertainty")
        return original_commit(self, *args, **kwargs)

    monkeypatch.setattr(SessionTransaction, "commit", uncertain_commit)
    case.sessions.clear()
    with pytest.raises(InvitationOperationConflict) as caught:
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    assert str(caught.value) == "invitation_operation_unavailable" and caught.value.__context__ is None
    assert len(case.sessions) == 2  # No reread or token operation after unknown commit.
    assert len(case.redis.calls) == 1
    with case.db() as session:
        count = session.scalar(sa.select(sa.func.count()).select_from(Intent))
        assert count == (1 if mode == "after" else 0)
    if mode == "before":
        assert_unwritten(case)


@pytest.mark.parametrize("mode", ["read_error", "changed_snapshot", "same_session"])
def test_fresh_reread_must_match_before_a_handle_is_returned(operation_case, monkeypatch, mode):
    case = operation_case
    prepared = prepare(case)
    original_read = case.service._read
    calls = [0]
    if mode == "same_session":
        original_factory = case.factory
        issued = []

        def factory():
            if len(issued) == 2:
                return issued[-1]
            result = original_factory()
            issued.append(result)
            return result

        monkeypatch.setattr(case.service, "_session_factory", factory)
    else:

        def bad_read(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2 and mode == "read_error":
                raise RuntimeError("private reread error")
            result, facts, session = original_read(*args, **kwargs)
            if calls[0] == 2:
                result = replace(result, scope_digest="0" * 64)
            return result, facts, session

        monkeypatch.setattr(case.service, "_read", bad_read)
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 1
    assert len(case.redis.calls) == 1


def test_no_bearer_raw_key_or_provider_token_in_sql_repr_or_failure(operation_case, caplog, capsys, monkeypatch):
    case = operation_case
    handle = create(case)
    rendered = repr(handle) + repr(handle.snapshot) + repr(case.attempt)
    with case.db() as session:
        row = session.get(Intent, str(handle.snapshot.operation_id))
        rendered += repr(tuple(getattr(row, column.name) for column in Intent.__table__.columns))
    monkeypatch.setattr(
        case.service, "_read", Mock(side_effect=RuntimeError(TOKEN + " member_invite: private-provider-token"))
    )
    with pytest.raises(InvitationOperationConflict) as caught:
        case.service.produce(case.attempt)
    rendered += str(caught.value) + caplog.text + str(capsys.readouterr())
    assert TOKEN not in rendered and "member_invite:" not in rendered and "private-provider-token" not in rendered
    assert caught.value.__context__ is None


def test_missing_join_is_snapshotted_without_creating_membership(operation_case):
    case = operation_case
    with case.db() as session:
        session.delete(session.get(TenantAccountJoin, case.ids["join"]))
        row = session.get(Issuance, case.ids["issuance"])
        data = json.loads(row.payload_json)
        data["invitation_authority"]["join_id_at_issue"] = None
        row.join_id_at_issue = None
        row.payload_json = canonical(data)
        row.payload_digest = sha256(row.payload_json.encode()).hexdigest()
        case.payload = row.payload_json
        session.commit()
    case.redis.entries[case.redis.token_key] = ("string", case.payload.encode(), 60000)
    handle = create(case)
    data = json.loads(handle.snapshot.desired_json)
    assert data["join_id_at_issue"] is None and data["observed_join_role"] is None
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_control_signals_are_sanitized_after_rollback(operation_case, monkeypatch, signal):
    case = operation_case
    prepared = prepare(case)
    monkeypatch.setattr(case.owner, "persist_invited_login_account", Mock(side_effect=signal("private owner data")))
    with pytest.raises(signal) as caught:
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    assert "private" not in str(caught.value) and caught.value.__context__ is None
    assert_unwritten(case)


@pytest.mark.parametrize("field", ["namespace_id", "subject", "subject_digest", "account_id"])
def test_actual_identity_row_must_match_raw_verified_identity(operation_case, field):
    case = operation_case
    create(case)
    with case.db() as session:
        value = str(uuid4()) if field.endswith("_id") else "changed" if field == "subject" else "0" * 64
        session.execute(sa.update(Identity).values(**{field: value}))
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)
    assert len(case.redis.calls) == 1


@pytest.mark.parametrize("field", ["account_id", "workspace_id", "issuance_id", "expected_generation"])
def test_changed_authenticated_attempt_never_selects_other_operation(operation_case, field):
    case = operation_case
    original = create(case)
    changed = replace(case.attempt, **{field: 1 if field == "expected_generation" else uuid4()})
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(changed)
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 1
        assert session.get(Intent, str(original.snapshot.operation_id)).desired_json == original.snapshot.desired_json
    assert len(case.redis.calls) == 1


@pytest.mark.parametrize("mutation", ["withdrawn", "payload_bytes", "join_role", "identity_generation"])
def test_sql_change_between_observation_and_root_cannot_create(operation_case, monkeypatch, mutation):
    case = operation_case
    prepared = prepare(case)
    original = case.store.observe_versioned_invitation

    def observe_then_change(token):
        result = original(token)
        with case.db() as session:
            if mutation == "withdrawn":
                session.get(Lifecycle, case.ids["lifecycle"]).state = "withdrawn"
            elif mutation == "payload_bytes":
                row = session.get(Issuance, case.ids["issuance"])
                row.payload_json += " "
                row.payload_digest = sha256(row.payload_json.encode()).hexdigest()
            elif mutation == "join_role":
                session.get(TenantAccountJoin, case.ids["join"]).role = "normal"
            else:
                session.add(
                    Identity(
                        namespace_id=str(case.context.namespace_id),
                        account_id=case.ids["account"],
                        issuer=case.context.issuer,
                        organization=case.context.organization,
                        subject=case.context.subject,
                        sync_generation=1,
                        last_applied_json="{}",
                        profile_sync_json="{}",
                    )
                )
            session.commit()
        return result

    monkeypatch.setattr(case.store, "observe_versioned_invitation", observe_then_change)
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 0
        assert session.get(Account, case.ids["account"]).initialized_at is None
    assert prepared._consumed is False and len(case.redis.calls) == 1


def test_current_store_mismatch_cannot_replace_same_attempt_owner(operation_case, monkeypatch):
    from services.account_adapters import RedisInvitationTokenStore

    case = operation_case
    prepared = prepare(case)
    monkeypatch.setattr(case.service, "_store", RedisInvitationTokenStore(redis=case.redis))
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    assert not case.redis.calls and prepared._consumed is False
    assert_unwritten(case)


def test_duplicate_produce_race_reuses_committed_operation_without_consuming_prepared_owner(
    operation_case, monkeypatch
):
    case = operation_case
    first_prepared = prepare(case)
    second_prepared = prepare(case)
    original_observe = case.store.observe_versioned_invitation
    winner = []

    def observe_with_winner(token):
        result = original_observe(token)
        monkeypatch.setattr(case.store, "observe_versioned_invitation", original_observe)
        winner.append(case.service.produce(case.attempt, token=TOKEN, prepared=second_prepared))
        return result

    monkeypatch.setattr(case.store, "observe_versioned_invitation", observe_with_winner)
    result = case.service.produce(case.attempt, token=TOKEN, prepared=first_prepared)
    assert result == winner[0] and first_prepared._consumed is False and second_prepared._consumed is True
    assert len(case.redis.calls) == 2  # One fresh observation in each explicit concurrent-style call.
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 1


def test_expiry_during_acknowledged_commit_never_returns_live_handle(operation_case, monkeypatch):
    from repositories import casdoor_invitation_operation_repository_extend as repository_module

    case = operation_case
    prepared = prepare(case)
    clock = [100.0]
    monkeypatch.setattr(service_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(repository_module, "monotonic", lambda: clock[0])
    original_create = CasdoorInvitationOperationRepository.create_pending

    def marked_create(self, *args, **kwargs):
        result = original_create(self, *args, **kwargs)
        self._session.info["expire-after-commit"] = True
        return result

    monkeypatch.setattr(CasdoorInvitationOperationRepository, "create_pending", marked_create)
    original_commit = SessionTransaction.commit

    def slow_commit(self, *args, **kwargs):
        result = original_commit(self, *args, **kwargs)
        if self._parent is None and self.session.info.get("expire-after-commit"):
            clock[0] = 200.0
        return result

    monkeypatch.setattr(SessionTransaction, "commit", slow_commit)
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    assert len(case.redis.calls) == 1
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 1
    # Durable pending state can later be retrieved through exact SQL-only retry.
    assert case.service.produce(case.attempt).snapshot.operation_id


def test_service_accepts_no_client_operation_or_authority_override(operation_case):
    import inspect

    case = operation_case
    parameters = inspect.signature(case.service.produce).parameters
    assert set(parameters) == {"attempt", "token", "prepared"}
    with pytest.raises(TypeError):
        case.service.produce(case.attempt, operation_id=str(uuid4()))
    assert_unwritten(case)


def test_active_initialized_unbound_account_identity_savepoint_rolls_back_with_root(operation_case, monkeypatch):
    from datetime import datetime

    case = operation_case
    with case.db() as session:
        account = session.get(Account, case.ids["account"])
        account.status = AccountStatus.ACTIVE
        account.initialized_at = datetime(2020, 1, 1)
        session.commit()
    prepared = prepare(case)
    original = CasdoorInvitationOperationRepository.create_pending

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("private root failure")

    monkeypatch.setattr(CasdoorInvitationOperationRepository, "create_pending", fail_after_insert)
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 0
        account = session.get(Account, case.ids["account"])
        assert account.name == "Original" and account.initialized_at == datetime(2020, 1, 1)
        assert protected(case, session) == case.protected


def test_changed_local_email_does_not_reuse_invitation_operation(operation_case):
    case = operation_case
    create(case)
    with case.db() as session:
        session.get(Account, case.ids["account"]).email = "changed@example.test"
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)
    assert len(case.redis.calls) == 1


@pytest.mark.parametrize("model", [Lifecycle, Issuance, Intent])
def test_authority_or_operation_nowait_contention_aborts_without_redis_or_writes(operation_case, monkeypatch, model):
    case = operation_case
    prepared = prepare(case)
    original = CasdoorInvitationOperationRepository._one

    def contended(self, candidate, *args):
        if candidate is model:
            raise sa.exc.OperationalError("NOWAIT", {}, Exception("lock unavailable"))
        return original(self, candidate, *args)

    monkeypatch.setattr(CasdoorInvitationOperationRepository, "_one", contended)
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt, token=TOKEN, prepared=prepared)
    assert not case.redis.calls and prepared._consumed is False
    assert_unwritten(case)
