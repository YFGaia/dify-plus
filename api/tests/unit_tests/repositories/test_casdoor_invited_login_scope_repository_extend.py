"""Real configuration, P3K/P3L and scope owners; sequential offline SQLite only."""

import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionContext
from core.casdoor.configuration import (
    CasdoorConfiguration,
    RoleRef,
    WorkspaceRoleMapping,
)
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.leases import WorkspaceMemberScope
from enums import DeploymentEdition
from models.account import Account, Tenant, TenantStatus
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from repositories.account_activation_repository import (
    SQLAlchemyAccountActivationRepository,
)
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationRepository,
)
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_invitation_finalization_repository_extend import (
    CasdoorInvitationFinalizationRepository,
)
from repositories.casdoor_invitation_operation_repository_extend import (
    InvitationOperationAttempt,
)
from repositories.casdoor_invited_login_scope_repository_extend import (
    CasdoorInvitedLoginScopeRepository,
    CompletedInvitationLoginScope,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
)
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
)
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.account_service import TenantService
from services.casdoor_invitation_finalization_service_extend import (
    CasdoorInvitationFinalizationService,
)
from services.casdoor_invitation_operation_service_extend import (
    CasdoorInvitationOperationService,
)
from services.casdoor_invited_login_account_service_extend import (
    CasdoorInvitedLoginAccountService,
)

from tests.unit_tests.repositories.test_casdoor_invitation_operation_repository_extend import (
    canonical,
    create,
)
from tests.unit_tests.services.test_account_invited_initialization import seed
from tests.unit_tests.services.test_invitation_token_consumption_extend import (
    TOKEN,
    FakeRedis,
)


def configuration_factory(session):
    return CasdoorConfigurationRepository(
        session, crypto=CasdoorCrypto(secret_key="synthetic-scope-key", key_version="1"), rbac_enabled=False
    )


@pytest.fixture
def invited_scope_case(sqlite_session_factory, config_overrides):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, RBAC_ENABLED=False, DATASET_OPERATOR_ENABLED=True)
    engine = sqlite_session_factory.kw["bind"]

    @sa.event.listens_for(engine, "connect")
    def sqlite_root(connection, _record):
        connection.isolation_level = None

    @sa.event.listens_for(engine, "begin")
    def explicit_root(connection):
        connection.exec_driver_sql("BEGIN")

    def build(*, absent=False, stage="completed"):
        sessions = []

        def factory():
            session = sqlite_session_factory()
            sessions.append(session)
            return session

        with sqlite_session_factory() as session:
            account, workspace, quota, join = seed(session)
            default, mapped = Tenant(name="Configured default"), Tenant(name="Configured mapping")
            session.add_all([default, mapped])
            session.flush()
            configuration = CasdoorConfiguration(
                browser_frontend_url="https://issuer.example.invalid",
                backend_api_url="https://issuer.example.invalid",
                expected_issuer="https://issuer.example.invalid",
                organization="OfflineOrg",
                application="OfflineApp",
                client_id="OfflineClient",
                default_workspace_id=UUID(default.id),
                workspace_mappings=(
                    WorkspaceRoleMapping(
                        workspace_id=UUID(mapped.id), admin=RoleRef(organization="OfflineOrg", name="operators")
                    ),
                ),
            )
            owner = configuration_factory(session)
            saved = owner.save_draft(configuration, etag=0, actor_account_id=UUID(account.id))
            revision = session.get(Revision, str(saved.draft_revision_id))
            integration = session.get(Integration, revision.integration_id)
            namespace = session.get(Namespace, revision.namespace_id)
            # Offline fixture state only: no activation/deployment acceptance.
            integration.enabled, integration.active_revision_id = True, revision.id
            if absent:
                session.delete(join)
                session.flush()
            authority = InvitationAuthorityRepository()
            lifecycle = authority.set_lifecycle_state(
                session, account_id=account.id, workspace_id=workspace.id, state="active"
            )
            payload = canonical(
                dict(
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
                        join_id_at_issue=None if absent else join.id,
                    ),
                )
            )
            issuance = authority.record_issuance(session, payload_json=payload)
            session.flush()
            # Validate true core/policy/digest before any immutable operation snapshot.
            owner._revision(integration.id, revision.id)
            session.commit()
            context = AdmissionContext(
                UUID(integration.id),
                UUID(revision.id),
                UUID(revision.id),
                UUID(namespace.id),
                namespace.lifecycle,
                namespace.fence_epoch,
                revision.config_digest,
                namespace.expected_issuer,
                namespace.organization,
                namespace.application,
                namespace.client_id,
                "ExactSubject",
            )
            key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
            attempt = InvitationOperationAttempt(
                context, key, UUID(account.id), UUID(workspace.id), UUID(issuance.issuance_id), 0
            )
            ids = dict(
                account=account.id,
                workspace=workspace.id,
                default=default.id,
                mapped=mapped.id,
                join=None if absent else join.id,
                quota=quota.id,
                lifecycle=lifecycle.lifecycle_id,
                issuance=issuance.issuance_id,
                revision=revision.id,
                integration=integration.id,
                namespace=namespace.id,
            )
        redis = FakeRedis("invited-scope")
        redis.entries[redis.token_key] = ("string", payload.encode(), 60000)
        original_eval = redis.eval

        def guarded_eval(script, *args):
            assert not any(s.in_transaction() for s in sessions)
            return original_eval(script, *args)

        redis.eval = guarded_eval
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
        invited_owner = CasdoorInvitedLoginAccountService(activation=activation)
        case = SimpleNamespace(
            db=sqlite_session_factory,
            engine=engine,
            factory=factory,
            sessions=sessions,
            ids=ids,
            context=context,
            key=key,
            attempt=attempt,
            redis=redis,
            store=store,
            activation=activation,
            owner=invited_owner,
            effects=effects,
            payload=payload,
            configuration=configuration,
        )
        case.service = CasdoorInvitationOperationService(
            session_factory=factory, store=store, invited_login=invited_owner
        )
        if stage != "issued":
            case.pending = create(case)
        if stage == "completed":
            case.finalizer = CasdoorInvitationFinalizationService(session_factory=factory, store=store)
            case.completed = case.finalizer.finalize(attempt, token=TOKEN)
            assert case.completed.facts.snapshot == case.pending.snapshot
        redis.calls.clear()
        sessions.clear()
        return case

    return build


def discover(case, attempt=None):
    with case.db() as session, session.begin():
        return CasdoorInvitedLoginScopeRepository(session, configuration_factory).discover_completed_invitation(
            case.attempt if attempt is None else attempt
        )


@pytest.mark.parametrize("absent", [False, True], ids=["existing-join", "new-join"])
def test_actual_completed_invitation_projects_full_scope_without_admitting(invited_scope_case, absent):
    case = invited_scope_case(absent=absent)
    result = discover(case)
    assert type(result) is CompletedInvitationLoginScope and result.attempt is case.attempt
    assert result.completion == case.completed.facts and result.completion.completed is True
    scope = result.scope
    assert scope.configuration == case.configuration
    assert scope.account.id == case.ids["account"]
    assert len(scope.identities) == 1 and scope.identities[0].sync_generation == 0
    assert scope.joins[0].role == ("admin" if absent else "editor")
    assert scope.joins[0].id == result.completion.join_id
    assert {row.id for row in scope.workspaces} == {case.ids[x] for x in ("workspace", "default", "mapped")}
    assert WorkspaceMemberScope(case.attempt.workspace_id, case.attempt.account_id) in scope.lease_scope.members
    assert {str(row.workspace_id) for row in scope.availability().workspaces} == {
        case.ids["default"],
        case.ids["mapped"],
    }
    assert len(scope.intents) == 1 and scope.intents[0].id == str(case.pending.snapshot.operation_id)
    assert scope.intents[0].operation_state == "applied" and scope.intents[0].termination_state == "confirmed"
    with case.db() as session, session.begin(), pytest.raises(CasdoorLoginScopeConflict):
        CasdoorLoginScopeRepository(session, configuration_factory)._intent_barrier(case.attempt.account_id, scope)
    assert not case.redis.calls and not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.scope = None


@pytest.mark.parametrize("stage", ["issued", "pending", "receipt-only"], ids=["issued", "pending", "receipt-only"])
def test_incomplete_chain_and_p2_receipt_are_never_scope_authority(invited_scope_case, stage):
    case = invited_scope_case(stage="issued" if stage == "issued" else "pending")
    if stage == "receipt-only":
        with case.db() as session, session.begin():
            facts = CasdoorInvitationFinalizationRepository(session).inspect(case.attempt)
            InvitationAuthorityRepository().record_consumption(
                session, receipt_json=json.loads(facts.snapshot.desired_json)["expected_receipt_json"]
            )
    with pytest.raises(CasdoorLoginScopeConflict):
        discover(case)
    assert not case.redis.calls


@pytest.mark.parametrize(
    "field",
    ["account", "identity", "workspace", "issuance", "revision", "fence", "generation", "role", "epoch", "proof"],
    ids=["account", "identity", "workspace", "issuance", "revision", "fence", "generation", "role", "epoch", "proof"],
)
def test_exact_completed_binding_rejects_current_drift(invited_scope_case, field):
    case = invited_scope_case()
    with case.db() as session, session.begin():
        if field == "account":
            session.get(Account, case.ids["account"]).email = "changed@example.test"
        elif field == "identity":
            session.execute(sa.update(Identity).values(subject="changed"))
        elif field == "workspace":
            session.get(Tenant, case.ids["workspace"]).status = TenantStatus.ARCHIVE
        elif field == "issuance":
            session.get(Issuance, case.ids["issuance"]).payload_digest = "0" * 64
        elif field == "revision":
            session.get(Integration, case.ids["integration"]).active_revision_id = str(uuid4())
        elif field == "fence":
            session.get(Namespace, case.ids["namespace"]).fence_epoch += 1
        elif field == "generation":
            session.scalar(sa.select(Identity)).sync_generation += 1
        elif field == "role":
            session.get(Join, case.completed.facts.join_id).role = "normal"
        elif field == "epoch":
            session.get(Lifecycle, case.ids["lifecycle"]).epoch += 1
        else:
            session.get(Intent, str(case.pending.snapshot.operation_id)).proof_ref = "invalid"
    with pytest.raises(CasdoorLoginScopeConflict):
        discover(case)
    assert not case.redis.calls


@pytest.mark.parametrize(
    "field",
    ["account_id", "workspace_id", "issuance_id", "expected_generation", "context", "key"],
    ids=["account", "workspace", "issuance", "generation", "context", "key"],
)
def test_attempt_selectors_cannot_rebind_completion(invited_scope_case, field):
    case = invited_scope_case()
    value = uuid4()
    if field == "expected_generation":
        value = 1
    elif field == "context":
        value = replace(case.context, config_digest="0" * 64)
    elif field == "key":
        value = VerifiedIdentityKey(case.key.namespace_id, case.key.issuer, case.key.organization, "different")
    with pytest.raises(CasdoorLoginScopeConflict):
        discover(case, replace(case.attempt, **{field: value}))


@pytest.mark.parametrize(
    "kind", ["facts", "committed", "proof", "token", "dict"], ids=["facts", "committed", "proof", "token", "dict"]
)
def test_client_dto_or_completion_projection_is_not_input_authority(invited_scope_case, kind):
    case = invited_scope_case()
    value = {
        "facts": case.completed.facts,
        "committed": case.completed,
        "proof": case.completed.facts.proof_ref,
        "token": TOKEN,
        "dict": vars(case),
    }[kind]
    with pytest.raises(CasdoorLoginScopeConflict):
        discover(case, value)


def add_history_scope(case):
    """Related historical scalars can be broad while the active config stays exact."""
    with case.db() as session, session.begin():
        namespace = dict(session.execute(sa.select(*Namespace.__table__.columns)).one()._mapping)
        revision = dict(session.execute(sa.select(*Revision.__table__.columns)).one()._mapping)
        nid, rid, iid, wid, jid, hid = (str(uuid4()) for _ in range(6))
        session.execute(sa.insert(Namespace).values(**dict(namespace, id=nid, lifecycle="archived")))
        session.execute(sa.insert(Revision).values(**dict(revision, id=rid, namespace_id=nid, revision_number=2)))
        parent = Tenant(name="Historical parent")
        parent.id, parent.status = wid, TenantStatus.ARCHIVE
        session.add(parent)
        session.add(
            Identity(
                id=iid,
                namespace_id=nid,
                account_id=case.ids["account"],
                issuer=namespace["expected_issuer"],
                organization=namespace["organization"],
                subject="HistoricalSubject",
                subject_digest=sha256(b"HistoricalSubject").hexdigest(),
                sync_generation=7,
                last_applied_json="{}",
                profile_sync_json="{}",
            )
        )
        join = Join(account_id=case.ids["account"], tenant_id=wid, role="normal")
        join.id = jid
        session.add(join)
        session.add(
            History(
                id=hid,
                namespace_id=nid,
                identity_id=iid,
                account_id=case.ids["account"],
                workspace_id=wid,
                join_id=jid,
                ownership="released",
                ownership_epoch=3,
                source="mapping",
                desired_generation=7,
                revision_id=rid,
                last_applied_fingerprint="0" * 64,
                finalization="finalized",
                tombstone=True,
                last_applied_roles_json="sensitive-text-not-read",
                desired_roles_json="sensitive-text-not-read",
                baseline_json="sensitive-text-not-read",
            )
        )
        intent = Intent(
            namespace_id=nid,
            identity_id=iid,
            account_id=case.ids["account"],
            workspace_id=wid,
            membership_id=hid,
            revision_id=rid,
            generation=7,
            ownership_epoch=3,
            fence_epoch=0,
            kind="member_remove",
            scope_digest="0" * 64,
            idempotency_key=uuid4().hex * 2,
            desired_json="sensitive-text-not-read",
            operation_state="unknown",
            termination_state="manual_recovery",
        )
        session.add(intent)
        session.flush()
        return dict(namespace=nid, revision=rid, identity=iid, workspace=wid, join=jid, history=hid, intent=intent.id)


def test_cross_namespace_histories_and_every_non_avatar_intent_are_preserved(invited_scope_case):
    case = invited_scope_case()
    ids = add_history_scope(case)
    result = discover(case)
    scope = result.scope
    assert {row.id for row in scope.identities} >= {ids["identity"]}
    assert {row.id for row in scope.histories} == {ids["history"]}
    assert {row.id for row in scope.joins} >= {ids["join"]}
    assert {row.id for row in scope.workspaces} >= {ids["workspace"]}
    assert {row.id for row in scope.intents} == {ids["intent"], str(case.pending.snapshot.operation_id)}
    assert next(row for row in scope.intents if row.id == ids["intent"]).operation_state == "unknown"
    assert WorkspaceMemberScope(UUID(ids["workspace"]), case.attempt.account_id) in scope.lease_scope.members
    with case.db() as session, session.begin(), pytest.raises(CasdoorLoginScopeConflict):
        CasdoorLoginScopeRepository(session, configuration_factory)._intent_barrier(case.attempt.account_id, scope)


@pytest.mark.parametrize(
    "fault",
    ["missing-parent", "configured-status", "foreign-history", "foreign-join", "missing-linked", "malformed-id"],
    ids=["missing-parent", "configured-status", "foreign-history", "foreign-join", "missing-linked", "malformed-id"],
)
def test_projection_associations_and_parent_integrity_fail_closed(invited_scope_case, fault):
    case = invited_scope_case()
    ids = add_history_scope(case)
    with case.db() as session, session.begin():
        if fault == "missing-parent":
            session.execute(sa.delete(Tenant).where(Tenant.id == ids["workspace"]))
        elif fault == "configured-status":
            session.get(Tenant, case.ids["default"]).status = TenantStatus.ARCHIVE
        elif fault == "foreign-history":
            session.get(History, ids["history"]).account_id = str(uuid4())
        elif fault == "foreign-join":
            session.get(Join, ids["join"]).account_id = str(uuid4())
        elif fault == "missing-linked":
            session.get(Intent, ids["intent"]).membership_id = str(uuid4())
        else:
            session.get(History, ids["history"]).workspace_id = "not-a-uuid"
    with pytest.raises(CasdoorLoginScopeConflict):
        discover(case)


@pytest.mark.parametrize(
    "state",
    ["no-root", "autobegin", "nested", "new", "dirty", "deleted"],
    ids=["no-root", "autobegin", "nested", "new", "dirty", "deleted"],
)
def test_requires_clean_explicit_caller_root(invited_scope_case, state):
    case = invited_scope_case()
    with case.db() as session:
        owner = CasdoorInvitedLoginScopeRepository(session, configuration_factory)
        if state == "no-root":
            with pytest.raises(CasdoorLoginScopeConflict):
                owner.discover_completed_invitation(case.attempt)
            return
        if state == "autobegin":
            session.execute(sa.select(Account.id))
        else:
            session.begin()
        if state == "nested":
            session.begin_nested()
        elif state == "new":
            session.add(Tenant(name="pending"))
        elif state == "dirty":
            session.get(Account, case.ids["account"]).name = "pending"
        elif state == "deleted":
            session.delete(session.get(Account, case.ids["account"]))
        with pytest.raises(CasdoorLoginScopeConflict):
            owner.discover_completed_invitation(case.attempt)


def test_same_session_completed_reader_then_projection_has_no_dml_or_extra_locks(invited_scope_case, monkeypatch):
    case = invited_scope_case()
    add_history_scope(case)
    calls, projected, statements = [], [], []
    original = CasdoorInvitationFinalizationRepository.inspect

    def inspected(owner, attempt):
        calls.append(owner._session)
        result = original(owner, attempt)
        assert result.completed is True
        projected.append(True)
        return result

    def before_execute(conn, clause, multiparams, params, options):
        statements.append((clause, bool(projected)))

    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", inspected)
    for owner, name in [
        (case.store, "observe_versioned_invitation"),
        (case.store, "consume_versioned_invitation"),
        (TenantService, "persist_tenant_member"),
        (CasdoorCrypto, "decrypt"),
    ]:
        monkeypatch.setattr(owner, name, Mock(side_effect=AssertionError("scope is SQL read only")))
    sa.event.listen(case.engine, "before_execute", before_execute)
    try:
        with case.db() as session, session.begin():
            with monkeypatch.context() as guard:
                for name in ("flush", "commit", "rollback"):
                    guard.setattr(session, name, Mock(side_effect=AssertionError("caller owns SQL lifecycle")))
                result = CasdoorInvitedLoginScopeRepository(
                    session, configuration_factory
                ).discover_completed_invitation(case.attempt)
                assert calls == [session] and result.completion == case.completed.facts
    finally:
        sa.event.remove(case.engine, "before_execute", before_execute)
    assert statements and any(getattr(query, "_for_update_arg", None) is not None for query, _ in statements)
    for query, expanded in statements:
        assert isinstance(query, sa.sql.Select)
        if expanded:
            assert getattr(query, "_for_update_arg", None) is None
        if "casdoor_managed_membership_extend" in str(query):
            assert all(
                name not in str(query) for name in ("last_applied_roles_json", "desired_roles_json", "baseline_json")
            )
    assert not case.redis.calls


@pytest.mark.parametrize(
    "kind", ["namespace", "join", "intent", "workspace-union"], ids=["namespace", "join", "intent", "workspace-union"]
)
def test_cap_plus_one_and_complete_workspace_union_are_bounded(invited_scope_case, kind):
    case = invited_scope_case()
    with case.db() as session, session.begin():
        if kind == "namespace":
            original = dict(session.execute(sa.select(*Namespace.__table__.columns)).one()._mapping)
            session.execute(
                sa.insert(Namespace), [dict(original, id=str(uuid4()), lifecycle="archived") for _ in range(2000)]
            )
        elif kind == "intent":
            identity_id = session.scalar(sa.select(Identity.id))
            session.execute(
                sa.insert(Intent),
                [
                    dict(
                        id=str(uuid4()),
                        namespace_id=case.ids["namespace"],
                        identity_id=identity_id,
                        account_id=case.ids["account"],
                        revision_id=case.ids["revision"],
                        generation=0,
                        ownership_epoch=0,
                        fence_epoch=0,
                        kind="member_remove",
                        scope_digest="0" * 64,
                        idempotency_key=uuid4().hex * 2,
                        desired_json="{}",
                        operation_state="applied",
                        termination_state="confirmed",
                    )
                    for _ in range(2048)
                ],
            )
        else:
            count = 2048 if kind == "join" else 2046
            workspaces = [str(uuid4()) for _ in range(count)]
            session.execute(sa.insert(Tenant), [dict(id=value, name="Cap fixture") for value in workspaces])
            session.execute(
                sa.insert(Join),
                [
                    dict(id=str(uuid4()), account_id=case.ids["account"], tenant_id=value, role="normal")
                    for value in workspaces
                ],
            )
    with pytest.raises(CasdoorLoginScopeConflict):
        discover(case)
    assert not case.redis.calls
