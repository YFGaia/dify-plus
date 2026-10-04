"""Offline actual-owner composition; SQLite is not distributed admission proof."""

from copy import copy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from time import monotonic
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction as Action
from core.casdoor.admission import AdmissionContext, AdmissionPlan
from core.casdoor.admission import SharedOwnerRequirement as Owner
from core.casdoor.auth_transactions import AuthMode
from enums import DeploymentEdition
from models.account import Account, AccountStatus
from models.account_money_extend import AccountMoneyExtend as Quota
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorNamespaceLifecycle
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from repositories.casdoor_account_preflight_repository_extend import (
    AccountCollisionUnknown,
    AccountPreflightConflict,
    CasdoorAccountPreflightRepository,
    CollisionKnowledge,
)
from repositories.casdoor_identity_repository_extend import (
    CasdoorIdentityConflict,
    CasdoorIdentityRepository,
    VerifiedIdentityKey,
)
from services import account_service
from services.account_activation_service import AccountActivationService, FrozenAccountError
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from services.entities.account_activation_entities import AccountSetup
from services.errors.account import SeatsLimitExceededError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'login.sqlite'}")
    metadata = sa.MetaData()
    for model in (Account, Quota, Integration, Namespace, Revision, Identity):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if column.server_default is not None and str(column.server_default.arg) == "CURRENT_TIMESTAMP(0)":
                column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        chain = dict(
            expected_issuer="https://issuer.example.test", organization="Org", application="App", client_id="C"
        )
        integration = Integration(enabled=True)
        session.add(integration)
        session.flush()
        namespace = Namespace(integration_id=integration.id, core_fingerprint="b" * 64, **chain)
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url=chain["expected_issuer"],
            backend_api_url=chain["expected_issuer"],
            button_text="SSO",
            default_workspace_id=str(uuid4()),
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
            **chain,
        )
        session.add(revision)
        session.flush()
        integration.active_revision_id = revision.id
        session.commit()
        context = AdmissionContext(
            UUID(integration.id),
            UUID(revision.id),
            UUID(revision.id),
            UUID(namespace.id),
            CasdoorNamespaceLifecycle.ACTIVE,
            0,
            "a" * 64,
            chain["expected_issuer"],
            "Org",
            "App",
            "C",
            "Subject",
        )
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        effects = [Mock() for _ in range(5)]
        tokens, policy, eligibility, cache, sync = effects

        def eligible(email):
            assert not session.in_transaction(), "shared freeze inside caller UoW"
            return None

        eligibility.get_freeze_type.side_effect = eligible
        activation = AccountActivationService(
            tokens=tokens,
            accounts=SQLAlchemyAccountActivationRepository(None),
            workspace_policy=policy,
            eligibility=eligibility,
            membership_cache=cache,
            member_access_sync=sync,
        )
        features, billing = Mock(), Mock()

        def license_check():
            assert not session.in_transaction(), "shared seat eligibility inside caller UoW"
            return True

        features.get_license.return_value.seats.is_available.side_effect = license_check
        billing.is_email_in_freeze.side_effect = lambda email: eligible(email) or False
        monkeypatch.setattr(account_service, "SystemFeatureService", features)
        monkeypatch.setattr(account_service, "BillingService", billing)
        monkeypatch.setattr(account_service.dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.CLOUD)
        monkeypatch.setattr(account_service.dify_config, "ACCOUNT_TOTAL_QUOTA", Decimal("15"))
        service = CasdoorLoginAccountService(activation=activation)
        yield session, context, key, service, effects, features, billing
        for effect in (tokens, policy, cache, sync):
            assert effect.mock_calls == []
    engine.dispose()


def seed(env, status=AccountStatus.PENDING, marker=None):
    session, context, key, *_ = env
    account = Account(
        name="Preserved",
        email="bound@example.test",
        status=status,
        initialized_at=marker,
        interface_language="ja-JP",
        timezone="Asia/Tokyo",
        interface_theme="dark",
    )
    session.add(account)
    session.flush()
    session.add(Quota(account_id=account.id, total_quota=Decimal("91.25"), used_quota=Decimal("4.5")))
    identity = Identity(
        namespace_id=str(context.namespace_id),
        account_id=account.id,
        issuer=context.issuer,
        organization=context.organization,
        subject=key.subject,
        remote_email="old-remote@example.test",
        email_verified=True,
        last_applied_json="{}",
        profile_sync_json="{}",
    )
    session.add(identity)
    session.commit()
    return account, identity


def inputs(env, action=Action.CREATE_INITIALIZED):
    session, context, key, *_ = env
    with session.begin():
        snapshot = CasdoorAccountPreflightRepository(session).reconstruct(
            context,
            key,
            collision_email="new@example.test" if action is Action.CREATE_INITIALIZED else None,
        )
    setup = AccountSetup("Ready", "en-US", "UTC")
    if action is Action.CREATE_INITIALIZED:
        plan = AdmissionPlan(
            context,
            AuthMode.LOGIN,
            action,
            creation_email="new@example.test",
            setup=setup,
            required_shared_owners=(Owner.ACCOUNT_CREATION_PREPARE, Owner.ACCOUNT_SETUP_PERSIST),
        )
    else:
        owners = (
            ()
            if action is Action.USE_BOUND
            else (
                Owner.BOUND_INITIALIZATION_PREPARE,
                Owner.ACCOUNT_SETUP_PERSIST
                if action is Action.INITIALIZE_BOUND
                else Owner.ACCOUNT_STATUS_ACTIVATION_PERSIST,
            )
        )
        plan = AdmissionPlan(
            context,
            AuthMode.LOGIN,
            action,
            account_id=snapshot.account.account_id,
            setup=setup if action is Action.INITIALIZE_BOUND else None,
            required_shared_owners=owners,
        )
    return plan, snapshot


def prepare(env, action=Action.CREATE_INITIALIZED):
    plan, preflight = inputs(env, action)
    return env[3]._prepare_login(plan=plan, preflight=preflight, deadline=monotonic() + 40)


def persist(env, prepared):
    session, context, key, service, *_ = env
    return service.persist_login_account(prepared, session=session, context=context, key=key)


def counts(engine):
    with Session(engine) as reader:
        return tuple(
            reader.scalar(sa.select(sa.func.count()).select_from(model)) for model in (Account, Quota, Identity)
        )


def test_new_original_owners_stable_uuid_one_quota_and_no_transaction_ownership(env, monkeypatch):
    session, _, _, _, effects, features, billing = env
    setup_owner = Mock(wraps=SQLAlchemyAccountActivationRepository.persist_account_setup)
    monkeypatch.setattr(SQLAlchemyAccountActivationRepository, "persist_account_setup", setup_owner)
    prepared = prepare(env)
    original_id = prepared._shared.account_id
    assert prepared.account_id == UUID(original_id)
    features.get_license.return_value.seats.is_available.assert_called_once()
    billing.is_email_in_freeze.assert_called_once_with("new@example.test")
    effects[2].get_freeze_type.assert_not_called()
    with session.begin():
        with monkeypatch.context() as patch:
            for method in ("commit", "rollback"):
                patch.setattr(session, method, Mock(side_effect=AssertionError("transaction lifetime stolen")))
            result = persist(env, prepared)
            assert result.account_id == UUID(original_id)
            assert counts(session.bind) == (0, 0, 0)
    setup_owner.assert_called_once()
    assert counts(session.bind) == (1, 1, 1)
    with Session(session.bind) as reader:
        account = reader.get(Account, original_id)
        assert (account.name, account.interface_language, account.timezone, account.interface_theme) == (
            "Ready",
            "en-US",
            "UTC",
            "light",
        )
        assert account.status is AccountStatus.ACTIVE and account.initialized_at is not None
        assert account.password is None and account.last_login_at is None
        quota = reader.scalar(sa.select(Quota))
        assert (quota.total_quota, quota.used_quota) == (15, 0)
        assert reader.get(Identity, str(result.identity_id)).account_id == original_id


@pytest.mark.parametrize("failure", ["outer", "identity"])
def test_new_failure_rolls_back_every_owner_and_requires_reprepare(env, monkeypatch, failure):
    session = env[0]
    prepared = prepare(env)
    original_bind = CasdoorIdentityRepository.bind

    def broken_bind(self, key, **kwargs):
        # A later owner changes a namespace fence after account/quota/setup.
        # The actual identity owner must reject and the whole caller rolls back.
        if failure == "identity":
            self._session.execute(sa.update(Namespace).values(fence_epoch=1))
        result = original_bind(self, key, **kwargs)
        assert self._session.scalar(sa.select(sa.func.count()).select_from(Quota)) == 1
        return result

    with monkeypatch.context() as patch:
        patch.setattr(CasdoorIdentityRepository, "bind", broken_bind)
        with pytest.raises((RuntimeError, CasdoorIdentityConflict)), session.begin():
            persist(env, prepared)
            raise RuntimeError("outer precommit conflict")
    assert counts(session.bind) == (0, 0, 0)
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(env, prepared)
    fresh = prepare(env)
    assert fresh.account_id != prepared.account_id
    with session.begin():
        persist(env, fresh)
    assert counts(session.bind) == (1, 1, 1)


@pytest.mark.parametrize("status", [AccountStatus.PENDING, AccountStatus.UNINITIALIZED, AccountStatus.ACTIVE])
@pytest.mark.parametrize("marker", [None, datetime(2020, 1, 2)])
def test_bound_owner_selection_rollback_retry_preserves_quota_and_metadata(env, monkeypatch, status, marker):
    session, _, _, _, effects, features, billing = env
    account, identity = seed(env, status, marker)
    aid, iid = account.id, identity.id
    action = (
        Action.INITIALIZE_BOUND
        if marker is None
        else (Action.USE_BOUND if status is AccountStatus.ACTIVE else Action.ACTIVATE_BOUND)
    )
    setup_owner = Mock(wraps=SQLAlchemyAccountActivationRepository.persist_account_setup)
    monkeypatch.setattr(SQLAlchemyAccountActivationRepository, "persist_account_setup", setup_owner)
    prepared = prepare(env, action)
    with pytest.raises(RuntimeError), session.begin():
        result = persist(env, prepared)
        assert result.identity_id == UUID(iid)
        raise RuntimeError("later owner rejected")
    with Session(session.bind) as reader:
        original = reader.get(Account, aid)
        assert (original.status, original.initialized_at, original.name) == (status, marker, "Preserved")
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(env, prepared)
    fresh = prepare(env, action)
    with session.begin():
        persist(env, fresh)
    assert counts(session.bind) == (1, 1, 1)
    with Session(session.bind) as reader:
        current = reader.get(Account, aid)
        assert current.status is AccountStatus.ACTIVE
        assert current.initialized_at == marker if marker else current.initialized_at is not None
        assert (current.name, current.interface_language, current.timezone, current.interface_theme) == (
            ("Ready", "en-US", "UTC", "light") if marker is None else ("Preserved", "ja-JP", "Asia/Tokyo", "dark")
        )
        quota = reader.scalar(sa.select(Quota))
        assert (quota.total_quota, quota.used_quota) == (Decimal("91.25"), Decimal("4.5"))
        assert reader.get(Identity, iid).remote_email == "old-remote@example.test"
    assert setup_owner.call_count == (2 if marker is None else 0)
    assert effects[2].get_freeze_type.call_count == (0 if action is Action.USE_BOUND else 2)
    features.get_license.assert_not_called()
    billing.is_email_in_freeze.assert_not_called()


@pytest.mark.parametrize("defect", ["missing", "nested", "new", "dirty", "deleted", "failed"])
def test_invalid_root_consumes_authentic_preparation_with_zero_sql(env, defect):
    session = env[0]
    account, _ = seed(env)
    prepared = prepare(env, Action.INITIALIZE_BOUND)
    if defect != "missing":
        session.begin()
    if defect == "nested":
        session.begin_nested()
    elif defect == "new":
        session.add(Account(name="Extra", email="extra@example.test"))
    elif defect == "dirty":
        account.name = "Dirty"
    elif defect == "deleted":
        session.delete(account)
    elif defect == "failed":
        session.add(Account(name="Bad", email=None))
        with pytest.raises(IntegrityError):
            session.flush()
    sql = []

    def capture(conn, cursor, statement, *args):
        sql.append(statement)

    sa.event.listen(session.bind, "before_cursor_execute", capture)
    try:
        with pytest.raises(AccountPreflightConflict):
            persist(env, prepared)
        assert sql == []
    finally:
        sa.event.remove(session.bind, "before_cursor_execute", capture)
        session.rollback()
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(env, prepared)


@pytest.mark.parametrize("defect", ["copy", "replace", "foreign", "expired", "context", "key"])
def test_private_preparation_identity_owner_deadline_and_attempt_guards(env, defect, monkeypatch):
    session, context, key, service, *_ = env
    prepared = prepare(env)
    bad = copy(prepared) if defect == "copy" else replace(prepared) if defect == "replace" else prepared
    receiver = CasdoorLoginAccountService(activation=service._activation) if defect == "foreign" else service
    if defect == "expired":
        monkeypatch.setattr(
            "services.casdoor_login_account_service_extend.time.monotonic", lambda: prepared.deadline + 1
        )
    with session.begin(), pytest.raises(AccountPreflightConflict):
        receiver.persist_login_account(
            bad,
            session=session,
            context=replace(context, fence_epoch=1) if defect == "context" else context,
            key=replace(key, subject="Other") if defect == "key" else key,
        )
    assert counts(session.bind) == (0, 0, 0)
    if defect in ("copy", "replace", "foreign"):
        with session.begin():
            persist(env, prepared)
    else:
        assert prepared._consumed


@pytest.mark.parametrize("defect", ["unchecked", "collision", "invite", "source", "mode", "action", "owners", "setup"])
def test_invalid_plan_or_preflight_rejects_before_any_shared_eligibility(env, defect):
    plan, snapshot = inputs(env)
    kwargs = {}
    if defect == "unchecked":
        snapshot = replace(snapshot, collision_knowledge=CollisionKnowledge.UNCHECKED)
    elif defect == "collision":
        snapshot = replace(snapshot, collision_account_ids=(uuid4(),))
    elif defect in ("invite", "source"):
        kwargs["invitation" if defect == "invite" else "source"] = object()
    elif defect == "mode":
        plan = replace(plan, mode=AuthMode.DIAGNOSTIC)
    elif defect == "action":
        plan = replace(plan, action=Action.INITIALIZE_INVITED)
    elif defect == "owners":
        plan = replace(plan, required_shared_owners=(Owner.INVITATION_ACTIVATION_PREPARE,))
    else:
        plan = replace(plan, setup=AccountSetup("Ready", "bogus", "UTC"))
    with pytest.raises(AccountPreflightConflict):
        env[3]._prepare_login(plan=plan, preflight=snapshot, deadline=monotonic() + 40, **kwargs)
    env[5].get_license.assert_not_called()
    env[4][2].get_freeze_type.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", AccountStatus.BANNED),
        ("status", AccountStatus.CLOSED),
        ("status", "unknown"),
        ("status", AccountStatus.UNINITIALIZED),
        ("email", "changed@example.test"),
        ("initialized_at", datetime(2021, 1, 1)),
    ],
)
def test_current_b1_denies_stale_identity_map_observations(env, field, value):
    session = env[0]
    account, _ = seed(env)
    prepared = prepare(env, Action.INITIALIZE_BOUND)
    with Session(session.bind) as writer, writer.begin():
        if value == "unknown":
            writer.execute(sa.text("UPDATE accounts SET status = :status"), {"status": value})
        else:
            writer.execute(sa.update(Account).values({field: value}))
    assert account.email == "bound@example.test" and account.status is AccountStatus.PENDING
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(env, prepared)
    assert counts(session.bind) == (1, 1, 1)


def test_fresh_orm_preload_preserves_current_preferences_on_status_only(env):
    session = env[0]
    account, _ = seed(env, marker=datetime(2020, 1, 1))
    aid = account.id
    prepared = prepare(env, Action.ACTIVATE_BOUND)
    with Session(session.bind) as writer, writer.begin():
        writer.execute(sa.update(Account).values(name="Fresh", timezone="Europe/London", interface_theme="system"))
    assert account.name == "Preserved"
    with session.begin():
        persist(env, prepared)
        assert account.name == "Fresh"
    with Session(session.bind) as reader:
        fresh = reader.get(Account, aid)
        assert (fresh.name, fresh.timezone, fresh.interface_theme) == ("Fresh", "Europe/London", "system")


@pytest.mark.parametrize(
    "model,field,value",
    [
        (Integration, "enabled", False),
        (Integration, "active_revision_id", str(uuid4())),
        (Namespace, "fence_epoch", 1),
        (Namespace, "lifecycle", "fencing"),
        (Revision, "config_digest", "c" * 64),
        (Identity, "account_id", str(uuid4())),
        (Identity, "subject", "Other"),
    ],
)
def test_owner_chain_and_binding_drift_aborts_before_writes(env, model, field, value):
    session = env[0]
    seed(env)
    prepared = prepare(env, Action.INITIALIZE_BOUND)
    with Session(session.bind) as writer, writer.begin():
        writer.execute(sa.update(model).values({field: value}))
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(env, prepared)
    with Session(session.bind) as reader:
        assert reader.scalar(sa.select(Account.initialized_at)) is None


@pytest.mark.parametrize("cause", ["collision", "overlimit", "candidate"])
def test_new_current_preflight_rechecks_absence_and_complete_collision_scan(env, monkeypatch, cause):
    session = env[0]
    prepared = prepare(env)
    with Session(session.bind) as writer, writer.begin():
        other = Account(
            name="Other",
            email="new@example.test" if cause == "collision" else "other@example.test",
        )
        if cause == "candidate":
            other.id = str(prepared.account_id)
        writer.add(other)
    if cause == "overlimit":
        monkeypatch.setattr("repositories.casdoor_account_preflight_repository_extend.MAX_COLLISION_ACCOUNTS", 0)
    with session.begin(), pytest.raises((AccountPreflightConflict, AccountCollisionUnknown)):
        persist(env, prepared)
    assert counts(session.bind) == (1, 0, 0)


def test_shared_freeze_and_seat_denials_are_not_bypassed(env):
    plan, snapshot = inputs(env)
    env[5].get_license.return_value.seats.is_available.side_effect = lambda: False
    with pytest.raises(SeatsLimitExceededError):
        env[3]._prepare_login(plan=plan, preflight=snapshot, deadline=monotonic() + 40)
    seed(env)
    plan, snapshot = inputs(env, Action.INITIALIZE_BOUND)
    env[4][2].get_freeze_type.side_effect = lambda email: "frozen"
    with pytest.raises(FrozenAccountError):
        env[3]._prepare_login(plan=plan, preflight=snapshot, deadline=monotonic() + 40)
    assert counts(env[0].bind) == (1, 1, 1)


@pytest.mark.parametrize("field,value", [("email", "raced@example.test"), ("initialized_at", datetime(2022, 1, 1))])
def test_orm_refresh_must_match_just_observed_b1_before_bound_assignments(env, monkeypatch, field, value):
    session = env[0]
    account, _ = seed(env)
    prepared = prepare(env, Action.INITIALIZE_BOUND)
    original = CasdoorAccountPreflightRepository.reconstruct

    def observe_then_change(self, *args, **kwargs):
        snapshot = original(self, *args, **kwargs)
        self._session.connection().execute(sa.update(Account).values({field: value}))
        return snapshot

    monkeypatch.setattr(CasdoorAccountPreflightRepository, "reconstruct", observe_then_change)
    setup = Mock(side_effect=AssertionError("setup before fresh consistency"))
    monkeypatch.setattr(SQLAlchemyAccountActivationRepository, "persist_account_setup", setup)
    with pytest.raises(AccountPreflightConflict), session.begin():
        persist(env, prepared)
    setup.assert_not_called()
    assert counts(session.bind) == (1, 1, 1)


def test_use_bound_only_reads_and_preserves_exact_association_snapshot(env):
    session = env[0]
    account, identity = seed(env, AccountStatus.ACTIVE, datetime(2020, 1, 1))
    aid, iid = account.id, identity.id
    prepared = prepare(env, Action.USE_BOUND)
    sql = []

    def capture(conn, cursor, statement, *args):
        sql.append(statement)

    sa.event.listen(session.bind, "before_cursor_execute", capture)
    try:
        with session.begin():
            result = persist(env, prepared)
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in sql)
        assert result.account_id == UUID(aid) and result.identity_id == UUID(iid)
    finally:
        sa.event.remove(session.bind, "before_cursor_execute", capture)


@pytest.mark.parametrize("deadline", [float("inf"), float("nan"), True, "later", -1, 10**1000])
def test_invalid_deadline_rejected_before_eligibility(env, deadline):
    plan, snapshot = inputs(env)
    with pytest.raises(AccountPreflightConflict):
        env[3]._prepare_login(plan=plan, preflight=snapshot, deadline=deadline)
    env[5].get_license.assert_not_called()


def test_eligibility_outlasting_attempt_does_not_issue_receipt(env, monkeypatch):
    plan, snapshot = inputs(env)
    deadline = monotonic() + 40

    def slow_eligibility():
        assert not env[0].in_transaction()
        monkeypatch.setattr("services.casdoor_login_account_service_extend.time.monotonic", lambda: deadline + 1)
        return True

    env[5].get_license.return_value.seats.is_available.side_effect = slow_eligibility
    with pytest.raises(AccountPreflightConflict):
        env[3]._prepare_login(plan=plan, preflight=snapshot, deadline=deadline)
    assert len(env[3]._issued) == 0
    assert counts(env[0].bind) == (0, 0, 0)
