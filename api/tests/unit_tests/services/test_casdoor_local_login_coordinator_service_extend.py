"""Actual signed/consumed LOCAL chain with offline transport only; no G0 proof."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.auth_transactions import (
    AuthMode,
    AuthTransactionError,
    AuthTransactionStore,
    CookiePolicy,
    CurrentAuthContext,
    TrustedAuthContext,
    new_browser_scope,
)
from core.casdoor.claims import ClaimsError, NativeTokenContract, NativeTokenSchema
from core.casdoor.configuration import PublicCertificatePolicy
from core.casdoor.crypto import TrustedCertificate
from core.casdoor.gateway import CasdoorTokenGateway, GatewayError, GatewayOperation
from core.casdoor.leases import CasdoorLeaseError
from core.casdoor.permissions import CasdoorManagementPolicy
from core.casdoor.role_graph import DirectorySnapshotContract
from core.helper import ssrf_proxy
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from extensions.ext_redis import RedisClientWrapper
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorManagedMembershipExtend,
    CasdoorNamespaceExtend,
    CasdoorValidationExtend,
)
from pydantic import SecretStr
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from services.casdoor_local_login_coordinator_service_extend import (
    CasdoorLocalLoginCoordinatorService,
    _CoordinationConflict,
)
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_auth_transactions import FakeRedis
from test_casdoor_local_login_service_extend import local as original_local
from test_casdoor_local_login_service_extend import login_env as login_env
from test_casdoor_login_account_service_extend import seed
from test_claims import sign
from test_gateway import SyntheticStrategy, response
from test_leases import FakeRedisLua

local_fixture = original_local

MODELS = (
    Account,
    AccountMoneyExtend,
    CasdoorIdentityExtend,
    TenantAccountJoin,
    CasdoorManagedMembershipExtend,
    CasdoorAuditExtend,
)


@pytest.fixture(scope="module")
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC).replace(microsecond=0)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline.example.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, TrustedCertificate(
        pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        kid="synthetic-kid",
        not_before=now - timedelta(days=1),
        accept_until=now + timedelta(days=1),
    )


@pytest.fixture
def chain(local_fixture, signing, monkeypatch):
    local = local_fixture
    key, pin = signing
    for model in (CasdoorAuditExtend, CasdoorValidationExtend):
        model.__table__.create(local.engine)
    opened = set()

    class TrackedSession(Session):
        def __init__(self):
            super().__init__(local.engine)
            opened.add(self)

        def close(self):
            super().close()
            opened.discard(self)

    configuration_service = CasdoorConfigurationService(
        session_factory=TrackedSession,
        management_policy=CasdoorManagementPolicy.from_deployment(""),
        secret_key="offline-coordinator-key",
        rbac_enabled=False,
    )
    config = local.config.model_copy(
        update={
            "certificates": (
                PublicCertificatePolicy(
                    pem=pin.pem, kid=pin.kid, not_before=pin.not_before, accept_until=pin.accept_until
                ),
            ),
            "name_sync": "managed",
        }
    )
    with local.session.begin():
        owner = configuration_service._repository(local.session)
        integration = owner._integration()
        snapshot = owner.save_draft(
            config, etag=integration.etag, actor_account_id=UUID(int=700), secret=SecretStr("offline-client-secret")
        )
        # Controlled active pointer is an offline fixture, not activation proof.
        integration.active_revision_id = str(snapshot.draft_revision_id)
        local.session.flush()
        revision = owner._revision(integration.id, integration.active_revision_id)
        secret = owner.crypto.decrypt(
            revision.encrypted_secret, context=owner._secret_context(revision.namespace_id, revision.id)
        )
        c = replace(
            local.env[1],
            revision_id=UUID(revision.id),
            active_revision_id=UUID(revision.id),
            config_digest=revision.config_digest,
        )
    local.env = (local.session, c, *local.env[2:])
    local.config = config
    redis = FakeRedisLua(monotonic)
    calls, prepared, sessions = [], [], []
    control = SimpleNamespace(hook=lambda path, count: None, bad_id={}, bad_native={}, signer=key)
    profile = {"sub": c.subject, "name": "Remote Name", "email": "new@example.test", "email_verified": True}
    user = {
        "owner": c.organization,
        "id": c.subject,
        "name": "person",
        "isForbidden": False,
        "isDeleted": False,
        "groups": [],
    }
    organization = {"owner": "admin", "name": c.organization, "accountItems": []}
    roles = [
        {
            "owner": c.organization,
            "name": "operators",
            "isEnabled": True,
            "roles": [],
            "users": [c.organization + "/person"],
            "groups": [],
        }
    ]
    policy = CookiePolicy("https://console.example.test")
    callback = "https://console.example.test/console/api/auth/casdoor/callback"
    context = TrustedAuthContext(c.namespace_id, c.revision_id, AuthMode.LOGIN, callback)
    now = datetime.now(UTC)
    raw_redis = FakeRedis()
    wrapper = RedisClientWrapper()
    wrapper.initialize(raw_redis)
    wrapper._get_prefix = lambda: "offline-c2"
    store = AuthTransactionStore(wrapper, configuration_service._repository(local.session).crypto, clock=lambda: now)
    browser = new_browser_scope(policy).value

    def guard():
        return CurrentAuthContext(c.namespace_id, c.revision_id, AuthMode.LOGIN, callback, None, allowed=True)

    created = store.create(context, browser_scope=browser, policy=policy, guard=guard)
    consume_args = dict(transaction_cookie=created.cookie.value, browser_scope=browser, policy=policy, guard=guard)
    consumed = store.consume(created.state, **consume_args)
    operation = GatewayOperation(config=config, client_secret=secret, registered_redirect_uri=callback)

    def external():
        assert not opened, "provider/preparation must run after all read Sessions close"
        assert not local.session.in_transaction()

    def transport(method, url, **kwargs):
        external()
        assert kwargs["deadline"] == operation.deadline
        path = urlsplit(url).path
        calls.append(path)
        control.hook(path, calls.count(path))
        if path.endswith("access_token"):
            common = {"iss": c.issuer, "aud": c.client_id, "iat": now.timestamp(), "exp": now.timestamp() + 300}
            return response(
                {
                    "token_type": "Bearer",
                    "id_token": sign(
                        control.signer, common | {"sub": c.subject, "nonce": consumed.nonce} | control.bad_id
                    ),
                    "access_token": sign(key, common | {"owner": c.organization, "id": c.subject} | control.bad_native),
                }
            )
        if path.endswith("userinfo"):
            return response(profile)
        data = {"/api/get-user": user, "/api/get-organization": organization, "/api/get-roles": roles}[path]
        if path in ("/api/get-organization", "/api/get-roles"):
            assert redis.data, "loader I/O requires actual acquired leases"
        return response({"status": "ok", "data": data})

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    original_prepare = CasdoorLoginAccountService._prepare_login

    def prepare(owner, **kwargs):
        external()
        assert not redis.data, "preparation cannot extend a held subset"
        result = original_prepare(owner, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(CasdoorLoginAccountService, "_prepare_login", prepare)
    coordinator = CasdoorLocalLoginCoordinatorService(
        session_factory=TrackedSession,
        configuration_service=configuration_service,
        account_owner=local.env[3],
        redis_client=redis,
    )
    strategy = SyntheticStrategy(operation)
    native = NativeTokenContract(
        NativeTokenSchema.FLAT_USER_V1, c.issuer, c.organization, c.application, c.client_id, "a" * 64, "b" * 64
    )
    directory = DirectorySnapshotContract(strategy.proof, "d" * 64, "e" * 64)

    def invoke(**changes):
        raw = CasdoorTokenGateway(operation).exchange_code("offline-code", consumed.code_verifier)
        args = dict(
            consumed=consumed,
            raw_tokens=raw,
            operation=operation,
            native_contract=native,
            directory_contract=directory,
            credential_strategy=strategy,
            correlation_id=UUID(int=701),
        )
        args.update(changes)
        return coordinator._coordinate_local_login(**args)

    yield SimpleNamespace(
        local=local,
        redis=redis,
        operation=operation,
        coordinator=coordinator,
        configuration_service=configuration_service,
        consumed=consumed,
        invoke=invoke,
        profile=profile,
        user=user,
        roles=roles,
        organization=organization,
        calls=calls,
        prepared=prepared,
        control=control,
        opened=opened,
        store=store,
        created=created,
        consume_args=consume_args,
        native=native,
        directory=directory,
        strategy=strategy,
        sessions=sessions,
    )
    assert not opened


def rows(chain):
    with Session(chain.local.engine) as reader:
        return {
            model: tuple(tuple(row) for row in reader.execute(sa.select(*model.__table__.columns).order_by(model.id)))
            for model in MODELS
        }


@pytest.mark.parametrize("bound", ["new", "initialize", "activate", "use"])
def test_actual_chain_commits_one_generation_and_keeps_pending(chain, bound):
    if bound != "new":
        seed(
            chain.local.env,
            AccountStatus.ACTIVE if bound == "use" else AccountStatus.PENDING,
            None if bound == "initialize" else datetime(2025, 1, 1),
        )
    outcome = chain.invoke()
    assert outcome.local_outcome == "committed" and outcome.cleanup_released
    assert outcome.persistence.generation == 1
    assert all(
        workspace.finalization is CasdoorFinalizationState.PENDING for workspace in outcome.persistence.workspaces
    )
    assert chain.calls == [
        "/api/login/oauth/access_token",
        "/api/userinfo",
        "/api/get-user",
        "/api/userinfo",
        "/api/get-organization",
        "/api/get-user",
        "/api/get-roles",
    ]
    assert not chain.redis.data
    assert len(chain.prepared) == 1 and chain.prepared[0]._consumed
    account_id = chain.prepared[0].account_id
    keys = [item[0] for item in chain.redis.sets]
    assert keys == sorted(set(keys))
    assert f"casdoor:lease:v1:account:{account_id}" in keys
    assert all(any(str(UUID(int=n)) in k and str(account_id) in k for k in keys) for n in (100, 200))
    with Session(chain.local.engine) as reader:
        assert reader.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 1
        assert reader.scalar(sa.select(Account.name)) == (
            "Preserved" if bound in ("activate", "use") else "Remote Name"
        )
        assert reader.scalar(sa.select(Account.status)) is AccountStatus.ACTIVE
        assert reader.scalar(sa.select(Account.initialized_at)) is not None
        assert reader.scalar(sa.select(AccountMoneyExtend.total_quota)) == (15 if bound == "new" else 91.25)
        assert reader.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 2
    with pytest.raises(AuthTransactionError):
        chain.store.consume(chain.created.state, **chain.consume_args)


@pytest.mark.parametrize("kind", ["nonce", "issuer", "audience", "signature", "native_org", "native_subject"])
def test_signed_negative_zero_business(chain, kind):
    before = rows(chain)
    if kind == "nonce":
        chain.control.bad_id = {"nonce": "wrong"}
    elif kind == "issuer":
        chain.control.bad_id = {"iss": "https://other.example.test"}
    elif kind == "audience":
        chain.control.bad_id = {"aud": "other"}
    elif kind == "signature":
        chain.control.signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif kind == "native_org":
        chain.control.bad_native = {"owner": "other"}
    else:
        chain.control.bad_native = {"id": "other"}
    with pytest.raises(ClaimsError):
        chain.invoke()
    assert rows(chain) == before and not chain.prepared and not chain.redis.sets


@pytest.mark.parametrize(
    "kind",
    [
        "userinfo_subject",
        "missing_email",
        "disabled",
        "deleted",
        "organization",
        "visibility",
        "missing_groups",
        "role_missing",
        "contract",
        "strategy",
    ],
)
def test_online_unknown_denies_without_business_fallback(chain, kind):
    before = rows(chain)
    args = {}
    if kind == "userinfo_subject":
        chain.profile["sub"] = "other"
    elif kind == "missing_email":
        chain.profile.pop("email")
    elif kind == "disabled":
        chain.user["isForbidden"] = True
    elif kind == "deleted":
        chain.user["isDeleted"] = True
    elif kind == "organization":
        chain.user["owner"] = "other"
    elif kind == "visibility":
        chain.organization.pop("accountItems")
    elif kind == "missing_groups":
        chain.user.pop("groups")
    elif kind == "role_missing":
        chain.roles[0].pop("roles")
    elif kind == "contract":
        args["directory_contract"] = None
    else:
        args["credential_strategy"] = None
    with pytest.raises(ValueError if kind == "missing_email" else Exception) as failure:
        chain.invoke(**args)
    assert failure.value.local_outcome == "not_started"
    assert failure.value.cleanup_released
    assert rows(chain) == before and not chain.redis.data


@pytest.mark.parametrize("persistent", [False, True])
def test_profile_drift_only_two_passes_reprepare_after_release(chain, persistent):
    def hook(path, count):
        if path == "/api/userinfo" and (count == 2 or persistent and count == 4):
            chain.profile["name"] = "Changed " + str(count)

    chain.control.hook = hook
    before = rows(chain)
    if persistent:
        with pytest.raises(_CoordinationConflict, match="drift_limit"):
            chain.invoke()
        assert rows(chain) == before
    else:
        assert chain.invoke().local_outcome == "committed"
    assert len(chain.prepared) == 2
    assert chain.prepared[0].account_id != chain.prepared[1].account_id
    assert chain.prepared[0]._consumed is False
    assert chain.calls.count("/api/login/oauth/access_token") == 1
    assert chain.calls.count("/api/userinfo") == 4
    assert not chain.redis.data
    assert {item.deadline for item in chain.prepared} == {chain.operation.deadline}


def test_scope_only_drift_retains_original_new_uuid(chain, monkeypatch):
    original = CasdoorLoginScopeRepository.discover
    discovered = []

    def discover(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        discovered.append(result)
        # Change a real scalar included in the complete scope after the first
        # snapshot, using its read transaction before any lease acquisition.
        if len(discovered) == 1:
            owner.session.add(
                CasdoorNamespaceExtend(
                    integration_id=str(chain.local.env[1].integration_id),
                    core_fingerprint="9" * 64,
                    expected_issuer="https://historical.example.test",
                    organization="History",
                    application="History",
                    client_id="History",
                )
            )
            owner.session.flush()
        return result

    monkeypatch.setattr(CasdoorLoginScopeRepository, "discover", discover)
    assert chain.invoke().local_outcome == "committed"
    assert len(chain.prepared) == 1
    tokens = {token for _, token, _ in chain.redis.sets}
    assert len(tokens) == 2
    by_token = [[key for key, value, _ in chain.redis.sets if value == token] for token in tokens]
    assert by_token[0] == by_token[1]
    assert not chain.redis.data


@pytest.mark.parametrize("kind", ["busy", "set_timeout", "ttl", "check", "winner"])
def test_real_lease_failure_cleanup_zero_business(chain, kind):
    before = rows(chain)
    fired = False

    def hook(command, key):
        nonlocal fired
        if fired:
            return
        if (
            kind in ("busy", "set_timeout")
            and command == ("before_set" if kind == "busy" else "after_set")
            and len(chain.redis.sets) == 3
        ):
            fired = True
            if kind == "busy":
                chain.redis.data[key] = (b"winner", monotonic() + 40)
            else:
                raise TimeoutError("offline")
        elif kind in ("ttl", "check", "winner") and command == "before_eval" and len(chain.redis.sets) >= 4:
            fired = True
            if kind == "check":
                raise TimeoutError("offline")
            chain.redis.data[key] = (
                b"winner" if kind == "winner" else chain.redis.data[key][0],
                monotonic() + 40 if kind == "winner" else 0,
            )

    chain.redis.hook = hook
    with pytest.raises(CasdoorLeaseError) as failure:
        chain.invoke()
    assert failure.value.local_outcome == "not_started" and failure.value.cleanup_released
    assert rows(chain) == before
    assert all(token == b"winner" for token, _ in chain.redis.data.values())


@pytest.mark.parametrize("failure", ["account", "identity", "member", "profile", "audit"])
def test_actual_write_fault_rolls_back_and_never_retries_spent_preparation(chain, failure):
    before = rows(chain)
    statements = {
        "account": "BEFORE INSERT ON accounts",
        "identity": "BEFORE INSERT ON casdoor_identity_extend",
        "member": "BEFORE INSERT ON tenant_account_joins",
        "profile": (
            "BEFORE UPDATE ON casdoor_identity_extend "
            "WHEN NEW.remote_profile_version IS NOT OLD.remote_profile_version"
        ),
        "audit": "BEFORE INSERT ON casdoor_audit_extend",
    }
    with chain.local.session.begin():
        chain.local.session.execute(
            sa.text("CREATE TRIGGER fail_c2 " + statements[failure] + " BEGIN SELECT RAISE(ABORT, 'offline'); END")
        )
    with pytest.raises(IntegrityError) as error:
        chain.invoke()
    assert error.value.local_outcome == "unknown" and error.value.cleanup_released
    assert rows(chain) == before and len(chain.prepared) == 1 and chain.prepared[0]._consumed
    assert not chain.redis.data


@pytest.mark.parametrize("retry_succeeds", [False, True])
def test_committed_cleanup_failure_remains_known_commit(chain, retry_succeeds):
    failures = set()

    def hook(command, key):
        if command == "before_release" and (not retry_succeeds or key not in failures):
            failures.add(key)
            raise TimeoutError("offline cleanup")

    chain.redis.hook = hook
    outcome = chain.invoke()
    assert outcome.local_outcome == "committed"
    assert outcome.cleanup_released is retry_succeeds
    assert len(chain.prepared) == 1
    assert len(chain.redis.releases) == 2 * len(chain.redis.sets)
    assert bool(chain.redis.data) is not retry_succeeds
    assert rows(chain)[Account]


def test_after_commit_ack_unknown_is_not_rollback(chain):
    def after_commit(session):
        # Only fail C1, whose actual account preparation is now spent.
        if chain.prepared and chain.prepared[0]._consumed and not session.in_nested_transaction():
            raise RuntimeError("offline ack lost")

    sa.event.listen(Session, "after_commit", after_commit)
    try:
        with pytest.raises(RuntimeError, match="offline ack lost") as error:
            chain.invoke()
    finally:
        sa.event.remove(Session, "after_commit", after_commit)
    assert error.value.local_outcome == "unknown" and error.value.cleanup_released
    assert rows(chain)[Account] and not chain.redis.data and len(chain.prepared) == 1


def test_production_factory_missing_proof_zero_io():
    class Deny:
        def __getattribute__(self, name):
            pytest.fail("production factory accessed a dependency")

    deny = Deny()
    with pytest.raises(ValueError) as error:
        CasdoorLocalLoginCoordinatorService.for_production(
            session_factory=deny, configuration_service=deny, account_owner=deny, redis_client=deny
        )
    assert error.value.reason == "deployment_proof_missing"
    with pytest.raises(TypeError):
        CasdoorLocalLoginCoordinatorService.for_production(
            session_factory=deny,
            configuration_service=deny,
            account_owner=deny,
            redis_client=deny,
            directory_contract=object(),
        )


@pytest.mark.parametrize("kind", ["rbac", "disabled", "fence", "revision", "secret", "redirect"])
def test_configuration_change_rejects_spent_context(chain, kind):
    before = rows(chain)
    if kind == "rbac":
        dify_config.RBAC_ENABLED = True
    elif kind == "secret":
        chain.operation.client_secret = "other-offline-secret"
    elif kind == "redirect":
        chain.operation.registered_redirect_uri = "https://other.example.test/callback"
    else:

        def hook(path, count):
            if path == "/api/userinfo" and count == 2:
                with chain.local.session.begin():
                    if kind == "disabled":
                        chain.local.session.execute(sa.update(CasdoorIntegrationExtend).values(enabled=False))
                    elif kind == "revision":
                        chain.local.session.execute(sa.update(CasdoorIntegrationExtend).values(active_revision_id=None))
                    else:
                        chain.local.session.execute(sa.update(CasdoorNamespaceExtend).values(fence_epoch=1))

        chain.control.hook = hook
    with pytest.raises(_CoordinationConflict):
        chain.invoke()
    assert rows(chain) == before and len(chain.prepared) <= 1 and not chain.redis.data


def test_added_archived_prior_scope_releases_whole_set_before_expansion(chain, monkeypatch):
    from models.account import TenantStatus

    seed(chain.local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    original = CasdoorLoginScopeRepository.discover
    observations = []

    def discover(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        observations.append(result)
        if len(observations) == 1:
            workspace = Tenant(name="Archived prior", status=TenantStatus.ARCHIVE)
            workspace.id = str(UUID(int=50))
            owner.session.add(workspace)
            owner.session.flush()
            owner.session.add(
                TenantAccountJoin(account_id=str(args[0].account_id), tenant_id=workspace.id, role="normal")
            )
            owner.session.flush()
        return result

    monkeypatch.setattr(CasdoorLoginScopeRepository, "discover", discover)
    outcome = chain.invoke()
    assert outcome.local_outcome == "committed" and outcome.cleanup_released
    assert len(chain.prepared) == 1
    tokens = list(dict.fromkeys(token for _, token, _ in chain.redis.sets))
    assert len(tokens) == 2
    first = [key for key, token, _ in chain.redis.sets if token == tokens[0]]
    second = [key for key, token, _ in chain.redis.sets if token == tokens[1]]
    assert set(first) < set(second)
    assert second == sorted(set(second))
    assert len(second) == len(first) + 1
    assert any(str(UUID(int=50)) in key for key in second)
    assert chain.redis.releases[: len(first)] == list(reversed(first))
    assert not chain.redis.data


@pytest.mark.parametrize("kind", ["admission", "account_name", "online_ref"])
def test_fresh_dependency_drift_restarts_before_preparation_is_spent(chain, kind):
    seed(chain.local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))

    def hook(path, count):
        if path == "/api/userinfo" and count == 2:
            if kind == "online_ref":
                chain.user["name"] = "renamed"
                chain.roles[0]["users"] = ["Org/renamed"]
            else:
                with chain.local.session.begin():
                    changes = {"status": AccountStatus.PENDING} if kind == "admission" else {"name": "Manual changed"}
                    chain.local.session.execute(sa.update(Account).values(**changes))

    chain.control.hook = hook
    outcome = chain.invoke()
    assert outcome.local_outcome == "committed" and outcome.cleanup_released
    assert chain.calls.count("/api/userinfo") == 4
    assert chain.calls.count("/api/login/oauth/access_token") == 1
    assert len(chain.prepared) == (1 if kind == "online_ref" else 2)
    if kind != "online_ref":
        assert chain.prepared[0]._consumed is False
        assert chain.prepared[0].account_id == chain.prepared[1].account_id
    assert not chain.redis.data


def test_drift_cleanup_pending_blocks_new_uuid_and_new_pass(chain):
    before = rows(chain)

    def provider(path, count):
        if path == "/api/userinfo" and count == 2:
            chain.profile["name"] = "Changed"

    def redis(command, key):
        if command == "before_release":
            raise TimeoutError("offline cleanup")

    chain.control.hook = provider
    chain.redis.hook = redis
    with pytest.raises(_CoordinationConflict, match="cleanup_pending") as error:
        chain.invoke()
    assert not error.value.cleanup_released and error.value.local_outcome == "not_started"
    assert len(chain.prepared) == 1 and chain.prepared[0]._consumed is False
    assert chain.calls.count("/api/userinfo") == 2
    assert len(chain.redis.releases) == 2 * len(chain.redis.sets)
    assert rows(chain) == before


def test_primary_provider_failure_survives_cleanup_failure(chain):
    def provider(path, count):
        if path == "/api/get-organization":
            chain.organization.pop("accountItems")

    def redis(command, key):
        if command == "before_release":
            raise TimeoutError("offline cleanup")

    chain.control.hook = provider
    chain.redis.hook = redis
    with pytest.raises(GatewayError) as error:
        chain.invoke()
    assert error.value.reason == "visibility_unknown"
    assert not error.value.cleanup_released and error.value.local_outcome == "not_started"
    assert len(chain.prepared) == 1 and not chain.prepared[0]._consumed


def test_cross_release_contracts_reject_before_directory(chain):
    with pytest.raises(_CoordinationConflict, match="directory_contract_binding"):
        chain.invoke(native_contract=replace(chain.native, release_fingerprint="f" * 64))
    assert chain.calls == ["/api/login/oauth/access_token", "/api/userinfo"]
    assert not chain.prepared and not chain.redis.sets


def test_bound_missing_remote_email_uses_actual_binding_without_email_fallback(chain):
    seed(chain.local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    chain.profile.pop("email")
    outcome = chain.invoke()
    assert outcome.persistence.generation == 1
    with Session(chain.local.engine) as reader:
        assert reader.scalar(sa.select(Account.email)) == "bound@example.test"


def test_unknown_role_response_cannot_become_known_empty(chain):
    before = rows(chain)
    chain.roles[0]["roles"] = ["Org/missing-role"]
    with pytest.raises(GatewayError) as error:
        chain.invoke()
    assert error.value.reason == "role_dangling"
    assert rows(chain) == before and not chain.redis.data


def test_expired_original_deadline_denies_before_preparation(chain):
    def provider(path, count):
        if path == "/api/userinfo":
            chain.operation.deadline = monotonic() - 1

    chain.control.hook = provider
    with pytest.raises(GatewayError) as error:
        chain.invoke()
    assert error.value.reason == "deadline"
    assert not chain.prepared and not chain.redis.sets


def test_c1_precommit_owner_loss_rolls_back_and_preserves_winner(chain, monkeypatch):
    original = CasdoorLoginScopeRepository.recheck_before_commit
    before = rows(chain)

    def tamper(owner, *args, **kwargs):
        original(owner, *args, **kwargs)
        key = chain.redis.sets[-1][0]
        chain.redis.data[key] = (b"winner", monotonic() + 40)

    monkeypatch.setattr(CasdoorLoginScopeRepository, "recheck_before_commit", tamper)
    with pytest.raises(CasdoorLeaseError) as error:
        chain.invoke()
    assert error.value.local_outcome == "unknown" and error.value.cleanup_released
    assert rows(chain) == before and len(chain.prepared) == 1 and chain.prepared[0]._consumed
    assert list(chain.redis.data.values())[0][0] == b"winner"


def test_no_sensitive_values_in_result_or_profile_audit(chain):
    outcome = chain.invoke()
    with Session(chain.local.engine) as reader:
        audits = repr(tuple(tuple(row) for row in reader.execute(sa.select(*CasdoorAuditExtend.__table__.columns))))
    visible = repr(outcome) + audits
    for sensitive in (
        chain.consumed.nonce,
        chain.consumed.code_verifier,
        chain.operation.client_secret,
        "offline-code",
        "Remote Name",
        "new@example.test",
        chain.operation.config.expected_issuer,
    ):
        assert sensitive not in visible


class _OfflineCancellation(BaseException):
    """Offline cancellation signal that original Exception handlers cannot catch."""


@pytest.mark.parametrize("signal", [_OfflineCancellation, KeyboardInterrupt, SystemExit])
def test_committed_cleanup_cancellation_propagates_with_known_commit(chain, signal):
    cancellation = signal("offline cleanup cancelled")

    def redis(command, key):
        if command == "before_release":
            raise cancellation

    chain.redis.hook = redis
    with pytest.raises(signal) as error:
        chain.invoke()
    assert error.value is cancellation
    assert error.value.local_outcome == "committed"
    assert error.value.cleanup_released is False
    assert rows(chain)[Account]
    assert len(chain.prepared) == 1 and chain.prepared[0]._consumed
    assert chain.redis.data
    assert len(chain.redis.releases) == 1  # No cancellation retry or business retry.


@pytest.mark.parametrize("stage", ["provider", "precommit"])
@pytest.mark.parametrize("cleanup_cancel", [False, True])
def test_primary_cancellation_keeps_identity_and_phase_facts(chain, monkeypatch, stage, cleanup_cancel):
    cancellation = _OfflineCancellation("offline primary cancelled")
    before = rows(chain)

    if stage == "provider":

        def provider(path, count):
            if path == "/api/get-organization":
                raise cancellation

        chain.control.hook = provider
    else:
        original = CasdoorLoginScopeRepository.recheck_before_commit

        def precommit(owner, *args, **kwargs):
            original(owner, *args, **kwargs)
            raise cancellation

        monkeypatch.setattr(CasdoorLoginScopeRepository, "recheck_before_commit", precommit)

    if cleanup_cancel:

        def redis(command, key):
            if command == "before_release":
                raise SystemExit("offline secondary cleanup cancelled")

        chain.redis.hook = redis

    with pytest.raises(_OfflineCancellation) as error:
        chain.invoke()
    assert error.value is cancellation
    assert error.value.local_outcome == ("not_started" if stage == "provider" else "unknown")
    assert error.value.cleanup_released is not cleanup_cancel
    assert rows(chain) == before
    assert len(chain.prepared) == 1
    assert chain.prepared[0]._consumed is (stage == "precommit")
    assert bool(chain.redis.data) is cleanup_cancel
    if cleanup_cancel:
        assert len(chain.redis.releases) == 1


def test_after_commit_primary_cancellation_keeps_unknown_and_committed_rows(chain):
    cancellation = _OfflineCancellation("offline root acknowledgement cancelled")

    def after_commit(session):
        if chain.prepared and chain.prepared[0]._consumed and not session.in_nested_transaction():
            raise cancellation

    def redis(command, key):
        if command == "before_release":
            raise KeyboardInterrupt("offline cleanup cancelled")

    chain.redis.hook = redis
    sa.event.listen(Session, "after_commit", after_commit)
    try:
        with pytest.raises(_OfflineCancellation) as error:
            chain.invoke()
    finally:
        sa.event.remove(Session, "after_commit", after_commit)
    assert error.value is cancellation
    assert error.value.local_outcome == "unknown"
    assert error.value.cleanup_released is False
    assert rows(chain)[Account]
    assert len(chain.prepared) == 1 and chain.prepared[0]._consumed
    assert chain.redis.data and len(chain.redis.releases) == 1


def test_drift_cleanup_cancellation_propagates_without_restarting(chain):
    cancellation = _OfflineCancellation("offline drift cleanup cancelled")
    before = rows(chain)

    def provider(path, count):
        if path == "/api/userinfo" and count == 2:
            chain.profile["name"] = "Changed"

    def redis(command, key):
        if command == "before_release":
            raise cancellation

    chain.control.hook = provider
    chain.redis.hook = redis
    with pytest.raises(_OfflineCancellation) as error:
        chain.invoke()
    assert error.value is cancellation
    assert error.value.local_outcome == "not_started"
    assert error.value.cleanup_released is False
    assert rows(chain) == before
    assert len(chain.prepared) == 1 and not chain.prepared[0]._consumed
    assert chain.calls.count("/api/userinfo") == 2
    assert chain.redis.data and len(chain.redis.releases) == 1


@pytest.mark.parametrize("cleanup", ["success", "failure", "cancelled"])
def test_actual_finalizer_tokens_delivered_only_after_owned_cleanup(chain, monkeypatch, cleanup):
    from test_casdoor_local_login_finalization_service_extend import attach_finalizer

    finalized = attach_finalizer(chain, monkeypatch)

    def hook(command, key):
        if command == "before_release":
            if cleanup == "cancelled":
                raise _OfflineCancellation("offline cleanup cancelled")
            if cleanup == "failure":
                raise TimeoutError("offline cleanup unconfirmed")

    chain.redis.hook = hook
    if cleanup == "cancelled":
        with pytest.raises(_OfflineCancellation) as error:
            chain.invoke(ip_address="192.0.2.7")
        outcome = error.value
    else:
        outcome = chain.invoke(ip_address="192.0.2.7")
        assert (outcome.tokens is not None) is (cleanup == "success")
    assert outcome.local_outcome == "committed"
    assert outcome.finalization_outcome == "committed"
    assert outcome.token_outcome == "issued"
    assert outcome.cleanup_released is (cleanup == "success")
    assert len(finalized.calls) == 2
