"""Actual signed C2/C1/LOCAL finalizer/Passport chain, offline SQLite/Redis wire."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
import sqlalchemy as sa
from configs import dify_config
from enums import DeploymentEdition
from models.account import Account, AccountStatus, TenantAccountJoin
from models.casdoor_extend import CasdoorFinalizationState as Finalization
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from services.account_login_adapters import RedisAccountSessionGateway
from services.account_service import AccountService
from services.casdoor_local_login_finalization_service_extend import CasdoorLocalLoginFinalizationService
from sqlalchemy.orm import Session
from test_casdoor_login_account_service_extend import seed

pytest_plugins = ["test_casdoor_local_login_coordinator_service_extend"]


def attach_finalizer(chain, monkeypatch):
    monkeypatch.setattr(dify_config, "SECRET_KEY", "offline-finalizer-signing-key-only")
    calls = []
    control = SimpleNamespace(fail=None, hook=lambda: None)
    cache_calls = []

    class BillingWire:
        def delete(self, key):
            assert not chain.opened and chain.redis.data
            cache_calls.append(key)
            return 0

    monkeypatch.setattr("services.billing_service.redis_client", BillingWire())

    class RedisWire:
        def setex(self, key, ttl, value):
            assert not chain.opened and chain.redis.data
            control.hook()
            calls.append((key, ttl, value))
            if len(calls) == control.fail:
                raise TimeoutError("synthetic token command timeout")

        def delete(self, *args):
            pytest.fail("account-wide revoke is not rollback")

    service = CasdoorLocalLoginFinalizationService(
        session_factory=chain.coordinator._session_factory,
        configuration_factory=chain.configuration_service._repository,
        session_gateway=RedisAccountSessionGateway(redis=RedisWire()),
    )
    chain.coordinator._finalization = service
    return SimpleNamespace(chain=chain, service=service, calls=calls, control=control, cache_calls=cache_calls)


@pytest.fixture
def finalized_chain(chain, monkeypatch):
    return attach_finalizer(chain, monkeypatch)


@pytest.mark.parametrize("bound", ["new", "initialize", "activate", "use"])
def test_full_signed_chain_finalizes_then_original_issuer(finalized_chain, bound):
    f = finalized_chain
    if bound != "new":
        seed(
            f.chain.local.env,
            AccountStatus.ACTIVE if bound == "use" else AccountStatus.PENDING,
            None if bound == "initialize" else datetime(2025, 1, 1),
        )
    outcome = f.chain.invoke(ip_address="192.0.2.7")
    assert outcome.cleanup_released and outcome.local_outcome == "committed"
    assert outcome.finalization_outcome == "committed" and outcome.token_outcome == "issued"
    pair = outcome.tokens
    claims = jwt.decode(pair.access_token, dify_config.SECRET_KEY, algorithms=["HS256"])
    assert claims["user_id"] == str(outcome.persistence.account_id)
    assert claims["sub"] == "Console API Passport"
    assert claims["iss"] == dify_config.DEPLOYMENT_EDITION.value
    assert pair.csrf_token and len(pair.refresh_token) == 128
    assert len(f.calls) == 2
    assert f.calls[0] == (
        "refresh_token:" + pair.refresh_token,
        timedelta(days=dify_config.REFRESH_TOKEN_EXPIRE_DAYS),
        claims["user_id"],
    )
    assert f.calls[1][0] == "account_refresh_token:" + claims["user_id"]
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.FINALIZED}
        account = s.get(Account, claims["user_id"])
        assert account.last_login_at and account.last_login_ip == "192.0.2.7"
        current = s.scalar(sa.select(TenantAccountJoin).where(TenantAccountJoin.current.is_(True)))
        assert current.id == s.scalar(sa.select(TenantAccountJoin.id).order_by(TenantAccountJoin.id).limit(1))
    assert not f.chain.redis.data


@pytest.mark.parametrize("fail", [1, 2])
def test_partial_original_issuer_keeps_finalized_db_and_never_revokes(finalized_chain, fail):
    f = finalized_chain
    f.control.fail = fail
    with pytest.raises(TimeoutError) as error:
        f.chain.invoke(ip_address="192.0.2.7")
    assert error.value.local_outcome == "committed"
    assert error.value.finalization_outcome == "committed"
    assert error.value.token_outcome == "unknown"
    assert error.value.cleanup_released
    assert len(f.calls) == fail
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.FINALIZED}
        assert s.scalar(sa.select(Account.last_login_ip)) == "192.0.2.7"


def test_cloud_cache_failure_keeps_c1_pending(finalized_chain, monkeypatch):
    from services.billing_service import BillingService

    f = finalized_chain
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.CLOUD)
    calls = []

    def cache(workspace):
        assert not f.chain.opened and f.chain.redis.data
        calls.append(workspace)
        raise TimeoutError("synthetic billing cache timeout")

    monkeypatch.setattr(BillingService, "clean_billing_info_cache", cache)
    with pytest.raises(TimeoutError) as error:
        f.chain.invoke(ip_address="192.0.2.7")
    assert error.value.local_outcome == "committed" and error.value.finalization_outcome == "not_started"
    assert calls and not f.calls
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.PENDING}
        assert s.scalar(sa.select(Account.last_login_at)) is None


def test_second_workspace_trigger_rolls_back_whole_i19_only(finalized_chain):
    f = finalized_chain
    with f.chain.local.engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TRIGGER reject_finalization BEFORE UPDATE OF finalization
            ON casdoor_managed_membership_extend WHEN NEW.workspace_id LIKE '%00c8'
            BEGIN SELECT RAISE(ABORT, 'offline second workspace'); END""")
    with pytest.raises(sa.exc.IntegrityError) as error:
        f.chain.invoke(ip_address="192.0.2.7")
    assert error.value.local_outcome == "committed" and not f.calls
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.PENDING}
        assert s.scalar(sa.select(Account.last_login_at)) is None
        assert s.scalar(sa.select(sa.func.count()).select_from(Account)) == 1


def test_legacy_login_info_helper_keeps_original_commit_behavior(monkeypatch):
    fixed = datetime(2025, 1, 2)
    monkeypatch.setattr("services.account_service.naive_utc_now", lambda: fixed)
    account = Account(name="Offline", email="offline@example.test")
    session = Mock()
    AccountService.persist_login_info(account, ip_address="192.0.2.8")
    assert account.last_login_at == fixed and account.last_login_ip == "192.0.2.8"
    session.commit.assert_not_called()
    AccountService.update_login_info(account, session, ip_address="192.0.2.9")
    session.add.assert_called_once_with(account)
    session.commit.assert_called_once_with()
    assert account.last_login_at == fixed and account.last_login_ip == "192.0.2.9"


def new_authorization(chain):
    from core.casdoor.gateway import CasdoorTokenGateway, GatewayOperation
    from test_gateway import SyntheticStrategy

    context = chain.consumed.context
    kwargs = chain.consume_args
    created = chain.store.create(
        context, browser_scope=kwargs["browser_scope"], policy=kwargs["policy"], guard=kwargs["guard"]
    )
    consumed = chain.store.consume(created.state, **(kwargs | {"transaction_cookie": created.cookie.value}))
    chain.control.bad_id = {"nonce": consumed.nonce}
    old = chain.operation
    operation = GatewayOperation(
        config=old.config,
        client_secret=old.client_secret,
        registered_redirect_uri=old.registered_redirect_uri,
        deadline=old.deadline,
    )
    raw = CasdoorTokenGateway(operation).exchange_code("offline-fresh-code", consumed.code_verifier)
    return chain.coordinator._coordinate_local_login(
        consumed=consumed,
        raw_tokens=raw,
        operation=operation,
        native_contract=chain.native,
        directory_contract=chain.directory,
        credential_strategy=SyntheticStrategy(operation),
        correlation_id=chain.prepared[0].account_id,
        ip_address="192.0.2.8",
    )


@pytest.mark.parametrize("first_failure", ["cache", "token", "none"])
@pytest.mark.parametrize("role", ["same", "withdrawal"])
def test_fresh_authorization_recovers_without_duplicate_business(finalized_chain, first_failure, role, monkeypatch):
    from models.account_money_extend import AccountMoneyExtend
    from models.casdoor_extend import CasdoorIdentityExtend
    from services.billing_service import BillingService

    f = finalized_chain
    original_cache = BillingService.clean_billing_info_cache
    if first_failure == "cache":
        monkeypatch.setattr(BillingService, "clean_billing_info_cache", Mock(side_effect=TimeoutError("offline cache")))
    if first_failure == "token":
        f.control.fail = 2
    if first_failure == "none":
        f.chain.invoke(ip_address="192.0.2.7")
    else:
        with pytest.raises(TimeoutError):
            f.chain.invoke(ip_address="192.0.2.7")
    monkeypatch.setattr(BillingService, "clean_billing_info_cache", original_cache)
    f.control.fail = None
    before_cache = len(f.cache_calls)
    if role != "same":
        # The mapping target remains configured. Known-empty roles legally
        # withdraw a managed target and C1 must deny, not invent removal here.
        f.chain.roles[0]["users"] = []
    if role != "same":
        with pytest.raises(ValueError):
            new_authorization(f.chain)
        assert len(f.cache_calls) == before_cache
        return
    outcome = new_authorization(f.chain)
    assert outcome.tokens and outcome.persistence.generation == 2
    assert len(f.chain.prepared) == 2
    assert len(f.cache_calls) - before_cache == (2 if first_failure == "cache" else 0)
    with Session(f.chain.local.engine) as s:
        for model, count in ((Account, 1), (AccountMoneyExtend, 1), (CasdoorIdentityExtend, 1), (TenantAccountJoin, 2)):
            assert s.scalar(sa.select(sa.func.count()).select_from(model)) == count
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.FINALIZED}


DRIFTS = [
    "disabled",
    "revision",
    "namespace",
    "generation",
    "subject",
    "banned",
    "closed",
    "marker",
    "join_role",
    "join_deleted",
    "epoch",
    "source",
    "baseline",
    "desired",
    "fingerprint",
    "tombstone",
    "manual",
]


def mutate_database(chain, kind):
    from models.account import TenantAccountRole
    from models.casdoor_extend import CasdoorIdentityExtend, CasdoorIntegrationExtend, CasdoorNamespaceExtend

    with Session(chain.local.engine) as s, s.begin():
        if kind in ("disabled", "revision"):
            s.execute(
                sa.update(CasdoorIntegrationExtend).values(
                    **({"enabled": False} if kind == "disabled" else {"active_revision_id": None})
                )
            )
        elif kind == "namespace":
            s.execute(sa.update(CasdoorNamespaceExtend).values(fence_epoch=1))
        elif kind in ("generation", "subject"):
            s.execute(
                sa.update(CasdoorIdentityExtend).values(
                    **({"sync_generation": 2} if kind == "generation" else {"subject": "drift"})
                )
            )
        elif kind in ("banned", "closed", "marker"):
            s.execute(sa.update(Account).values(**({"initialized_at": None} if kind == "marker" else {"status": kind})))
        elif kind == "join_deleted":
            s.execute(sa.delete(TenantAccountJoin))
        elif kind == "join_role":
            s.execute(sa.update(TenantAccountJoin).values(role=TenantAccountRole.EDITOR))
        else:
            changes = {
                "epoch": {"ownership_epoch": 1},
                "source": {"source": "adopt"},
                "baseline": {"baseline_json": "{}"},
                "desired": {"desired_roles_json": "{}"},
                "fingerprint": {"last_applied_fingerprint": "a" * 64},
                "tombstone": {"tombstone": True},
                "manual": {"finalization": Finalization.MANUAL_RECOVERY},
            }
            s.execute(sa.update(History).values(**changes[kind]))


@pytest.mark.parametrize("kind", DRIFTS)
@pytest.mark.parametrize("stage", ["post_c1", "post_cache"])
def test_real_database_drift_denies_issue(finalized_chain, monkeypatch, kind, stage):
    from services.billing_service import BillingService

    f = finalized_chain
    if stage == "post_c1":
        original = f.chain.coordinator._local._persist_local_login

        def write(**kwargs):
            result = original(**kwargs)
            mutate_database(f.chain, kind)
            return result

        monkeypatch.setattr(f.chain.coordinator._local, "_persist_local_login", write)
    else:
        original = BillingService.clean_billing_info_cache
        invoked = False

        def cache(workspace):
            nonlocal invoked
            original(workspace)
            if not invoked:
                invoked = True
                mutate_database(f.chain, kind)

        monkeypatch.setattr(BillingService, "clean_billing_info_cache", cache)
    with pytest.raises(ValueError) as error:
        f.chain.invoke(ip_address="192.0.2.7")
    assert error.value.local_outcome == "committed" and error.value.cleanup_released
    assert not f.calls
    with Session(f.chain.local.engine) as s:
        assert s.scalar(sa.select(Account.last_login_at)) is None


@pytest.mark.parametrize("state", ["pending", "in_flight", "unknown", "applied", "failed", "cancelled"])
@pytest.mark.parametrize("scope", ["null", "linked"])
def test_all_state_real_nonavatar_intents_block(finalized_chain, monkeypatch, state, scope):
    from uuid import uuid4

    from models.casdoor_extend import CasdoorSyncIntentExtend

    f = finalized_chain
    original = f.chain.coordinator._local._persist_local_login

    def write(**kwargs):
        result = original(**kwargs)
        with Session(f.chain.local.engine) as s, s.begin():
            s.add(
                CasdoorSyncIntentExtend(
                    namespace_id=str(result.namespace_id),
                    identity_id=str(result.identity_id),
                    account_id=str(result.account_id),
                    workspace_id=None,
                    membership_id=str(result.workspaces[0].membership_id) if scope == "linked" else None,
                    revision_id=str(result.revision_id),
                    generation=0,
                    ownership_epoch=0,
                    fence_epoch=0,
                    kind="resource_grant",
                    scope_digest="a" * 64,
                    idempotency_key=uuid4().hex * 2,
                    desired_json="{}",
                    operation_state=state,
                    termination_state="confirmed",
                )
            )
        return result

    monkeypatch.setattr(f.chain.coordinator._local, "_persist_local_login", write)
    with pytest.raises(ValueError) as error:
        f.chain.invoke(ip_address="192.0.2.7")
    assert error.value.local_outcome == "committed" and not f.calls


@pytest.mark.parametrize("mode", ["ack_unknown", "after_commit_drift"])
def test_confirmed_finalization_commit_never_guesses_after_ack_or_read_drift(finalized_chain, mode):
    f = finalized_chain

    def flush(session, context):
        if any(isinstance(obj, Account) and obj.last_login_ip == "192.0.2.7" for obj in session.dirty):
            session.info["offline_i19"] = True

    def committed(session):
        if session.info.pop("offline_i19", False):
            if mode == "ack_unknown":
                raise RuntimeError("offline finalization ack unknown")
            mutate_database(f.chain, "banned")

    sa.event.listen(Session, "after_flush", flush)
    sa.event.listen(Session, "after_commit", committed)
    try:
        with pytest.raises((ValueError, RuntimeError)) as error:
            f.chain.invoke(ip_address="192.0.2.7")
    finally:
        sa.event.remove(Session, "after_flush", flush)
        sa.event.remove(Session, "after_commit", committed)
    assert error.value.local_outcome == "committed" and not f.calls
    assert error.value.finalization_outcome == ("unknown" if mode == "ack_unknown" else "committed")
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.FINALIZED}
        assert s.scalar(sa.select(Account.last_login_ip)) == "192.0.2.7"


@pytest.mark.parametrize("ip", [None, "", "invalid", "x" * 46, "192.0.2.1\n", 123])
def test_finalizer_rejects_invalid_server_ip(finalized_chain, ip):
    f = finalized_chain
    with pytest.raises(ValueError) as error:
        f.chain.invoke(ip_address=ip)
    assert error.value.local_outcome == "committed" and not f.calls


def test_flush_trigger_login_metadata_drift_rolls_back_i19(finalized_chain):
    f = finalized_chain
    with f.chain.local.engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TRIGGER tamper_ip AFTER UPDATE OF last_login_ip ON accounts
            BEGIN UPDATE accounts SET last_login_ip='192.0.2.99' WHERE id=NEW.id; END""")
    with pytest.raises(ValueError):
        f.chain.invoke(ip_address="192.0.2.7")
    assert not f.calls
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.PENDING}
        assert s.scalar(sa.select(Account.last_login_at)) is None


@pytest.mark.parametrize("prior", ["normal", "archived", "unselected"])
@pytest.mark.parametrize("role", ["owner", "editor"])
def test_preserved_members_and_original_current_tenant_policy(finalized_chain, prior, role):
    from uuid import UUID

    from models.account import Tenant, TenantStatus

    f = finalized_chain
    account, _ = seed(f.chain.local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    account_id = account.id
    f.chain.local.session.rollback()
    old_opened = datetime(2025, 1, 3)
    with Session(f.chain.local.engine) as s, s.begin():
        archived = Tenant(
            name="Prior workspace", status=TenantStatus.ARCHIVE if prior == "archived" else TenantStatus.NORMAL
        )
        archived.id = str(UUID(int=50))
        s.add(archived)
        s.flush()
        first_join = TenantAccountJoin(
            account_id=account_id,
            tenant_id=archived.id,
            role="normal",
            current=prior != "unselected",
            last_opened_at=old_opened,
        )
        first_join.id = str(UUID(int=10))
        preserved_join = TenantAccountJoin(
            account_id=account_id, tenant_id=str(UUID(int=100)), role=role, current=False
        )
        preserved_join.id = str(UUID(int=20))
        s.add_all([first_join, preserved_join])
    outcome = f.chain.invoke(ip_address="192.0.2.7")
    assert outcome.tokens
    with Session(f.chain.local.engine) as s:
        preserved = s.get(TenantAccountJoin, str(UUID(int=20)))
        assert preserved.role.value == role
        assert s.scalar(sa.select(sa.func.count()).select_from(History)) == 1
        selected = s.scalar(sa.select(TenantAccountJoin).where(TenantAccountJoin.current.is_(True)))
        assert selected.id == str(UUID(int=20 if prior == "archived" else 10))
        if prior == "normal":
            assert selected.last_opened_at == old_opened
        else:
            assert selected.last_opened_at != old_opened


@pytest.mark.parametrize("ownership", ["local_override", "released"])
@pytest.mark.parametrize("missing", [False, True])
def test_previous_override_never_adopted_or_recreated(finalized_chain, ownership, missing):
    f = finalized_chain
    first = f.chain.invoke(ip_address="192.0.2.7")
    member = first.persistence.workspaces[0]
    with Session(f.chain.local.engine) as s, s.begin():
        s.execute(
            sa.update(History)
            .where(History.id == str(member.membership_id))
            .values(ownership=ownership, tombstone=missing)
        )
        if missing:
            s.execute(sa.delete(TenantAccountJoin).where(TenantAccountJoin.id == str(member.join_id)))
        expected = tuple(
            s.execute(sa.select(*History.__table__.columns).where(History.id == str(member.membership_id))).one()
        )
    issued = len(f.calls)
    if missing:
        with pytest.raises(ValueError):
            new_authorization(f.chain)
        assert len(f.calls) == issued
    else:
        result = new_authorization(f.chain)
        assert result.tokens
    with Session(f.chain.local.engine) as s:
        assert (
            tuple(s.execute(sa.select(*History.__table__.columns).where(History.id == str(member.membership_id))).one())
            == expected
        )


@pytest.mark.parametrize("role", ["admin", "normal"])
def test_existing_managed_role_change_uses_new_c1_generation(finalized_chain, role):
    from uuid import UUID

    from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json, roles_fingerprint
    from models.account import TenantAccountRole

    f = finalized_chain
    first = f.chain.invoke(ip_address="192.0.2.7")
    # Reconstruct an earlier legitimate LOCAL role baseline, then execute the
    # actual new online authorization and C1 role owner against today's mapping.
    item = next(w for w in first.persistence.workspaces if w.workspace_id == UUID(int=100 if role == "admin" else 200))
    observation = MembershipObservation(
        item.workspace_id, first.persistence.account_id, item.join_id, TenantAccountRole(role), MembershipBackend.LOCAL
    )
    with Session(f.chain.local.engine) as s, s.begin():
        s.execute(sa.update(TenantAccountJoin).where(TenantAccountJoin.id == str(item.join_id)).values(role=role))
        s.execute(
            sa.update(History)
            .where(History.id == str(item.membership_id))
            .values(
                last_applied_roles_json=role_baseline_json(observation),
                last_applied_fingerprint=roles_fingerprint(observation),
            )
        )
    previous_cache = len(f.cache_calls)
    outcome = new_authorization(f.chain)
    assert outcome.tokens and outcome.persistence.generation == 2
    changed = next(w for w in outcome.persistence.workspaces if w.workspace_id == item.workspace_id)
    assert changed.role_changed and changed.current_role.value == ("normal" if role == "admin" else "admin")
    assert len(f.cache_calls) == previous_cache


def test_actual_cas_zero_rowcount_rolls_back_prior_space(finalized_chain):
    f = finalized_chain
    with f.chain.local.engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TRIGGER ignore_finalization BEFORE UPDATE OF finalization
            ON casdoor_managed_membership_extend WHEN NEW.workspace_id LIKE '%00c8'
            BEGIN SELECT RAISE(IGNORE); END""")
    with pytest.raises(ValueError) as error:
        f.chain.invoke(ip_address="192.0.2.7")
    assert error.value.local_outcome == "committed" and error.value.finalization_outcome == "not_committed"
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.PENDING}
    assert not f.calls


@pytest.mark.parametrize("stage", ["flush", "issued"])
def test_real_lease_loss_never_delivers_tokens(finalized_chain, stage):
    from core.casdoor.leases import CasdoorLeaseError

    f = finalized_chain

    def steal():
        for key in f.chain.redis.data:
            f.chain.redis.data[key] = b"concurrent-winner"

    def flush(session, context):
        if any(isinstance(obj, Account) and obj.last_login_ip == "192.0.2.7" for obj in session.dirty):
            steal()

    if stage == "flush":
        sa.event.listen(Session, "after_flush", flush)
    else:
        f.control.hook = steal
    try:
        with pytest.raises(CasdoorLeaseError) as error:
            f.chain.invoke(ip_address="192.0.2.7")
    finally:
        if stage == "flush":
            sa.event.remove(Session, "after_flush", flush)
    assert error.value.local_outcome == "committed"
    assert error.value.token_outcome == ("not_started" if stage == "flush" else "issued")
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {
            Finalization.PENDING if stage == "flush" else Finalization.FINALIZED
        }
    assert len(f.calls) == (0 if stage == "flush" else 2)


def test_nocloud_fallback_has_no_cache_resource_or_tenant_dispatch(finalized_chain, monkeypatch):
    from events.tenant_event import tenant_was_created, tenant_was_updated
    from services.account_adapters import RBACWorkspaceMemberAccessSync
    from tasks.initialize_created_app_rbac_access_task import sync_joined_workspace_member_rbac_access_task

    f = finalized_chain
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    f.chain.roles[0]["users"] = []

    def deny(*args, **kwargs):
        pytest.fail("LOCAL finalization must not dispatch a resource task or tenant event")

    monkeypatch.setattr(tenant_was_created, "send", deny)
    monkeypatch.setattr(tenant_was_updated, "send", deny)
    monkeypatch.setattr(sync_joined_workspace_member_rbac_access_task, "delay", deny)
    monkeypatch.setattr(RBACWorkspaceMemberAccessSync, "sync", deny)
    result = f.chain.invoke(ip_address="2001:db8::7")
    assert result.tokens and not f.cache_calls
    assert len(result.persistence.workspaces) == 1
    assert result.persistence.workspaces[0].current_role.value == "normal"
    csrf = jwt.decode(result.tokens.csrf_token, dify_config.SECRET_KEY, algorithms=["HS256"])
    assert csrf["sub"] == str(result.persistence.account_id)
    assert csrf["exp"] == jwt.decode(result.tokens.access_token, dify_config.SECRET_KEY, algorithms=["HS256"])["exp"]


@pytest.mark.parametrize("tamper", ["duplicate", "bool_schema", "float_schema", "bool_fence", "float_fence"])
def test_post_c1_noncanonical_desired_cannot_finalize(finalized_chain, monkeypatch, tamper):
    import json

    f = finalized_chain
    original = f.chain.coordinator._local._persist_local_login

    def write(**kwargs):
        result = original(**kwargs)
        with Session(f.chain.local.engine) as s, s.begin():
            row = s.scalar(sa.select(History).order_by(History.id).limit(1))
            old = row.desired_roles_json
            if tamper == "duplicate":
                changed = old.replace('"backend":"local"', '"backend":"remote","backend":"local"')
            elif tamper.endswith("schema"):
                changed = old.replace(
                    '"schema_version":1', '"schema_version":' + ("true" if tamper == "bool_schema" else "1.0")
                )
            else:
                changed = old.replace(
                    '"fence_epoch":0', '"fence_epoch":' + ("false" if tamper == "bool_fence" else "0.0")
                )
            assert changed != old
            # The former json.loads equality would accept all these raw bytes.
            assert json.loads(changed) == json.loads(old)
            row.desired_roles_json = changed
        return result

    monkeypatch.setattr(f.chain.coordinator._local, "_persist_local_login", write)
    with pytest.raises(ValueError) as error:
        f.chain.invoke(ip_address="192.0.2.7")
    assert error.value.local_outcome == "committed" and not f.calls
    with Session(f.chain.local.engine) as s:
        assert set(s.scalars(sa.select(History.finalization))) == {Finalization.PENDING}
