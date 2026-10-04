"""Actual shared invitation/B1/identity SQLite composition and caller rollback."""

from copy import copy
from dataclasses import fields, replace
from datetime import datetime
from time import monotonic
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core.casdoor.admission import AdmissionAction as Action
from core.casdoor.admission import AdmissionPlan, InvitationObservation
from core.casdoor.admission import SharedOwnerRequirement as Owner
from core.casdoor.auth_transactions import AuthMode
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from repositories.casdoor_account_preflight_repository_extend import (
    AccountPreflightConflict,
    CasdoorAccountPreflightRepository,
)
from services.account_activation_service import InvalidAccountInitializationError
from services.casdoor_invited_login_account_service_extend import CasdoorInvitedLoginAccountService
from services.entities.account_activation_entities import AccountSetup, InvitationLookup
from tests.unit_tests.services.test_account_invited_initialization import seed, service_for
from tests.unit_tests.services.test_casdoor_login_account_service_extend import env as base_env  # noqa: F401


@pytest.fixture
def env(base_env):  # noqa: F811
    session, context, key, *_ = base_env
    for model in (Tenant, TenantAccountJoin):
        metadata = sa.MetaData()
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if column.server_default is not None and str(column.server_default.arg) == "CURRENT_TIMESTAMP(0)":
                column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
        metadata.create_all(session.bind)
    return session, context, key


def setup_case(env, status=AccountStatus.PENDING, marker=None, bound=False):
    session, context, key = env
    account, tenant, quota, join = seed(session, status, marker)
    if bound:
        session.add(
            Identity(
                namespace_id=str(context.namespace_id),
                account_id=account.id,
                issuer=context.issuer,
                organization=context.organization,
                subject=context.subject,
                remote_email="old@example.test",
                email_verified=True,
                last_applied_json="{}",
                profile_sync_json="{}",
            )
        )
        session.commit()
    activation, effects, _ = service_for(
        sessionmaker(bind=session.bind, expire_on_commit=False), account, tenant, False
    )

    def eligible(email):
        assert not session.in_transaction()

    effects[2].get_freeze_type.side_effect = eligible
    service = CasdoorInvitedLoginAccountService(activation=activation)
    return {
        "env": env,
        "account": account,
        "tenant": tenant,
        "aid": account.id,
        "tid": tenant.id,
        "quota": quota,
        "join": join,
        "activation": activation,
        "effects": effects,
        "service": service,
    }


def prepare(case, **overrides):
    session, context, key = case["env"]
    with session.begin():
        snapshot = CasdoorAccountPreflightRepository(session).reconstruct(
            context, key, candidate_account_id=UUID(case["aid"])
        )
    setup = AccountSetup("Ready", "en-US", "UTC") if snapshot.account.initialized_at is None else None
    shared = case["activation"].prepare_invited_initialization(
        InvitationLookup(None, None, "real-token"), setup=setup, authenticated_account_id=None
    )
    bound = snapshot.exact_binding is not None
    owners = (Owner.INVITATION_ACTIVATION_PREPARE,)
    if setup:
        action = Action.INITIALIZE_BOUND if bound else Action.INITIALIZE_INVITED
        owners += (Owner.ACCOUNT_SETUP_PERSIST,)
    elif snapshot.account.status is not AccountStatus.ACTIVE:
        action = Action.ACTIVATE_BOUND if bound else Action.ACTIVATE_INVITED
        owners += (Owner.ACCOUNT_STATUS_ACTIVATION_PERSIST,)
    else:
        action = Action.USE_BOUND if bound else Action.USE_INVITED
    plan = AdmissionPlan(
        context,
        AuthMode.LOGIN,
        action,
        account_id=snapshot.account.account_id,
        setup=setup,
        required_shared_owners=owners,
    )
    invitation = InvitationObservation(
        context.namespace_id, context.subject, snapshot.account.account_id, snapshot.account.email, UUID(case["tid"])
    )
    kwargs = {
        "plan": plan,
        "preflight": snapshot,
        "invitation": invitation,
        "shared": shared,
        "deadline": monotonic() + 40,
    }
    kwargs.update(overrides)
    return case["service"]._prepare_invited_login(**kwargs)


def persist(case, prepared, **overrides):
    session, context, key = case["env"]
    kwargs = {"session": session, "context": context, "key": key}
    kwargs.update(overrides)
    return case["service"].persist_invited_login_account(prepared, **kwargs)


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED, AccountStatus.ACTIVE])
@pytest.mark.parametrize("marker", [None, datetime(2020, 1, 2)])
@pytest.mark.parametrize("bound", [False, True])
def test_actual_invitation_matrix_original_account_owners_preserve_other_state(env, status, marker, bound):
    case = setup_case(env, status, marker, bound)
    session = env[0]
    prepared = prepare(case)
    before_join = (case["join"].role, case["join"].current, case["join"].last_opened_at)
    with session.begin():
        result = persist(case, prepared)
        session.flush()
        with Session(session.bind) as reader:
            assert reader.get(Account, case["aid"]).initialized_at == marker
    assert result.account_id == UUID(case["aid"])
    assert result.workspace_id == UUID(case["tid"])
    with Session(session.bind) as reader:
        current = reader.get(Account, case["aid"])
        assert current.status is AccountStatus.ACTIVE
        assert current.initialized_at == marker if marker else current.initialized_at is not None
        assert current.name == ("Original" if marker else "Ready")
        assert current.password == "original"
        join = reader.get(TenantAccountJoin, case["join"].id)
        assert (join.role, join.current, join.last_opened_at) == before_join
        quota = reader.get(type(case["quota"]), case["quota"].id)
        assert (quota.total_quota, quota.used_quota) == (case["quota"].total_quota, case["quota"].used_quota)
        assert reader.scalar(sa.select(sa.func.count()).select_from(Account)) == 1
        assert reader.scalar(sa.select(sa.func.count()).select_from(Identity)) == 1
    case["effects"][2].get_freeze_type.assert_called_once()
    case["effects"][0].delete.assert_not_called()
    assert case["effects"][3].mock_calls == case["effects"][4].mock_calls == []


@pytest.mark.parametrize("failure", ["identity_trigger", "outer"])
def test_real_later_failure_rolls_back_account_and_earlier_caller_write(env, failure):
    case = setup_case(env)
    session = env[0]
    prepared = prepare(case)
    if failure == "identity_trigger":
        with session.bind.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TRIGGER reject_identity BEFORE INSERT ON casdoor_identity_extend "
                    "BEGIN SELECT RAISE(ABORT, 'identity rejected'); END"
                )
            )
    with pytest.raises((IntegrityError, RuntimeError)), session.begin():  # noqa: PT012
        session.connection().exec_driver_sql("BEGIN")
        session.execute(sa.update(Tenant).values(name="earlier caller"))
        persist(case, prepared)
        session.flush()
        raise RuntimeError("later outer barrier")
    with Session(session.bind) as reader:
        assert reader.get(Account, case["aid"]).initialized_at is None
        assert reader.get(Tenant, case["tid"]).name == "Invitation workspace"
        assert reader.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(case, prepared)
    if failure == "identity_trigger":
        with session.bind.begin() as connection:
            connection.execute(sa.text("DROP TRIGGER reject_identity"))
    fresh = prepare(case)
    with session.begin():
        persist(case, fresh)


@pytest.mark.parametrize("forgery", ["copy", "replace", "equal_fields", "foreign", "shared"])
def test_exact_private_issuance_and_original_shared_authenticity(env, forgery):
    case = setup_case(env)
    session = env[0]
    real = prepare(case)
    fake = copy(real) if forgery == "copy" else replace(real) if forgery == "replace" else real
    if forgery == "equal_fields":
        fake = type(real)(**{f.name: getattr(real, f.name) for f in fields(real) if f.init})
    receiver = case["service"]
    if forgery == "foreign":
        receiver = CasdoorInvitedLoginAccountService(activation=case["activation"])
    if forgery == "shared":
        fake = receiver._prepare_invited_login(
            plan=real.plan,
            preflight=real.preflight,
            invitation=real.invitation,
            shared=replace(real._shared),
            deadline=real.deadline,
        )
    with session.begin(), pytest.raises((AccountPreflightConflict, InvalidAccountInitializationError)):
        receiver.persist_invited_login_account(fake, session=session, context=env[1], key=env[2])
    with session.begin():
        assert session.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
        assert session.get(Account, case["aid"]).initialized_at is None
        persist(case, real)


@pytest.mark.parametrize("defect", ["missing", "nested", "new", "dirty", "deleted", "failed"])
def test_clean_root_rejects_before_sql_and_consumes(env, defect):
    case = setup_case(env)
    session = env[0]
    prepared = prepare(case)
    if defect != "missing":
        session.begin()
    if defect == "nested":
        session.begin_nested()
    elif defect == "new":
        session.add(Account(name="extra", email="extra@example.test"))
    elif defect == "dirty":
        case["account"].name = "dirty"
    elif defect == "deleted":
        session.delete(case["account"])
    elif defect == "failed":
        session.add(Account(name="bad", email=None))
        with pytest.raises(IntegrityError):
            session.flush()
    statements = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    sa.event.listen(session.bind, "before_cursor_execute", capture)
    try:
        with pytest.raises(AccountPreflightConflict):
            persist(case, prepared)
        assert statements == []
    finally:
        sa.event.remove(session.bind, "before_cursor_execute", capture)
        session.rollback()
    assert prepared._consumed


@pytest.mark.parametrize(
    "defect", ["action", "owners", "mode", "source", "account", "workspace", "email", "namespace", "subject", "setup"]
)
def test_consistency_matrix_denies_substitution(env, defect):
    case = setup_case(env)
    real = prepare(case)
    kwargs = {
        "plan": real.plan,
        "preflight": real.preflight,
        "invitation": real.invitation,
        "shared": real._shared,
        "deadline": real.deadline,
    }
    if defect == "action":
        kwargs["plan"] = replace(real.plan, action=Action.INITIALIZE_BOUND)
    elif defect == "owners":
        kwargs["plan"] = replace(
            real.plan, required_shared_owners=(Owner.BOUND_INITIALIZATION_PREPARE, Owner.ACCOUNT_SETUP_PERSIST)
        )
    elif defect == "mode":
        kwargs["plan"] = replace(real.plan, mode=AuthMode.LINK)
    elif defect == "source":
        kwargs["source"] = object()
    elif defect == "setup":
        kwargs["plan"] = replace(real.plan, setup=AccountSetup("Different", "en-US", "UTC"))
    else:
        field = {"account": "account_id", "workspace": "workspace_id", "namespace": "namespace_id"}.get(defect, defect)
        kwargs["invitation"] = replace(
            real.invitation, **{field: uuid4() if field.endswith("_id") else "other@example.test"}
        )
    with pytest.raises(AccountPreflightConflict):
        case["service"]._prepare_invited_login(**kwargs)


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (Account, "email", "other@example.test"),
        (Account, "name", "Fresh"),
        (Account, "status", AccountStatus.BANNED),
        (Account, "initialized_at", datetime(2022, 1, 1)),
        (Tenant, "status", "archive"),
        (Integration, "enabled", False),
        (Integration, "active_revision_id", str(uuid4())),
        (Namespace, "fence_epoch", 1),
    ],
)
def test_fresh_locked_database_observations_reject_drift(env, model, field, value):
    case = setup_case(env)
    prepared = prepare(case)
    session = env[0]
    with Session(session.bind) as writer, writer.begin():
        writer.execute(sa.update(model).values({field: value}))
    assert case["account"].name == "Original"
    with session.begin(), pytest.raises((AccountPreflightConflict, InvalidAccountInitializationError)):
        persist(case, prepared)
    with Session(session.bind) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0


@pytest.mark.parametrize("defect", ["context", "key", "expired"])
def test_authentic_attempt_context_and_deadline_failure_consumes(env, defect, monkeypatch):
    case = setup_case(env)
    prepared = prepare(case)
    kwargs = {}
    if defect == "context":
        kwargs["context"] = replace(env[1], fence_epoch=1)
    elif defect == "key":
        kwargs["key"] = replace(env[2], subject="other")
    else:
        monkeypatch.setattr(
            "services.casdoor_invited_login_account_service_extend.time.monotonic", lambda: prepared.deadline + 1
        )
    with env[0].begin(), pytest.raises(AccountPreflightConflict):
        persist(case, prepared, **kwargs)
    assert prepared._consumed


@pytest.mark.parametrize("deadline", [float("inf"), float("nan"), True, "later", -1, 10**1000])
def test_unbounded_deadline_denied(env, deadline):
    case = setup_case(env)
    with pytest.raises(AccountPreflightConflict):
        prepare(case, deadline=deadline)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("marker", [None, datetime(2020, 1, 2)])
@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.ACTIVE])
def test_original_pure_policy_selects_exact_invitation_owner_tuple(env, bound, marker, status):
    from core.casdoor.admission import decide_admission
    from core.casdoor.claims import (
        StructuredUserRef,
        VerifiedIDToken,
        VerifiedNativeAccessToken,
        VerifiedOnlineUser,
        VerifiedProfile,
        VerifiedTokenBundle,
    )

    case = setup_case(env, status, marker, bound)
    real = prepare(case)
    context = env[1]
    plan = decide_admission(
        context=context,
        mode=AuthMode.LOGIN,
        tokens=VerifiedTokenBundle(
            VerifiedIDToken(context.issuer, context.subject, context.client_id, 1, 10),
            VerifiedNativeAccessToken(context.subject, context.organization, context.application, 1, 10),
        ),
        online=VerifiedOnlineUser(context.subject, StructuredUserRef(context.organization, "directory")),
        profile=VerifiedProfile(context.subject, real.invitation.email.upper(), True, "Ready", "en-US", "UTC"),
        account=real.preflight.account,
        binding=real.preflight.exact_binding,
        invitation=real.invitation,
    )
    assert plan == real.plan
    # Removing invitation eligibility, even from a bound plan, must fail here.
    with pytest.raises(AccountPreflightConflict):
        case["service"]._prepare_invited_login(
            plan=replace(plan, required_shared_owners=plan.required_shared_owners[1:]),
            preflight=real.preflight,
            invitation=real.invitation,
            shared=real._shared,
            deadline=real.deadline,
        )


@pytest.mark.parametrize("defect", ["reverse_subject", "exact_other_account", "missing_workspace", "same_email_other"])
def test_exact_uuid_selection_and_reciprocal_binding_never_email_merge(env, defect):
    case = setup_case(env)
    real = prepare(case)
    session, context, _ = env
    with Session(session.bind) as writer, writer.begin():
        other = Account(name="Other", email=real.invitation.email)
        writer.add(other)
        writer.flush()
        other_id = other.id
        if defect in ("reverse_subject", "exact_other_account"):
            writer.add(
                Identity(
                    namespace_id=str(context.namespace_id),
                    account_id=case["aid"] if defect == "reverse_subject" else other_id,
                    issuer=context.issuer,
                    organization=context.organization,
                    subject="Other" if defect == "reverse_subject" else context.subject,
                    last_applied_json="{}",
                    profile_sync_json="{}",
                )
            )
        elif defect == "missing_workspace":
            writer.execute(sa.delete(Tenant).where(Tenant.id == case["tid"]))
    if defect == "same_email_other":
        with session.begin():
            assert persist(case, real).account_id == UUID(case["aid"])
        with Session(session.bind) as reader:
            assert reader.get(Account, other_id).initialized_at is None
    else:
        with session.begin(), pytest.raises(AccountPreflightConflict):
            persist(case, real)


def test_bound_active_noop_is_select_only_and_preserves_remote_snapshot(env):
    case = setup_case(env, AccountStatus.ACTIVE, datetime(2020, 1, 2), True)
    real = prepare(case)
    statements = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    sa.event.listen(env[0].bind, "before_cursor_execute", capture)
    try:
        with env[0].begin():
            persist(case, real)
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        sa.event.remove(env[0].bind, "before_cursor_execute", capture)
    with Session(env[0].bind) as reader:
        assert reader.scalar(sa.select(Identity.remote_email)) == "old@example.test"
    case["effects"][2].get_freeze_type.assert_called_once()


@pytest.mark.parametrize(
    "defect", ["foreign_shared", "consumed_shared", "uninvited", "none_shared", "different_context"]
)
def test_no_receipt_or_wrong_shared_owner_is_not_invitation_authority(env, defect):
    case = setup_case(env)
    real = prepare(case)
    kwargs = {
        "plan": real.plan,
        "preflight": real.preflight,
        "invitation": real.invitation,
        "shared": real._shared,
        "deadline": real.deadline,
    }
    if defect == "foreign_shared":
        activation, _, _ = service_for(sessionmaker(bind=env[0].bind), case["account"], case["tenant"])
        kwargs["shared"] = activation.prepare_invited_initialization(
            InvitationLookup(None, None, "real-token"), setup=real.plan.setup, authenticated_account_id=None
        )
    elif defect == "consumed_shared":
        with env[0].begin():
            persist(case, real)
    elif defect == "uninvited":
        kwargs["invitation"] = None
    elif defect == "none_shared":
        kwargs["shared"] = None
    else:
        kwargs["preflight"] = replace(real.preflight, context=replace(env[1], config_digest="b" * 64))
    with pytest.raises(AccountPreflightConflict):
        case["service"]._prepare_invited_login(**kwargs)
