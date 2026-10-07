"""Actual registered invited start/init/native callback/SQL/session, offline only.

Synthetic signed provider and Redis wire models do not establish live G0, provider,
physical Lua or browser acceptance. No consumed transaction or receipt is replaced.
"""

import json
import math
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import jwt
import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor import auth_transactions as auth
from enums import DeploymentEdition
from libs.token import _real_cookie_name
from models.account import Account, AccountIntegrate, AccountStatus, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorIdentityExtend,
    CasdoorManagedMembershipExtend,
    CasdoorSyncIntentExtend,
)
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from repositories.account_activation_repository import (
    SQLAlchemyAccountActivationRepository,
)
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
)
from services import account_adapters as adapters
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from sqlalchemy.orm import Session
from test_auth_initialization import UNSET, canonical, create, consume, record
from test_auth_initialization import env as original_init_env
from test_casdoor_local_http_extend import (
    PREFIX,
    begin,
    callback,
    cookie,
    mounted as original_mounted,
    send,
)
from test_invitation_token_consumption_extend import TOKEN, FakeRedis as InvitationRedis

pytest_plugins = ("test_casdoor_local_http_service_extend",)
mounted = original_mounted
init_env = original_init_env


def initialization_wire(wire, original, script, numkeys, *args):
    """Exact v1/v2 outer Lua protocol model, without pretending to run Lua."""
    if script != auth.CONSUME_INITIALIZATION_SCRIPT:
        return original(script, numkeys, *args)
    assert numkeys == 1
    key, *argv = args
    with wire.lock:
        wire.calls.append((script, numkeys, args))
        if wire.before_fault:
            raise ConnectionError("synthetic init before-IO fault")
        raw, expiry = wire.records.get(key, (None, 0))
        result = None
        if raw and expiry - wire.now >= 1 and len(raw.encode()) <= 16384:
            try:
                item = json.loads(raw)
            except ValueError:
                item = None
            if (
                type(item) is dict
                and set(item) == {"schema_version", "owner", "context", "data"}
                and type(item["schema_version"]) in (int, float)
                and math.isfinite(item["schema_version"])
                and item["schema_version"] in (1, 2)
                and type(item["owner"]) is str
                and item["owner"] == argv[0]
                and type(item["context"]) is str
                and item["context"] == argv[1]
                and type(item["data"]) is str
                and 0 < len(item["data"].encode()) <= 16384
            ):
                del wire.records[key]
                result = raw
    if wire.after_eval:
        wire.after_eval()
    if wire.lose_reply:
        raise TimeoutError("synthetic init lost reply")
    return result if wire.reply is UNSET else wire.reply


@pytest.fixture
def invited_mounted(mounted, monkeypatch):
    m, f = mounted, mounted.f
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    for model in (Issuance, AccountIntegrate):
        model.__table__.create(f.local.engine, checkfirst=True)
    with Session(f.local.engine) as session, session.begin():
        account = Account(
            name="Local preserved name",
            email="new@example.test",
            status=AccountStatus.PENDING,
            password="synthetic-owned-password",
            password_salt="synthetic-owned-salt",
            interface_language="ja-JP",
            timezone="Asia/Tokyo",
            interface_theme="dark",
        )
        session.add(account)
        session.flush()
        session.add(
            AccountIntegrate(
                account_id=account.id,
                provider="oauth2",
                open_id="synthetic-old-provider",
                encrypted_token="synthetic-old-envelope",
            )
        )
        authority = InvitationAuthorityRepository()
        lifecycle = authority.set_lifecycle_state(
            session,
            account_id=account.id,
            workspace_id=str(f.local.config.default_workspace_id),
            state="active",
        )
        payload = canonical(
            dict(
                account_id=account.id,
                email=account.email,
                workspace_id=str(f.local.config.default_workspace_id),
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
        )
        issuance = authority.record_issuance(session, payload_json=payload)
        account_id, issuance_id = account.id, issuance.issuance_id
    wire = InvitationRedis(prefix=f.redis._get_prefix())
    wire.entries[wire.token_key] = ("string", payload.encode(), 60000)
    store = RedisInvitationTokenStore(redis=f.redis)
    effects = [Mock() for _ in range(4)]
    effects[1].get_freeze_type.return_value = None
    activation = AccountActivationService(
        tokens=store,
        accounts=SQLAlchemyAccountActivationRepository(f.coordinator._session_factory),
        workspace_policy=effects[0],
        eligibility=effects[1],
        membership_cache=effects[2],
        member_access_sync=effects[3],
    )
    original_eval = f.redis.eval

    def redis_eval(script, numkeys, *args):
        assert not f.opened and not f.local.session.in_transaction()
        if script in (
            adapters._INVITATION_OBSERVE,
            adapters._INVITATION_CONSUME,
            adapters._INVITATION_READBACK,
        ):
            return wire.eval(script, numkeys, *args)
        return original_eval(script, numkeys, *args)

    monkeypatch.setattr(f.redis, "eval", redis_eval)

    def redis_get(key):
        assert not f.opened and not f.local.session.in_transaction()
        return wire.get(key)

    monkeypatch.setattr(f.redis, "get", redis_get, raising=False)
    init_eval = f.init.eval
    monkeypatch.setattr(f.init, "eval", lambda *args: initialization_wire(f.init, init_eval, *args))
    # Reuse the two pre-existing concrete offline production seams. All bound
    # runtime/caller/store/native/SQL methods execute their production sources.
    monkeypatch.setattr(f.service, "_account_activation", activation)
    f.control.errors = []
    original_error = f.service._public_error

    def observed_error(error, *args):
        f.control.errors.append((type(error).__name__, getattr(error, "reason", None)))
        return original_error(error, *args)

    monkeypatch.setattr(f.service, "_public_error", observed_error)
    f.control.completed = []
    original_complete = f.service.complete

    def observed_complete(**kwargs):
        result = original_complete(**kwargs)
        f.control.completed.append(result)
        return result

    monkeypatch.setattr(f.service, "complete", observed_complete)
    return SimpleNamespace(
        m=m,
        account=account_id,
        issuance=issuance_id,
        wire=wire,
        effects=effects,
        activation=activation,
    )


@pytest.mark.parametrize(
    "status,initialized,quota",
    [
        (AccountStatus.PENDING, False, False),
        (AccountStatus.UNINITIALIZED, False, False),
        (AccountStatus.ACTIVE, False, True),
        (AccountStatus.PENDING, True, True),
    ],
)
def test_registered_invite_auto_session_original_init_native_and_sql(invited_mounted, status, initialized, quota):
    case, m = invited_mounted, invited_mounted.m
    with Session(m.f.local.engine) as session, session.begin():
        account = session.get(Account, case.account)
        account.status = status
        if initialized:
            account.initialized_at = datetime(2024, 1, 1)
        if quota:
            session.add(
                AccountMoneyExtend(
                    account_id=case.account,
                    total_quota=Decimal("91.25"),
                    used_quota=Decimal("4.5"),
                )
            )
    first = send(
        m,
        "/login",
        query_string={
            "invite_token": TOKEN,
            "locale": "zh-Hans",
            "timezone": "Asia/Shanghai",
            "return_path": "/apps/invited",
        },
    )
    assert first.status_code == 303
    assert TOKEN not in first.location + first.get_data(as_text=True) + str(first.headers)
    raw_records = [raw for raw, _ in m.f.init.records.values()]
    assert len(raw_records) == 1 and TOKEN not in raw_records[0]
    outer = json.loads(raw_records[0])
    assert outer["schema_version"] == 2 and "encrypted_invite" in json.loads(outer["data"])
    second = send(m, "/login", query_string=first.location.split("?", 1)[1])
    assert second.status_code == 302 and TOKEN not in second.location
    state = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(second.location.split("?", 1)[1])["state"][0]
    response = callback(m, state)
    assert (
        response.status_code == 302 and response.location == m.f.settings.CONSOLE_WEB_URL + "/apps/invited"
    ), m.f.control.errors
    assert len(response.headers.getlist("Set-Cookie")) == 4
    access = cookie(m, _real_cookie_name("access_token"))
    refresh = cookie(m, _real_cookie_name("refresh_token"))
    csrf = cookie(m, _real_cookie_name("csrf_token"))
    assert access and refresh and csrf and access.http_only and refresh.http_only and not csrf.http_only
    claims = jwt.decode(access.value, dify_config.SECRET_KEY, algorithms=["HS256"])
    assert claims["user_id"] == case.account
    produced = m.f.control.completed[-1]
    consumed = m.f.control.consumed[-1]
    assert produced.tokens.access_token == access.value
    assert str(produced.provenance.account_id) == case.account
    assert produced.provenance.namespace_id == consumed.context.namespace_id
    assert produced.provenance.revision_id == consumed.context.revision_id
    assert produced.provenance.id_token_expires_at > datetime.now().timestamp()
    assert produced.phases.local_outcome == "committed" and produced.phases.finalization_outcome == "committed"
    assert produced.phases.token_outcome == "issued" and produced.phases.cleanup_released is True
    assert m.f.control.consumed[-1].context.invite == TOKEN
    assert m.f.control.consumed[-1].nonce == m.f.control.created[-1].nonce
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is None
    assert len(m.f.control.tokens) == 2
    assert len([p for p, _ in m.f.control.requests if p.endswith("access_token")]) == 1
    with Session(m.f.local.engine) as session:
        account = session.get(Account, case.account)
        assert account.status == AccountStatus.ACTIVE and account.initialized_at is not None
        assert (account.password, account.password_salt, account.email) == (
            "synthetic-owned-password",
            "synthetic-owned-salt",
            "new@example.test",
        )
        assert account.last_login_ip == "192.0.2.7"
        if not initialized:
            assert (account.interface_language, account.timezone) == (
                "zh-Hans",
                "Asia/Shanghai",
            )
        else:
            assert (account.interface_language, account.timezone) == (
                "ja-JP",
                "Asia/Tokyo",
            )
        money = session.scalar(sa.select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == case.account))
        assert money is not None
        if quota:
            assert (money.total_quota, money.used_quota) == (
                Decimal("91.25"),
                Decimal("4.5"),
            )
        identity = session.scalar(
            sa.select(CasdoorIdentityExtend).where(CasdoorIdentityExtend.account_id == case.account)
        )
        assert identity.sync_generation == 1
        intent = session.scalar(
            sa.select(CasdoorSyncIntentExtend).where(CasdoorSyncIntentExtend.account_id == case.account)
        )
        assert intent.termination_state.value == "confirmed" and intent.operation_state.value == "applied"
        assert session.get(Issuance, case.issuance).state == "consumed"
        history = session.scalar(
            sa.select(CasdoorManagedMembershipExtend).where(CasdoorManagedMembershipExtend.account_id == case.account)
        )
        assert history.finalization.value == "finalized"
        joins = tuple(session.scalars(sa.select(TenantAccountJoin).where(TenantAccountJoin.account_id == case.account)))
        join = next(row for row in joins if row.tenant_id == str(m.f.local.config.default_workspace_id))
        assert join.role == "editor" and sum(row.current is True for row in joins) == 1
        assert (
            session.scalar(sa.select(AccountIntegrate).where(AccountIntegrate.account_id == case.account)).open_id
            == "synthetic-old-provider"
        )
    assert case.wire.entries.get(case.wire.token_key) is None
    assert len([c for c in case.wire.calls if c[0] == adapters._INVITATION_CONSUME]) == 1
    assert TOKEN not in response.location + response.get_data(as_text=True)
    retry = callback(m, state)
    assert retry.status_code == 400 and len(m.f.control.tokens) == 2


@pytest.fixture
def encrypted_init(init_env, monkeypatch):
    e = init_env
    original = e.raw.eval
    monkeypatch.setattr(e.raw, "eval", lambda *args: initialization_wire(e.raw, original, *args))
    return e


def test_encrypted_invite_init_original_context_is_one_use(encrypted_init):
    from dataclasses import replace

    e = encrypted_init
    expected = replace(e.context, invite=TOKEN)
    handle = create(e, context=expected)
    _, item, _ = record(e)
    assert item["schema_version"] == 2 and TOKEN not in canonical(item)
    assert consume(e, handle) == expected
    with pytest.raises(auth.AuthTransactionError):
        consume(e, handle)


@pytest.mark.parametrize("fault", ["ciphertext", "cross-handle", "public-invite", "postguard"])
def test_encrypted_init_invalid_or_drifted_stays_spent(encrypted_init, fault):
    from dataclasses import replace

    e = encrypted_init
    handle = create(e, context=replace(e.context, invite=TOKEN))
    key, item, expiry = record(e)
    data = json.loads(item["data"])
    if fault == "ciphertext":
        data["encrypted_invite"] = "invalid"
    elif fault == "cross-handle":
        other = create(e, context=replace(e.context, invite=TOKEN))
        other_key, _, _ = record(e)
        e.raw.records[other_key] = (canonical(item), expiry)
        with pytest.raises(auth.AuthTransactionError):
            consume(e, other)
        assert other_key not in e.raw.records
        return
    elif fault == "public-invite":
        data["context"]["invite"] = TOKEN
    elif fault == "postguard":
        e.raw.after_eval = lambda: e.current.__setitem__(0, replace(e.current[0], allowed=False))
    item["data"] = canonical(data)
    e.raw.records[key] = (canonical(item), expiry)
    with pytest.raises(auth.AuthTransactionError):
        consume(e, handle)
    assert key not in e.raw.records


@pytest.mark.parametrize(
    "fault",
    [
        "native-nonce",
        "unverified-email",
        "other-email",
        "expired-invite",
        "token-first",
        "token-second",
        "runtime-close",
        "lease-close",
    ],
)
def test_registered_invite_failures_never_deliver_session(invited_mounted, monkeypatch, fault):
    from core.helper import ssrf_proxy
    from test_gateway import response as provider_response

    case, m = invited_mounted, invited_mounted.m
    state = begin(m, invite_token=TOKEN)
    if fault == "native-nonce":
        m.f.control.bad_nonce = True
    elif fault in ("unverified-email", "other-email"):
        original = ssrf_proxy.make_request_with_deadline

        def transport(method, url, **kwargs):
            result = original(method, url, **kwargs)
            if url.endswith("/api/userinfo"):
                profile = result.json()
                profile["email_verified"] = fault != "unverified-email"
                if fault == "other-email":
                    profile["email"] = "different@example.test"
                return provider_response(profile)
            return result

        monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    elif fault == "expired-invite":
        case.wire.entries.clear()
    elif fault in ("token-first", "token-second"):
        m.f.control.fail_token = 1 if fault == "token-first" else 2
    elif fault == "runtime-close":
        m.f.control.fail_close = True
    elif fault == "lease-close":
        m.f.control.fail_release = True
    response = callback(m, state)
    assert not any(cookie(m, _real_cookie_name(name)) for name in ("access_token", "refresh_token", "csrf_token"))
    assert cookie(m, auth.transaction_cookie_name(state), PREFIX + "/callback") is None
    assert TOKEN not in response.get_data(as_text=True) + str(response.headers)
    assert response.status_code != 302 or "/signin/casdoor-result?handoff=" in response.location
    assert len(m.f.control.consumed) == 1
    if fault in ("native-nonce", "unverified-email", "other-email", "expired-invite"):
        assert not m.f.control.tokens
        with Session(m.f.local.engine) as session:
            assert session.get(Account, case.account).status == AccountStatus.PENDING
            assert not tuple(session.scalars(sa.select(CasdoorIdentityExtend)))
            assert session.get(Issuance, case.issuance).state == "issued"
    elif fault != "lease-close":
        with Session(m.f.local.engine) as session:
            assert session.get(Issuance, case.issuance).state == "consumed"
    if fault in ("token-first", "token-second", "runtime-close", "lease-close"):
        assert len([c for c in case.wire.calls if c[0] == adapters._INVITATION_CONSUME]) == 1


def test_registered_invite_init_query_closed_and_legacy_ordinary_compatible(
    invited_mounted,
):
    m = invited_mounted.m
    first = send(m, "/login", query_string={"invite_token": TOKEN})
    assert first.status_code == 303
    from urllib.parse import parse_qs, urlsplit

    handle = parse_qs(urlsplit(first.location).query)["init"][0]
    for query in (
        {"init": handle, "invite_token": TOKEN},
        [("invite_token", TOKEN), ("invite_token", TOKEN)],
        {"invite_token": ""},
        {"invite_token": "x" * 513},
        {"selector": "invited", "invite_token": TOKEN},
    ):
        response = send(m, "/login", query_string=query)
        assert response.status_code == 400
    assert not m.f.control.created and not m.f.control.requests
    second = send(m, "/login", query_string={"init": handle})
    assert second.status_code == 302
    assert m.f.control.created[-1].state


def test_registered_invite_logging_and_query_schema_hide_authority(invited_mounted, caplog):
    import logging
    from controllers.console.auth.casdoor_extend import CasdoorLoginQuery
    from controllers.console import api

    m = invited_mounted.m
    caplog.set_level(logging.DEBUG, logger="extensions.ext_request_logging")
    state = begin(m, invite_token=TOKEN, return_path="/apps")
    response = callback(m, state)
    assert response.status_code == 302
    assert TOKEN not in caplog.text and state not in caplog.text
    assert "Received Request GET" in caplog.text
    assert TOKEN not in repr(CasdoorLoginQuery(invite_token=TOKEN))
    with m.app.test_request_context():
        schema = api.__schema__
    params = schema["paths"]["/auth/casdoor/login"]["get"]["parameters"]
    invitation = next(p for p in params if p["name"] == "invite_token")
    assert invitation["in"] == "query"
    invitation_schema = CasdoorLoginQuery.model_json_schema()["properties"]["invite_token"]
    assert any(item.get("maxLength") == 512 for item in invitation_schema["anyOf"])
