"""Actual P3K/P3L -> P3M-B -> B3 SQLite persistence; no live acceptance."""

from dataclasses import replace
from datetime import datetime
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa

from core.casdoor.claims import StructuredUserRef
from core.casdoor.configuration import RoleRef
from core.casdoor.leases import CasdoorLeaseError, CasdoorLeases
from core.casdoor.role_graph import EffectiveRoleSnapshot
from models.account import Account, AccountStatus, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_invited_login_guard_repository_extend import _REGISTRY, CasdoorInvitedLoginGuardRepository
from services.casdoor_invited_local_membership_service_extend import CasdoorInvitedLocalMembershipService
from tests.unit_tests.core.casdoor.test_leases import FakeRedisLua
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    configuration_factory,
    discover,
)
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    invited_scope_case as original_case,
)

invited_scope_case = original_case


@pytest.fixture
def writer_case(invited_scope_case):
    leases_created = []

    def build(*, absent=False, invite_target=False):
        case = invited_scope_case(absent=absent, stage="issued" if invite_target else "completed")
        if invite_target:
            from services.casdoor_invitation_finalization_service_extend import CasdoorInvitationFinalizationService
            from tests.unit_tests.repositories.test_casdoor_invitation_operation_repository_extend import create
            from tests.unit_tests.services.test_invitation_token_consumption_extend import TOKEN

            with case.db() as session, session.begin():
                integration = session.get(Integration, case.ids["integration"])
                case.configuration = type(case.configuration).model_validate(
                    dict(case.configuration.model_dump(), default_workspace_id=UUID(case.ids["workspace"]))
                )
                saved = configuration_factory(session).save_draft(
                    case.configuration, etag=integration.etag, actor_account_id=case.attempt.account_id
                )
                revision = session.get(Revision, str(saved.draft_revision_id))
                integration.active_revision_id = revision.id
                case.ids["revision"] = revision.id
                case.context = replace(
                    case.context,
                    revision_id=UUID(revision.id),
                    active_revision_id=UUID(revision.id),
                    config_digest=revision.config_digest,
                )
                case.attempt = replace(case.attempt, context=case.context)
            case.pending = create(case)
            case.completed = CasdoorInvitationFinalizationService(
                session_factory=case.factory, store=case.store
            ).finalize(case.attempt, token=TOKEN)
        with case.db() as session, session.begin():
            account = session.get(Account, case.ids["account"])
            account.status = AccountStatus.ACTIVE
            account.initialized_at = datetime(2026, 1, 1)
        case.observed = discover(case)
        case.deadline = monotonic() + 40
        case.lease_redis = FakeRedisLua(monotonic)
        case.leases = CasdoorLeases(
            case.lease_redis, case.observed.scope.lease_scope, deadline=case.deadline, ttl_seconds=60
        )
        case.leases.acquire()
        leases_created.append(case.leases)
        case.roles = EffectiveRoleSnapshot(
            "ExactSubject",
            StructuredUserRef("OfflineOrg", "person"),
            (RoleRef(organization="OfflineOrg", name="operators"),),
        )
        case.writer = CasdoorInvitedLocalMembershipService(
            session_factory=case.factory, configuration_factory=configuration_factory
        )
        return case

    yield build
    for leases in leases_created:
        leases.release()


def persist(case):
    return case.writer.persist_invited_local_memberships(
        case.attempt, roles=case.roles, leases=case.leases, deadline=case.deadline
    )


@pytest.mark.parametrize("absent", [False, True])
def test_actual_completed_invitation_runs_real_b3_once(writer_case, absent):
    case = writer_case(absent=absent)
    commits = []

    def committed(_connection):
        commits.append(True)

    sa.event.listen(case.engine, "commit", committed)
    try:
        result = persist(case)
    finally:
        sa.event.remove(case.engine, "commit", committed)
    assert result.generation == 1
    assert len(result.workspaces) == 2
    assert all(item.membership_created for item in result.workspaces)
    assert len(commits) == 1
    assert not _REGISTRY
    with case.db() as session:
        assert session.scalar(sa.select(Identity.sync_generation)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(History)) == 2


MODELS = (Intent, Issuance, Lifecycle, Integration, Revision, Namespace, Account, Identity, Tenant, Join, History)


def database(case):
    with case.db() as session:
        return {
            model.__tablename__: tuple(
                session.execute(sa.select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns))
            )
            for model in MODELS
        }


@pytest.mark.parametrize("absent", [False, True])
def test_invited_unmanaged_target_is_preserved_with_exact_barrier_exemption(writer_case, absent, monkeypatch):
    from core.casdoor.local_roles import LocalRoleOutcome
    from repositories.casdoor_invitation_finalization_repository_extend import CasdoorInvitationFinalizationRepository
    from repositories.casdoor_membership_repository_extend import CasdoorMembershipRepository

    case = writer_case(absent=absent, invite_target=True)
    before = database(case)
    original = CasdoorInvitedLoginGuardRepository.persist_once

    def no_withdrawal(*args, **kwargs):
        raise AssertionError("withdrawal/regrant owner is unreachable")

    def persisted(owner, guard):
        result = original(owner, guard)
        monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", no_withdrawal)
        return result

    monkeypatch.setattr(CasdoorMembershipRepository, "_read_local_state", no_withdrawal)
    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "persist_once", persisted)
    result = persist(case)
    invite = next(item for item in result.workspaces if item.workspace_id == case.attempt.workspace_id)
    assert invite.outcome is LocalRoleOutcome.PRESERVED
    assert not invite.membership_created
    assert invite.membership_id is None
    assert invite.join_id == UUID(case.completed.facts.join_id)
    after = database(case)
    for model in (Intent, Issuance, Integration, Revision, Namespace, Account, Tenant):
        assert before[model.__tablename__] == after[model.__tablename__]
    assert before[Lifecycle.__tablename__][0] in after[Lifecycle.__tablename__]
    assert before[Join.__tablename__][0] in after[Join.__tablename__]
    assert len(after[History.__tablename__]) == 1


def changed_value(column, value):
    from datetime import timedelta
    from uuid import uuid4

    if hasattr(column.type, "_enum_class"):
        return next(item for item in column.type._enum_class if item != value)
    if isinstance(column.type, sa.Enum):
        return next(item for item in column.type.enum_class if item != value)
    if isinstance(column.type, sa.Boolean):
        return not value
    if isinstance(column.type, (sa.Integer, sa.BigInteger)):
        return (value or 0) + 1
    if isinstance(column.type, sa.DateTime):
        return (value or datetime(2026, 1, 1)) + timedelta(seconds=1)
    if column.name.endswith("digest"):
        return "0" * 64 if value != "0" * 64 else "1" * 64
    if column.table.name == "invitation_authority_lifecycle_extend" and column.name == "state":
        return "withdrawn"
    if column.name == "id" or column.name.endswith("_id"):
        return str(uuid4())
    return "changed" if value != "changed" else "different"


IMMUTABLE = [
    (model, column.name)
    for model in (Intent, Issuance, Lifecycle, Integration, Revision, Namespace, Account, Identity, Tenant, Join)
    for column in model.__table__.columns
    if not (model is Identity and column.name in ("sync_generation", "updated_at"))
]


@pytest.mark.parametrize(("model", "column"), IMMUTABLE, ids=[f"{m.__tablename__}.{c}" for m, c in IMMUTABLE])
def test_every_immutable_column_drift_after_actual_b3_rolls_back(writer_case, monkeypatch, model, column, request):
    case = writer_case()
    before = database(case)
    original = CasdoorInvitedLoginGuardRepository.persist_once
    attempts = []

    def persisted(owner, guard):
        result = original(owner, guard)
        attempts.append(result)
        table = model.__table__
        primary = tuple(table.primary_key.columns)
        row = before[model.__tablename__][0]
        condition = [key == getattr(row, key.name) for key in primary]
        try:
            owner.session.execute(
                sa.update(table)
                .where(*condition)
                .values({column: changed_value(table.c[column], getattr(row, column))})
            )
        except (ValueError, sa.exc.SQLAlchemyError):
            request.node.user_properties.append(("immutable_rejection_owner", "schema_or_bind_constraint"))
            raise
        request.node.user_properties.append(("immutable_rejection_owner", "postwrite_proof"))
        return result

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "persist_once", persisted)
    with pytest.raises((ValueError, sa.exc.SQLAlchemyError, CasdoorLeaseError)):
        persist(case)
    assert len(attempts) == 1
    assert database(case) == before
    assert not _REGISTRY


@pytest.mark.parametrize(
    "kind",
    [
        "pending",
        "uninitialized",
        "history",
        "rbac",
        "enterprise",
        "partial-leases",
        "lost-lease",
        "expired",
        "wrong-subject",
    ],
)
def test_unsupported_boundary_rejects_before_generation_dml(writer_case, monkeypatch, config_overrides, kind):
    from enums import DeploymentEdition
    from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import add_history_scope

    case = writer_case()
    if kind in ("pending", "uninitialized"):
        with case.db() as session, session.begin():
            account = session.get(Account, case.ids["account"])
            if kind == "pending":
                account.status = AccountStatus.PENDING
            else:
                account.initialized_at = None
    elif kind == "history":
        add_history_scope(case)
    elif kind == "rbac":
        config_overrides(RBAC_ENABLED=True)
    elif kind == "enterprise":
        config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.ENTERPRISE)
    elif kind == "partial-leases":
        case.leases._keys = case.leases.canonical_keys[:-1]
    elif kind == "lost-lease":
        case.leases.release()
    elif kind == "expired":
        case.deadline = monotonic() - 1
    elif kind == "wrong-subject":
        case.roles = replace(case.roles, subject="another")
    before = database(case)
    writes = []

    def executed(connection, statement, parameters, context, many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", executed)
    try:
        with pytest.raises((ValueError, sa.exc.SQLAlchemyError, CasdoorLeaseError)):
            persist(case)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", executed)
    assert writes == []
    assert database(case) == before
    assert not _REGISTRY


@pytest.mark.parametrize(
    ("method", "position"),
    [("_leases", i) for i in range(1, 7)]
    + [("_invitation_unchanged", 1), ("_scope", 1), ("_snapshot_rows", 1), ("verify_after", 1)],
)
def test_each_owner_check_failure_rolls_back_without_retry(writer_case, monkeypatch, method, position):
    case = writer_case()
    before = database(case)
    original = getattr(CasdoorInvitedLoginGuardRepository, method)
    calls = []

    def fail(owner, *args, **kwargs):
        calls.append(True)
        if len(calls) == position:
            raise RuntimeError("injected bounded proof failure")
        return original(owner, *args, **kwargs)

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, method, fail)
    with pytest.raises(RuntimeError, match="injected bounded proof failure"):
        persist(case)
    assert len(calls) == position
    assert database(case) == before
    assert not _REGISTRY


def test_unknown_commit_never_retries_or_returns_success(writer_case):
    case = writer_case()
    commits = []

    def uncertain(session):
        commits.append(True)
        raise RuntimeError("unknown commit")

    def factory():
        session = case.factory()
        sa.event.listen(session, "after_commit", uncertain)
        return session

    case.writer = CasdoorInvitedLocalMembershipService(
        session_factory=factory, configuration_factory=configuration_factory
    )
    with pytest.raises(RuntimeError, match="unknown commit"):
        persist(case)
    assert len(commits) == 1
    assert not _REGISTRY
    # This simulated uncertainty occurs after SQLite committed: no rollback claim.
    with case.db() as session:
        assert session.scalar(sa.select(Identity.sync_generation)) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "generation",
        "backwards-time",
        "extra-join",
        "extra-history",
        "history-tombstone",
        "history-namespace",
        "history-role-text",
        "extra-intent",
        "other-identity",
    ],
)
def test_postwrite_delta_drift_is_rejected_and_wholly_rolled_back(writer_case, monkeypatch, kind):
    from uuid import uuid4

    case = writer_case()
    before = database(case)
    original = CasdoorInvitedLoginGuardRepository.persist_once

    def persisted(owner, guard):
        result = original(owner, guard)
        session = owner.session
        if kind == "generation":
            session.execute(sa.update(Identity).values(sync_generation=2))
        elif kind == "backwards-time":
            session.execute(sa.update(Identity).values(updated_at=datetime(2000, 1, 1)))
        elif kind == "extra-join":
            session.execute(
                sa.insert(Join).values(
                    id=str(uuid4()),
                    account_id=case.ids["account"],
                    tenant_id=str(uuid4()),
                    role="normal",
                    current=False,
                )
            )
        elif kind == "extra-history":
            row = dict(session.execute(sa.select(*History.__table__.columns).limit(1)).one()._mapping)
            session.execute(
                sa.insert(History).values(**dict(row, id=str(uuid4()), workspace_id=str(uuid4()), join_id=str(uuid4())))
            )
        elif kind.startswith("history-"):
            values = {
                "history-tombstone": {"tombstone": True},
                "history-namespace": {"namespace_id": str(uuid4())},
                "history-role-text": {"desired_roles_json": "{}"},
            }[kind]
            session.execute(sa.update(History).values(**values))
        elif kind == "extra-intent":
            row = dict(session.execute(sa.select(*Intent.__table__.columns)).one()._mapping)
            session.execute(sa.insert(Intent).values(**dict(row, id=str(uuid4()), idempotency_key=uuid4().hex * 2)))
        else:
            row = dict(session.execute(sa.select(*Identity.__table__.columns)).one()._mapping)
            session.execute(sa.insert(Identity).values(**dict(row, id=str(uuid4()), namespace_id=str(uuid4()))))
        return result

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "persist_once", persisted)
    with pytest.raises((ValueError, sa.exc.SQLAlchemyError, CasdoorLeaseError)):
        persist(case)
    assert database(case) == before
    assert not _REGISTRY


@pytest.mark.parametrize("position", [5, 6])
@pytest.mark.parametrize(("model", "column"), [(Integration, "etag"), (Account, "name"), (Intent, "updated_at")])
def test_drift_at_each_postwrite_lease_io_boundary_is_detected(writer_case, monkeypatch, position, model, column):
    case = writer_case()
    before = database(case)
    original = CasdoorInvitedLoginGuardRepository._leases
    calls = []

    def lease_checked(owner, value):
        original(owner, value)
        calls.append(True)
        if len(calls) == position:
            row = before[model.__tablename__][0]
            owner.session.execute(
                sa.update(model).values({column: changed_value(model.__table__.c[column], getattr(row, column))})
            )

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "_leases", lease_checked)
    with pytest.raises(ValueError):
        persist(case)
    assert len(calls) >= position
    assert database(case) == before
    assert not _REGISTRY


def test_actual_prelock_is_first_sql_owner_in_fresh_explicit_begin(writer_case, monkeypatch):
    from sqlalchemy.orm import SessionTransactionOrigin

    from repositories.casdoor_invited_login_scope_repository_extend import CasdoorInvitedLoginScopeRepository

    case = writer_case()
    queries = []
    original = CasdoorInvitedLoginScopeRepository.prelock_and_recheck_completed_invitation

    def observed(owner, attempt):
        assert queries == []
        root = owner.session.get_transaction()
        assert root.origin is SessionTransactionOrigin.BEGIN
        assert not root._connections
        return original(owner, attempt)

    def executed(connection, clause, multiparams, params, options):
        queries.append(clause)

    monkeypatch.setattr(CasdoorInvitedLoginScopeRepository, "prelock_and_recheck_completed_invitation", observed)
    sa.event.listen(case.engine, "before_execute", executed)
    try:
        persist(case)
    finally:
        sa.event.remove(case.engine, "before_execute", executed)
    assert queries[0].get_final_froms()[0].name == Integration.__tablename__


@pytest.mark.parametrize("state", ["autobegin", "explicit-root", "pending"])
def test_service_rejects_nonfresh_session_before_prelock(writer_case, monkeypatch, state):
    case = writer_case()
    before = database(case)
    session = case.db()
    if state == "autobegin":
        session.execute(sa.select(1))
    elif state == "explicit-root":
        session.begin()
    else:
        session.add(Tenant(name="must not flush"))
    case.writer = CasdoorInvitedLocalMembershipService(
        session_factory=lambda: session, configuration_factory=configuration_factory
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("prelock must not be reached")

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "prelock", forbidden)
    with pytest.raises(ValueError):
        persist(case)
    assert database(case) == before
