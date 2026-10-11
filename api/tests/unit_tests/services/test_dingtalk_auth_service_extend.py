"""Security invariants for DingTalk onboarding and identity admission.

SQLite verifies bounded local writes. Provider and Redis protocol adapters are
injected; these tests do not claim live DingTalk registration/login acceptance.
"""

import json
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from models.account import Account, AccountIntegrate, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.system_extend import SystemIntegrationExtend
from models.system_management_scope_extend import SystemManagementScopeExtend
from services.account_service import AccountService
from services.dingtalk_auth_service_extend import DingTalkAuthError, DingTalkAuthService, DingTalkIdentity
from services.system_manage_extend import _encrypt_blowfish
from sqlalchemy.orm import sessionmaker


class MemoryRedis:
    def __init__(self, clock):
        self.clock = clock
        self.values = {}

    def get(self, key):
        item = self.values.get(key)
        return item[0] if item and item[1] > self.clock() else None

    def set(self, key, value, *, nx=False, ex=300):
        if nx and self.get(key) is not None:
            return False
        self.values[key] = (value, self.clock() + ex)
        return True

    def eval(self, script, count, key, value, *update):
        if self.get(key) != value:
            return 0
        if update:
            (replacement,) = update
            self.values[key] = (replacement, self.values[key][1])
            return 1
        self.values.pop(key, None)
        return 1


@pytest.fixture
def system(monkeypatch):
    engine = sa.create_engine("sqlite://")
    for table in (
        Account.__table__,
        Tenant.__table__,
        TenantAccountJoin.__table__,
        AccountIntegrate.__table__,
        SystemManagementScopeExtend.__table__,
    ):
        table.create(engine)
    SystemIntegrationExtend.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(name="Initialization")
        old = Tenant(name="Earlier imported workspace")
        old.created_at = datetime(2020, 1, 1)
        owner = Account(
            name="Local owner",
            email="owner@example.test",
            normalized_email="owner@example.test",
            initialized_at=datetime(2026, 10, 11),
            status=AccountStatus.ACTIVE,
        )
        session.add_all([tenant, old, owner])
        session.flush()
        session.add_all(
            [
                TenantAccountJoin(account_id=owner.id, tenant_id=tenant.id, role=TenantAccountRole.OWNER, current=True),
                SystemManagementScopeExtend(tenant_id=tenant.id),
                SystemIntegrationExtend(
                    id=1,
                    classify=1,
                    status=False,
                    app_key="synthetic-client",
                    corp_id="synthetic-corp",
                    app_secret=_encrypt_blowfish("synthetic-secret", "test-key"),
                    config=json.dumps({"_dingtalk": {"revision": "r1"}}),
                ),
            ]
        )
        session.flush()
        owner.set_tenant_id_with_session(tenant.id, session=session)
    clock = [1000]
    settings = SimpleNamespace(
        SECRET_KEY="test-key",
        CONSOLE_API_URL="https://console.example.test",
        CONSOLE_WEB_URL="https://console.example.test",
        DEPLOY_ENV="PRODUCTION",
        DEFAULT_LANGUAGE="en-US",
        RBAC_ENABLED=False,
    )
    redis = MemoryRedis(lambda: clock[0])
    service = DingTalkAuthService(session_factory=factory, redis=redis, settings=settings, clock=lambda: clock[0])
    monkeypatch.setattr("services.dingtalk_auth_service_extend.sensitive_redis_call", nullcontext)
    yield service, owner, tenant.id, factory, redis, clock
    engine.dispose()


def verify(system):
    service, owner, _, factory, _, _ = system
    with factory.begin() as session:
        record = session.get(SystemIntegrationExtend, 1)
        config = service._config(record)
        config["_dingtalk"].update(connection_revision="r1", verified_revision="r1")
        service._save_config(record, config)
    service.enable(owner)


def test_old_enabled_configuration_is_not_publicly_enabled(system):
    service, _, _, factory, _, _ = system
    with factory.begin() as session:
        record = session.get(SystemIntegrationExtend, 1)
        record.status = True
        record.config = "{}"
    assert service.display()["enabled"] is False


def test_save_cannot_activate_and_changes_invalidate_verification(system):
    service, owner, _, _, _, _ = system
    with pytest.raises(DingTalkAuthError, match="login_verification_required"):
        service.save(owner, {"status": True})
    verify(system)
    view = service.save(owner, {"app_key": "replacement"})
    assert view["status"] is False
    assert view["verified"] is False
    assert view["revision"] != "r1"
    with pytest.raises(DingTalkAuthError, match="configuration_changed"):
        service.save(owner, {"expected_revision": "r1", "app_key": "stale"})


def test_blank_secret_preserves_and_email_credentials_are_encrypted(system):
    service, owner, _, factory, _, _ = system
    view = service.save(
        owner,
        {
            "app_secret": "",
            "config": {
                "email_api": {
                    "enabled": True,
                    "url": "https://directory.example.test",
                    "headers": {"X-Key": "private-header"},
                    "authorization": {"type": "bearer", "token": "private-bearer"},
                    "body_data": {"raw": '{"key":"private-body"}'},
                }
            },
        },
    )
    assert view["secret_configured"]
    assert view["app_secret"] == ""
    assert "private-" not in json.dumps(view)
    with factory() as session:
        record = session.get(SystemIntegrationExtend, 1)
        assert "private-" not in record.config
        assert service._config(record)["email_api"]["authorization"]["token"] == "private-bearer"
    service.save(owner, {"config": view["config"]})
    with factory() as session:
        assert service._config(session.get(SystemIntegrationExtend, 1))["email_api"]["headers"] == {
            "X-Key": "private-header"
        }


def test_server_metadata_cannot_be_submitted(system):
    service, owner, *_ = system
    with pytest.raises(DingTalkAuthError, match="server_owned_configuration"):
        service.save(owner, {"config": {"_dingtalk": {"verified_revision": "r1"}}})


@pytest.mark.parametrize("change", ["normal", "banned", "wrong_workspace"])
def test_management_reads_use_fresh_database_authority(system, change):
    service, owner, tenant_id, factory, _, _ = system
    with factory.begin() as session:
        if change == "banned":
            session.get(Account, owner.id).status = AccountStatus.BANNED
        elif change == "normal":
            session.scalar(sa.select(TenantAccountJoin)).role = TenantAccountRole.NORMAL
        else:
            anchor = session.get(SystemManagementScopeExtend, "initialization")
            anchor.tenant_id = session.scalar(sa.select(Tenant.id).where(Tenant.id != tenant_id))
    with pytest.raises(DingTalkAuthError, match="system_management_forbidden"):
        service.get(owner)


def test_single_use_state_browser_binding_and_expiry(system):
    service, _, _, _, _, clock = system
    verify(system)
    uri, state, cookie = service.authorization_start(server_ip="one")
    assert "state=" + state in uri
    with pytest.raises(DingTalkAuthError, match="invalid_transaction"):
        service.consume(state, "x" * 43)
    transaction = service.consume(state, cookie)
    assert transaction["revision"] == "r1"
    with pytest.raises(DingTalkAuthError, match="invalid_transaction"):
        service.consume(state, cookie)
    _, other, other_cookie = service.authorization_start(server_ip="two")
    clock[0] += 301
    with pytest.raises(DingTalkAuthError, match="invalid_transaction"):
        service.consume(other, other_cookie)


def test_pending_login_rejects_disable_before_provider_exchange(system, monkeypatch):
    service, owner, *_ = system
    verify(system)
    _, state, cookie = service.authorization_start(server_ip="one")
    transaction = service.consume(state, cookie)
    service.disable(owner)
    provider = Mock()
    monkeypatch.setattr(service, "_identity", provider)
    with pytest.raises(DingTalkAuthError, match="login_disabled"):
        service.complete(transaction, "code")
    provider.assert_not_called()


def registration(system, monkeypatch, responses=None):
    service, owner, *_ = system
    responses = responses or [
        {"nonce": "private-nonce"},
        {
            "device_code": "private-device",
            "verification_uri_complete": "https://login.dingtalk.com/qr",
            "expires_in": 300,
            "interval": 1,
        },
    ]
    request = Mock(side_effect=responses)
    monkeypatch.setattr(service, "_request", request)
    return service.registration_start(owner), request


def test_zero_credential_registration_encrypts_device_and_throttles_poll(system, monkeypatch):
    service, owner, _, _, redis, _ = system
    qr, request = registration(system, monkeypatch)
    assert request.call_args_list[0].kwargs["json"] == {}
    assert qr["interval"] == 5
    assert "private-" not in json.dumps(qr)
    assert "private-device" not in str(redis.values)
    request.side_effect = None
    request.return_value = {"status": "WAITING"}
    service.registration_poll(owner, qr["transaction_id"])
    service.registration_poll(owner, qr["transaction_id"])
    assert request.call_count == 3


def test_registration_success_saves_unverified_and_terminal_replay_does_not_write(system, monkeypatch):
    service, owner, *_ = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {"status": "SUCCESS", "client_id": "new-client", "client_secret": "private-new-secret"}
    result = service.registration_poll(owner, qr["transaction_id"])
    assert result["status"] == "SUCCESS"
    assert "private-" not in json.dumps(result)
    view = service.get(owner)
    assert view["app_key"] == "new-client"
    assert view["secret_configured"] and not view["status"] and not view["verified"]
    revision = view["revision"]
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "SUCCESS"
    assert service.get(owner)["revision"] == revision
    assert request.call_count == 3


@pytest.mark.parametrize("transient", ["UNKNOWN", "provisioning", "", None, 1, "request_failed"])
def test_registration_transient_then_success_accepts_only_success_credentials(system, monkeypatch, transient):
    service, owner, _, _, redis, clock = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {
        "status": transient,
        "client_id": "untrusted-client",
        "client_secret": "private-untrusted-secret",
    }
    if transient == "request_failed":
        request.side_effect = DingTalkAuthError("provider_request_failed", 502)
    key = service._registration_key(qr["transaction_id"])
    deadline = redis.values[key][1]
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "WAITING"
    assert service.get(owner)["app_key"] == "synthetic-client"
    stored = service._open(redis.get(key), key)
    assert stored["retry_started_at"] == clock[0]
    assert "untrusted" not in json.dumps(stored)
    clock[0] += 5
    request.side_effect = None
    request.return_value = {"status": " success ", "client_id": "new-client", "client_secret": "private-new-secret"}
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "SUCCESS"
    assert service.get(owner)["app_key"] == "new-client"
    assert redis.values[key][1] <= deadline


@pytest.mark.parametrize("transient", ["UNKNOWN", "request_failed"])
def test_registration_transient_fails_at_retry_deadline(system, monkeypatch, transient):
    service, owner, _, _, redis, clock = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {"status": transient}
    if transient == "request_failed":
        request.side_effect = DingTalkAuthError("provider_request_failed", 502)
    key = service._registration_key(qr["transaction_id"])
    deadline = redis.values[key][1]
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "WAITING"
    clock[0] += 119
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "WAITING"
    calls = request.call_count
    clock[0] += 1
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "FAIL"
    assert request.call_count == calls
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "FAIL"
    assert service.get(owner)["app_key"] == "synthetic-client"
    assert redis.values[key][1] <= deadline
    stored = service._open(redis.get(key), key)
    assert "device_code" not in stored and "retry_started_at" not in stored


def test_registration_waiting_resets_continuous_transient_window(system, monkeypatch):
    service, owner, _, _, redis, clock = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {"status": "UNKNOWN"}
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "WAITING"
    clock[0] += 115
    request.return_value = {"status": " waiting "}
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "WAITING"
    key = service._registration_key(qr["transaction_id"])
    assert "retry_started_at" not in service._open(redis.get(key), key)
    clock[0] += 5
    request.return_value = {"status": "UNKNOWN"}
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "WAITING"
    assert service._open(redis.get(key), key)["retry_started_at"] == clock[0]


def test_registration_retry_cannot_extend_original_ttl(system, monkeypatch):
    service, owner, _, _, redis, clock = system
    qr, request = registration(
        system,
        monkeypatch,
        [
            {"nonce": "private-nonce"},
            {
                "device_code": "private-device",
                "verification_uri_complete": "https://login.dingtalk.com/qr",
                "expires_in": 20,
            },
        ],
    )
    request.side_effect = None
    request.return_value = {"status": "UNKNOWN"}
    key = service._registration_key(qr["transaction_id"])
    deadline = redis.values[key][1]
    for elapsed in (0, 5, 10, 15):
        clock[0] = 1000 + elapsed
        assert service.registration_poll(owner, qr["transaction_id"])["status"] == "WAITING"
        assert redis.values[key][1] <= deadline
    calls = request.call_count
    clock[0] = deadline
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "EXPIRED"
    assert request.call_count == calls


def test_registration_poll_response_after_ttl_cannot_save_credentials(system, monkeypatch):
    service, owner, _, _, _, clock = system
    qr, request = registration(system, monkeypatch)

    def expired_during_provider(*args, **kwargs):
        clock[0] += 301
        return {"status": "SUCCESS", "client_id": "expired-client", "client_secret": "private-expired-secret"}

    request.side_effect = expired_during_provider
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "EXPIRED"
    assert service.get(owner)["app_key"] == "synthetic-client"


def test_registration_success_without_credentials_is_never_accepted(system, monkeypatch):
    service, owner, *_ = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {"status": " success ", "client_id": "new-client"}
    with pytest.raises(DingTalkAuthError, match="invalid_provider_response"):
        service.registration_poll(owner, qr["transaction_id"])
    assert service.get(owner)["app_key"] == "synthetic-client"


@pytest.mark.parametrize("status", [" waiting ", " fail ", " expired "])
def test_registration_normalizes_provider_status(system, monkeypatch, status):
    service, owner, *_ = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {"status": status}
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == status.strip().upper()


@pytest.mark.parametrize(
    ("unified", "legacy", "expected"),
    [
        ("unified-app", "legacy-app", "unified-app"),
        (None, "legacy-app", "legacy-app"),
        (123, "legacy-app", "legacy-app"),
    ],
)
def test_registration_success_maps_provider_metadata(system, monkeypatch, unified, legacy, expected):
    service, owner, _, factory, *_ = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {
        "status": "SUCCESS",
        "client_id": "new-client",
        "client_secret": "private-new-secret",
        "corp_id": "new-corp",
        "agent_id": "new-agent",
        "unified_app_id": unified,
        "app_id": legacy,
        "user_id": "private-user-id",
        "robot_code": "private-robot-code",
    }
    result = service.registration_poll(owner, qr["transaction_id"])
    assert result["status"] == "SUCCESS"
    view = service.get(owner)
    assert (view["corp_id"], view["agent_id"], view["app_id"]) == ("new-corp", "new-agent", expected)
    assert view["app_key"] == "new-client" and view["secret_configured"]
    assert "private-" not in json.dumps(view)
    with factory() as session:
        assert "private-user-id" not in session.get(SystemIntegrationExtend, 1).config
        assert "private-robot-code" not in session.get(SystemIntegrationExtend, 1).config


def test_registration_retry_cannot_overwrite_cancellation_after_lock_expiry(system, monkeypatch):
    service, owner, _, _, redis, clock = system
    qr, request = registration(system, monkeypatch)
    key = service._registration_key(qr["transaction_id"])

    def cancelled_during_provider(*args, **kwargs):
        clock[0] += 31
        assert service.registration_poll(owner, qr["transaction_id"], cancel=True)["status"] == "CANCELLED"
        return {"status": "UNKNOWN"}

    request.side_effect = cancelled_during_provider
    with pytest.raises(DingTalkAuthError, match="registration_cancelled"):
        service.registration_poll(owner, qr["transaction_id"])
    assert service._open(redis.get(key), key)["status"] == "CANCELLED"
    assert service.get(owner)["app_key"] == "synthetic-client"


@pytest.mark.parametrize("change", ["revision", "authority"])
def test_registration_retry_rechecks_configuration_and_authority_after_provider(system, monkeypatch, change):
    service, owner, _, factory, redis, _ = system
    qr, request = registration(system, monkeypatch)
    key = service._registration_key(qr["transaction_id"])
    before = redis.get(key)

    def changed_during_provider(*args, **kwargs):
        if change == "revision":
            service.save(owner, {"app_key": "replacement"})
        else:
            with factory.begin() as session:
                session.scalar(sa.select(TenantAccountJoin)).role = TenantAccountRole.NORMAL
        return {"status": "UNKNOWN"}

    request.side_effect = changed_during_provider
    with pytest.raises(DingTalkAuthError, match="configuration_changed|system_management_forbidden"):
        service.registration_poll(owner, qr["transaction_id"])
    assert redis.get(key) == before


@pytest.mark.parametrize("terminal", ["FAIL", "EXPIRED"])
def test_registration_provider_terminal_status(system, monkeypatch, terminal):
    service, owner, *_ = system
    qr, request = registration(system, monkeypatch)
    request.side_effect = None
    request.return_value = {"status": terminal}
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == terminal
    assert service.get(owner)["app_key"] == "synthetic-client"


def test_registration_cancel_revision_change_and_expiry(system, monkeypatch):
    service, owner, _, _, _, clock = system
    qr, _ = registration(system, monkeypatch)
    assert service.registration_poll(owner, qr["transaction_id"], cancel=True)["status"] == "CANCELLED"
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "CANCELLED"
    clock[0] += 11
    qr, request = registration(system, monkeypatch)
    service.save(owner, {"app_key": "replacement"})
    with pytest.raises(DingTalkAuthError, match="configuration_changed"):
        service.registration_poll(owner, qr["transaction_id"])
    assert request.call_count == 2
    clock[0] += 301
    assert service.registration_poll(owner, qr["transaction_id"])["status"] == "EXPIRED"


def test_existing_email_cannot_be_claimed_without_binding(system, monkeypatch):
    service, owner, *_ = system
    verify(system)
    monkeypatch.setattr(AccountService, "prepare_account_creation", Mock(return_value=object()))
    with pytest.raises(DingTalkAuthError, match="binding_required"):
        service._login(
            service._snapshot(enabled=True), DingTalkIdentity("subject", owner.email, "Remote"), ip_address=None
        )


@pytest.mark.parametrize("change", ["banned", "closed", "removed"])
def test_bound_login_cannot_restore_disabled_account_or_removed_membership(system, monkeypatch, change):
    service, owner, _, factory, *_ = system
    verify(system)
    with factory.begin() as session:
        service._bind(session, "subject", owner.id)
        if change == "removed":
            session.delete(session.scalar(sa.select(TenantAccountJoin)))
        else:
            session.get(Account, owner.id).status = AccountStatus(change)
    issue = Mock()
    monkeypatch.setattr(AccountService, "issue_token_pair", issue)
    with pytest.raises(DingTalkAuthError, match="account_unavailable|workspace_membership_required"):
        service._login(
            service._snapshot(enabled=True), DingTalkIdentity("subject", owner.email, "Remote"), ip_address=None
        )
    issue.assert_not_called()


def test_existing_owner_role_preserved_and_current_tenant_uses_anchor(system, monkeypatch):
    service, owner, tenant_id, factory, *_ = system
    verify(system)
    with factory.begin() as session:
        service._bind(session, "subject", owner.id)
    issue = Mock(return_value="tokens")
    monkeypatch.setattr(AccountService, "issue_token_pair", issue)
    assert (
        service._login(
            service._snapshot(enabled=True), DingTalkIdentity("subject", owner.email, "Remote"), ip_address="127.0.0.1"
        )
        == "tokens"
    )
    with factory() as session:
        join = session.scalar(sa.select(TenantAccountJoin).where(TenantAccountJoin.current.is_(True)))
        assert join.tenant_id == tenant_id
        assert join.role == TenantAccountRole.OWNER


def test_diagnostic_binds_only_explicit_current_account_without_issuing_session(system, monkeypatch):
    service, owner, _, factory, *_ = system
    monkeypatch.setattr(service, "_redis", lambda operation: operation())
    monkeypatch.setattr(AccountService, "resolve_refresh_token", lambda token: owner.id)
    monkeypatch.setattr(service, "_identity", lambda snapshot, code: DingTalkIdentity("subject", owner.email, "Remote"))
    issue = Mock()
    monkeypatch.setattr(AccountService, "issue_token_pair", issue)
    _, state, cookie = service.authorization_start(
        account=owner, refresh_token="local-session", bind=True, server_ip="a"
    )
    transaction = service.consume(state, cookie)
    assert service.complete(transaction, "code", account=owner, refresh_token="local-session") is None
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Account)) == 1
        assert session.scalar(sa.select(AccountIntegrate.account_id)) == owner.id
        assert session.scalar(sa.select(TenantAccountJoin.role)) == TenantAccountRole.OWNER
    assert service.get(owner)["verified"] and not service.get(owner)["status"]
    issue.assert_not_called()


def test_provider_exceptions_and_raw_responses_do_not_escape(system, monkeypatch):
    service, *_ = system
    request = Mock(side_effect=RuntimeError("private-secret-and-url"))
    monkeypatch.setattr("services.dingtalk_auth_service_extend.ssrf_proxy.make_request", request)
    with pytest.raises(DingTalkAuthError, match="provider_request_failed") as caught:
        service._request("POST", "https://api.dingtalk.com/one", json={"secret": "private-secret"})
    assert "private-" not in str(caught.value)
    assert request.call_args.kwargs["follow_redirects"] is False
    assert request.call_args.kwargs["max_retries"] == 0


def test_registration_other_administrator_cannot_poll(system, monkeypatch):
    service, owner, tenant_id, factory, *_ = system
    qr, request = registration(system, monkeypatch)
    with factory.begin() as session:
        other = Account(
            name="Other admin",
            email="other@example.test",
            initialized_at=datetime(2026, 10, 11),
            status=AccountStatus.ACTIVE,
        )
        session.add(other)
        session.flush()
        session.add(
            TenantAccountJoin(account_id=other.id, tenant_id=tenant_id, role=TenantAccountRole.ADMIN, current=True)
        )
        session.flush()
        other.set_tenant_id_with_session(tenant_id, session=session)
    with pytest.raises(DingTalkAuthError, match="registration_owner_mismatch"):
        service.registration_poll(other, qr["transaction_id"])
    assert request.call_count == 2


def test_diag_no_checkbox_does_not_bind_and_fresh_config_change_prevents_success(system, monkeypatch):
    service, owner, _, factory, *_ = system
    monkeypatch.setattr(AccountService, "resolve_refresh_token", lambda token: owner.id)
    monkeypatch.setattr(service, "_identity", lambda snapshot, code: DingTalkIdentity("subject", owner.email, "Remote"))
    _, state, cookie = service.authorization_start(account=owner, refresh_token="local-session", server_ip="a")
    assert (
        service.complete(service.consume(state, cookie), "code", account=owner, refresh_token="local-session") is None
    )
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(AccountIntegrate)) == 0
    _, state, cookie = service.authorization_start(account=owner, refresh_token="local-session", server_ip="b")
    transaction = service.consume(state, cookie)

    def mutate(snapshot, code):
        service.save(owner, {"app_key": "changed-mid-diagnostic"})
        return DingTalkIdentity("subject", owner.email, "Remote")

    monkeypatch.setattr(service, "_identity", mutate)
    with pytest.raises(DingTalkAuthError, match="configuration_changed"):
        service.complete(transaction, "code", account=owner, refresh_token="local-session")
    assert not service.get(owner)["verified"]


def test_new_account_binding_member_and_quota_are_atomic(system, monkeypatch):
    from models.account_money_extend import AccountMoneyExtend
    from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
    from services.account_service import SystemFeatureService, TenantService

    service, owner, tenant_id, factory, *_ = system
    engine = factory.kw["bind"]
    quota = AccountMoneyExtend.__table__.to_metadata(sa.MetaData())
    for field in ("created_at", "updated_at"):
        quota.c[field].server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
    quota.create(engine)
    InvitationAuthorityLifecycleExtend.__table__.create(engine)
    verify(system)
    monkeypatch.setattr(
        SystemFeatureService, "get_license", lambda: SimpleNamespace(seats=SimpleNamespace(is_available=lambda: True))
    )
    monkeypatch.setattr("services.account_service.dify_config.DEPLOYMENT_EDITION", "SELF_HOSTED")
    issue = Mock(return_value="tokens")
    monkeypatch.setattr(AccountService, "issue_token_pair", issue)
    persist = TenantService.persist_tenant_member
    monkeypatch.setattr(TenantService, "persist_tenant_member", Mock(side_effect=RuntimeError("member write failed")))
    with pytest.raises(RuntimeError, match="member write failed"):
        service._login(
            service._snapshot(enabled=True), DingTalkIdentity("subject", "new@example.test", "New"), ip_address=None
        )
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Account)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(AccountIntegrate)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(AccountMoneyExtend)) == 0
    issue.assert_not_called()
    monkeypatch.setattr(TenantService, "persist_tenant_member", persist)
    assert (
        service._login(
            service._snapshot(enabled=True), DingTalkIdentity("subject", "new@example.test", "New"), ip_address=None
        )
        == "tokens"
    )
    with factory() as session:
        new = session.scalar(sa.select(Account).where(Account.email == "new@example.test"))
        assert new.status == AccountStatus.ACTIVE and new.initialized_at is not None
        assert session.scalar(sa.select(AccountIntegrate.account_id)) == new.id
        join = session.scalar(sa.select(TenantAccountJoin).where(TenantAccountJoin.account_id == new.id))
        assert join.tenant_id == tenant_id and join.role == TenantAccountRole.NORMAL and join.current
        assert session.scalar(sa.select(AccountMoneyExtend.account_id)) == new.id


def test_identity_uses_enterprise_directory_email_and_rejects_subject_mismatch(system, monkeypatch):
    service, *_ = system
    snapshot = service._snapshot()
    responses = [
        {"accessToken": "private-user-token"},
        {"unionId": "union"},
        {"accessToken": "private-app-token"},
        {"errcode": 0, "result": {"userid": "member"}},
        {
            "errcode": 0,
            "result": {
                "userid": "member",
                "unionid": "union",
                "active": True,
                "org_email": "real@example.test",
                "name": "Name",
            },
        },
    ]
    request = Mock(side_effect=responses)
    monkeypatch.setattr(service, "_request", request)
    identity = service._identity(snapshot, "auth-code")
    assert identity.email == "real@example.test"
    assert len(identity.subject) == 64 and "union" not in identity.subject
    assert request.call_args_list[0].kwargs["json"]["grantType"] == "authorization_code"
    responses[-1]["result"]["unionid"] = "other"
    request.side_effect = responses
    with pytest.raises(DingTalkAuthError, match="enterprise_member_required"):
        service._identity(snapshot, "auth-code")


def test_hidden_email_credentials_are_reused_only_in_privileged_test(system, monkeypatch):
    service, owner, *_ = system
    view = service.save(
        owner,
        {
            "config": {
                "email_api": {
                    "enabled": True,
                    "url": "https://directory.example.test",
                    "headers": {"X-Key": "private-header"},
                    "authorization": {"type": "bearer", "token": "private-token"},
                }
            }
        },
    )
    lookup = Mock(return_value={"result": "success", "email": "real@example.test"})
    monkeypatch.setattr("services.dingtalk_auth_service_extend.lookup_email", lookup)
    result = service.test_email(owner, "member", view["config"]["email_api"])
    assert result == {"result": "success", "email": "real@example.test"}
    assert lookup.call_args.args[1]["headers"] == {"X-Key": "private-header"}
    assert lookup.call_args.args[1]["authorization"]["token"] == "private-token"
    assert "private-" not in json.dumps(result)


def test_legacy_email_credentials_are_migrated_on_management_read(system):
    service, owner, _, factory, *_ = system
    with factory.begin() as session:
        record = session.get(SystemIntegrationExtend, 1)
        record.status = True
        record.config = json.dumps(
            {
                "email_api": {
                    "enabled": True,
                    "url": "https://directory.example.test",
                    "authorization": {"type": "bearer", "token": "private-legacy"},
                }
            }
        )
    view = service.get(owner)
    assert view["revision"] and not view["status"]
    assert "private-" not in json.dumps(view)
    with factory() as session:
        record = session.get(SystemIntegrationExtend, 1)
        assert "private-" not in record.config
        assert service._config(record)["email_api"]["authorization"]["token"] == "private-legacy"
