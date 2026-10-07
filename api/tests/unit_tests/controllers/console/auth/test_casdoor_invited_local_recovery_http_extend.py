"""Two genuine mounted authorizations; synthetic provider/wires, no live G0 claim."""

from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor import auth_transactions as auth
from libs.token import _real_cookie_name
from models.account import Account, AccountIntegrate, AccountStatus, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorIdentityExtend,
    CasdoorManagedMembershipExtend,
    CasdoorSyncIntentExtend,
)
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend,
    InvitationAuthorityLifecycleExtend,
)
from services import account_adapters as adapters
from services.casdoor_local_login_finalization_service_extend import CasdoorLocalLoginFinalizationService
from sqlalchemy.orm import Session, SessionTransaction
from test_casdoor_invited_local_http_extend import invited_mounted as original_invited_mounted
from test_casdoor_invited_local_http_extend import mounted as mounted
from test_casdoor_local_http_extend import PREFIX, cookie, send
from test_invitation_token_consumption_extend import TOKEN

pytest_plugins = ("test_casdoor_local_http_service_extend",)


def new_attempt(case, *, token=TOKEN, bootstrap=True):
    m = case.m
    # A fresh browser-scope bootstrap proves the original encrypted init again.
    # Existing scope legitimately uses the original direct new-state 302 path.
    if bootstrap:
        m.client.delete_cookie(
            auth.SCOPE_COOKIE_NAME, domain=urlsplit(m.f.settings.CONSOLE_API_URL).hostname, path=PREFIX
        )
    start = send(
        m,
        "/login",
        query_string={
            "invite_token": token,
            "return_path": "/apps/invited",
            "locale": "zh-Hans",
            "timezone": "Asia/Shanghai",
        },
    )
    assert start.status_code == (303 if bootstrap else 302)
    assert token not in start.location + str(start.headers)
    initialized = send(m, "/login", query_string=urlsplit(start.location).query) if bootstrap else start
    assert initialized.status_code == 302
    state = parse_qs(urlsplit(initialized.location).query)["state"][0]
    code = "synthetic-new-code-" + str(len(m.f.control.created))
    return state, code


def finish(case, attempt):
    state, code = attempt
    return send(case.m, "/callback", query_string={"state": state, "code": code})


def durable(case):
    with Session(case.m.f.local.engine) as session:
        result = {}
        for model in (
            InvitationAuthorityIssuanceExtend,
            InvitationAuthorityLifecycleExtend,
            CasdoorSyncIntentExtend,
            CasdoorManagedMembershipExtend,
            AccountIntegrate,
        ):
            result[model.__tablename__] = tuple(
                tuple(row)
                for row in session.execute(
                    sa.select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns)
                )
            )
        result["proofs"] = tuple(
            tuple(row)
            for row in session.execute(
                sa.select(*CasdoorAuditExtend.__table__.columns)
                .where(
                    CasdoorAuditExtend.action.in_(
                        ("invited_local_membership_write", "invited_local_membership_finalization")
                    )
                )
                .order_by(CasdoorAuditExtend.id)
            )
        )
        result["generation"] = tuple(
            session.execute(sa.select(CasdoorIdentityExtend.id, CasdoorIdentityExtend.sync_generation))
        )
        result["joins"] = tuple(
            session.execute(
                sa.select(
                    TenantAccountJoin.id,
                    TenantAccountJoin.account_id,
                    TenantAccountJoin.tenant_id,
                    TenantAccountJoin.role,
                ).order_by(TenantAccountJoin.id)
            )
        )
        result["credentials"] = tuple(
            session.execute(
                sa.select(
                    Account.email,
                    Account.password,
                    Account.password_salt,
                    Account.interface_language,
                    Account.timezone,
                    Account.interface_theme,
                )
            )
        )
        result["quota"] = tuple(
            tuple(row)
            for row in session.execute(sa.select(*AccountMoneyExtend.__table__.columns).order_by(AccountMoneyExtend.id))
        )
        return result


def no_pair(case):
    assert all(
        cookie(case.m, _real_cookie_name(name)) is None for name in ("access_token", "refresh_token", "csrf_token")
    )


invited_mounted = original_invited_mounted


@pytest.mark.parametrize("fault", ["metadata", "issuer-first", "issuer-second", "cleanup"])
@pytest.mark.parametrize("missing_quota", [False, True])
@pytest.mark.parametrize("bootstrap", [False, True])
def test_new_mounted_authorization_recovers_original_finalized_invitation(
    invited_mounted, monkeypatch, fault, missing_quota, bootstrap
):
    case, f = invited_mounted, invited_mounted.m.f
    from core.helper import ssrf_proxy
    from services.casdoor_invitation_operation_service_extend import CasdoorInvitationOperationService
    from services.casdoor_invitation_finalization_service_extend import CasdoorInvitationFinalizationService
    from services.casdoor_invited_local_membership_service_extend import CasdoorInvitedLocalMembershipService
    from services.casdoor_invited_local_finalization_service_extend import CasdoorInvitedLocalFinalizationService

    counts = {}
    for owner, name in (
        (CasdoorInvitationOperationService, "_produce_invited_login_operation"),
        (CasdoorInvitationFinalizationService, "_finalize_invited_login"),
        (CasdoorInvitedLocalMembershipService, "persist_invited_local_memberships"),
        (CasdoorInvitedLocalFinalizationService, "finalize_invited_local_memberships"),
    ):
        original_method = getattr(owner, name)

        def counted(*args, _method=original_method, _name=name, **kwargs):
            counts[_name] = counts.get(_name, 0) + 1
            return _method(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)
    exchange_codes = []
    original_transport = ssrf_proxy.make_request_with_deadline

    def actual_transport(method, url, **kwargs):
        if url.endswith("access_token"):
            exchange_codes.append(kwargs["data"]["code"])
        return original_transport(method, url, **kwargs)

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", actual_transport)
    stored = {}
    original_setex = f.redis.setex

    def setex(name, ttl, value):
        # Model an acknowledged write with a lost reply using actual gateway args.
        stored[name] = (ttl, value)
        return original_setex(name, ttl, value)

    monkeypatch.setattr(f.redis, "setex", setex)
    first = new_attempt(case)
    if fault == "metadata":
        original = CasdoorLocalLoginFinalizationService._login_metadata

        def reject(*args, **kwargs):
            raise TimeoutError("synthetic metadata fault")

        monkeypatch.setattr(CasdoorLocalLoginFinalizationService, "_login_metadata", reject)
    elif fault.startswith("issuer"):
        f.control.fail_token = 1 if fault == "issuer-first" else 2
    else:
        f.control.fail_close = True
    rejected = finish(case, first)
    assert rejected.status_code != 302 or "/signin/casdoor-result?handoff=" in rejected.location
    no_pair(case)
    if fault == "metadata":
        monkeypatch.setattr(CasdoorLocalLoginFinalizationService, "_login_metadata", original)
    f.control.fail_token, f.control.fail_close = None, False
    before = durable(case)
    assert len(before["proofs"]) == 2 and before["generation"][0][1] == 1
    if missing_quota:
        with Session(f.local.engine) as session, session.begin():
            session.execute(sa.delete(AccountMoneyExtend).where(AccountMoneyExtend.account_id == case.account))
        before = durable(case)
    token_count = len(f.control.tokens)
    original_refresh = {
        key: value for key, value in stored.items() if "refresh_token:" in key and "account_refresh_token:" not in key
    }
    second = new_attempt(case, bootstrap=bootstrap)
    assert second[0] != first[0] and second[1] != first[1]
    assert f.control.created[-1].nonce != f.control.created[-2].nonce
    response = finish(case, second)
    assert response.status_code == 302 and response.location.endswith("/apps/invited"), f.control.errors
    after = durable(case)
    assert all(after[k] == before[k] for k in before if k != "quota")
    if missing_quota:
        assert len(after["quota"]) == 1
        with Session(f.local.engine) as session:
            money = session.scalar(sa.select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == case.account))
            assert money.total_quota == dify_config.ACCOUNT_TOTAL_QUOTA and money.used_quota == 0
    else:
        assert after["quota"] == before["quota"]
    assert len(f.control.tokens) == token_count + 2
    assert all(stored[key] == value for key, value in original_refresh.items())
    assert len([key for key in stored if "refresh_token:" in key and "account_refresh_token:" not in key]) == (
        2 if token_count else 1
    )
    produced = f.control.completed[-1]
    assert produced.tokens is not None and str(produced.provenance.account_id) == case.account
    assert produced.provenance.namespace_id == f.control.consumed[-1].context.namespace_id
    assert produced.provenance.revision_id == f.control.consumed[-1].context.revision_id
    assert produced.phases.cleanup_released is True and produced.phases.token_outcome == "issued"
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == 1
    assert len(f.control.consumed) == 2
    assert len(counts) == 4 and set(counts.values()) == {1}
    assert exchange_codes == [first[1], second[1]]
    assert f.control.consumed[-1].auth_started_at > f.control.consumed[-2].auth_started_at
    assert f.control.consumed[-1].code_verifier != f.control.consumed[-2].code_verifier
    assert cookie(case.m, auth.transaction_cookie_name(second[0]), PREFIX + "/callback") is None
    access = cookie(case.m, _real_cookie_name("access_token"))
    claims = jwt.decode(access.value, dify_config.SECRET_KEY, algorithms=["HS256"])
    assert claims["user_id"] == case.account
    assert all(cookie(case.m, _real_cookie_name(name)) for name in ("refresh_token", "csrf_token"))
    calls = len(f.control.requests)
    for spent in (first, second):
        assert finish(case, spent).status_code == 400
    assert len(f.control.requests) == calls and len(f.control.tokens) == token_count + 2


@pytest.mark.parametrize("fault", ["wrong-bearer", "extra-intent", "subject", "quota-trigger", "role-drift"])
def test_actual_recovery_denies_drift_without_second_pair(invited_mounted, monkeypatch, fault):
    case, f = invited_mounted, invited_mounted.m.f
    first = new_attempt(case)
    f.control.fail_token = 1
    finish(case, first)
    f.control.fail_token = None
    before_tokens = len(f.control.tokens)
    token = TOKEN
    if fault == "wrong-bearer":
        token = "other-synthetic-bearer"
    elif fault == "extra-intent":
        with Session(f.local.engine) as session, session.begin():
            original = session.scalar(sa.select(CasdoorSyncIntentExtend))
            values = {column.name: getattr(original, column.name) for column in original.__table__.columns}
            from uuid import uuid4

            values.update(id=str(uuid4()), idempotency_key="extra-synthetic-intent")
            session.execute(sa.insert(CasdoorSyncIntentExtend).values(**values))
    elif fault == "quota-trigger":
        with f.local.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM account_money_extend")
            connection.exec_driver_sql("""CREATE TRIGGER recovery_quota_delta AFTER INSERT ON account_money_extend
            BEGIN UPDATE accounts SET interface_language='unexpected'; END""")
    else:
        from core.helper import ssrf_proxy
        from test_gateway import response as provider_response

        original = ssrf_proxy.make_request_with_deadline

        def changed(method, url, **kwargs):
            result = original(method, url, **kwargs)
            payload = result.json()
            if fault == "subject" and url.endswith("access_token"):
                payload["id_token"] = "synthetic-invalid-native"
            if fault == "role-drift" and url.endswith("get-roles"):
                payload["data"] = []
            return provider_response(payload)

        monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", changed)
    second = new_attempt(case, token=token)
    before = durable(case)
    finish(case, second)
    no_pair(case)
    assert len(f.control.tokens) == before_tokens and durable(case) == before


@pytest.mark.parametrize("phase", ["pending", "invitation_completed", "memberships_written"])
def test_incomplete_original_phases_resume_only_missing_owners_by_c(invited_mounted, monkeypatch, phase):
    from services.casdoor_invitation_finalization_service_extend import CasdoorInvitationFinalizationService
    from services.casdoor_invited_local_membership_service_extend import CasdoorInvitedLocalMembershipService
    from services.casdoor_invited_local_finalization_service_extend import CasdoorInvitedLocalFinalizationService

    owner, name = {
        "pending": (CasdoorInvitationFinalizationService, "_finalize_invited_login"),
        "invitation_completed": (CasdoorInvitedLocalMembershipService, "persist_invited_local_memberships"),
        "memberships_written": (CasdoorInvitedLocalFinalizationService, "finalize_invited_local_memberships"),
    }[phase]
    original = getattr(owner, name)

    def stop(*args, **kwargs):
        raise TimeoutError("synthetic incomplete phase")

    monkeypatch.setattr(owner, name, stop)
    case = invited_mounted
    finish(case, new_attempt(case))
    monkeypatch.setattr(owner, name, original)
    before = durable(case)
    response = finish(case, new_attempt(case, bootstrap=False))
    assert response.status_code == 302 and response.location.endswith("/apps/invited"), case.m.f.control.errors
    after = durable(case)
    assert after["generation"][0][1] == 1 and len(after["proofs"]) == 2
    assert all(after[k] == before[k] for k in ("credentials", "quota", "account_integrates"))
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == 1


@pytest.mark.parametrize("unknown", ["profile", "quota"])
def test_ack_unknown_durable_row_recovers_only_on_another_actual_authorization(invited_mounted, monkeypatch, unknown):
    case, f = invited_mounted, invited_mounted.m.f
    original_commit, original_flush = SessionTransaction.commit, Session.flush
    lost = []

    def flush(session, *args, **kwargs):
        selected = (
            any(isinstance(row, Account) and row.last_login_at is not None for row in session.dirty)
            if unknown == "profile"
            else any(isinstance(row, AccountMoneyExtend) for row in session.new)
        )
        result = original_flush(session, *args, **kwargs)
        if selected:
            session.info["d18_actual_write"] = True
        return result

    def commit(transaction, *args, **kwargs):
        session = transaction.session
        selected = transaction._parent is None and not lost and session.info.get("d18_actual_write")
        result = original_commit(transaction, *args, **kwargs)
        if selected:
            lost.append(True)
            raise TimeoutError("synthetic acknowledged commit reply lost")
        return result

    if unknown == "profile":
        monkeypatch.setattr(Session, "flush", flush)
        monkeypatch.setattr(SessionTransaction, "commit", commit)
        finish(case, new_attempt(case))
    else:
        f.control.fail_token = 1
        finish(case, new_attempt(case))
        f.control.fail_token = None
        with Session(f.local.engine) as session, session.begin():
            session.execute(sa.delete(AccountMoneyExtend))
        monkeypatch.setattr(Session, "flush", flush)
        monkeypatch.setattr(SessionTransaction, "commit", commit)
        finish(case, new_attempt(case, bootstrap=False))
    assert lost == [True]
    no_pair(case)
    before = durable(case)
    assert len(before["quota"]) == 1
    count = len(f.control.tokens)
    response = finish(case, new_attempt(case, bootstrap=False))
    assert response.status_code == 302 and response.location.endswith("/apps/invited"), f.control.errors
    after = durable(case)
    assert after == before
    assert len(f.control.tokens) == count + 2
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == 1


@pytest.mark.parametrize("changed", ["identity", "join", "history", "intent", "receipt", "link"])
def test_missing_quota_trigger_other_lineage_delta_rolls_back(invited_mounted, changed):
    case, f = invited_mounted, invited_mounted.m.f
    f.control.fail_token = 1
    finish(case, new_attempt(case))
    f.control.fail_token = None
    statements = {
        "identity": "UPDATE casdoor_identity_extend SET remote_email='unexpected@example.test'",
        "join": "UPDATE tenant_account_joins SET role='normal'",
        "history": "UPDATE casdoor_managed_membership_extend SET ownership_epoch=ownership_epoch+1",
        "intent": "UPDATE casdoor_sync_intent_extend SET attempt_count=attempt_count+1",
        "receipt": "UPDATE casdoor_audit_extend SET summary_json='{}' WHERE action='invited_local_membership_finalization'",
        "link": "UPDATE account_integrates SET encrypted_token='unexpected'",
    }
    with f.local.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM account_money_extend")
        connection.exec_driver_sql(
            "CREATE TRIGGER recovery_lineage_delta AFTER INSERT ON account_money_extend BEGIN "
            + statements[changed]
            + "; END"
        )
    before = durable(case)
    finish(case, new_attempt(case, bootstrap=False))
    no_pair(case)
    assert durable(case) == before and len(f.control.tokens) == 1


@pytest.mark.parametrize("local_edit", [False, True])
def test_fresh_recovery_profile_uses_original_owner_and_preserves_local_email(invited_mounted, monkeypatch, local_edit):
    from core.helper import ssrf_proxy
    from test_gateway import response as provider_response

    case, f = invited_mounted, invited_mounted.m.f
    from pydantic import SecretStr
    from uuid import UUID

    # Configure the actual profile owner before either transaction exists;
    # this controlled active pointer is the original offline setup seam.
    managed = f.local.config.model_copy(update={"name_sync": "managed"})
    with Session(f.local.engine) as session, session.begin():
        owner = f.config._repository(session)
        integration = owner._integration()
        saved = owner.save_draft(
            managed,
            etag=integration.etag,
            actor_account_id=UUID(int=700),
            secret=SecretStr("offline-http-client-secret"),
        )
        integration.active_revision_id = str(saved.draft_revision_id)
        session.flush()
    f.local.config = managed
    from datetime import datetime

    with Session(f.local.engine) as session, session.begin():
        account = session.get(Account, case.account)
        account.status = AccountStatus.ACTIVE
        account.initialized_at = datetime(2024, 1, 1)
        account.name = ""
    # Only the real first profile FILLED_EMPTY write establishes management;
    # INITIALIZE_INVITED with a nonempty name deliberately never adopts it.
    f.control.fail_token = 1
    finish(case, new_attempt(case))
    f.control.fail_token = None
    if local_edit:
        with Session(f.local.engine) as session, session.begin():
            session.get(Account, case.account).name = "Manual local name"
    before = durable(case)
    original = ssrf_proxy.make_request_with_deadline

    def profile(method, url, **kwargs):
        result = original(method, url, **kwargs)
        if url.endswith("userinfo"):
            value = result.json()
            value.update(name="Fresh remote name", email="changed@example.test")
            return provider_response(value)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", profile)
    result = finish(case, new_attempt(case, bootstrap=False))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), f.control.errors
    assert durable(case) == before
    with Session(f.local.engine) as session:
        account = session.get(Account, case.account)
        identity = session.scalar(
            sa.select(CasdoorIdentityExtend).where(CasdoorIdentityExtend.account_id == case.account)
        )
        assert account.name == ("Manual local name" if local_edit else "Fresh remote name")
        assert account.email == "new@example.test" and identity.remote_email == "changed@example.test"


@pytest.mark.parametrize("loss", ["tail-entry", "late-lease", "cleanup"])
def test_recovery_late_failures_never_deliver_an_issued_pair(invited_mounted, monkeypatch, loss):
    from services.casdoor_invited_local_login_coordinator_service_extend import (
        CasdoorInvitedLocalLoginCoordinatorService,
    )

    case, f = invited_mounted, invited_mounted.m.f
    f.control.fail_token = 1
    finish(case, new_attempt(case))
    f.control.fail_token = None
    if loss == "tail-entry":
        original = CasdoorInvitedLocalLoginCoordinatorService._ensure_recovery_quota

        def drift(owner, continuation, tail):
            result = original(owner, continuation, tail)
            with Session(f.local.engine) as session, session.begin():
                session.get(Account, case.account).password = "synthetic concurrent credential"
            return result

        monkeypatch.setattr(CasdoorInvitedLocalLoginCoordinatorService, "_ensure_recovery_quota", drift)
    elif loss == "late-lease":
        f.control.token_hook = lambda: f.lease.data.clear() if len(f.control.tokens) == 3 else None
    second = new_attempt(case, bootstrap=False)
    if loss == "cleanup":
        f.control.fail_close = True
    result = finish(case, second)
    assert result.status_code != 302 or "/signin/casdoor-result?handoff=" in result.location
    no_pair(case)
    assert len(f.control.tokens) == (1 if loss == "tail-entry" else 3)
    assert all(getattr(row, "provenance", None) is None for row in f.control.completed)
