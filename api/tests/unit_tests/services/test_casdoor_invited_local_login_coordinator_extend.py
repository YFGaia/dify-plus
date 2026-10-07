"""Actual store consumption/signed wire/SQL invitation operation, offline only.

These private caller cases are not mounted callback or live provider acceptance.
Dedicated mounted tests exercise the production dispatch separately.
"""

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import Mock

import pytest
import jwt
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.auth_transactions import AuthTransactionError
from core.casdoor.gateway import CasdoorTokenGateway
from enums import DeploymentEdition
from models.account import (
    Account,
    AccountIntegrate,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
)
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorIdentityExtend,
    CasdoorIntentKind,
    CasdoorSyncIntentExtend,
)
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from repositories.account_activation_repository import (
    SQLAlchemyAccountActivationRepository,
)
from repositories.casdoor_invitation_operation_repository_extend import (
    CasdoorInvitationOperationRepository,
)
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
)
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.casdoor_invited_local_login_coordinator_service_extend import (
    CasdoorInvitedLocalLoginCoordinatorService,
)
from sqlalchemy.orm import Session, SessionTransaction
from test_casdoor_local_login_coordinator_service_extend import chain as original_chain
from test_casdoor_local_login_coordinator_service_extend import (
    local_fixture as local_fixture,
)
from test_casdoor_local_login_coordinator_service_extend import login_env as login_env
from test_casdoor_local_login_coordinator_service_extend import signing as signing
from test_invitation_token_consumption_extend import TOKEN, FakeRedis as InvitationRedis
from test_casdoor_local_login_finalization_service_extend import attach_finalizer

chain = original_chain


@pytest.fixture
def invited(chain, monkeypatch):
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    Issuance.__table__.create(chain.local.engine)
    AccountIntegrate.__table__.create(chain.local.engine)
    with Session(chain.local.engine) as session, session.begin():
        account = Account(
            name="Local old name",
            email=chain.profile["email"],
            status=AccountStatus.PENDING,
            interface_language="ja-JP",
            timezone="Asia/Tokyo",
            interface_theme="dark",
            password="owned-local-password",
        )
        session.add(account)
        session.flush()
        session.add(
            AccountMoneyExtend(
                account_id=account.id,
                total_quota=Decimal("91.25"),
                used_quota=Decimal("4.5"),
            )
        )
        session.add(
            AccountIntegrate(
                account_id=account.id,
                provider="oauth2",
                open_id="legacy-owned-subject",
                encrypted_token="legacy-synthetic-envelope",
            )
        )
        authority = InvitationAuthorityRepository()
        lifecycle = authority.set_lifecycle_state(
            session,
            account_id=account.id,
            workspace_id=str(chain.local.config.default_workspace_id),
            state="active",
        )
        data = dict(
            account_id=account.id,
            email=account.email,
            workspace_id=str(chain.local.config.default_workspace_id),
            role="editor",
            requires_setup=True,
            invitation_authority=dict(
                schema_version=1,
                issuance_id=str(uuid4()),
                lifecycle_id=lifecycle.lifecycle_id,
                lifecycle_epoch=lifecycle.epoch,
                token_digest=sha256(TOKEN.encode()).hexdigest(),
                join_id_at_issue=None,
            ),
        )
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        issuance = authority.record_issuance(session, payload_json=payload)
        account_id, issuance_id = account.id, issuance.issuance_id
    redis = InvitationRedis()
    redis.entries[redis.token_key] = ("string", payload.encode(), 60000)
    token_store = RedisInvitationTokenStore(redis=redis)
    effects = [Mock() for _ in range(4)]
    effects[1].get_freeze_type.return_value = None
    factory = chain.coordinator._session_factory
    activation = AccountActivationService(
        tokens=token_store,
        accounts=SQLAlchemyAccountActivationRepository(factory),
        workspace_policy=effects[0],
        eligibility=effects[1],
        membership_cache=effects[2],
        member_access_sync=effects[3],
    )
    caller = CasdoorInvitedLocalLoginCoordinatorService(
        ordinary=chain.coordinator, activation=activation
    )
    context = replace(
        chain.consumed.context, invite=TOKEN, locale="zh-Hans", timezone="Asia/Shanghai"
    )
    created = chain.store.create(
        context,
        browser_scope=chain.consume_args["browser_scope"],
        policy=chain.consume_args["policy"],
        guard=chain.consume_args["guard"],
    )
    consumed = chain.store.consume(
        created.state,
        **(chain.consume_args | {"transaction_cookie": created.cookie.value}),
    )
    chain.control.bad_id["nonce"] = consumed.nonce

    def invoke(**overrides):
        raw = CasdoorTokenGateway(chain.operation).exchange_code(
            "offline-invited-code", case.consumed.code_verifier
        )
        return case.caller._coordinate_local_login(
            **(
                dict(
                    consumed=case.consumed,
                    raw_tokens=raw,
                    operation=chain.operation,
                    native_contract=chain.native,
                    directory_contract=chain.directory,
                    credential_strategy=chain.strategy,
                    correlation_id=uuid4(),
                )
                | overrides
            )
        )

    case = SimpleNamespace(
        chain=chain,
        caller=caller,
        invoke=invoke,
        consumed=consumed,
        created=created,
        account=account_id,
        issuance=issuance_id,
        redis=redis,
        store=token_store,
        effects=effects,
        payload=payload,
    )
    return case


def snapshot(case):
    with Session(case.chain.local.engine) as session:
        return tuple(
            tuple(
                tuple(row)
                for row in session.execute(
                    sa.select(*model.__table__.columns).order_by(model.id)
                )
            )
            for model in (
                Account,
                AccountMoneyExtend,
                CasdoorIdentityExtend,
                CasdoorSyncIntentExtend,
                TenantAccountJoin,
                AccountIntegrate,
            )
        )


@pytest.mark.parametrize(
    "initialized,status,bound,quota",
    [
        (False, AccountStatus.PENDING, False, True),
        (False, AccountStatus.UNINITIALIZED, False, False),
        (False, AccountStatus.ACTIVE, False, True),
        (True, AccountStatus.PENDING, False, True),
        (True, AccountStatus.ACTIVE, False, True),
        (False, AccountStatus.PENDING, True, True),
        (True, AccountStatus.ACTIVE, True, True),
    ],
)
def test_actual_consumed_signed_invite_operation_keeps_session_pending(
    invited, initialized, status, bound, quota
):
    case = invited
    c = case.chain.local.env[1]
    with Session(case.chain.local.engine) as session, session.begin():
        account = session.get(Account, case.account)
        account.status = status
        if initialized:
            account.initialized_at = datetime(2024, 1, 1)
        if not quota:
            session.execute(
                sa.delete(AccountMoneyExtend).where(
                    AccountMoneyExtend.account_id == case.account
                )
            )
        if bound:
            session.add(
                CasdoorIdentityExtend(
                    namespace_id=str(c.namespace_id),
                    account_id=case.account,
                    issuer=c.issuer,
                    organization=c.organization,
                    subject=c.subject,
                    last_applied_json="{}",
                    profile_sync_json="{}",
                )
            )
    before = snapshot(case)
    result = case.invoke()
    assert result.local_outcome == "committed" and result.cleanup_released is True
    assert result.tokens is None and result.token_outcome == "not_started"
    assert result.finalization_outcome == "not_started"
    assert not case.chain.redis.data
    with Session(case.chain.local.engine) as session:
        account = session.get(Account, case.account)
        assert (
            account.status is AccountStatus.ACTIVE
            and account.initialized_at is not None
        )
        if initialized:
            assert (
                account.name,
                account.interface_language,
                account.timezone,
                account.interface_theme,
                account.initialized_at,
            ) == (
                "Local old name",
                "ja-JP",
                "Asia/Tokyo",
                "dark",
                datetime(2024, 1, 1),
            )
        else:
            assert (
                account.name,
                account.interface_language,
                account.timezone,
                account.interface_theme,
            ) == (
                "Remote Name",
                "zh-Hans",
                "Asia/Shanghai",
                "light",
            )
        assert (
            account.password == "owned-local-password"
            and account.email == case.chain.profile["email"]
        )
        identity = session.scalar(sa.select(CasdoorIdentityExtend))
        assert identity.account_id == case.account and identity.sync_generation == 0
        pending = session.scalar(sa.select(CasdoorSyncIntentExtend))
        assert pending.id == str(result.operation.snapshot.operation_id)
        assert pending.generation == 0 and pending.kind.value == "invitation_finalize"
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin))
            == 0
        )
        money = session.scalar(sa.select(AccountMoneyExtend))
        assert (money.total_quota, money.used_quota) == (
            (Decimal("91.25"), Decimal("4.5"))
            if quota
            else (Decimal("15"), Decimal("0"))
        )
        assert session.get(Issuance, case.issuance).state == "issued"
    assert case.redis.token_key in case.redis.entries
    assert all(
        call[0]
        == __import__(
            "services.account_adapters", fromlist=["_INVITATION_OBSERVE"]
        )._INVITATION_OBSERVE
        for call in case.redis.calls
    )
    assert len(case.effects[1].get_freeze_type.call_args_list) == 1
    assert not case.effects[2].mock_calls and not case.effects[3].mock_calls
    if quota:
        assert snapshot(case)[1] == before[1]
    assert snapshot(case)[5] == before[5]


@pytest.mark.parametrize(
    "fault",
    [
        "email",
        "unverified",
        "banned",
        "closed",
        "issuer",
        "disabled",
        "withdrawn",
        "reissued",
        "raw_mismatch",
        "legacy",
    ],
)
def test_first_authority_admission_failure_never_writes(invited, fault):
    case = invited
    if fault == "email":
        case.chain.profile["email"] = "other@example.test"
    elif fault == "unverified":
        case.chain.profile["email_verified"] = False
    elif fault == "issuer":
        case.chain.control.bad_id["iss"] = "https://other.example.test"
    elif fault == "disabled":
        case.chain.user["isForbidden"] = True
    elif fault in ("banned", "closed", "withdrawn", "reissued"):
        with Session(case.chain.local.engine) as session, session.begin():
            if fault in ("banned", "closed"):
                session.get(Account, case.account).status = AccountStatus(fault)
            elif fault == "withdrawn":
                InvitationAuthorityRepository().set_lifecycle_state(
                    session,
                    account_id=case.account,
                    workspace_id=str(case.chain.local.config.default_workspace_id),
                    state="withdrawn",
                )
            else:
                session.get(Issuance, case.issuance).payload_digest = "0" * 64
    else:
        data = json.loads(case.payload)
        if fault == "legacy":
            del data["invitation_authority"]
        else:
            data["role"] = "normal"
        case.redis.entries[case.redis.token_key] = (
            "string",
            json.dumps(data).encode(),
            60000,
        )
    before = snapshot(case)
    with pytest.raises(Exception):
        case.invoke()
    assert snapshot(case) == before and not case.chain.redis.data


def test_real_auth_consume_cannot_be_replayed(invited):
    case = invited
    with pytest.raises(AuthTransactionError):
        case.chain.store.consume(
            case.created.state,
            **(
                case.chain.consume_args
                | {"transaction_cookie": case.created.cookie.value}
            ),
        )


def test_root_lease_loss_after_original_account_owner_rolls_back_all(
    invited, monkeypatch
):
    case = invited
    owner = case.caller._invited_account
    original = owner.persist_invited_login_account

    def lost(*args, **kwargs):
        result = original(*args, **kwargs)
        case.chain.redis.data.clear()
        return result

    monkeypatch.setattr(owner, "persist_invited_login_account", lost)
    before = snapshot(case)
    with pytest.raises(Exception) as failure:
        case.invoke()
    assert failure.value.local_outcome == "unknown"
    assert snapshot(case) == before


def test_public_shape_cannot_construct_an_operation_guard(invited):
    from services.casdoor_invited_local_login_coordinator_service_extend import (
        _InvitedOperationGuard,
        _operation_guard,
    )

    case = invited
    fake = _InvitedOperationGuard(
        case.caller, case.consumed, case.chain.operation, None, None, None, None
    )
    with pytest.raises(Exception):
        _operation_guard(
            fake, owner=case.caller._operation_owner, attempt=None, prepared=None
        )


@pytest.mark.parametrize("phase", ["before", "after"])
def test_unknown_commit_has_no_success_or_automatic_replay(invited, monkeypatch, phase):
    case = invited
    original_create = CasdoorInvitationOperationRepository.create_pending
    calls = []

    def marked_create(self, *args, **kwargs):
        calls.append(1)
        result = original_create(self, *args, **kwargs)
        self._session.info["d13-created"] = True
        return result

    monkeypatch.setattr(
        CasdoorInvitationOperationRepository, "create_pending", marked_create
    )
    commit = SessionTransaction.commit

    def unknown(self, *args, **kwargs):
        if self._parent is None and self.session.info.get("d13-created"):
            if phase == "after":
                commit(self, *args, **kwargs)
            raise RuntimeError("private unknown commit")
        return commit(self, *args, **kwargs)

    monkeypatch.setattr(SessionTransaction, "commit", unknown)
    before = snapshot(case)
    with pytest.raises(Exception) as error:
        case.invoke()
    assert (
        error.value.local_outcome == "unknown" and error.value.cleanup_released is True
    )
    assert calls == [1] and not case.chain.redis.data
    with Session(case.chain.local.engine) as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(CasdoorSyncIntentExtend)
        ) == int(phase == "after")
    if phase == "before":
        assert snapshot(case) == before


@pytest.mark.parametrize("field", ["password", "interface_language", "email"])
def test_unauthorized_account_delta_after_real_owner_is_whole_root_rollback(
    invited, monkeypatch, field
):
    case = invited
    original = case.caller._invited_account.persist_invited_login_account

    def drift(*args, **kwargs):
        result = original(*args, **kwargs)
        account = kwargs["session"].get(Account, case.account)
        setattr(
            account,
            field,
            "en-US" if field == "interface_language" else "private-drift@example.test",
        )
        kwargs["session"].flush()
        return result

    monkeypatch.setattr(
        case.caller._invited_account, "persist_invited_login_account", drift
    )
    before = snapshot(case)
    with pytest.raises(Exception):
        case.invoke()
    assert snapshot(case) == before and not case.chain.redis.data


def test_freeze_uses_one_original_owner_and_no_seat_or_login_effect(invited):
    case = invited
    case.effects[1].get_freeze_type.return_value = "email_domain_suspended"
    before = snapshot(case)
    with pytest.raises(Exception):
        case.invoke()
    assert case.effects[1].get_freeze_type.call_count == 1
    assert snapshot(case) == before and not case.chain.redis.sets
    assert not case.chain.local.env[5].get_license.mock_calls


def test_canonical_scope_holds_default_mapping_and_invited_workspace(invited):
    case = invited
    workspace = str(UUID(int=300))
    data = json.loads(case.payload)
    with Session(case.chain.local.engine) as session, session.begin():
        tenant = Tenant(name="Invitation outside mapping")
        tenant.id = workspace
        session.add(tenant)
        session.flush()
        lifecycle = InvitationAuthorityRepository().set_lifecycle_state(
            session, account_id=case.account, workspace_id=workspace, state="active"
        )
        data["workspace_id"] = workspace
        data["invitation_authority"]["lifecycle_id"] = lifecycle.lifecycle_id
        row = session.get(Issuance, case.issuance)
        row.workspace_id, row.lifecycle_id = workspace, lifecycle.lifecycle_id
        row.payload_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
        row.payload_digest = sha256(row.payload_json.encode()).hexdigest()
        payload = row.payload_json
    case.redis.entries[case.redis.token_key] = ("string", payload.encode(), 60000)
    result = case.invoke()
    assert result.cleanup_released
    keys = [item[0] for item in case.chain.redis.sets]
    assert keys == sorted(keys) and len(keys) == len(set(keys))
    for n in (100, 200, 300):
        assert f"casdoor:lease:v1:member:{UUID(int=n)}:{case.account}" in keys
    assert f"casdoor:lease:v1:account:{case.account}" in keys


def test_scope_expansion_releases_whole_set_then_rebuilds_once(invited, monkeypatch):
    case = invited
    original = case.caller._discover
    calls = [0]
    workspace = str(UUID(int=700))

    def expand(*args, **kwargs):
        result = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 1:
            with Session(case.chain.local.engine) as session, session.begin():
                tenant = Tenant(name="Concurrent unmanaged workspace")
                tenant.id = workspace
                session.add(tenant)
                session.flush()
                session.add(
                    TenantAccountJoin(
                        account_id=case.account, tenant_id=workspace, role="normal"
                    )
                )
        return result

    monkeypatch.setattr(case.caller, "_discover", expand)
    result = case.invoke()
    assert result.cleanup_released and not case.chain.redis.data
    assert case.effects[1].get_freeze_type.call_count == 2
    assert f"casdoor:lease:v1:member:{workspace}:{case.account}" in [
        r[0] for r in case.chain.redis.sets
    ]
    with Session(case.chain.local.engine) as session:
        assert (
            session.scalar(
                sa.select(sa.func.count()).select_from(CasdoorSyncIntentExtend)
            )
            == 1
        )


def test_unconfirmed_cleanup_preserves_committed_pending_and_never_issues(
    invited, monkeypatch
):
    monkeypatch.setattr(invited.caller._base, "_cleanup", lambda *args: False)
    result = invited.invoke()
    assert result.local_outcome == "committed" and result.cleanup_released is False
    assert result.tokens is None and result.token_outcome == "not_started"


def test_unknown_role_graph_is_before_account_operation(invited):
    case = invited
    del case.chain.organization["accountItems"]
    before = snapshot(case)
    with pytest.raises(Exception):
        case.invoke()
    assert snapshot(case) == before and not case.chain.redis.data


def test_production_factory_rejects_missing_deployment_proof_before_any_io(invited):
    case = invited
    before = snapshot(case)
    with pytest.raises(Exception):
        CasdoorInvitedLocalLoginCoordinatorService.for_production(
            session_factory=case.chain.coordinator._session_factory,
            configuration_service=case.chain.configuration_service,
            account_activation=case.caller._activation,
            redis_client=case.chain.redis,
        )
    assert (
        snapshot(case) == before and not case.redis.calls and not case.chain.redis.sets
    )


def test_original_provider_link_unexpected_delta_is_rolled_back(invited, monkeypatch):
    case = invited
    original = case.caller._invited_account.persist_invited_login_account

    def changed_link(*args, **kwargs):
        result = original(*args, **kwargs)
        session = kwargs["session"]
        session.scalar(sa.select(AccountIntegrate)).open_id = "unexpected-link-change"
        session.flush()
        return result

    monkeypatch.setattr(
        case.caller._invited_account, "persist_invited_login_account", changed_link
    )
    before = snapshot(case)
    with pytest.raises(Exception):
        case.invoke()
    assert snapshot(case) == before


@pytest.mark.parametrize(
    "kind",
    [
        CasdoorIntentKind.ROLE_REPLACE,
        CasdoorIntentKind.RESOURCE_GRANT,
        CasdoorIntentKind.PROFILE_AVATAR,
    ],
)
def test_only_avatar_intents_are_not_required_permission_barriers(invited, kind):
    case = invited
    c = case.chain.local.env[1]
    with Session(case.chain.local.engine) as session, session.begin():
        identity = CasdoorIdentityExtend(
            namespace_id=str(c.namespace_id),
            account_id=case.account,
            issuer=c.issuer,
            organization=c.organization,
            subject=c.subject,
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        session.add(identity)
        session.flush()
        session.add(
            CasdoorSyncIntentExtend(
                namespace_id=str(c.namespace_id),
                identity_id=identity.id,
                account_id=case.account,
                workspace_id=str(case.chain.local.config.default_workspace_id),
                revision_id=str(c.revision_id),
                generation=0,
                ownership_epoch=0,
                fence_epoch=0,
                kind=kind,
                scope_digest="a" * 64,
                idempotency_key="c" * 64,
                desired_json="{}",
            )
        )
    before = snapshot(case)
    if kind is CasdoorIntentKind.PROFILE_AVATAR:
        assert case.invoke().tokens is None
        after = snapshot(case)
        assert len(after[3]) == len(before[3]) + 1
        assert before[3][0] in after[3]
    else:
        with pytest.raises(Exception):
            case.invoke()
        assert snapshot(case) == before and not case.chain.redis.sets


@pytest.mark.parametrize("mode", ["rbac", "enterprise"])
def test_nonlocal_modes_cannot_enter_invited_account_operation(
    invited, monkeypatch, mode
):
    case = invited
    if mode == "rbac":
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    else:
        monkeypatch.setattr(
            dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.ENTERPRISE
        )
    before = snapshot(case)
    with pytest.raises(Exception):
        case.invoke()
    assert (
        snapshot(case) == before and not case.chain.redis.sets and not case.redis.calls
    )


@pytest.fixture
def invited_session(invited, monkeypatch):
    case = invited
    finalizer = attach_finalizer(case.chain, monkeypatch)
    case.caller = CasdoorInvitedLocalLoginCoordinatorService(
        ordinary=case.chain.coordinator,
        activation=case.caller._activation,
    )
    case.issuer = finalizer
    return case


@pytest.mark.parametrize(
    "initialized,status,bound,quota",
    [
        (False, AccountStatus.PENDING, False, True),
        (False, AccountStatus.UNINITIALIZED, False, False),
        (False, AccountStatus.ACTIVE, False, True),
        (True, AccountStatus.PENDING, False, True),
        (True, AccountStatus.ACTIVE, False, True),
        (False, AccountStatus.PENDING, True, True),
        (True, AccountStatus.ACTIVE, True, True),
    ],
)
def test_actual_invited_attempt_automatically_reaches_original_session(
    invited_session,
    initialized,
    status,
    bound,
    quota,
):
    case = invited_session
    context = case.chain.local.env[1]
    with Session(case.chain.local.engine) as session, session.begin():
        account = session.get(Account, case.account)
        account.status = status
        if initialized:
            account.initialized_at = datetime(2024, 1, 1)
        if not quota:
            session.execute(
                sa.delete(AccountMoneyExtend).where(
                    AccountMoneyExtend.account_id == case.account
                )
            )
        if bound:
            session.add(
                CasdoorIdentityExtend(
                    namespace_id=str(context.namespace_id),
                    account_id=case.account,
                    issuer=context.issuer,
                    organization=context.organization,
                    subject=context.subject,
                    last_applied_json="{}",
                    profile_sync_json="{}",
                )
            )
    result = case.invoke(ip_address="127.0.0.1")
    assert result.local_outcome == result.finalization_outcome == "committed"
    assert result.token_outcome == "issued" and result.cleanup_released is True
    assert result.tokens is not None and len(case.issuer.calls) == 2
    assert not case.chain.redis.data and case.redis.token_key not in case.redis.entries
    decoded = jwt.decode(
        result.tokens.access_token, dify_config.SECRET_KEY, algorithms=["HS256"]
    )
    assert decoded["user_id"] == case.account
    assert result.provenance.account_id == UUID(case.account)
    assert result.provenance.namespace_id == case.consumed.context.namespace_id
    with Session(case.chain.local.engine) as session:
        account = session.get(Account, case.account)
        assert (
            account.status is AccountStatus.ACTIVE
            and account.initialized_at is not None
        )
        assert (
            account.email == case.chain.profile["email"]
            and account.password == "owned-local-password"
        )
        assert (
            account.last_login_at is not None and account.last_login_ip == "127.0.0.1"
        )
        assert (
            account.interface_language,
            account.timezone,
            account.interface_theme,
        ) == (
            ("ja-JP", "Asia/Tokyo", "dark")
            if initialized
            else ("zh-Hans", "Asia/Shanghai", "light")
        )
        identity = session.scalar(sa.select(CasdoorIdentityExtend))
        assert identity.sync_generation == 1 and identity.profile_sync_json != "{}"
        intent = session.scalar(
            sa.select(CasdoorSyncIntentExtend).where(
                CasdoorSyncIntentExtend.kind == CasdoorIntentKind.INVITATION_FINALIZE
            )
        )
        assert (
            intent.operation_state.value == "applied"
            and intent.termination_state.value == "confirmed"
        )
        assert session.get(Issuance, case.issuance).state == "consumed"
        joins = tuple(session.scalars(sa.select(TenantAccountJoin)))
        assert joins and sum(join.current is True for join in joins) == 1
        invited_join = next(
            join
            for join in joins
            if join.tenant_id == str(case.chain.local.config.default_workspace_id)
        )
        assert invited_join.role.value == "editor"
        from models.casdoor_extend import CasdoorManagedMembershipExtend

        histories = tuple(session.scalars(sa.select(CasdoorManagedMembershipExtend)))
        assert histories and all(
            row.finalization.value == "finalized" for row in histories
        )
        money = session.scalar(sa.select(AccountMoneyExtend))
        assert (money.total_quota, money.used_quota) == (
            (Decimal("91.25"), Decimal("4.5"))
            if quota
            else (Decimal("15"), Decimal("0"))
        )
        assert (
            session.scalar(sa.select(AccountIntegrate.encrypted_token))
            == "legacy-synthetic-envelope"
        )


@pytest.mark.parametrize("fail", [1, 2])
def test_invited_original_issuer_uncertainty_never_returns_pair_or_revokes(
    invited_session, fail
):
    case = invited_session
    case.issuer.control.fail = fail
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert error.value.local_outcome == error.value.finalization_outcome == "committed"
    assert (
        error.value.token_outcome == "unknown" and error.value.cleanup_released is True
    )
    assert len(case.issuer.calls) == fail and not case.chain.redis.data
    with Session(case.chain.local.engine) as session:
        assert session.get(Issuance, case.issuance).state == "consumed"
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 1


def test_invited_issued_pair_is_suppressed_when_real_leases_are_lost(invited_session):
    case = invited_session

    def lose():
        for key in case.chain.redis.data:
            case.chain.redis.data[key] = b"concurrent-winner"

    case.issuer.control.hook = lose
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert (
        error.value.finalization_outcome == "committed"
        and error.value.token_outcome == "issued"
    )
    assert error.value.cleanup_released is True and len(case.issuer.calls) == 2


def test_invited_unconfirmed_outer_cleanup_suppresses_pair_and_native_provenance(
    invited_session, monkeypatch
):
    case = invited_session
    monkeypatch.setattr(case.caller._base, "_cleanup", lambda *_: False)
    result = case.invoke(ip_address="127.0.0.1")
    assert (
        result.finalization_outcome == "committed" and result.token_outcome == "issued"
    )
    assert (
        result.cleanup_released is False
        and result.tokens is None
        and result.provenance is None
    )
    assert len(case.issuer.calls) == 2


@pytest.mark.parametrize(
    "column",
    ["password", "interface_language", "email", "remote_email", "link", "quota"],
)
def test_invited_profile_owner_unexpected_delta_rolls_back_only_its_root(
    invited_session, monkeypatch, column
):
    from repositories.casdoor_profile_repository_extend import CasdoorProfileRepository

    case = invited_session
    original = CasdoorProfileRepository.persist

    def changed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        session = self._session
        if column in ("password", "interface_language", "email"):
            session.execute(
                sa.update(Account)
                .where(Account.id == case.account)
                .values(**{column: "unexpected"})
            )
        elif column == "remote_email":
            session.execute(
                sa.update(CasdoorIdentityExtend).values(
                    remote_email="unexpected@example.com"
                )
            )
        elif column == "quota":
            session.execute(sa.update(AccountMoneyExtend).values(total_quota=999))
        else:
            session.execute(
                sa.update(AccountIntegrate).values(encrypted_token="unexpected")
            )
        return result

    monkeypatch.setattr(CasdoorProfileRepository, "persist", changed)
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert (
        error.value.local_outcome == "committed"
        and error.value.token_outcome == "not_started"
    )
    assert not case.issuer.calls and not case.chain.redis.data
    with Session(case.chain.local.engine) as session:
        account = session.get(Account, case.account)
        assert (
            account.password == "owned-local-password"
            and account.interface_language == "zh-Hans"
        )
        assert (
            account.email == case.chain.profile["email"]
            and account.last_login_at is None
        )
        assert session.scalar(sa.select(AccountMoneyExtend.total_quota)) == Decimal(
            "91.25"
        )
        assert (
            session.scalar(sa.select(AccountIntegrate.encrypted_token))
            == "legacy-synthetic-envelope"
        )
        assert session.get(Issuance, case.issuance).state == "consumed"


def test_invited_metadata_sql_trigger_is_whole_root_rollback(invited_session):
    case = invited_session
    with case.chain.local.engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TRIGGER invited_metadata_drift AFTER UPDATE OF last_login_at ON accounts
        WHEN NEW.last_login_at IS NOT NULL BEGIN UPDATE accounts SET interface_language='unexpected' WHERE id=NEW.id; END""")
    with pytest.raises(Exception):
        case.invoke(ip_address="127.0.0.1")
    assert not case.issuer.calls
    with Session(case.chain.local.engine) as session:
        account = session.get(Account, case.account)
        assert account.interface_language == "zh-Hans" and account.last_login_at is None


@pytest.mark.parametrize(
    "state", ["pending", "in_flight", "unknown", "applied", "failed", "cancelled"]
)
def test_invited_tail_rejects_other_required_intent_in_every_state(
    invited_session, monkeypatch, state
):
    from models.casdoor_extend import CasdoorOperationState
    from services.casdoor_invited_local_finalization_service_extend import (
        CasdoorInvitedLocalFinalizationService,
    )

    case = invited_session
    original = CasdoorInvitedLocalFinalizationService.finalize_invited_local_memberships

    def extra(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        with Session(case.chain.local.engine) as session, session.begin():
            identity = session.scalar(sa.select(CasdoorIdentityExtend))
            session.add(
                CasdoorSyncIntentExtend(
                    namespace_id=identity.namespace_id,
                    identity_id=identity.id,
                    account_id=case.account,
                    workspace_id=None,
                    revision_id=str(case.consumed.context.revision_id),
                    generation=identity.sync_generation,
                    fence_epoch=1,
                    ownership_epoch=0,
                    kind=CasdoorIntentKind.RESOURCE_GRANT,
                    scope_digest="1" * 64,
                    idempotency_key="tail-extra-" + state,
                    desired_json="{}",
                    operation_state=CasdoorOperationState(state),
                )
            )
        return result

    monkeypatch.setattr(
        CasdoorInvitedLocalFinalizationService,
        "finalize_invited_local_memberships",
        extra,
    )
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert error.value.token_outcome == "not_started" and not case.issuer.calls


def test_invited_guard_cannot_be_reconstructed_from_a_committed_operation(
    invited, monkeypatch
):
    case = invited
    original = case.caller._operation_owner._produce_invited_login_operation
    actual = []

    def capture(attempt, **kwargs):
        actual.append(attempt)
        return original(attempt, **kwargs)

    monkeypatch.setattr(
        case.caller._operation_owner, "_produce_invited_login_operation", capture
    )
    case.invoke()
    calls = len(case.redis.calls)
    with pytest.raises(Exception):
        case.caller._invitation_finalizer._finalize_invited_login(
            actual[0],
            token=TOKEN,
            caller_guard=object(),
        )
    assert len(case.redis.calls) == calls


def test_invited_token_consumption_uncertainty_is_pending_and_never_replayed(
    invited_session,
):
    case = invited_session
    case.redis.fail_after = True
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert (
        error.value.local_outcome == "committed"
        and error.value.token_outcome == "not_started"
    )
    assert not case.issuer.calls
    from services.account_adapters import _INVITATION_CONSUME

    assert sum(call[0] == _INVITATION_CONSUME for call in case.redis.calls) == 1
    assert case.redis.token_key not in case.redis.entries


def test_invited_f1_uncertain_ack_does_not_replay_or_issue(
    invited_session, monkeypatch
):
    from services.casdoor_invited_local_finalization_service_extend import (
        CasdoorInvitedLocalFinalizationService,
    )

    case = invited_session
    original = CasdoorInvitedLocalFinalizationService.finalize_invited_local_memberships
    calls = []

    def unknown(self, *args, **kwargs):
        calls.append(1)
        original(self, *args, **kwargs)
        raise TimeoutError("offline F1 commit acknowledgement unknown")

    monkeypatch.setattr(
        CasdoorInvitedLocalFinalizationService,
        "finalize_invited_local_memberships",
        unknown,
    )
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert (
        error.value.finalization_outcome == "unknown"
        and error.value.token_outcome == "not_started"
    )
    assert len(calls) == 1 and not case.issuer.calls


@pytest.mark.parametrize("preferences", ["request", "idp", "default"])
def test_invited_initialization_preserves_request_idp_default_order(
    invited_session, preferences
):
    case = invited_session
    if preferences != "default":
        case.chain.profile.update(locale="ja-JP", zoneinfo="Asia/Tokyo")
    context = case.consumed.context
    if preferences != "request":
        context = replace(context, locale=None, timezone=None)
    created = case.chain.store.create(
        context,
        browser_scope=case.chain.consume_args["browser_scope"],
        policy=case.chain.consume_args["policy"],
        guard=case.chain.consume_args["guard"],
    )
    case.consumed = case.chain.store.consume(
        created.state,
        **(case.chain.consume_args | {"transaction_cookie": created.cookie.value}),
    )
    case.chain.control.bad_id["nonce"] = case.consumed.nonce
    result = case.invoke(ip_address="127.0.0.1")
    assert result.token_outcome == "issued" and result.tokens is not None
    with Session(case.chain.local.engine) as session:
        account = session.get(Account, case.account)
        assert (account.interface_language, account.timezone) == {
            "request": ("zh-Hans", "Asia/Shanghai"),
            "idp": ("ja-JP", "Asia/Tokyo"),
            "default": ("en-US", "America/New_York"),
        }[preferences]


def test_invited_full_scope_cannot_expand_after_the_real_operation(
    invited_session, monkeypatch
):
    case = invited_session
    original = case.caller._invitation_finalizer._finalize_invited_login

    def expand(attempt, **kwargs):
        with Session(case.chain.local.engine) as session, session.begin():
            tenant = Tenant(name="new unleased workspace")
            tenant.id = str(UUID(int=700))
            session.add(tenant)
            session.flush()
            session.add(
                TenantAccountJoin(
                    account_id=case.account, tenant_id=tenant.id, role="normal"
                )
            )
        return original(attempt, **kwargs)

    monkeypatch.setattr(
        case.caller._invitation_finalizer, "_finalize_invited_login", expand
    )
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert (
        error.value.local_outcome == "committed"
        and error.value.token_outcome == "not_started"
    )
    from services.account_adapters import _INVITATION_CONSUME

    assert not any(call[0] == _INVITATION_CONSUME for call in case.redis.calls)
    assert not case.issuer.calls


@pytest.mark.parametrize("mode", ["deadline", "rbac"])
def test_invited_original_consume_is_not_replayed_after_outer_gate_changes(
    invited_session, monkeypatch, mode
):
    case = invited_session
    original = case.store.consume_versioned_invitation

    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        if mode == "deadline":
            case.chain.operation.deadline = 0.0
        else:
            monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
        return result

    monkeypatch.setattr(case.store, "consume_versioned_invitation", change)
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert (
        error.value.local_outcome == "committed"
        and error.value.token_outcome == "not_started"
    )
    from services.account_adapters import _INVITATION_CONSUME

    assert sum(call[0] == _INVITATION_CONSUME for call in case.redis.calls) == 1
    assert not case.issuer.calls
    with Session(case.chain.local.engine) as session:
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin))
            == 0
        )
        assert session.get(Issuance, case.issuance).state == "issued"


def test_invited_fresh_f2_rejects_role_drift_after_f1(invited_session, monkeypatch):
    from services.casdoor_invited_local_finalization_service_extend import (
        CasdoorInvitedLocalFinalizationService,
    )

    case = invited_session
    original = CasdoorInvitedLocalFinalizationService.finalize_invited_local_memberships

    def drift(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        with Session(case.chain.local.engine) as session, session.begin():
            session.execute(
                sa.update(TenantAccountJoin)
                .where(
                    TenantAccountJoin.tenant_id == str(UUID(int=200)),
                )
                .values(role="normal")
            )
        return result

    monkeypatch.setattr(
        CasdoorInvitedLocalFinalizationService,
        "finalize_invited_local_memberships",
        drift,
    )
    with pytest.raises(Exception) as error:
        case.invoke(ip_address="127.0.0.1")
    assert error.value.token_outcome == "not_started" and not case.issuer.calls


def test_invited_configured_avatar_creates_original_pending_outbox_and_session(
    invited_session,
):
    from pydantic import SecretStr

    case = invited_session
    config = case.chain.local.config.model_copy(update={"avatar_sync": True})
    with case.chain.coordinator._session_factory() as session, session.begin():
        owner = case.chain.configuration_service._repository(session)
        integration = owner._integration()
        draft = owner.save_draft(
            config,
            etag=integration.etag,
            actor_account_id=UUID(int=700),
            secret=SecretStr("offline-client-secret"),
        )
        integration.active_revision_id = str(draft.draft_revision_id)
        revision = owner._revision(integration.id, integration.active_revision_id)
        namespace_id, revision_id = UUID(revision.namespace_id), UUID(revision.id)
    # Real server SQL configuration and actual store create/consume, no consumed
    # transaction, verified profile or receipt is constructed by this fixture.
    case.chain.operation.config = config
    context = replace(
        case.consumed.context, namespace_id=namespace_id, revision_id=revision_id
    )

    def guard():
        return replace(
            case.chain.consume_args["guard"](),
            namespace_id=namespace_id,
            revision_id=revision_id,
        )

    created = case.chain.store.create(
        context,
        browser_scope=case.chain.consume_args["browser_scope"],
        policy=case.chain.consume_args["policy"],
        guard=guard,
    )
    case.consumed = case.chain.store.consume(
        created.state,
        **(
            case.chain.consume_args
            | {"guard": guard, "transaction_cookie": created.cookie.value}
        ),
    )
    case.chain.control.bad_id["nonce"] = case.consumed.nonce
    case.chain.profile["picture"] = "https://images.example.test/avatar.png"
    result = case.invoke(ip_address="127.0.0.1")
    assert result.token_outcome == "issued" and result.tokens is not None
    with Session(case.chain.local.engine) as session:
        avatar = session.scalar(
            sa.select(CasdoorSyncIntentExtend).where(
                CasdoorSyncIntentExtend.kind == CasdoorIntentKind.PROFILE_AVATAR,
            )
        )
        assert avatar.operation_state.value == "pending" and avatar.generation == 1
        assert case.chain.profile["picture"] not in avatar.desired_json
        assert session.get(Account, case.account).avatar is None


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_invited_existing_original_join_is_preserved_through_real_session(
    invited_session, role
):
    from services.account_service import TenantService
    from extensions.redis_names import serialize_redis_name

    case = invited_session
    token = str(uuid4())
    with Session(case.chain.local.engine) as session, session.begin():
        account = session.get(Account, case.account)
        workspace = session.get(
            Tenant, str(case.chain.local.config.default_workspace_id)
        )
        original_member = TenantService.persist_tenant_member(
            workspace, account, session, role
        )
        authority = InvitationAuthorityRepository()
        lifecycle = authority.get_lifecycle(
            session, account_id=case.account, workspace_id=workspace.id
        )
        data = json.loads(case.payload)
        data["invitation_authority"].update(
            issuance_id=str(uuid4()),
            lifecycle_epoch=lifecycle.epoch,
            token_digest=sha256(token.encode()).hexdigest(),
            join_id_at_issue=original_member.join.id,
        )
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        issuance = authority.record_issuance(session, payload_json=payload)
        case.issuance = issuance.issuance_id
        join_id = original_member.join.id
    case.redis.entries.clear()
    case.redis.token_key = serialize_redis_name(
        "member_invite:token:" + token, ""
    ).encode()
    case.redis.entries[case.redis.token_key] = ("string", payload.encode(), 60000)
    created = case.chain.store.create(
        replace(case.consumed.context, invite=token),
        browser_scope=case.chain.consume_args["browser_scope"],
        policy=case.chain.consume_args["policy"],
        guard=case.chain.consume_args["guard"],
    )
    case.consumed = case.chain.store.consume(
        created.state,
        **(case.chain.consume_args | {"transaction_cookie": created.cookie.value}),
    )
    case.chain.control.bad_id["nonce"] = case.consumed.nonce
    result = case.invoke(ip_address="127.0.0.1")
    assert result.tokens is not None and result.token_outcome == "issued"
    with Session(case.chain.local.engine) as session:
        assert session.get(TenantAccountJoin, join_id).role.value == role
        assert session.get(Issuance, case.issuance).state == "consumed"
