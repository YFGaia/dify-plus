"""SQLite root commits with original B2A/B3A/F12 and actual leases, fake Redis.

Synthetic configuration/roles do not attest authentication or live concurrency.
"""

import json
from copy import copy
from dataclasses import replace
from datetime import datetime
from time import monotonic
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.admission import AdmissionAction as Action
from core.casdoor.claims import StructuredUserRef
from core.casdoor.configuration import CasdoorConfiguration, RoleRef, WorkspaceRoleMapping
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.leases import CasdoorLeaseError, CasdoorLeases
from core.casdoor.role_graph import EffectiveRoleSnapshot
from models.account import Account, AccountStatus, Tenant, TenantAccountRole
from models.account import TenantAccountJoin as Join
from models.account_money_extend import AccountMoneyExtend as Quota
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.casdoor_account_preflight_repository_extend import AccountPreflightConflict
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict, CasdoorLoginScopeRepository
from services.casdoor_local_login_service_extend import CasdoorLocalLoginService
from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_casdoor_login_account_service_extend import counts, prepare, seed
from test_casdoor_login_account_service_extend import env as original_env
from test_leases import FakeRedisLua

login_env = original_env


def config_factory(session):
    return CasdoorConfigurationRepository(
        session, crypto=CasdoorCrypto(secret_key="offline-key", key_version="1"), rbac_enabled=False
    )


@pytest.fixture
def local(login_env, monkeypatch):
    session, c, *_ = login_env
    engine = session.get_bind()
    for model in (Tenant, Join, History, Intent, InvitationAuthorityLifecycleExtend):
        model.__table__.create(engine)

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    ref = RoleRef(organization="Org", name="operators")
    configuration = CasdoorConfiguration(
        browser_frontend_url=c.issuer,
        backend_api_url=c.issuer,
        expected_issuer=c.issuer,
        organization=c.organization,
        application=c.application,
        client_id=c.client_id,
        default_workspace_id=UUID(int=100),
        workspace_mappings=(WorkspaceRoleMapping(workspace_id=UUID(int=200), admin=ref),),
    )
    with session.begin():
        for n in (100, 200):
            space = Tenant(name=f"Space {n}")
            space.id = str(UUID(int=n))
            session.add(space)
        revision = session.get(Revision, str(c.revision_id))
        data = json.loads(configuration.canonical_json())
        policy_fields = (
            "schema_version",
            "scope",
            "default_normal_fallback",
            "name_sync",
            "avatar_sync",
            "avatar_mode",
            "rp_logout",
            "self_unlink",
        )
        session.execute(
            sa.update(Revision).values(
                default_workspace_id=str(configuration.default_workspace_id),
                policy_json=json.dumps({k: data[k] for k in policy_fields}),
                mappings_json=json.dumps(data["workspace_mappings"]),
                button_text=configuration.button_text,
            )
        )
        session.expire(revision)
        digest = config_factory(session)._validation_digest(configuration, revision)
        session.execute(sa.update(Revision).values(config_digest=digest))
        session.execute(
            sa.update(Namespace).values(core_fingerprint=config_factory(session)._core_fingerprint(configuration))
        )
    env = (session, replace(c, config_digest=digest), *login_env[2:])
    roles = EffectiveRoleSnapshot(c.subject, StructuredUserRef("Org", "person"), (ref,))
    service = CasdoorLocalLoginService(session_factory=lambda: Session(engine), configuration_factory=config_factory)
    yield SimpleNamespace(env=env, engine=engine, session=session, roles=roles, service=service, config=configuration)


def discover(local, prepared):
    with Session(local.engine) as reader, reader.begin():
        return CasdoorLoginScopeRepository(reader, config_factory).discover(prepared)


def ready(local, action=Action.CREATE_INITIALIZED):
    prepared = prepare(local.env, action)
    scope = discover(local, prepared)
    redis = FakeRedisLua(monotonic)
    leases = CasdoorLeases(redis, scope.lease_scope, deadline=prepared.deadline, ttl_seconds=45)
    leases.acquire()
    return prepared, scope, redis, leases


def run(local, bundle, **kwargs):
    prepared, scope, _, leases = bundle
    return local.service._persist_local_login(
        account_owner=local.env[3], prepared=prepared, discovered=scope, roles=local.roles, leases=leases, **kwargs
    )


def test_complete_new_commit_one_quota_generation_default_and_mapping(local):
    bundle = ready(local)
    try:
        result = run(local, bundle)
        assert result.generation == 1
        assert [x.current_role for x in result.workspaces] == [TenantAccountRole.NORMAL, TenantAccountRole.ADMIN]
        assert all(x.finalization is CasdoorFinalizationState.PENDING for x in result.workspaces)
        assert counts(local.engine) == (1, 1, 1)
        with Session(local.engine) as reader:
            assert reader.scalar(sa.select(Quota.total_quota)) == 15
            assert reader.scalar(sa.select(Identity.sync_generation)) == 1
            assert reader.scalar(sa.select(sa.func.count()).select_from(Join)) == 2
            assert reader.scalar(sa.select(sa.func.count()).select_from(History)) == 2
    finally:
        assert bundle[3].release()


@pytest.mark.parametrize(
    "action,status,marker",
    [
        (Action.INITIALIZE_BOUND, AccountStatus.PENDING, None),
        (Action.ACTIVATE_BOUND, AccountStatus.PENDING, datetime(2025, 1, 1)),
        (Action.USE_BOUND, AccountStatus.ACTIVE, datetime(2025, 1, 1)),
    ],
)
def test_bound_actions_preserve_quota_and_repeat_same_role_generation(local, action, status, marker):
    seed(local.env, status, marker)
    bundle = ready(local, action)
    try:
        assert run(local, bundle).generation == 1
    finally:
        bundle[3].release()
    bundle = ready(local, Action.USE_BOUND)
    try:
        result = run(local, bundle)
        assert result.generation == 2
        assert not any(x.membership_created for x in result.workspaces)
        with Session(local.engine) as reader:
            assert reader.scalar(sa.select(Quota.total_quota)) == 91.25
    finally:
        bundle[3].release()


@pytest.mark.parametrize(
    "table,event,condition",
    [
        ("casdoor_identity_extend", "INSERT", "1"),
        ("tenant_account_joins", "INSERT", f"NEW.tenant_id = '{UUID(int=200)}'"),
        ("casdoor_managed_membership_extend", "UPDATE", f"NEW.workspace_id = '{UUID(int=200)}'"),
    ],
)
def test_real_sql_failure_rolls_back_whole_root(local, table, event, condition):
    bundle = ready(local)
    with local.session.begin():
        local.session.execute(
            sa.text(
                f"CREATE TRIGGER fail_write BEFORE {event} ON {table} WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'offline failure'); END"
            )
        )
    try:
        with pytest.raises(IntegrityError):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
        with Session(local.engine) as reader:
            assert reader.scalar(sa.select(sa.func.count()).select_from(Join)) == 0
            assert reader.scalar(sa.select(sa.func.count()).select_from(History)) == 0
        assert bundle[0]._consumed
    finally:
        bundle[3].release()


@pytest.mark.parametrize("kind", ["copy", "foreign", "consumed"])
def test_original_preparation_cannot_be_substituted(local, kind):
    bundle = list(ready(local))
    if kind == "copy":
        bundle[0] = copy(bundle[0])
    elif kind == "foreign":
        from services.casdoor_login_account_service_extend import CasdoorLoginAccountService

        local.env = (*local.env[:3], CasdoorLoginAccountService(activation=local.env[3]._activation), *local.env[4:])
    else:
        object.__setattr__(bundle[0], "_consumed", True)
    try:
        with pytest.raises(AccountPreflightConflict):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
    finally:
        bundle[3].release()


@pytest.mark.parametrize("kind", ["lease", "fence", "config", "generation", "alias"])
def test_postwrite_failures_roll_back_every_owner(local, monkeypatch, kind):
    bundle = ready(local)
    original = CasdoorLocalMembershipService.persist_local_memberships

    def sabotage(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        session = owner._session
        if kind == "lease":
            key = bundle[3].canonical_keys[0]
            bundle[2].data[key] = (b"different-owner", monotonic() + 40)
        elif kind == "fence":
            session.execute(sa.update(Namespace).values(fence_epoch=1))
        elif kind == "config":
            session.execute(sa.update(Revision).values(config_digest="f" * 64))
        elif kind == "generation":
            session.execute(sa.update(Identity).values(sync_generation=91))
        else:
            session.add(Account(name="Race", email="NEW@example.test", status=AccountStatus.ACTIVE))
            session.flush()
        return result

    monkeypatch.setattr(CasdoorLocalMembershipService, "persist_local_memberships", sabotage)
    try:
        with pytest.raises((ValueError, CasdoorLeaseError)):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
    finally:
        bundle[3].release()


@pytest.mark.parametrize("kwargs", [{"invitation": object()}, {"source": object()}])
def test_invitation_and_source_are_rejected_before_dml(local, kwargs):
    bundle = ready(local)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle, **kwargs)
        assert counts(local.engine) == (0, 0, 0)
        assert not bundle[0]._consumed
    finally:
        bundle[3].release()


def committed(local):
    bundle = ready(local)
    try:
        return run(local, bundle)
    finally:
        bundle[3].release()


@pytest.mark.parametrize("kind", ["unmanaged", "owner", "override", "tombstone"])
def test_preserved_existing_scope_never_adopted(local, kind):
    from models.casdoor_extend import CasdoorMembershipOwnership

    result = committed(local)
    with local.session.begin():
        if kind in ("unmanaged", "owner"):
            local.session.execute(sa.delete(History))
            if kind == "owner":
                local.session.execute(sa.update(Join).values(role=TenantAccountRole.OWNER))
        elif kind == "override":
            local.session.execute(sa.update(History).values(ownership=CasdoorMembershipOwnership.LOCAL_OVERRIDE))
        else:
            local.session.execute(sa.update(History).values(tombstone=True))
            local.session.execute(sa.delete(Join))
    bundle = ready(local, Action.USE_BOUND)
    try:
        observed = run(local, bundle)
        assert observed.generation == result.generation + 1
        assert all(
            not x.membership_created and not x.metadata_changed and not x.role_changed for x in observed.workspaces
        )
        with Session(local.engine) as reader:
            assert reader.scalar(sa.select(sa.func.count()).select_from(History)) == (
                0 if kind in ("unmanaged", "owner") else 2
            )
            if kind == "tombstone":
                assert reader.scalar(sa.select(sa.func.count()).select_from(Join)) == 0
    finally:
        bundle[3].release()


def test_lost_mapped_role_requires_withdrawal_before_first_dml(local):
    committed(local)
    local.roles = replace(local.roles, effective_roles=())
    bundle = ready(local, Action.USE_BOUND)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert not bundle[0]._consumed
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)
        bundle[3].release()


@pytest.mark.parametrize(
    "field,value",
    [
        ("finalization", "finalized"),
        ("ownership_epoch", 7),
        ("tombstone", True),
        ("desired_generation", 0),
        ("last_applied_fingerprint", "0" * 64),
        ("desired_roles_json", "{}"),
        ("last_applied_roles_json", "{}"),
    ],
)
def test_final_metadata_corruption_rolls_back_new_root(local, monkeypatch, field, value):
    bundle = ready(local)
    original = CasdoorLocalMembershipService.persist_local_memberships

    def sabotage(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        owner._session.execute(sa.update(History).values(**{field: value}))
        return result

    monkeypatch.setattr(CasdoorLocalMembershipService, "persist_local_memberships", sabotage)
    try:
        with pytest.raises((ValueError, IntegrityError)):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
    finally:
        bundle[3].release()


@pytest.mark.parametrize("late", [False, True])
def test_real_final_lease_loss_is_checked_after_scope_reconstruction(local, monkeypatch, late):
    bundle = ready(local)
    original = CasdoorLoginScopeRepository.recheck_before_commit

    def verify(repository, *args):
        original(repository, *args)
        key = bundle[3].canonical_keys[-1]
        if late:
            bundle[2].data[key] = (bundle[2].data[key][0], monotonic() - 1)
        else:
            bundle[2].data[key] = (b"winner", monotonic() + 40)

    monkeypatch.setattr(CasdoorLoginScopeRepository, "recheck_before_commit", verify)
    try:
        with pytest.raises(CasdoorLeaseError):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
    finally:
        bundle[3].release()


def test_rbac_rejected_before_first_dml(local, monkeypatch):
    bundle = ready(local)
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert not bundle[0]._consumed
    finally:
        bundle[3].release()


@pytest.mark.parametrize("kind", ["gmail", "tail", "overflow"])
def test_actual_f12_forced_postwrite_scan_with_original_new_uuid(local, monkeypatch, kind):
    from uuid import uuid4

    from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
    from test_casdoor_login_account_service_extend import inputs

    plan, preflight = inputs(local.env)
    if kind == "gmail":
        plan = replace(plan, creation_email="firstlast@gmail.com")
    prepared = local.env[3]._prepare_login(plan=plan, preflight=preflight, deadline=monotonic() + 40)
    scope = discover(local, prepared)
    redis = FakeRedisLua(monotonic)
    leases = CasdoorLeases(redis, scope.lease_scope, deadline=prepared.deadline, ttl_seconds=45)
    leases.acquire()
    bundle = (prepared, scope, redis, leases)
    original = CasdoorLoginAccountService.persist_login_account

    def inject(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        session = kwargs["session"]
        if kind == "gmail":
            rows = [
                dict(id=str(uuid4()), name="Alias", email="First.Last+tag@googlemail.com", status=AccountStatus.ACTIVE)
            ]
        else:
            rows = [
                dict(
                    id=str(UUID(int=i + 1)), name="Other", email=f"other-{i}@example.test", status=AccountStatus.ACTIVE
                )
                for i in range(2049 if kind == "overflow" else 2047)
            ]
            if kind == "tail":
                rows.append(
                    dict(
                        id="ffffffff-ffff-ffff-ffff-ffffffffffff",
                        name="Tail",
                        email="NEW@example.test",
                        status=AccountStatus.ACTIVE,
                    )
                )
        session.execute(sa.insert(Account), rows)
        return result

    monkeypatch.setattr(CasdoorLoginAccountService, "persist_login_account", inject)
    try:
        with pytest.raises(ValueError):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
    finally:
        leases.release()


def test_bound_does_not_force_postwrite_email_scan(local, monkeypatch):
    from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository

    seed(local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    bundle = ready(local, Action.USE_BOUND)

    def forbidden(*args, **kwargs):
        raise AssertionError("Bound must retain UNCHECKED fast path")

    monkeypatch.setattr(CasdoorAccountPreflightRepository, "_observe_postwrite_new_collisions", forbidden)
    try:
        assert run(local, bundle).generation == 1
    finally:
        bundle[3].release()


@pytest.mark.parametrize("kind", ["prepared", "scope", "key", "plan_context", "leases"])
def test_partial_private_shapes_reject_with_domain_error_without_sql(local, kind):
    from core.casdoor.admission import AdmissionContext
    from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
    from repositories.casdoor_login_scope_repository_extend import LoginScope
    from services.casdoor_login_account_service_extend import _PreparedLoginAccount

    bundle = list(ready(local))
    cleanup = bundle[3]
    if kind == "prepared":
        bundle[0] = object.__new__(_PreparedLoginAccount)
    elif kind == "scope":
        bundle[1] = object.__new__(LoginScope)
    elif kind == "key":
        object.__setattr__(bundle[0], "key", object.__new__(VerifiedIdentityKey))
    elif kind == "plan_context":
        object.__setattr__(bundle[0], "plan", replace(bundle[0].plan, context=object.__new__(AdmissionContext)))
    else:
        bundle[3] = object.__new__(CasdoorLeases)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        with pytest.raises((CasdoorLoginScopeConflict, AccountPreflightConflict)):
            run(local, bundle)
        assert not statements
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)
        cleanup.release()


def revise(local, configuration):
    """Synthetic immutable revision switch, using actual original digest owner."""
    from uuid import uuid4

    from models.casdoor_extend import CasdoorIntegrationExtend as Integration

    session = local.session
    c = local.env[1]
    with session.begin():
        base = dict(
            session.execute(sa.select(*Revision.__table__.columns).where(Revision.id == str(c.revision_id)))
            .one()
            ._mapping
        )
        revision_id = str(uuid4())
        data = json.loads(configuration.canonical_json())
        session.execute(
            sa.insert(Revision).values(
                **dict(
                    base,
                    id=revision_id,
                    revision_number=base["revision_number"] + 1,
                    mappings_json=json.dumps(data["workspace_mappings"]),
                )
            )
        )
        revision = session.get(Revision, revision_id)
        digest = config_factory(session)._validation_digest(configuration, revision)
        session.execute(sa.update(Revision).where(Revision.id == revision_id).values(config_digest=digest))
        session.execute(sa.update(Integration).values(active_revision_id=revision_id))
    local.env = (
        session,
        replace(c, revision_id=UUID(revision_id), active_revision_id=UUID(revision_id), config_digest=digest),
        *local.env[2:],
    )


def test_original_managed_default_admin_downgrades_to_normal_on_new_revision(local):
    ref = local.roles.effective_roles[0]
    configuration = local.config.model_copy(
        update={
            "workspace_mappings": (
                WorkspaceRoleMapping(workspace_id=UUID(int=100), admin=ref),
                *local.config.workspace_mappings,
            )
        }
    )
    revise(local, configuration)
    result = committed(local)
    assert result.workspaces[0].current_role is TenantAccountRole.ADMIN
    revise(local, local.config)
    bundle = ready(local, Action.USE_BOUND)
    try:
        result = run(local, bundle)
        assert result.generation == 2
        assert result.workspaces[0].current_role is TenantAccountRole.NORMAL
        assert result.workspaces[0].role_changed
        assert result.workspaces[0].finalization is CasdoorFinalizationState.PENDING
    finally:
        bundle[3].release()


@pytest.mark.parametrize("target", ["account_status", "setup_name", "default", "pointer", "backend", "intent"])
def test_final_parent_account_and_intent_changes_rollback(local, monkeypatch, target):
    from uuid import uuid4

    from models.casdoor_extend import CasdoorIntegrationExtend as Integration

    bundle = ready(local)
    original = CasdoorLocalMembershipService.persist_local_memberships

    def sabotage(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        session = owner._session
        if target == "account_status":
            session.execute(sa.update(Account).values(status=AccountStatus.PENDING))
        elif target == "setup_name":
            session.execute(sa.update(Account).values(name="Corrupted"))
        elif target == "default":
            session.execute(sa.update(Tenant).where(Tenant.id == str(UUID(int=100))).values(status="archive"))
        elif target == "pointer":
            session.execute(sa.update(Integration).values(active_revision_id=None))
        elif target == "backend":
            monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
        else:
            session.add(
                Intent(
                    namespace_id=str(result.namespace_id),
                    identity_id=str(result.identity_id),
                    account_id=str(result.account_id),
                    workspace_id=None,
                    revision_id=str(result.revision_id),
                    generation=1,
                    ownership_epoch=0,
                    fence_epoch=0,
                    kind="resource_grant",
                    scope_digest="a" * 64,
                    idempotency_key=uuid4().hex * 2,
                    desired_json="{}",
                    operation_state="applied",
                    termination_state="confirmed",
                )
            )
            session.flush()
        return result

    monkeypatch.setattr(CasdoorLocalMembershipService, "persist_local_memberships", sabotage)
    try:
        with pytest.raises(ValueError):
            run(local, bundle)
        assert counts(local.engine) == (0, 0, 0)
    finally:
        bundle[3].release()


def test_historical_managed_namespace_transfer_is_pending_before_dml(local):
    from uuid import uuid4

    committed(local)
    with local.session.begin():
        ns = dict(local.session.execute(sa.select(*Namespace.__table__.columns)).one()._mapping)
        revision = dict(local.session.execute(sa.select(*Revision.__table__.columns)).one()._mapping)
        old_ns, old_revision = str(uuid4()), str(uuid4())
        local.session.execute(sa.insert(Namespace).values(**dict(ns, id=old_ns, lifecycle="archived")))
        local.session.execute(
            sa.insert(Revision).values(**dict(revision, id=old_revision, namespace_id=old_ns, revision_number=2))
        )
        local.session.execute(
            sa.update(History).values(namespace_id=old_ns, revision_id=old_revision, identity_id=str(uuid4()))
        )
    bundle = ready(local, Action.USE_BOUND)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            run(local, bundle)
        assert not bundle[0]._consumed
        with local.session.begin():
            assert local.session.scalar(sa.select(Identity.sync_generation)) == 1
    finally:
        bundle[3].release()


def test_exception_after_root_commit_is_not_reported_as_rollback_or_retried(local):
    bundle = ready(local)
    commits = []

    def fail_ack(session):
        if not session.in_nested_transaction():
            commits.append(1)
            raise RuntimeError("Synthetic root commit acknowledgement failure")

    sa.event.listen(Session, "after_commit", fail_ack)
    try:
        with pytest.raises(RuntimeError, match="acknowledgement"):
            run(local, bundle)
    finally:
        sa.event.remove(Session, "after_commit", fail_ack)
        bundle[3].release()
    assert commits == [1]
    assert counts(local.engine) == (1, 1, 1)
    assert bundle[0]._consumed
