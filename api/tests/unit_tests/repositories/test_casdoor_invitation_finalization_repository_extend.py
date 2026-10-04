"""Local sequential/fault proofs using actual SQL/P1/P2/P3K and membership owners."""

import json
from datetime import timedelta
from hashlib import sha256
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, TenantAccountJoin
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_invitation_finalization_repository_extend import CasdoorInvitationFinalizationRepository
from repositories.casdoor_invitation_operation_repository_extend import InvitationOperationConflict
from services.account_service import TenantService
from services.casdoor_invitation_finalization_service_extend import CasdoorInvitationFinalizationService
from tests.unit_tests.repositories.test_casdoor_invitation_operation_repository_extend import (
    TOKEN,
    canonical,
    create,
    protected,
)
from tests.unit_tests.repositories.test_casdoor_invitation_operation_repository_extend import (
    operation_case as base_operation_case,  # noqa: F401
)
from tests.unit_tests.services.test_invitation_token_consumption_extend import FakeRedis


@pytest.fixture
def finalizer_case(base_operation_case, config_overrides):  # noqa: F811
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, RBAC_ENABLED=False, DATASET_OPERATOR_ENABLED=True)
    case = base_operation_case

    def build(*, absent=False, role="admin", epoch=None):
        with case.db() as session:
            issuance = session.get(Issuance, case.ids["issuance"])
            data = json.loads(issuance.payload_json)
            data["role"] = issuance.role = role
            if absent:
                session.delete(session.get(TenantAccountJoin, case.ids["join"]))
                data["invitation_authority"]["join_id_at_issue"] = issuance.join_id_at_issue = None
            if epoch is not None:
                session.get(Lifecycle, case.ids["lifecycle"]).epoch = epoch
                data["invitation_authority"]["lifecycle_epoch"] = issuance.lifecycle_epoch = epoch
            issuance.payload_json = case.payload = canonical(data)
            issuance.payload_digest = sha256(issuance.payload_json.encode()).hexdigest()
            session.commit()
        case.redis.entries[case.redis.token_key] = ("string", case.payload.encode(), 60000)
        case.pending = create(case)
        case.redis.calls.clear()
        case.sessions.clear()

        def guarded_eval(script, *args):
            assert not any(session.in_transaction() for session in case.sessions)
            return FakeRedis.eval(case.redis, script, *args)

        case.redis.eval = guarded_eval
        case.finalizer = CasdoorInvitationFinalizationService(session_factory=case.factory, store=case.store)
        return case

    return build


def state(case):
    with case.db() as session:
        return [
            list(session.execute(sa.select(*model.__table__.columns)).all())
            for model in (Account, TenantAccountJoin, Lifecycle, Issuance, Intent)
        ]


def sql_finalize(case):
    with case.factory() as session, session.begin():
        repo = CasdoorInvitationFinalizationRepository(session)
        facts = repo.inspect(case.attempt)
        receipt = json.loads(facts.snapshot.desired_json)["expected_receipt_json"]
        return repo.finalize(case.attempt, expected=facts, receipt_json=receipt)


@pytest.mark.parametrize("absent", [False, True])
def test_actual_sql_atomic_completion_and_readonly_replay(finalizer_case, absent, monkeypatch):
    case = finalizer_case(absent=absent)
    before = case.pending.snapshot
    completed = case.finalizer.finalize(case.attempt, token=TOKEN)
    assert completed.facts.completed and completed.facts.membership_created is absent
    assert completed.facts.result_epoch == (2 if absent else 1)
    assert len(case.sessions) == 3 and len({id(s) for s in case.sessions}) == 3
    assert completed.facts.snapshot == before
    with case.db() as session:
        row = session.get(Intent, str(before.operation_id))
        issuance = session.get(Issuance, case.ids["issuance"])
        join = session.get(TenantAccountJoin, completed.facts.join_id)
        assert join.role == ("admin" if absent else "editor")
        assert (
            issuance.state == "consumed"
            and issuance.consumption_receipt_json == json.loads(before.desired_json)["expected_receipt_json"]
        )
        assert row.terminated_at == row.updated_at and row.proof_ref == completed.facts.proof_ref
        assert row.termination_proof_kind == "invitation_finalize_sql_v1" and len(row.proof_ref.encode()) <= 128
        if not absent:
            assert protected(case, session) == case.protected
    frozen = state(case)
    monkeypatch.setattr(case.store, "observe_versioned_invitation", Mock(side_effect=AssertionError("SQL only")))
    monkeypatch.setattr(TenantService, "persist_tenant_member", Mock(side_effect=AssertionError("no helper replay")))
    assert case.finalizer.finalize(case.attempt) == completed
    assert state(case) == frozen
    assert all(not s.in_transaction() for s in case.sessions)
    for effect in case.effects[2:]:
        assert not effect.mock_calls


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unknown",
        "whitespace",
        "bool",
        "overflow",
        "receipt_extra",
        "receipt_bool",
        "digest",
        "deadline",
        "oversize",
    ],
)
def test_pending_snapshot_strict_canonical_closed_schema(finalizer_case, mutation):
    case = finalizer_case()
    with case.db() as session:
        row = session.get(Intent, str(case.pending.snapshot.operation_id))
        data = json.loads(row.desired_json)
        raw = row.desired_json
        if mutation == "duplicate":
            raw = '{"schema_version":1,' + raw[1:]
        elif mutation == "unknown":
            data["extra"] = 1
        elif mutation == "whitespace":
            raw = " " + raw
        elif mutation == "bool":
            data["schema_version"] = True
        elif mutation == "overflow":
            data["lifecycle_epoch"] = 2**53
        elif mutation.startswith("receipt_"):
            receipt = json.loads(data["expected_receipt_json"])
            receipt["extra" if mutation == "receipt_extra" else "schema_version"] = True
            data["expected_receipt_json"] = canonical(receipt)
        elif mutation == "deadline":
            data["reconcile_until_utc"] = "2099-01-01T00:00:00.000000Z"
        elif mutation == "oversize":
            data["kind"] = "x" * 17000
        if raw == row.desired_json:
            raw = canonical(data)
        row.desired_json = raw
        row.scope_digest = "a" * 64 if mutation == "digest" else sha256(raw.encode()).hexdigest()
        session.commit()
    before = state(case)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert not case.redis.calls and state(case) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "bool",
        "epoch_zero",
        "epoch_leading",
        "epoch_overflow",
        "created",
        "join",
        "extra",
        "unicode",
        "oversize",
        "kind",
        "timestamp",
        "receipt",
        "lifecycle",
        "deleted_join",
        "role",
    ],
)
def test_completed_proof_and_current_self_change_strict(finalizer_case, mutation):
    case = finalizer_case(absent=True)
    completed = case.finalizer.finalize(case.attempt, token=TOKEN)
    with case.db() as session:
        row = session.get(Intent, str(case.pending.snapshot.operation_id))
        parts = row.proof_ref.split(":")
        if mutation == "hash":
            parts[-1] = "0" * 64
        elif mutation == "bool":
            parts[2] = "True"
        elif mutation == "epoch_zero":
            parts[3] = "0"
        elif mutation == "epoch_leading":
            parts[3] = "02"
        elif mutation == "epoch_overflow":
            parts[3] = str(2**53)
        elif mutation == "created":
            parts[2] = "0"
        elif mutation == "join":
            parts[1] = str(uuid4())
        elif mutation == "extra":
            parts.append("extra")
        elif mutation == "unicode":
            parts[0] = "v一"
        elif mutation == "oversize":
            parts[-1] = "a" * 200
        elif mutation == "kind":
            row.termination_proof_kind = "other"
        elif mutation == "timestamp":
            row.updated_at += timedelta(seconds=1)
        elif mutation == "receipt":
            session.get(Issuance, case.ids["issuance"]).consumption_receipt_json += " "
        elif mutation == "lifecycle":
            session.get(Lifecycle, case.ids["lifecycle"]).epoch += 1
        elif mutation == "deleted_join":
            session.delete(session.get(TenantAccountJoin, completed.facts.join_id))
        elif mutation == "role":
            session.get(TenantAccountJoin, completed.facts.join_id).role = "normal"
        row.proof_ref = ":".join(parts)
        session.commit()
    before, calls = state(case), len(case.redis.calls)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt)
    assert state(case) == before and len(case.redis.calls) == calls


@pytest.mark.parametrize(
    "fault", ["existing", "role", "account", "epoch", "raise_after_helper", "receipt_flush", "marker_flush", "audit"]
)
def test_every_helper_or_sql_failure_rolls_back_whole_root(finalizer_case, monkeypatch, fault):
    case = finalizer_case(absent=True)
    before = state(case)
    original = TenantService.persist_tenant_member

    def helper(tenant, account, session, role):
        result = original(tenant, account, session, role)
        if fault == "existing":
            return type(result)(join=result.join, membership_created=False)
        if fault == "role":
            result.join.role = "normal"
        if fault == "account":
            result.join.account_id = str(uuid4())
        if fault == "epoch":
            session.get(Lifecycle, case.ids["lifecycle"]).epoch += 1
        if fault == "raise_after_helper":
            raise RuntimeError("injected")
        return result

    monkeypatch.setattr(TenantService, "persist_tenant_member", helper)
    if fault in ("receipt_flush", "marker_flush"):
        from sqlalchemy.orm import Session

        original_flush = Session.flush

        def flush(session, *args, **kwargs):
            dirty = list(session.dirty)
            if any(isinstance(r, Issuance if fault == "receipt_flush" else Intent) for r in dirty):
                raise RuntimeError("injected flush")
            return original_flush(session, *args, **kwargs)

        monkeypatch.setattr(Session, "flush", flush)
    if fault == "audit":
        original_inspect = CasdoorInvitationFinalizationRepository.inspect

        def inspect(repo, attempt):
            result = original_inspect(repo, attempt)
            if result.completed:
                raise RuntimeError("injected audit")
            return result

        monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", inspect)
    with pytest.raises((InvitationOperationConflict, RuntimeError)):
        sql_finalize(case)
    assert state(case) == before


def test_p2_receipt_without_completion_never_reconstructs_marker(finalizer_case):
    case = finalizer_case()
    from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository

    with case.db() as session, session.begin():
        InvitationAuthorityRepository().record_consumption(
            session, receipt_json=json.loads(case.pending.snapshot.desired_json)["expected_receipt_json"]
        )
    before = state(case)
    with pytest.raises(InvitationOperationConflict):
        case.finalizer.finalize(case.attempt, token=TOKEN)
    assert state(case) == before and not case.redis.calls


def test_repository_requires_clean_explicit_root(finalizer_case):
    case = finalizer_case()
    with case.factory() as session:
        with pytest.raises(InvitationOperationConflict):
            CasdoorInvitationFinalizationRepository(session).inspect(case.attempt)
        with session.begin(), session.begin_nested(), pytest.raises(InvitationOperationConflict):
            CasdoorInvitationFinalizationRepository(session).inspect(case.attempt)
        with session.begin():
            session.get(Account, case.ids["account"]).name = "dirty"
            with pytest.raises(InvitationOperationConflict):
                CasdoorInvitationFinalizationRepository(session).inspect(case.attempt)
            session.rollback()


def test_finalizer_lock_order_and_nowait_compilation(finalizer_case, monkeypatch):
    from sqlalchemy.dialects import mysql, postgresql
    from sqlalchemy.orm import Session

    case = finalizer_case()
    queries = []
    original = Session._execute_internal

    def capture(session, statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            queries.append(statement)
        return original(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "_execute_internal", capture)
    with case.factory() as session, session.begin():
        CasdoorInvitationFinalizationRepository(session).inspect(case.attempt)
    assert len(queries) == 10
    assert [q.get_final_froms()[0].name for q in queries] == [
        "casdoor_integration_extend",
        "casdoor_namespace_extend",
        "casdoor_config_revision_extend",
        "accounts",
        "tenants",
        "casdoor_identity_extend",
        "invitation_authority_lifecycle_extend",
        "invitation_authority_issuance_extend",
        "tenant_account_joins",
        "casdoor_sync_intent_extend",
    ]
    for dialect in (mysql.dialect(), postgresql.dialect()):
        assert all(q._for_update_arg.nowait and "NOWAIT" in str(q.compile(dialect=dialect)) for q in queries)


def test_maximum_epoch_completion_proof_size_and_closed_digest_fields(finalizer_case):
    case = finalizer_case(absent=True, epoch=2**53 - 2)
    result = case.finalizer.finalize(case.attempt, token=TOKEN)
    assert result.facts.result_epoch == 2**53 - 1
    assert len(result.facts.proof_ref.encode("ascii")) == 123
    data = json.loads(result.facts.snapshot.desired_json)
    expected = {
        "operation_id": str(result.facts.snapshot.operation_id),
        "scope_digest": result.facts.snapshot.scope_digest,
        "expected_receipt_sha256": sha256(data["expected_receipt_json"].encode()).hexdigest(),
        "issuance_id": data["issuance_id"],
        "lifecycle_id": data["lifecycle_id"],
        "input_epoch": 2**53 - 2,
        "result_epoch": 2**53 - 1,
        "join_id": result.facts.join_id,
        "join_role": "admin",
        "membership_created": True,
    }
    assert len(expected) == 10
    assert (
        result.facts.proof_ref.split(":")[-1]
        == sha256(b"casdoor:invitation-finalize:sql-completion:v1:" + canonical(expected).encode("ascii")).hexdigest()
    )


@pytest.mark.parametrize("completed", [False, True])
def test_operation_resource_and_retry_fields_never_claim_remote_work(finalizer_case, completed):
    case = finalizer_case()
    if completed:
        case.finalizer.finalize(case.attempt, token=TOKEN)
    with case.db() as session:
        row = session.get(Intent, str(case.pending.snapshot.operation_id))
        assert row.membership_id is None and row.ownership_epoch == row.attempt_count == 0
        assert all(
            getattr(row, name) is None
            for name in (
                "resource_type",
                "resource_id",
                "attempt_id",
                "lease_owner",
                "lease_expires_at",
                "sent_at",
                "acknowledged_at",
                "readback_at",
                "retry_at",
                "error_code",
            )
        )
