"""Real original producer/consume/B3/F1 then private SQL rediscovery, offline."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
import sqlalchemy as sa

from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as F_ACTION
from core.casdoor.invited_write_receipt import RECEIPT_KIND as D_ACTION
from models.account import Account, AccountStatus, TenantStatus
from models.account import TenantAccountJoin as Join
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_invited_login_scope_repository_extend import (
    CasdoorInvitedLoginScopeRepository,
    InvitationRecoveryLoginScope,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
    InvitationAuthorityValidationError,
)
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    configuration_factory,
    invited_scope_case as original_case,
)
from tests.unit_tests.services.test_casdoor_invited_finalization_receipt_reader_extend import prepare
from tests.unit_tests.services.test_casdoor_invited_local_finalization_service_extend import all_state
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    persist,
    writer_case as original_writer,
)
from tests.unit_tests.services.test_invitation_token_consumption_extend import TOKEN

DIGEST = sha256(TOKEN.encode()).hexdigest()
invited_scope_case = original_case
writer_case = original_writer


def select(case, *, context=None, key=None, digest=DIGEST, remote_email=None):
    with case.db() as session, session.begin():
        return CasdoorInvitedLoginScopeRepository(session, configuration_factory)._discover_invitation_recovery(
            context or case.context, key or case.key, token_digest=digest, remote_email=remote_email
        )


def initialized(case):
    with case.db() as session, session.begin():
        account = session.get(Account, case.ids["account"])
        account.status = AccountStatus.ACTIVE
        account.initialized_at = datetime(2024, 1, 1)
    return case


@pytest.mark.parametrize("absent", [False, True], ids=["preserved-join", "created-join"])
@pytest.mark.parametrize("phase", ["pending", "invitation_completed", "memberships_written", "finalized"])
def test_real_original_chain_selects_original_generation_and_phase(invited_scope_case, writer_case, absent, phase):
    if phase in ("pending", "invitation_completed"):
        case = initialized(invited_scope_case(absent=absent, stage="pending" if phase == "pending" else "completed"))
    else:
        case = writer_case(absent=absent)
        if phase == "finalized":
            prepare(case)
        else:
            persist(case)
    before = all_state(case)
    redis_before = tuple(case.redis.calls)
    result = select(case, remote_email="fresh.remote@example.test")
    assert type(result) is InvitationRecoveryLoginScope
    assert result.attempt == case.attempt and result.attempt.expected_generation == 0
    assert result.snapshot == case.pending.snapshot
    assert result.phase == phase
    assert result.scope.identities[0].sync_generation == (1 if phase in ("memberships_written", "finalized") else 0)
    assert result.lifecycle[0].epoch == (2 if absent and phase != "pending" else 1)
    assert "fresh.remote@example.test" in result.scope.lease_scope.emails
    assert {str(member.workspace_id) for member in result.scope.lease_scope.members} == {
        case.ids[name] for name in ("workspace", "default", "mapped")
    }
    assert all_state(case) == before and tuple(case.redis.calls) == redis_before
    assert not hasattr(result, "__dict__") and TOKEN not in repr(result) and DIGEST not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.phase = "finalized"


@pytest.mark.parametrize("phase", ["pending", "invitation_completed", "finalized"])
def test_missing_quota_is_observed_without_initialization_or_replay(invited_scope_case, writer_case, phase):
    case = (
        writer_case()
        if phase == "finalized"
        else initialized(invited_scope_case(stage="pending" if phase == "pending" else "completed"))
    )
    if phase == "finalized":
        prepare(case)
    with case.db() as session, session.begin():
        session.execute(sa.delete(AccountMoneyExtend).where(AccountMoneyExtend.account_id == case.ids["account"]))
    before = all_state(case)
    result = select(case)
    assert result.phase == phase and result.quota == ()
    assert all_state(case) == before


def test_digest_no_match_or_original_operation_missing_grants_nothing(invited_scope_case):
    case = invited_scope_case(stage="issued")
    assert select(case) is None
    assert select(case, digest="0" * 64) is None
    assert not case.redis.calls


@pytest.mark.parametrize("digest", [TOKEN, "A" * 64, "a" * 63, "a" * 65, None, 4, True])
def test_invalid_digest_rejected_before_sql(invited_scope_case, monkeypatch, digest):
    case = invited_scope_case(stage="issued")
    with case.db() as session, session.begin():
        monkeypatch.setattr(session, "execute", lambda *_args, **_kwargs: pytest.fail("invalid digest reached SQL"))
        with pytest.raises(InvitationAuthorityValidationError, match="^invalid_invitation_authority_facts$"):
            InvitationAuthorityRepository().get_issuance_by_token_digest(session, token_digest=digest)


@pytest.mark.parametrize(
    "fault",
    [
        "subject",
        "namespace",
        "revision",
        "digest",
        "fence",
        "account",
        "identity",
        "epoch",
        "withdrawn",
        "join-role",
        "extra-intent",
        "inactive",
        "uninitialized",
        "tenant",
    ],
)
def test_cross_subject_scope_lifecycle_and_extra_required_intent_reject(writer_case, fault):
    case = writer_case(absent=True)
    prepare(case)
    context, key = case.context, case.key
    if fault == "subject":
        context = replace(context, subject="DifferentSubject")
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
    elif fault == "namespace":
        context = replace(context, namespace_id=uuid4())
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
    elif fault == "revision":
        revision = uuid4()
        context = replace(context, revision_id=revision, active_revision_id=revision)
    elif fault == "digest":
        context = replace(context, config_digest="0" * 64)
    elif fault == "fence":
        context = replace(context, fence_epoch=context.fence_epoch + 1)
    else:
        with case.db() as session, session.begin():
            if fault == "account":
                session.get(Issuance, case.ids["issuance"]).account_id = str(uuid4())
            elif fault == "identity":
                session.execute(
                    sa.update(Identity)
                    .where(Identity.account_id == case.ids["account"])
                    .values(subject="DifferentSubject")
                )
            elif fault in ("epoch", "withdrawn"):
                lifecycle = session.get(Lifecycle, case.ids["lifecycle"])
                if fault == "epoch":
                    lifecycle.epoch += 1
                else:
                    lifecycle.state = "withdrawn"
            elif fault == "join-role":
                session.scalar(sa.select(Join).where(Join.tenant_id == case.ids["workspace"])).role = "normal"
            elif fault == "extra-intent":
                original = session.get(Intent, str(case.pending.snapshot.operation_id))
                values = {column.name: getattr(original, column.name) for column in Intent.__table__.columns}
                values.update(id=str(uuid4()), idempotency_key=sha256(b"extra-required").hexdigest())
                session.add(Intent(**values))
            elif fault == "inactive":
                session.get(Account, case.ids["account"]).status = AccountStatus.BANNED
            elif fault == "uninitialized":
                session.get(Account, case.ids["account"]).initialized_at = None
            elif fault == "tenant":
                from models.account import Tenant

                session.get(Tenant, case.ids["workspace"]).status = TenantStatus.ARCHIVE
    before = all_state(case)
    with pytest.raises(CasdoorLoginScopeConflict, match="^authorization_pending$"):
        select(case, context=context, key=key)
    assert all_state(case) == before


@pytest.mark.parametrize(
    "fault",
    [
        "noncanonical",
        "duplicate",
        "oversize",
        "generation",
        "receipt",
        "scope-hash",
        "proof",
        "too-old",
        "future",
        "f-receipt",
        "d-receipt",
        "history-role",
    ],
)
def test_corrupt_original_operation_or_receipt_never_selects(writer_case, fault):
    case = writer_case(absent=True)
    prepare(case)
    with case.db() as session, session.begin():
        operation = session.get(Intent, str(case.pending.snapshot.operation_id))
        if fault == "noncanonical":
            operation.desired_json = json.dumps(json.loads(operation.desired_json), indent=2)
        elif fault == "duplicate":
            operation.desired_json = '{"schema_version":1,' + operation.desired_json[1:]
        elif fault == "oversize":
            operation.desired_json = "x" * 16385
        elif fault in ("generation", "receipt"):
            data = json.loads(operation.desired_json)
            data["generation" if fault == "generation" else "expected_receipt_json"] = (
                True if fault == "generation" else "{}"
            )
            operation.desired_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
            operation.scope_digest = sha256(operation.desired_json.encode()).hexdigest()
        elif fault == "scope-hash":
            operation.scope_digest = "0" * 64
        elif fault == "proof":
            operation.proof_ref = operation.proof_ref[:-1] + ("0" if operation.proof_ref[-1] != "0" else "1")
        elif fault in ("too-old", "future"):
            operation.created_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
                days=-8 if fault == "too-old" else 1
            )
        elif fault in ("f-receipt", "d-receipt"):
            session.scalar(
                sa.select(Audit).where(Audit.action == (F_ACTION if fault == "f-receipt" else D_ACTION))
            ).summary_json = "{}"
        elif fault == "history-role":
            session.scalar(sa.select(Join).where(Join.tenant_id == case.ids["default"])).role = "admin"
    before = all_state(case)
    with pytest.raises(CasdoorLoginScopeConflict, match="^authorization_pending$"):
        select(case)
    assert all_state(case) == before


def test_header_bounds_payload_before_text_materialization(invited_scope_case):
    case = invited_scope_case(stage="issued")
    with case.db() as session, session.begin():
        session.get(Issuance, case.ids["issuance"]).payload_json = "x" * 8193
    statements = []

    def observed(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", observed)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            select(case)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", observed)
    assert len(statements) == 2  # Explicit BEGIN, then only bounded byte header.
    assert "length(CAST(" in statements[-1]
    assert "payload_json," not in statements[-1]


def test_selector_candidate_cannot_reconstruct_existing_finalization_capability(writer_case):
    from repositories.casdoor_invited_login_guard_repository_extend import CasdoorInvitedLoginGuardRepository

    case = writer_case()
    prepare(case)
    result = select(case)
    assert result.phase == "finalized" and result.scope.histories
    with case.db() as session, session.begin(), pytest.raises(CasdoorLoginScopeConflict):
        CasdoorInvitedLoginGuardRepository(session, configuration_factory).prelock(
            result, roles=case.roles, leases=case.leases, deadline=case.deadline
        )


def test_read_path_contains_no_email_or_newest_operation_selector(invited_scope_case):
    case = initialized(invited_scope_case(stage="pending"))
    statements = []

    def observed(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", observed)
    try:
        assert select(case).phase == "pending"
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", observed)
    assert any("WHERE invitation_authority_issuance_extend.token_digest =" in item for item in statements)
    assert any(f"WHERE {Intent.__tablename__}.idempotency_key =" in item for item in statements)
    assert not any("accounts.email =" in item or "DESC" in item or "FOR UPDATE" in item for item in statements)


def test_exact_original_payload_bytes_are_not_reserialized(invited_scope_case):
    case = invited_scope_case(stage="issued")
    with case.db() as session, session.begin():
        row = session.get(Issuance, case.ids["issuance"])
        original = json.dumps(json.loads(row.payload_json), indent=2, ensure_ascii=False)
        row.payload_json = original
        row.payload_digest = sha256(original.encode()).hexdigest()
    with case.db() as session, session.begin():
        result = InvitationAuthorityRepository().get_issuance_by_token_digest(session, token_digest=DIGEST)
    assert result.payload_json == original and result.payload_digest == sha256(original.encode()).hexdigest()


def test_seven_day_horizon_rejects_even_matching_canonical_pending_snapshot(invited_scope_case):
    from repositories.casdoor_invitation_operation_repository_extend import _deadline

    case = initialized(invited_scope_case(stage="pending"))
    with case.db() as session, session.begin():
        operation = session.get(Intent, str(case.pending.snapshot.operation_id))
        operation.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
        operation.updated_at = operation.created_at
        data = json.loads(operation.desired_json)
        data["reconcile_until_utc"] = _deadline(operation.created_at)
        operation.desired_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
        operation.scope_digest = sha256(operation.desired_json.encode()).hexdigest()
    with pytest.raises(CasdoorLoginScopeConflict):
        select(case)


def test_duplicate_digest_header_rejects_before_payload_contents(invited_scope_case, monkeypatch):
    case = invited_scope_case(stage="issued")
    with case.db() as session, session.begin():
        execute = session.execute
        calls = []

        def duplicate_header(statement, *args, **kwargs):
            calls.append(statement)
            header = tuple(execute(statement, *args, **kwargs))
            return (*header, *header)

        monkeypatch.setattr(session, "execute", duplicate_header)
        with pytest.raises(InvitationAuthorityValidationError):
            InvitationAuthorityRepository().get_issuance_by_token_digest(session, token_digest=DIGEST)
        assert len(calls) == 1 and "length" in str(calls[0])


@pytest.mark.parametrize("mode", ["rbac", "enterprise"])
def test_unsupported_policy_rejected_before_any_selector_sql(writer_case, config_overrides, monkeypatch, mode):
    from enums import DeploymentEdition

    case = writer_case()
    prepare(case)
    config_overrides(
        **({"RBAC_ENABLED": True} if mode == "rbac" else {"DEPLOYMENT_EDITION": DeploymentEdition.ENTERPRISE})
    )
    with case.db() as session, session.begin():
        monkeypatch.setattr(session, "execute", lambda *_args, **_kwargs: pytest.fail("unsupported mode reached SQL"))
        with pytest.raises(CasdoorLoginScopeConflict):
            CasdoorInvitedLoginScopeRepository(session, configuration_factory)._discover_invitation_recovery(
                case.context, case.key, token_digest=DIGEST
            )


def test_unclean_root_does_not_autoflush_a_candidate(invited_scope_case, monkeypatch):
    case = initialized(invited_scope_case(stage="pending"))
    with case.db() as session, session.begin():
        account = session.get(Account, case.ids["account"])
        account.name = "Uncommitted change"
        monkeypatch.setattr(session, "execute", lambda *_args, **_kwargs: pytest.fail("dirty discovery reached SQL"))
        with pytest.raises(CasdoorLoginScopeConflict):
            CasdoorInvitedLoginScopeRepository(session, configuration_factory)._discover_invitation_recovery(
                case.context, case.key, token_digest=DIGEST
            )
        session.rollback()


@pytest.mark.parametrize("role", ["owner", "unsupported"])
def test_pending_creation_must_still_meet_original_role_policy(invited_scope_case, role):
    case = initialized(invited_scope_case(stage="pending", absent=True))
    with case.db() as session, session.begin():
        issuance = session.get(Issuance, case.ids["issuance"])
        payload = json.loads(issuance.payload_json)
        payload["role"] = role
        issuance.role = role
        issuance.payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        issuance.payload_digest = sha256(issuance.payload_json.encode()).hexdigest()
        operation = session.get(Intent, str(case.pending.snapshot.operation_id))
        data = json.loads(operation.desired_json)
        receipt = json.loads(data["expected_receipt_json"])
        data["payload_digest"] = receipt["payload_digest"] = issuance.payload_digest
        data["expected_receipt_json"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        operation.desired_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
        operation.scope_digest = sha256(operation.desired_json.encode()).hexdigest()
    with pytest.raises(CasdoorLoginScopeConflict):
        select(case)
