"""Actual P2/identity/intent SQLite rows; no Redis service or concurrent DB claim."""

import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from core.casdoor.admission import (
    AdmissionAction,
    AdmissionContext,
    AdmissionPlan,
    InvitationObservation,
    SharedOwnerRequirement,
)
from core.casdoor.auth_transactions import AuthMode
from models.account import Account, AccountStatus, TenantAccountJoin
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorNamespaceLifecycle,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_invitation_operation_repository_extend import (
    CasdoorInvitationOperationRepository,
    InvitationOperationAttempt,
    InvitationOperationConflict,
    operation_idempotency_key,
)
from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository
from services import account_adapters
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.casdoor_invitation_operation_service_extend import CasdoorInvitationOperationService
from services.casdoor_invited_login_account_service_extend import CasdoorInvitedLoginAccountService
from services.entities.account_activation_entities import AccountSetup, InvitationLookup
from tests.unit_tests.services.test_account_invited_initialization import seed
from tests.unit_tests.services.test_invitation_token_consumption_extend import TOKEN, FakeRedis


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def operation_case(sqlite_session_factory):
    sessions = []
    engine = sqlite_session_factory.kw["bind"]

    @sa.event.listens_for(engine, "connect")
    def sqlite_transaction_mode(connection, _record):
        # A physical root is required around the unchanged identity savepoint.
        # This is per-test SQLite setup, never production engine configuration.
        connection.isolation_level = None

    @sa.event.listens_for(engine, "begin")
    def explicit_sqlite_root(connection):
        connection.exec_driver_sql("BEGIN")

    def factory():
        session = sqlite_session_factory()
        sessions.append(session)
        return session

    with sqlite_session_factory() as session:
        account, workspace, quota, join = seed(session)
        integration = Integration(enabled=True)
        session.add(integration)
        session.flush()
        chain = dict(
            expected_issuer="https://issuer.example.invalid",
            organization="OfflineOrg",
            application="OfflineApp",
            client_id="OfflineClient",
        )
        namespace = Namespace(integration_id=integration.id, core_fingerprint="a" * 64, **chain)
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="b" * 64,
            browser_frontend_url=chain["expected_issuer"],
            backend_api_url=chain["expected_issuer"],
            button_text="SSO",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
            **chain,
        )
        session.add(revision)
        session.flush()
        integration.active_revision_id = revision.id
        repo = InvitationAuthorityRepository()
        lifecycle = repo.set_lifecycle_state(session, account_id=account.id, workspace_id=workspace.id, state="active")
        data = dict(
            account_id=account.id,
            email=account.email,
            workspace_id=workspace.id,
            role="admin",
            requires_setup=False,
            invitation_authority=dict(
                schema_version=1,
                issuance_id=str(uuid4()),
                lifecycle_id=lifecycle.lifecycle_id,
                lifecycle_epoch=lifecycle.epoch,
                token_digest=sha256(TOKEN.encode()).hexdigest(),
                join_id_at_issue=join.id,
            ),
        )
        record = repo.record_issuance(session, payload_json=canonical(data))
        session.commit()
        context = AdmissionContext(
            UUID(integration.id),
            UUID(revision.id),
            UUID(revision.id),
            UUID(namespace.id),
            CasdoorNamespaceLifecycle.ACTIVE,
            0,
            revision.config_digest,
            namespace.expected_issuer,
            namespace.organization,
            namespace.application,
            namespace.client_id,
            "ExactSubject",
        )
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        attempt = InvitationOperationAttempt(
            context, key, UUID(account.id), UUID(workspace.id), UUID(record.issuance_id), 0
        )
        ids = dict(
            account=account.id,
            workspace=workspace.id,
            quota=quota.id,
            join=join.id,
            lifecycle=lifecycle.lifecycle_id,
            integration=integration.id,
            namespace=namespace.id,
            revision=revision.id,
            issuance=record.issuance_id,
        )
        protected = (join.role, join.current, join.last_opened_at, quota.total_quota, quota.used_quota)
    redis = FakeRedis("operation")
    redis.entries[redis.token_key] = ("string", record.payload_json.encode(), 60000)
    original_eval = redis.eval

    def observe_only(script, *args):
        assert not any(session.in_transaction() for session in sessions), "Redis observation under SQL root"
        assert script == account_adapters._INVITATION_OBSERVE, "consume/readback forbidden"
        return original_eval(script, *args)

    redis.eval = observe_only
    store = RedisInvitationTokenStore(redis=redis)
    effects = [Mock() for _ in range(4)]
    effects[1].get_freeze_type.return_value = None
    activation = AccountActivationService(
        tokens=store,
        accounts=SQLAlchemyAccountActivationRepository(factory),
        workspace_policy=effects[0],
        eligibility=effects[1],
        membership_cache=effects[2],
        member_access_sync=effects[3],
    )
    owner = CasdoorInvitedLoginAccountService(activation=activation)
    service = CasdoorInvitationOperationService(session_factory=factory, store=store, invited_login=owner)
    return SimpleNamespace(
        factory=factory,
        sessions=sessions,
        db=sqlite_session_factory,
        ids=ids,
        protected=protected,
        context=context,
        key=key,
        attempt=attempt,
        redis=redis,
        store=store,
        activation=activation,
        owner=owner,
        service=service,
        effects=effects,
        payload=record.payload_json,
    )


def prepare(case):
    with case.factory() as session, session.begin():
        preflight = CasdoorAccountPreflightRepository(session).reconstruct(
            case.context, case.key, candidate_account_id=case.attempt.account_id
        )
    setup = AccountSetup("Initialized", "en-US", "UTC") if preflight.account.initialized_at is None else None
    shared = case.activation.prepare_invited_initialization(
        InvitationLookup(None, None, TOKEN), setup=setup, authenticated_account_id=case.ids["account"]
    )
    owners = (SharedOwnerRequirement.INVITATION_ACTIVATION_PREPARE,)
    bound = preflight.exact_binding is not None
    if setup:
        action = AdmissionAction.INITIALIZE_BOUND if bound else AdmissionAction.INITIALIZE_INVITED
        owners += (SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,)
    elif preflight.account.status != AccountStatus.ACTIVE:
        action = AdmissionAction.ACTIVATE_BOUND if bound else AdmissionAction.ACTIVATE_INVITED
        owners += (SharedOwnerRequirement.ACCOUNT_STATUS_ACTIVATION_PERSIST,)
    else:
        action = AdmissionAction.USE_BOUND if bound else AdmissionAction.USE_INVITED
    plan = AdmissionPlan(
        case.context,
        AuthMode.LOGIN,
        action,
        account_id=case.attempt.account_id,
        setup=setup,
        required_shared_owners=owners,
    )
    invitation = InvitationObservation(
        case.context.namespace_id,
        case.context.subject,
        case.attempt.account_id,
        preflight.account.email,
        case.attempt.workspace_id,
    )
    return case.owner._prepare_invited_login(
        plan=plan, preflight=preflight, invitation=invitation, shared=shared, deadline=monotonic() + 40
    )


def observe(case):
    result = case.store.observe_versioned_invitation(TOKEN)
    assert result.status == "observed"
    return result.observation


def stage(case, session, prepared, observation, **kwargs):
    repository = CasdoorInvitationOperationRepository(session)
    existing, facts = repository.inspect(case.attempt)
    assert existing is None
    case.owner.persist_invited_login_account(prepared, session=session, context=case.context, key=case.key)
    return repository.create_pending(
        case.attempt,
        store=case.store,
        observation=observation,
        observation_deadline=kwargs.get("deadline", monotonic() + 20),
        expected_facts=facts,
    )


def create(case):
    return case.service.produce(case.attempt, token=TOKEN, prepared=prepare(case))


def protected(case, session):
    from models.account_money_extend import AccountMoneyExtend

    join = session.get(TenantAccountJoin, case.ids["join"])
    quota = session.get(AccountMoneyExtend, case.ids["quota"])
    return join.role, join.current, join.last_opened_at, quota.total_quota, quota.used_quota


def test_repository_stages_exact_pending_snapshot_without_commit(operation_case):
    case = operation_case
    prepared, observation = prepare(case), observe(case)
    with case.factory() as session, session.begin():
        snapshot = stage(case, session, prepared, observation)
        data = json.loads(snapshot.desired_json)
        assert snapshot.scope_digest == sha256(snapshot.desired_json.encode()).hexdigest()
        assert snapshot.idempotency_key == operation_idempotency_key(case.attempt.issuance_id)
        assert snapshot.operation_id.version == 4 and data["operation_id"] == str(snapshot.operation_id)
        assert (
            data["expected_receipt_json"].encode()
            == case.store._consumption_arguments(observation, str(snapshot.operation_id))[1]
        )
        assert data["observed_join_role"] == "editor"  # The invitation desired role is admin.
        with case.db() as reader:
            assert reader.get(Intent, str(snapshot.operation_id)) is None
            assert reader.get(Account, case.ids["account"]).initialized_at is None
    with case.db() as reader, reader.begin():
        reread, facts = CasdoorInvitationOperationRepository(reader).inspect(case.attempt)
        assert reread == snapshot and facts.payload_json == case.payload
        row = reader.get(Intent, str(snapshot.operation_id))
        assert row.membership_id is None and row.ownership_epoch == 0
        assert row.operation_state.value == "pending" and row.termination_state.value == "not_started"
        assert reader.get(Issuance, case.ids["issuance"]).state == "issued"
        assert protected(case, reader) == case.protected
    assert len(case.redis.calls) == 1 and len(case.redis.entries) == 1
    assert TOKEN not in snapshot.desired_json and "member_invite:" not in snapshot.desired_json


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unknown",
        "missing",
        "malformed",
        "noncanonical",
        "digest",
        "receipt_duplicate",
        "receipt_unknown",
        "deadline_extend",
        "deadline_shorten",
        "deadline_expired",
    ],
)
def test_operation_json_and_deadline_corruption_fail_closed(operation_case, mutation):
    case = operation_case
    handle = create(case)
    with case.db() as session:
        row = session.get(Intent, str(handle.snapshot.operation_id))
        data = json.loads(row.desired_json)
        if mutation == "duplicate":
            raw = '{"schema_version":1,' + row.desired_json[1:]
        elif mutation == "unknown":
            raw = canonical({**data, "unknown": 1})
        elif mutation == "missing":
            del data["operation_id"]
            raw = canonical(data)
        elif mutation == "malformed":
            raw = "["
        elif mutation == "noncanonical":
            raw = json.dumps(data)
        elif mutation.startswith("receipt_"):
            receipt = data["expected_receipt_json"]
            data["expected_receipt_json"] = (
                '{"status":"consumed",' + receipt[1:]
                if mutation.endswith("duplicate")
                else canonical({**json.loads(receipt), "unknown": 1})
            )
            raw = canonical(data)
        elif mutation in ("deadline_extend", "deadline_shorten"):
            data["reconcile_until_utc"] = (
                "2099-01-01T00:00:00.000000Z" if mutation.endswith("extend") else "2000-01-01T00:00:00.000000Z"
            )
            raw = canonical(data)
        else:
            raw = row.desired_json
        row.desired_json = raw
        row.scope_digest = "0" * 64 if mutation == "digest" else sha256(raw.encode()).hexdigest()
        if mutation == "deadline_expired":
            row.created_at -= timedelta(days=8)
            from repositories.casdoor_invitation_operation_repository_extend import _deadline

            data["reconcile_until_utc"] = _deadline(row.created_at)
            row.desired_json = canonical(data)
            row.scope_digest = sha256(row.desired_json.encode()).hexdigest()
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)
    assert len(case.redis.calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "integration_id",
        "namespace_id",
        "revision_id",
        "config_digest",
        "identity_id",
        "subject_digest",
        "account_id",
        "workspace_id",
        "generation",
        "fence_epoch",
        "issuance_id",
        "payload_digest",
        "lifecycle_id",
        "lifecycle_epoch",
        "join_id_at_issue",
        "observed_join_role",
        "operation_id",
    ],
)
def test_changed_snapshot_fact_rejected_even_with_new_scope_digest(operation_case, field):
    case = operation_case
    handle = create(case)
    with case.db() as session:
        row = session.get(Intent, str(handle.snapshot.operation_id))
        data = json.loads(row.desired_json)
        data[field] = "changed"
        row.desired_json = canonical(data)
        row.scope_digest = sha256(row.desired_json.encode()).hexdigest()
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)
    assert len(case.redis.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("membership_id", str(uuid4())),
        ("ownership_epoch", 1),
        ("attempt_count", 1),
        ("operation_state", "unknown"),
        ("termination_state", "unconfirmed"),
        ("proof_ref", "not-proof"),
    ],
)
def test_row_cannot_claim_membership_or_progress(operation_case, field, value):
    case = operation_case
    handle = create(case)
    with case.db() as session:
        session.execute(
            sa.update(Intent).where(Intent.id == str(handle.snapshot.operation_id)).values(**{field: value})
        )
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)


def test_one_issuance_idempotency_key_excludes_subject_namespace_changes(operation_case):
    case = operation_case
    handle = create(case)
    changed = replace(
        case.attempt,
        context=replace(case.context, subject="AnotherSubject"),
        key=replace(case.key, subject="AnotherSubject"),
    )
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(changed)
    assert (
        handle.snapshot.idempotency_key
        == sha256(b"casdoor:invitation-finalize:operation:v1:" + str(case.attempt.issuance_id).encode()).hexdigest()
    )
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 1


def test_parent_and_authority_lock_queries_request_nowait(operation_case, monkeypatch):
    from sqlalchemy.dialects import mysql, postgresql
    from sqlalchemy.orm import Session

    case = operation_case
    statements = []
    original = Session._execute_internal

    def capture(self, statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            statements.append(statement)
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "_execute_internal", capture)
    with case.factory() as session, session.begin():
        CasdoorInvitationOperationRepository(session).inspect(case.attempt)
    assert statements and all(statement._for_update_arg.nowait for statement in statements)
    for dialect in (mysql.dialect(), postgresql.dialect()):
        assert all("NOWAIT" in str(statement.compile(dialect=dialect)) for statement in statements)


@pytest.mark.parametrize("deadline", [float("inf"), float("nan"), -1.0, True])
def test_repository_rejects_unbounded_or_expired_observation_and_root_rolls_back(operation_case, deadline):
    case = operation_case
    prepared, observation = prepare(case), observe(case)
    with pytest.raises(InvitationOperationConflict), case.factory() as session, session.begin():
        stage(case, session, prepared, observation, deadline=deadline)
    with case.db() as reader:
        assert reader.get(Account, case.ids["account"]).initialized_at is None
        assert reader.scalar(sa.select(sa.func.count()).select_from(Intent)) == 0


def test_repository_rejects_nonroot_and_dirty_caller_state(operation_case):
    case = operation_case
    with case.factory() as session:
        with pytest.raises(InvitationOperationConflict):
            CasdoorInvitationOperationRepository(session).inspect(case.attempt)
        with session.begin():
            row = session.get(Account, case.ids["account"])
            row.name = "dirty caller"
            with pytest.raises(InvitationOperationConflict):
                CasdoorInvitationOperationRepository(session).inspect(case.attempt)
            session.rollback()
        with session.begin(), session.begin_nested(), pytest.raises(InvitationOperationConflict):
            CasdoorInvitationOperationRepository(session).inspect(case.attempt)


def test_positive_active_revision_chain_has_exact_seven_fields(operation_case):
    case = operation_case
    handle = create(case)
    with case.db() as session:
        integration = session.get(Integration, case.ids["integration"])
        namespace = session.get(Namespace, case.ids["namespace"])
        revision = session.get(Revision, case.ids["revision"])
        actual = (
            revision.integration_id,
            revision.expected_issuer,
            revision.organization,
            revision.application,
            revision.client_id,
            revision.namespace_id,
            revision.config_digest,
        )
        expected = (
            str(case.context.integration_id),
            case.context.issuer,
            case.context.organization,
            case.context.application,
            case.context.client_id,
            str(case.context.namespace_id),
            case.context.config_digest,
        )
        assert len(actual) == len(expected) == 7 and actual == expected
        assert integration.enabled and integration.active_revision_id == revision.id
        assert namespace.integration_id == integration.id and namespace.id == revision.namespace_id
        data = json.loads(handle.snapshot.desired_json)
        assert (data["integration_id"], data["namespace_id"], data["revision_id"], data["config_digest"]) == (
            integration.id,
            namespace.id,
            revision.id,
            revision.config_digest,
        )


@pytest.mark.parametrize(
    "field",
    ["integration_id", "expected_issuer", "organization", "application", "client_id", "namespace_id", "config_digest"],
)
def test_each_active_revision_binding_is_revalidated(operation_case, field):
    case = operation_case
    create(case)
    with case.db() as session:
        value = str(uuid4()) if field.endswith("_id") else "0" * 64 if field == "config_digest" else "changed"
        session.execute(sa.update(Revision).where(Revision.id == case.ids["revision"]).values(**{field: value}))
        session.commit()
    with pytest.raises(InvitationOperationConflict):
        case.service.produce(case.attempt)
    assert len(case.redis.calls) == 1
