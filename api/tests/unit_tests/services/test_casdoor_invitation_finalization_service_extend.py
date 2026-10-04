"""Offline service sequencing with actual P1/P3J and deterministic RESP transport."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import SessionTransaction

from enums import DeploymentEdition
from models.account import Account, Tenant, TenantAccountJoin
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories import casdoor_invitation_finalization_repository_extend as repo_module
from repositories.casdoor_invitation_operation_repository_extend import InvitationOperationConflict
from repositories.casdoor_required_intent_repository_extend import CasdoorRequiredIntentRepository
from services import account_adapters as adapters
from services import casdoor_invitation_finalization_service_extend as service_module
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import InvitationConsumptionReadback, InvitationObservationResult
from tests.unit_tests.repositories.test_casdoor_invitation_finalization_repository_extend import (
    TOKEN,
    base_operation_case,  # noqa: F401
    state,
)
from tests.unit_tests.repositories.test_casdoor_invitation_finalization_repository_extend import (
    finalizer_case as finalizer_fixture,
)
from tests.unit_tests.services import test_invitation_publication_transport_extend as transport
from tests.unit_tests.services.test_invitation_token_consumption_extend import FakeRedis

finalizer_case = finalizer_fixture


def calls(case, script):
    return [call for call in case.redis.calls if call[0] == script]


class Wire(transport.Wire):
    def __init__(self, case):
        super().__init__(eval_mode="exact")
        self.case = case

    def answer(self, sock, frame):
        if frame[0].upper() != b"EVAL":
            return super().answer(sock, frame)
        self.frames.append(frame)
        self.business = True
        self.eval_responses += 1
        assert not any(s.in_transaction() for s in self.case.sessions)
        assert frame[1] == adapters._INVITATION_READBACK.encode() and frame[2] == b"2"
        return transport.resp(FakeRedis.eval(self.case.redis, adapters._INVITATION_READBACK, 2, *frame[3:]))


def recovery_transport(case, monkeypatch):
    wire = Wire(case)
    _wrapper, raw, _pool = transport.client_for(wire, monkeypatch)
    # P1 uses its existing deterministic store; P3J uses real redis-py raw checkout.
    case.redis._require_client = lambda: raw
    return wire


@pytest.mark.parametrize("absent", [False, True])
def test_only_exact_not_confirmed_consumes_once_and_barrier_remains(finalizer_case, absent):
    case = finalizer_case(absent=absent)
    completed = case.finalizer.finalize(case.attempt, token=TOKEN)
    assert completed.facts.completed
    assert len(calls(case, adapters._INVITATION_OBSERVE)) == 1
    assert len(calls(case, adapters._INVITATION_READBACK)) == 1
    consumption = calls(case, adapters._INVITATION_CONSUME)
    assert len(consumption) == 1 and 1 <= consumption[0][2][-1] <= 604799
    with case.db() as session, session.begin():
        assert CasdoorRequiredIntentRepository(session).read_locked(case.attempt.account_id, case.attempt.workspace_id)


@pytest.mark.parametrize("status", ["unknown", "unavailable"])
def test_p1_uncertain_never_consumes_or_finalizes(finalizer_case, monkeypatch, status):
    case = finalizer_case()
    before = state(case)
    monkeypatch.setattr(
        case.store, "read_invitation_consumption", lambda *a, **k: InvitationConsumptionReadback(status)
    )
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not calls(case, adapters._INVITATION_CONSUME) and state(case) == before


def test_p1_confirmed_has_zero_service_consume_calls(finalizer_case, monkeypatch):
    case = finalizer_case()
    original = case.store.read_invitation_consumption

    def read(observation, *, operation_id):
        assert (
            case.store.consume_versioned_invitation(
                observation, operation_id=operation_id, receipt_ttl_seconds=60
            ).status
            == "consumed"
        )
        return original(observation, operation_id=operation_id)

    monkeypatch.setattr(case.store, "read_invitation_consumption", read)
    case.finalizer.finalize(case.attempt, token=TOKEN)
    # Only the deliberately interleaved external consumer above, no service consume.
    assert len(calls(case, adapters._INVITATION_CONSUME)) == 1


@pytest.mark.parametrize("consumed", [False, True])
def test_observation_absent_or_unavailable_uses_actual_p3j_only(finalizer_case, monkeypatch, consumed):
    case = finalizer_case(absent=True)
    if consumed:
        observed = case.store.observe_versioned_invitation(TOKEN).observation
        assert (
            case.store.consume_versioned_invitation(
                observed, operation_id=str(case.pending.snapshot.operation_id), receipt_ttl_seconds=60
            ).status
            == "consumed"
        )
        # New exact issuing store has no observation registry entries.
        case.store = case.finalizer._store = RedisInvitationTokenStore(redis=case.redis)
    else:
        monkeypatch.setattr(
            case.store, "observe_versioned_invitation", lambda _: InvitationObservationResult("unavailable")
        )
    case.redis.calls.clear()
    wire = recovery_transport(case, monkeypatch)
    if consumed:
        assert case.finalizer.finalize(case.attempt, token=TOKEN).facts.completed
    else:
        before = state(case)
        with pytest.raises(InvitationOperationConflict):
            case.finalizer.finalize(case.attempt, token=TOKEN)
        assert state(case) == before
    assert wire.eval_responses == 1 and not calls(case, adapters._INVITATION_CONSUME)


@pytest.mark.parametrize("after_effect", [False, True])
def test_consume_unknown_permits_one_actual_p3j_read_only(finalizer_case, monkeypatch, after_effect):
    case = finalizer_case()
    wire = recovery_transport(case, monkeypatch)
    original = case.redis.eval

    def eval_(script, *args):
        if script == adapters._INVITATION_CONSUME:
            if after_effect:
                case.redis.fail_after = True
                return original(script, *args)
            case.redis.calls.append((script, args[0], args[1:]))
            raise RuntimeError("before effect")
        return original(script, *args)

    case.redis.eval = eval_
    if after_effect:
        assert case.finalizer.finalize(case.attempt, token=TOKEN).facts.completed
    else:
        before = state(case)
        with pytest.raises(InvitationOperationConflict):
            case.finalizer.finalize(case.attempt, token=TOKEN)
        assert state(case) == before
    assert len(calls(case, adapters._INVITATION_CONSUME)) == 1 and wire.eval_responses == 1


@pytest.mark.parametrize(
    "edition,rbac",
    [(DeploymentEdition.ENTERPRISE, False), (DeploymentEdition.CLOUD, False), (DeploymentEdition.COMMUNITY, True)],
)
def test_policy_stops_before_any_redis(finalizer_case, config_overrides, edition, rbac):
    case = finalizer_case()
    config_overrides(DEPLOYMENT_EDITION=edition, RBAC_ENABLED=rbac)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not case.redis.calls


@pytest.mark.parametrize(
    "role,enabled,epoch",
    [("owner", True, None), ("invalid", True, None), ("dataset_operator", False, None), ("admin", True, 2**53 - 1)],
)
def test_new_join_role_and_epoch_gates(finalizer_case, config_overrides, role, enabled, epoch):
    case = finalizer_case(absent=True, role=role, epoch=epoch)
    config_overrides(DATASET_OPERATOR_ENABLED=enabled)
    before = state(case)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not case.redis.calls and state(case) == before


@pytest.mark.parametrize(
    "model,field,value,key",
    [
        (Integration, "enabled", False, "integration"),
        (Integration, "active_revision_id", "00000000-0000-4000-8000-000000000001", "integration"),
        (Namespace, "fence_epoch", 1, "namespace"),
        (Namespace, "expected_issuer", "other", "namespace"),
        (Revision, "config_digest", "0" * 64, "revision"),
        (Revision, "organization", "other", "revision"),
        (Account, "email", "other@example.com", "account"),
        (Account, "status", "banned", "account"),
        (Tenant, "status", "archive", "workspace"),
        (Lifecycle, "state", "withdrawn", "lifecycle"),
        (Lifecycle, "epoch", 2, "lifecycle"),
        (TenantAccountJoin, "role", "normal", "join"),
    ],
)
def test_current_chain_drift_stops_all_io(finalizer_case, model, field, value, key):
    case = finalizer_case()
    with case.db() as session:
        session.execute(
            sa.update(model)
            .where((model.lifecycle_id if model is Lifecycle else model.id) == case.ids[key])
            .values(**{field: value})
        )
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not case.redis.calls


@pytest.mark.parametrize(
    "field,value", [("subject", "other"), ("sync_generation", 1), ("issuer", "other"), ("subject_digest", "0" * 64)]
)
def test_exact_identity_drift_rejected(finalizer_case, field, value):
    case = finalizer_case()
    with case.db() as session:
        identity = session.scalar(sa.select(Identity))
        session.execute(sa.update(Identity).where(Identity.id == identity.id).values(**{field: value}))
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not case.redis.calls


def test_deadlines_fixed_and_historical_completed_replay(finalizer_case, monkeypatch):
    case = finalizer_case()
    completed = case.finalizer.finalize(case.attempt, token=TOKEN)
    future = completed.facts.snapshot.created_at + timedelta(days=8)
    monkeypatch.setattr(repo_module, "_now", lambda: future)
    monkeypatch.setattr(service_module, "_now", lambda: future)
    count = len(case.redis.calls)
    assert case.finalizer.finalize(case.attempt) == completed
    assert len(case.redis.calls) == count


def test_pending_fixed_deadline_expiry_prevents_all_io(finalizer_case, monkeypatch):
    case = finalizer_case()
    future = case.pending.snapshot.created_at + timedelta(days=7)
    monkeypatch.setattr(repo_module, "_now", lambda: future)
    monkeypatch.setattr(service_module, "_now", lambda: future)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not case.redis.calls


@pytest.mark.parametrize("elapsed", [0.061, 30.001])
def test_monotonic_window_includes_observation_io(finalizer_case, monkeypatch, elapsed):
    case = finalizer_case()
    case.redis.entries[case.redis.token_key] = ("string", case.payload.encode(), 60 if elapsed < 1 else 60000)
    now = [100.0]
    monkeypatch.setattr(service_module, "monotonic", lambda: now[0])
    original = case.redis.eval

    def eval_(script, *args):
        result = original(script, *args)
        now[0] += elapsed
        return result

    case.redis.eval = eval_
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert len(case.redis.calls) == 1


@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_uncertainty_returns_no_completion_and_retry_reads_sql(finalizer_case, monkeypatch, after_commit):
    case = finalizer_case(absent=True)
    original = SessionTransaction.commit

    def commit(tx, *args, **kwargs):
        if tx.parent is None and len(case.sessions) == 2 and tx.session is case.sessions[1]:
            if after_commit:
                original(tx, *args, **kwargs)
            raise RuntimeError("commit acknowledgement lost")
        return original(tx, *args, **kwargs)

    before = state(case)
    with monkeypatch.context() as patch:
        patch.setattr(SessionTransaction, "commit", commit)
        with pytest.raises(InvitationOperationConflict):
            case.finalizer.finalize(case.attempt, token=TOKEN)
    if after_commit:
        assert case.finalizer.finalize(case.attempt).facts.completed
    else:
        assert state(case) == before


def test_same_operation_interleaving_replayed_without_duplicate_join(finalizer_case, monkeypatch):
    case = finalizer_case(absent=True)
    original = case.store.read_invitation_consumption

    def interleave(observation, *, operation_id):
        pending = original(observation, operation_id=operation_id)
        assert pending.status == "not_confirmed"
        assert (
            case.store.consume_versioned_invitation(
                observation, operation_id=operation_id, receipt_ttl_seconds=60
            ).status
            == "consumed"
        )
        return pending

    monkeypatch.setattr(case.store, "read_invitation_consumption", interleave)
    original_consume = case.store.consume_versioned_invitation
    statuses = []

    def consume(*args, **kwargs):
        result = original_consume(*args, **kwargs)
        statuses.append(result.status)
        return result

    monkeypatch.setattr(case.store, "consume_versioned_invitation", consume)
    result = case.finalizer.finalize(case.attempt, token=TOKEN)
    assert statuses == ["consumed", "replayed"] and result.facts.result_epoch == 2
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 1
    assert case.finalizer.finalize(case.attempt) == result


@pytest.mark.parametrize("token", [None, "", "not-a-token", "11111111-1111-4111-8111-111111111112"])
def test_missing_or_wrong_bearer_never_consumes(finalizer_case, token, monkeypatch):
    case = finalizer_case()
    recovery_transport(case, monkeypatch)
    before = state(case)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=token)
    assert not calls(case, adapters._INVITATION_CONSUME) and state(case) == before


@pytest.mark.parametrize("mutation", ["copied", "foreign_store", "prefix", "payload", "receipt_key"])
def test_fresh_observation_authenticity_and_exact_receipt(finalizer_case, monkeypatch, mutation):
    case = finalizer_case()
    original = case.store.observe_versioned_invitation

    def observe(token):
        result = original(token)
        observation = result.observation
        if mutation == "copied":
            observation = replace(observation)
        elif mutation == "foreign_store":
            observation = RedisInvitationTokenStore(redis=case.redis).observe_versioned_invitation(token).observation
        elif mutation == "prefix":
            case.redis.prefix = "different"
        elif mutation == "payload":
            object.__setattr__(observation, "_raw", b"different")
        elif mutation == "receipt_key":
            object.__setattr__(observation, "key_digest", "0" * 64)
        return InvitationObservationResult("observed", observation)

    monkeypatch.setattr(case.store, "observe_versioned_invitation", observe)
    before = state(case)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not calls(case, adapters._INVITATION_READBACK) and not calls(case, adapters._INVITATION_CONSUME)
    assert state(case) == before


def test_fresh_postcommit_reread_failure_returns_no_completion(finalizer_case, monkeypatch):
    case = finalizer_case(absent=True)
    original = case.finalizer._read

    def read(attempt, *, previous=None):
        if len(case.sessions) == 2:
            raise RuntimeError("fresh read failed")
        return original(attempt, previous=previous)

    with monkeypatch.context() as patch:
        patch.setattr(case.finalizer, "_read", read)
        with pytest.raises(InvitationOperationConflict):
            case.finalizer.finalize(case.attempt, token=TOKEN)
    assert case.finalizer.finalize(case.attempt).facts.completed


def test_no_new_consume_when_fixed_deadline_has_less_than_one_second(finalizer_case, monkeypatch):
    case = finalizer_case()
    last_half_second = case.pending.snapshot.created_at + timedelta(days=7, microseconds=-500000)
    monkeypatch.setattr(repo_module, "_now", lambda: last_half_second)
    monkeypatch.setattr(service_module, "_now", lambda: last_half_second)
    before = state(case)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not calls(case, adapters._INVITATION_CONSUME) and state(case) == before


@pytest.mark.parametrize("stage", ["readback", "consume", "sql"])
def test_deadline_checked_after_each_blocking_stage(finalizer_case, monkeypatch, stage):
    case = finalizer_case()
    clock = [100.0]
    monkeypatch.setattr(service_module, "monotonic", lambda: clock[0])
    if stage in ("readback", "consume"):
        name = "read_invitation_consumption" if stage == "readback" else "consume_versioned_invitation"
        original = getattr(case.store, name)

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = 131.0
            return result

        monkeypatch.setattr(case.store, name, slow)
    else:
        original = repo_module.CasdoorInvitationFinalizationRepository.finalize

        def slow(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            clock[0] = 131.0
            return result

        monkeypatch.setattr(repo_module.CasdoorInvitationFinalizationRepository, "finalize", slow)
    before = state(case)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert state(case) == before
    if stage == "readback":
        assert not calls(case, adapters._INVITATION_CONSUME)


@pytest.mark.parametrize("status", ["unavailable", "mismatch", "receipt_conflict"])
def test_nonpositive_consume_results_never_finalize(finalizer_case, monkeypatch, status):
    from services.entities.account_activation_entities import InvitationConsumptionResult

    case = finalizer_case()
    before = state(case)
    consume = Mock(return_value=InvitationConsumptionResult(status))
    monkeypatch.setattr(case.store, "consume_versioned_invitation", consume)
    recover = Mock(side_effect=AssertionError("only unknown recovers"))
    monkeypatch.setattr(case.store, "recover_invitation_consumption", recover)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert consume.call_count == 1 and recover.call_count == 0 and state(case) == before


@pytest.mark.parametrize(
    "field,value", [("enabled", False), ("active_revision_id", "00000000-0000-4000-8000-000000000001")]
)
def test_scope_changes_after_consumption_prevent_sql_completion(finalizer_case, monkeypatch, field, value):
    case = finalizer_case(absent=True)
    original = case.store.consume_versioned_invitation

    def consume(*args, **kwargs):
        result = original(*args, **kwargs)
        with case.db() as session, session.begin():
            setattr(session.get(Integration, case.ids["integration"]), field, value)
        return result

    monkeypatch.setattr(case.store, "consume_versioned_invitation", consume)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    with case.db() as session:
        row = session.get(Intent, str(case.pending.snapshot.operation_id))
        assert row.operation_state.value == "pending" and row.proof_ref is None
        assert session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0
        assert session.get(Lifecycle, case.ids["lifecycle"]).epoch == 1


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_control_signal_sanitization(finalizer_case, monkeypatch, signal):
    case = finalizer_case()
    monkeypatch.setattr(case.store, "observe_versioned_invitation", Mock(side_effect=signal("private data")))
    with pytest.raises(signal) as caught:
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert "private data" not in str(caught.value) and caught.value.__context__ is None
