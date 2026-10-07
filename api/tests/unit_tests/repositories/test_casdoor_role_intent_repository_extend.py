"""Actual SQLite local staging only; synthetic saved plans are not remote proof."""

import ast
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.configuration import RoleRef
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.mapping import DesiredWorkspacePlan, DesiredWorkspaceTarget, MappingIdentityContext
from core.casdoor.ownership import (
    ExternalMemberRolesProjection,
    MemberRole,
    MembershipBackend,
    MembershipObservation,
    RolesKnowledge,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict, CasdoorRoleIntentRepository
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def serialize(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def connect(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (Account, Tenant, TenantAccountJoin, Integration, Namespace, Revision, Identity, History, Intent):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        integration = Integration(enabled=True)
        account = Account(name="Synthetic", email="local-role@example.test", status=AccountStatus.ACTIVE)
        workspace = Tenant(name="Synthetic existing workspace")
        session.add_all([integration, account, workspace])
        session.flush()
        namespace = Namespace(
            integration_id=integration.id,
            expected_issuer="https://synthetic.example.test",
            organization="Org",
            application="App",
            client_id="Client",
            core_fingerprint="b" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url=namespace.expected_issuer,
            backend_api_url=namespace.expected_issuer,
            expected_issuer=namespace.expected_issuer,
            organization="Org",
            application="App",
            client_id="Client",
            button_text="Casdoor",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json=serialize({"schema_version": 1, "default_normal_fallback": True}),
            mappings_json=serialize(
                [{"workspace_id": workspace.id, "admin": {"organization": "Org", "name": "Administrators"}}]
            ),
        )
        identity = Identity(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=namespace.expected_issuer,
            organization="Org",
            subject="Subject",
            sync_generation=1,
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        join = TenantAccountJoin(account_id=account.id, tenant_id=workspace.id, role=TenantAccountRole.NORMAL)
        session.add_all([revision, identity, join])
        session.flush()
        integration.active_revision_id = revision.id
        context = MappingIdentityContext(
            UUID(integration.id),
            UUID(revision.id),
            UUID(namespace.id),
            UUID(identity.id),
            UUID(account.id),
            "a" * 64,
            namespace.expected_issuer,
            "Org",
            "App",
            "Client",
            "Subject",
        )
        target = DesiredWorkspaceTarget(
            UUID(workspace.id),
            "admin",
            "builtin-admin-id",
            CasdoorDecisionReason.ROLE_MAPPING,
            (RoleRef(organization="Org", name="Administrators"),),
        )
        version = GenerationPlanVersion(DesiredWorkspacePlan(context, (target,)), 0, 1)
        observation = MembershipObservation(
            UUID(workspace.id),
            UUID(account.id),
            UUID(join.id),
            join.role,
            MembershipBackend.REMOTE,
            ExternalMemberRolesProjection(
                UUID(workspace.id),
                UUID(account.id),
                RolesKnowledge.COMPLETE,
                (MemberRole("builtin-normal-id", True, "global_system_default", "normal", ("read",)),),
            ),
        )
        baseline = role_baseline_json(observation)
        history = History(
            namespace_id=namespace.id,
            identity_id=identity.id,
            account_id=account.id,
            workspace_id=workspace.id,
            join_id=join.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=7,
            source=CasdoorMembershipSource.ADOPT,
            desired_generation=1,
            revision_id=revision.id,
            last_applied_roles_json=baseline,
            last_applied_fingerprint=roles_fingerprint(observation),
            desired_roles_json=serialize(
                {
                    "schema_version": 1,
                    "backend": "remote",
                    "target_role": "admin",
                    "builtin_id": "builtin-admin-id",
                    "role_ids": ["builtin-admin-id"],
                    "fence_epoch": 0,
                }
            ),
            baseline_json=baseline,
            finalization=CasdoorFinalizationState.PENDING,
            tombstone=False,
        )
        session.add(history)
        session.commit()
        yield SimpleNamespace(
            session=session,
            engine=engine,
            integration=integration,
            account=account,
            workspace=workspace,
            namespace=namespace,
            revision=revision,
            identity=identity,
            join=join,
            history=history,
            version=version,
            target=target,
            repo=CasdoorRoleIntentRepository(session),
        )
    engine.dispose()


def enqueue(s):
    return s.repo.enqueue(s.version, s.target)


def stored(s):
    return s.session.execute(sa.select(*Intent.__table__.columns)).all()


def test_insert_is_exact_pristine_intent_and_idempotent_without_dml(storage):
    s = storage
    with s.session.begin():
        history = s.session.execute(sa.select(*History.__table__.columns)).one()
        first = enqueue(s)
        assert first.created and not s.session.new and not s.session.dirty
        row = stored(s)[0]
        value = json.loads(row.desired_json)
        assert value["desired"] == json.loads(history.desired_roles_json)
        assert value["desired_payload_digest"] == first.desired_payload_digest
        assert value["historical_baseline_fingerprint"] == history.last_applied_fingerprint
        assert first.desired_payload_digest == hashlib.sha256(history.desired_roles_json.encode()).hexdigest()
        assert value["join_id"] == s.join.id and value["integration_id"] == s.integration.id
        assert (row.kind, row.operation_state, row.termination_state, row.attempt_count) == (
            CasdoorIntentKind.ROLE_REPLACE,
            CasdoorOperationState.PENDING,
            CasdoorTerminationState.NOT_STARTED,
            0,
        )
        assert s.session.execute(sa.select(*History.__table__.columns)).one() == history
        assert s.session.scalar(sa.select(TenantAccountJoin.role)) is TenantAccountRole.NORMAL
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with s.session.begin():
        again = enqueue(s)
        assert not again.created and replace(again, created=True) == first
        assert len(stored(s)) == 1
    assert not any(stmt.startswith(("INSERT", "UPDATE", "DELETE")) for stmt in statements)


def test_outer_rollback_removes_intent_and_preserves_saved_desired(storage):
    s = storage
    with pytest.raises(RuntimeError, match="later"), s.session.begin():
        enqueue(s)
        assert len(stored(s)) == 1
        raise RuntimeError("later caller failure")
    with s.session.begin():
        assert not stored(s)
        assert s.session.scalar(sa.select(History.desired_roles_json)) == s.history.desired_roles_json


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted", "nested", "no_root", "inactive", "rbac_off"])
def test_clean_root_rejects_before_any_sql(storage, monkeypatch, pending):
    s = storage
    if pending != "no_root":
        s.session.begin()
    if pending == "new":
        s.session.add(Account(name="Pending", email="pending@example.test"))
    elif pending == "dirty":
        s.account.name = "Pending"
    elif pending == "deleted":
        s.session.delete(s.account)
    elif pending == "nested":
        s.session.begin_nested()
    elif pending == "inactive":
        account_id = s.account.id
        s.session.expunge(s.account)
        duplicate = Account(name="Duplicate", email="duplicate@example.test")
        duplicate.id = account_id
        s.session.add(duplicate)
        with pytest.raises(IntegrityError):
            s.session.flush()
    elif pending == "rbac_off":
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises((CasdoorRoleIntentConflict, RuntimeError)):
        enqueue(s)
    assert not statements
    s.session.rollback()


@pytest.mark.parametrize(
    "bad", ["owner", "dataset", "bool", "duplicate", "generation_bool", "fence_bool", "builtin_long"]
)
def test_bad_plan_refuses_before_sql(storage, bad):
    s = storage
    if bad in ("owner", "dataset", "bool"):
        s.target = replace(s.target, target_role={"owner": "owner", "dataset": "dataset_operator", "bool": True}[bad])
    elif bad == "builtin_long":
        s.target = replace(s.target, builtin_id="汉" * 86)
    s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,) * (2 if bad == "duplicate" else 1)))
    if bad == "generation_bool":
        s.version = replace(s.version, generation=True)
    if bad == "fence_bool":
        s.version = replace(s.version, fence_epoch=False)
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    assert not statements


@pytest.mark.parametrize(
    "kind",
    ["owner", "unmanaged", "released", "override", "tombstone", "join", "fingerprint", "generation", "finalized"],
)
def test_protected_saved_scope_never_enqueues_or_changes_membership(storage, kind):
    s = storage
    with s.session.begin():
        if kind == "owner":
            s.join.role = TenantAccountRole.OWNER
        elif kind == "unmanaged":
            s.session.delete(s.history)
        elif kind in ("released", "override"):
            s.history.ownership = (
                CasdoorMembershipOwnership.RELEASED if kind == "released" else CasdoorMembershipOwnership.LOCAL_OVERRIDE
            )
        elif kind == "tombstone":
            s.history.tombstone = True
        elif kind == "join":
            s.history.join_id = str(uuid4())
        elif kind == "fingerprint":
            s.history.last_applied_fingerprint = "f" * 64
        elif kind == "generation":
            s.history.desired_generation = 0
        else:
            s.history.finalization = CasdoorFinalizationState.FINALIZED
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    with s.session.begin():
        assert not stored(s)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sync_generation", 2),
        ("fence_epoch", 1),
        ("enabled", False),
        ("active_revision_id", None),
        ("subject", "Changed"),
        ("config_digest", "c" * 64),
        ("account_id", str(uuid4())),
        ("status", AccountStatus.BANNED),
        ("status", AccountStatus.CLOSED),
    ],
)
def test_full_current_parent_chain_refuses_stale_database(storage, field, value):
    s = storage
    model = (
        s.identity
        if field in ("sync_generation", "subject", "account_id")
        else s.namespace
        if field == "fence_epoch"
        else s.revision
        if field == "config_digest"
        else s.account
        if field == "status"
        else s.integration
    )
    with s.session.begin():
        s.session.execute(sa.update(type(model)).where(type(model).id == model.id).values({field: value}))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)


@pytest.mark.parametrize(
    "change", ["extra", "schema_bool", "fence_bool", "ids", "backend", "duplicate_keys", "noncanonical", "builtin"]
)
def test_saved_desired_is_strict_exact_schema_not_arbitrary_payload(storage, change):
    s = storage
    with s.session.begin():
        desired = json.loads(s.history.desired_roles_json)
        if change == "extra":
            desired["authenticated"] = True
        elif change == "schema_bool":
            desired["schema_version"] = True
        elif change == "fence_bool":
            desired["fence_epoch"] = False
        elif change == "ids":
            desired["role_ids"] *= 2
        elif change == "backend":
            desired["backend"] = "local"
        elif change == "builtin":
            desired["builtin_id"] = "owner"
        s.history.desired_roles_json = (
            serialize(desired)[:-1] + ',"schema_version":1}'
            if change == "duplicate_keys"
            else json.dumps(desired)
            if change == "noncanonical"
            else serialize(desired)
        )
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)


@pytest.mark.parametrize(
    "kind",
    ["owner", "custom_owner", "builtin_wrongcategory", "duplicate_role", "missing_field", "float", "noncanonical"],
)
def test_historical_baseline_owner_and_metadata_inconsistency_refuse(storage, kind):
    s = storage
    with s.session.begin():
        data = json.loads(s.history.last_applied_roles_json)
        role = data["roles"][0]
        if kind in ("owner", "custom_owner"):
            role["role_tag"] = "owner"
            role["is_builtin"] = kind == "owner"
        elif kind == "builtin_wrongcategory":
            role["category"] = "custom"
        elif kind == "duplicate_role":
            data["roles"] *= 2
        elif kind == "missing_field":
            del role["is_builtin"]
        elif kind == "float":
            data["schema_version"] = 1.0
        text = json.dumps(data) if kind == "noncanonical" else serialize(data)
        s.history.last_applied_roles_json = text
        s.history.last_applied_fingerprint = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)


@pytest.mark.parametrize("field", ["last_applied_roles_json", "desired_roles_json", "baseline_json"])
def test_history_bytes_checked_before_text_materialization(storage, field):
    s = storage
    with s.session.begin():
        setattr(s.history, field, "汉" * 22000)
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    assert any("CAST(" in stmt and "BLOB" in stmt for stmt in statements)
    assert not any("SELECT casdoor_managed_membership_extend.namespace_id" in stmt for stmt in statements)
    assert not any(stmt.startswith(("INSERT", "UPDATE", "DELETE")) for stmt in statements)


@pytest.mark.parametrize(
    "change", ["payload", "foreign_scope", "unknown", "in_flight", "confirmed", "attempt", "proof", "retry", "oversize"]
)
def test_same_key_collision_or_nonpristine_state_never_reused(storage, change):
    s = storage
    with s.session.begin():
        enqueue(s)
    with s.session.begin():
        update = {
            "payload": {"desired_json": "{}"},
            "foreign_scope": {"account_id": str(uuid4())},
            "unknown": {"operation_state": CasdoorOperationState.UNKNOWN},
            "in_flight": {"operation_state": CasdoorOperationState.IN_FLIGHT},
            "confirmed": {"termination_state": CasdoorTerminationState.CONFIRMED},
            "attempt": {"attempt_count": 1},
            "proof": {"proof_ref": "caller-proof"},
            "retry": {"error_code": "retry"},
            "oversize": {"desired_json": "汉" * 22000},
        }[change]
        s.session.execute(sa.update(Intent).values(**update))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    with s.session.begin():
        assert len(stored(s)) == 1


@pytest.mark.parametrize("kind", list(CasdoorIntentKind))
@pytest.mark.parametrize("association", ["scope", "workspace_null", "membership"])
def test_related_intents_any_generation_and_state_block_new_staging(storage, kind, association):
    s = storage
    with s.session.begin():
        s.session.add(
            Intent(
                namespace_id=s.namespace.id,
                identity_id=s.identity.id,
                account_id=s.account.id if association != "membership" else str(uuid4()),
                workspace_id=s.workspace.id if association == "scope" else None,
                membership_id=s.history.id if association == "membership" else None,
                revision_id=s.revision.id,
                generation=0,
                ownership_epoch=0,
                fence_epoch=0,
                kind=kind,
                scope_digest="a" * 64,
                idempotency_key="b" * 64,
                desired_json="{}",
                operation_state=CasdoorOperationState.UNKNOWN,
                termination_state=CasdoorTerminationState.UNCONFIRMED,
            )
        )
    if kind is CasdoorIntentKind.PROFILE_AVATAR:
        with s.session.begin():
            assert enqueue(s).created
    else:
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            enqueue(s)
    with s.session.begin():
        row = s.session.execute(sa.select(Intent.operation_state).where(Intent.idempotency_key == "b" * 64)).one()
        assert row.operation_state is CasdoorOperationState.UNKNOWN


def test_new_generation_never_cancels_old_unknown_attempt(storage):
    s = storage
    with s.session.begin():
        enqueue(s)
    with s.session.begin():
        s.session.execute(
            sa.update(Intent).values(
                operation_state=CasdoorOperationState.UNKNOWN,
                termination_state=CasdoorTerminationState.UNCONFIRMED,
                attempt_count=1,
            )
        )
        s.identity.sync_generation = 2
        s.history.desired_generation = 2
    s.version = replace(s.version, generation=2)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    with s.session.begin():
        row = stored(s)[0]
        assert row.generation == 1 and row.attempt_count == 1
        assert row.operation_state is CasdoorOperationState.UNKNOWN


@pytest.mark.parametrize("multiple", [False, True])
def test_mixed_old_namespace_history_rejected_before_text(storage, multiple):
    s = storage
    with s.session.begin():
        namespace = Namespace(
            integration_id=s.integration.id,
            expected_issuer=s.namespace.expected_issuer,
            organization="Org",
            application="App",
            client_id="Client",
            core_fingerprint="c" * 64,
        )
        s.session.add(namespace)
        s.session.flush()
        if multiple:
            s.session.add(
                History(
                    namespace_id=namespace.id,
                    identity_id=str(uuid4()),
                    account_id=s.account.id,
                    workspace_id=s.workspace.id,
                    join_id=s.join.id,
                    ownership=CasdoorMembershipOwnership.RELEASED,
                    source=CasdoorMembershipSource.ADOPT,
                    desired_generation=1,
                    revision_id=s.revision.id,
                    last_applied_roles_json="{}",
                    last_applied_fingerprint="a" * 64,
                    desired_roles_json="{}",
                    baseline_json="{}",
                )
            )
        else:
            s.history.namespace_id = namespace.id
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    assert not any("SELECT casdoor_managed_membership_extend.namespace_id" in stmt for stmt in statements)


def test_new_plan_cannot_advance_or_overwrite_saved_desired(storage):
    s = storage
    s.target = replace(
        s.target,
        target_role="normal",
        builtin_id="builtin-normal-id",
        reason=CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK,
        matched_role_refs=(),
    )
    s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,)))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    with s.session.begin():
        assert json.loads(s.session.scalar(sa.select(History.desired_roles_json)))["target_role"] == "admin"
        assert not stored(s)


@pytest.mark.parametrize("change", ["mapping", "ref", "fallback", "policy", "oversize"])
def test_saved_configuration_mapping_and_fixed_fallback_are_required(storage, change):
    s = storage
    with s.session.begin():
        if change in ("mapping", "oversize"):
            s.session.execute(sa.update(Revision).values(mappings_json="[]" if change == "mapping" else "汉" * 180000))
        elif change == "policy":
            s.session.execute(sa.update(Revision).values(policy_json='{"default_normal_fallback":false}'))
        elif change == "fallback":
            s.session.execute(sa.update(Revision).values(default_workspace_id=str(uuid4())))
    if change == "ref":
        s.target = replace(s.target, matched_role_refs=(RoleRef(organization="Org", name="Caller"),))
    if change == "fallback":
        s.target = replace(
            s.target,
            target_role="normal",
            builtin_id="builtin-normal-id",
            reason=CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK,
            matched_role_refs=(),
        )
    s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,)))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)


def test_lock_sql_shape_and_no_remote_or_state_transition_api(storage):
    s = storage
    statements = []
    sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
    with s.session.begin():
        enqueue(s)
    locked = [stmt for stmt in statements if isinstance(stmt, sa.sql.Select) and stmt._for_update_arg is not None]
    tables = [next(iter(stmt.get_final_froms())).name for stmt in locked]
    assert tables[:2] == [Integration.__tablename__, Namespace.__tablename__]
    assert (
        tables.index(Account.__tablename__) < tables.index(Identity.__tablename__) < tables.index(Tenant.__tablename__)
    )
    assert (
        tables.index(TenantAccountJoin.__tablename__)
        < tables.index(History.__tablename__)
        < tables.index(Intent.__tablename__)
    )
    for dialect in (postgresql.dialect(), mysql.dialect()):
        assert all("FOR UPDATE" in str(stmt.compile(dialect=dialect)) for stmt in locked)
    assert not any(isinstance(stmt, sa.sql.dml.Update) for stmt in statements)
    tree = ast.parse(
        Path(__file__).parents[3].joinpath("repositories/casdoor_role_intent_repository_extend.py").read_text()
    )
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"commit", "rollback", "flush", "delay", "send_request", "replace"}
        for n in ast.walk(tree)
    )
    assert not any(hasattr(s.repo, name) for name in ("mark_applied", "mark_confirmed", "dispatch", "cancel"))


@pytest.mark.parametrize(
    "field",
    [
        "integration_id",
        "revision_id",
        "namespace_id",
        "identity_id",
        "account_id",
        "issuer",
        "organization",
        "application",
        "client_id",
        "subject",
        "config_digest",
    ],
)
def test_caller_chain_never_substitutes_for_exact_database_chain(storage, field):
    s = storage
    value = uuid4() if field.endswith("_id") else "b" * 64 if field == "config_digest" else "Wrong"
    context = replace(s.version.plan.context, **{field: value})
    s.version = replace(s.version, plan=replace(s.version.plan, context=context))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    with s.session.begin():
        assert not stored(s)


@pytest.mark.parametrize(
    "field", ["namespace_id", "identity_id", "account_id", "workspace_id", "join_id", "revision_id"]
)
def test_history_scope_substitution_refuses(storage, field):
    s = storage
    with s.session.begin():
        # Valid FK substitutes still fail exact ownership, not only constraints.
        value = s.namespace.id if field == "namespace_id" else s.revision.id if field == "revision_id" else str(uuid4())
        if field in ("namespace_id", "revision_id"):
            foreign = Namespace(
                integration_id=s.integration.id,
                expected_issuer=s.namespace.expected_issuer,
                organization="Other",
                application="App",
                client_id="Client",
                core_fingerprint="c" * 64,
            )
            s.session.add(foreign)
            s.session.flush()
            if field == "namespace_id":
                value = foreign.id
            else:
                revision = Revision(
                    integration_id=s.integration.id,
                    namespace_id=foreign.id,
                    revision_number=2,
                    config_digest="d" * 64,
                    browser_frontend_url=s.namespace.expected_issuer,
                    backend_api_url=s.namespace.expected_issuer,
                    expected_issuer=s.namespace.expected_issuer,
                    organization="Other",
                    application="App",
                    client_id="Client",
                    button_text="Casdoor",
                    default_workspace_id=s.workspace.id,
                    certificates_json="[]",
                    policy_json="{}",
                    mappings_json="[]",
                )
                s.session.add(revision)
                s.session.flush()
                value = revision.id
        setattr(s.history, field, value)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)


def test_actual_flushed_i11_registration_can_stage_without_finalizing(storage):
    from repositories.casdoor_membership_repository_extend import CasdoorMembershipRepository
    from services.account_service import TenantService

    s = storage
    with s.session.begin():
        s.session.delete(s.history)
        s.session.delete(s.join)
    with s.session.begin():
        members = CasdoorMembershipRepository(s.session)
        historical_fixture = ExternalMemberRolesProjection(
            UUID(s.workspace.id), UUID(s.account.id), RolesKnowledge.COMPLETE, ()
        )
        token = members.prepare_new(s.version, s.target, backend=MembershipBackend.REMOTE, remote=historical_fixture)
        member = TenantService.persist_tenant_member(s.workspace, s.account, s.session, "normal")
        saved = members.register_new(token, member)
        assert not s.session.new and not s.session.dirty
        receipt = enqueue(s)
        assert receipt.created and receipt.membership_id == saved.membership_id
        assert s.session.scalar(sa.select(History.finalization)) is CasdoorFinalizationState.PENDING
        assert s.session.scalar(sa.select(TenantAccountJoin.role)) is TenantAccountRole.NORMAL
        payload = json.loads(stored(s)[0].desired_json)
        assert "permission_keys" not in payload["desired"] and "roles" not in payload["desired"]
        # Offline helper/registration fixtures are not current remote proof.


def test_canonical_initial_baseline_and_id_byte_bound_preserved(storage):
    s = storage
    s.target = replace(s.target, builtin_id="x" * 255)
    s.version = replace(s.version, plan=replace(s.version.plan, targets=(s.target,)))
    initial = s.history.baseline_json
    with s.session.begin():
        s.history.baseline_json = initial
        desired = json.loads(s.history.desired_roles_json)
        desired["builtin_id"] = s.target.builtin_id
        desired["role_ids"] = [s.target.builtin_id]
        s.history.desired_roles_json = serialize(desired)
    with s.session.begin():
        assert enqueue(s).created
        assert s.session.scalar(sa.select(History.baseline_json)) == initial


def test_namespace_bound_fails_before_history_or_insert(storage, monkeypatch):
    s = storage
    monkeypatch.setattr("repositories.casdoor_role_intent_repository_extend.MAX_NAMESPACES", 0)
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        enqueue(s)
    assert not any(History.__tablename__ in stmt or stmt.startswith("INSERT") for stmt in statements)


def test_complete_historical_custom_roles_are_retained_without_adopting_metadata(storage):
    s = storage
    historical = MembershipObservation(
        UUID(s.workspace.id),
        UUID(s.account.id),
        UUID(s.join.id),
        s.join.role,
        MembershipBackend.REMOTE,
        ExternalMemberRolesProjection(
            UUID(s.workspace.id),
            UUID(s.account.id),
            RolesKnowledge.COMPLETE,
            (MemberRole("custom-id", False, "workspace_custom", "", ("read",)),),
        ),
    )
    with s.session.begin():
        s.history.last_applied_roles_json = role_baseline_json(historical)
        s.history.last_applied_fingerprint = roles_fingerprint(historical)
    with s.session.begin():
        assert enqueue(s).created
        payload = json.loads(stored(s)[0].desired_json)
        assert payload["historical_baseline_fingerprint"] == roles_fingerprint(historical)
        assert payload["desired"]["role_ids"] == ["builtin-admin-id"]
