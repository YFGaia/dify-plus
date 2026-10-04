"""Noncommit invitation/setup owner and unchanged legacy finalization boundaries."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorIntentKind,
    CasdoorNamespaceExtend,
    CasdoorSyncIntentExtend,
)
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from services import account_service
from services.account_activation_service import (
    AccountActivationService,
    EmailDomainSuspendedError,
    FrozenAccountError,
    InvalidInvitationError,
    InvitationAccountMismatchError,
)
from services.account_service import AccountService
from services.entities.account_activation_entities import (
    AccountInvitation,
    AccountSetup,
    ActivationCommand,
    InvitationLookup,
    InvitationToken,
)


def _setup():
    return AccountSetup(name="Initialized", interface_language="en-US", timezone="UTC")


def _seed(session, *, status=AccountStatus.PENDING, existing_role=None):
    tenant = Tenant(name="Invited workspace")
    account = Account(name="Pending", email="invitee@example.test", status=status)
    session.add_all([tenant, account])
    session.flush()
    if existing_role is not None:
        session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=existing_role))
    session.commit()
    return tenant, account


def _invitation(tenant, account, *, requires_setup=True, role="admin"):
    return AccountInvitation(
        account_id=account.id,
        account_email=account.email,
        account_status=account.status.value,
        workspace_id=tenant.id,
        workspace_name=tenant.name,
        role=role,
        requires_setup=requires_setup,
    )


def _command(tenant, *, email="Invitee@Example.test"):
    return ActivationCommand(
        invitation=InvitationLookup(workspace_id=tenant.id, email=email, token="synthetic-invitation"),
        name=_setup().name,
        interface_language=_setup().interface_language,
        timezone=_setup().timezone,
    )


def _service(repository, tenant, account, *, role="admin", requires_setup=True):
    tokens = Mock()
    tokens.find.return_value = InvitationToken(
        account_id=account.id,
        email=account.email,
        workspace_id=tenant.id,
        role=role,
        requires_setup=requires_setup,
    )
    policy, eligibility, cache, sync = Mock(), Mock(), Mock(), Mock()
    eligibility.get_freeze_type.return_value = None
    service = AccountActivationService(
        tokens=tokens,
        accounts=repository,
        workspace_policy=policy,
        eligibility=eligibility,
        membership_cache=cache,
        member_access_sync=sync,
    )
    return service, tokens, policy, eligibility, cache, sync


def _assert_no_effects(tokens, cache, sync):
    tokens.revoke.assert_not_called()
    cache.invalidate.assert_not_called()
    sync.sync.assert_not_called()


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_preparation_reads_original_token_and_freeze_without_writes_or_policy_change(
    sqlite_session, sqlite_session_factory, monkeypatch
):
    tenant, account = _seed(sqlite_session)
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    service, tokens, policy, eligibility, cache, sync = _service(repository, tenant, account)
    repository_activate = Mock()
    repository_persist = Mock()
    monkeypatch.setattr(repository, "activate", repository_activate)
    monkeypatch.setattr(repository, "persist_activation", repository_persist)
    receipt = service.prepare_activation(_command(tenant), authenticated_account_id=account.id)
    assert receipt.invitation == _invitation(tenant, account)
    assert receipt.setup == _setup()
    assert receipt.role == "admin"
    tokens.find.assert_called_once_with(_command(tenant).invitation)
    eligibility.get_freeze_type.assert_called_once_with(account.email)
    policy.ensure_allowed.assert_not_called()  # Legacy check(), not activate(), owns this policy.
    repository_activate.assert_not_called()
    repository_persist.assert_not_called()
    _assert_no_effects(tokens, cache, sync)
    with pytest.raises(FrozenInstanceError):
        receipt.role = "owner"
    with sqlite_session_factory() as observer:
        assert observer.get(Account, account.id).initialized_at is None
        assert _count(observer, TenantAccountJoin) == 0


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (None, "normal"),
        ("owner", "normal"),
        ("custom", "normal"),
        ("normal", "normal"),
        ("admin", "admin"),
        ("editor", "editor"),
        ("dataset_operator", "dataset_operator"),
    ],
)
def test_preparation_reuses_legacy_invitation_role_owner(role, expected, sqlite_session, sqlite_session_factory):
    tenant, account = _seed(sqlite_session)
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    service, tokens, _, _, cache, sync = _service(repository, tenant, account, role=role)
    assert service.prepare_activation(_command(tenant), authenticated_account_id=None).role == expected
    _assert_no_effects(tokens, cache, sync)


@pytest.mark.parametrize(
    ("freeze", "error"), [("freeze", FrozenAccountError), ("email_domain_suspended", EmailDomainSuspendedError)]
)
def test_preparation_keeps_freeze_owner_and_exception_before_setup(
    freeze, error, sqlite_session, sqlite_session_factory
):
    tenant, account = _seed(sqlite_session)
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    service, tokens, _, eligibility, cache, sync = _service(repository, tenant, account)
    eligibility.get_freeze_type.return_value = freeze
    with pytest.raises(error):
        service.prepare_activation(
            ActivationCommand(invitation=_command(tenant).invitation), authenticated_account_id=None
        )
    _assert_no_effects(tokens, cache, sync)


def test_preparation_mismatch_stays_before_freeze_and_setup(sqlite_session, sqlite_session_factory):
    tenant, account = _seed(sqlite_session)
    service, tokens, _, eligibility, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )
    eligibility.get_freeze_type.return_value = "freeze"
    with pytest.raises(InvitationAccountMismatchError):
        service.prepare_activation(
            ActivationCommand(invitation=_command(tenant).invitation), authenticated_account_id="different-account"
        )
    eligibility.get_freeze_type.assert_not_called()
    _assert_no_effects(tokens, cache, sync)


@pytest.mark.parametrize("missing", ["name", "interface_language", "timezone"])
def test_preparation_missing_setup_is_rejected_before_consumption(missing, sqlite_session, sqlite_session_factory):
    tenant, account = _seed(sqlite_session)
    service, tokens, _, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )
    with pytest.raises(InvalidInvitationError):
        service.prepare_activation(replace(_command(tenant), **{missing: None}), authenticated_account_id=None)
    _assert_no_effects(tokens, cache, sync)


@pytest.mark.parametrize("email", [None, "invitee@example.test", "Invitee@Example.test"])
def test_missing_token_preparation_does_not_freeze_or_write(email, sqlite_session, sqlite_session_factory):
    tenant, account = _seed(sqlite_session)
    repository = Mock(wraps=SQLAlchemyAccountActivationRepository(sqlite_session_factory))
    service, tokens, _, eligibility, cache, sync = _service(repository, tenant, account)
    tokens.find.return_value = None
    with pytest.raises(InvalidInvitationError):
        service.prepare_activation(_command(tenant, email=email), authenticated_account_id=None)
    assert tokens.find.call_count == (2 if email == "Invitee@Example.test" else 1)
    repository.resolve.assert_not_called()
    eligibility.get_freeze_type.assert_not_called()
    _assert_no_effects(tokens, cache, sync)


def test_prepare_uppercase_fallback_and_check_policy_stay_owned_by_existing_service(
    sqlite_session, sqlite_session_factory
):
    tenant, account = _seed(sqlite_session)
    service, tokens, policy, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )
    normalized = tokens.find.return_value
    tokens.find.side_effect = [replace(normalized, email="Invitee@Example.test"), normalized]
    receipt = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    assert receipt.invitation.account_email == account.email
    assert [call.args[0].email for call in tokens.find.call_args_list] == ["Invitee@Example.test", account.email]
    policy.ensure_allowed.assert_not_called()
    tokens.find.side_effect = None
    assert service.check(_command(tenant).invitation).is_valid
    policy.ensure_allowed.assert_called_once_with(tenant.id)
    policy.ensure_allowed.side_effect = RuntimeError("workspace invite policy")
    with pytest.raises(RuntimeError, match="workspace invite policy"):
        service.check(_command(tenant).invitation)
    _assert_no_effects(tokens, cache, sync)


def test_noncommit_persistence_locks_exact_account_and_outer_rollback_restores_setup_and_current_membership(
    sqlite_session, sqlite_session_factory, monkeypatch
):
    tenant, account = _seed(sqlite_session)
    other = Tenant(name="Other")
    sqlite_session.add(other)
    sqlite_session.flush()
    old_join = TenantAccountJoin(tenant_id=other.id, account_id=account.id, role=TenantAccountRole.EDITOR, current=True)
    sqlite_session.add(old_join)
    sqlite_session.commit()
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    service, tokens, _, _, cache, sync = _service(repository, tenant, account)
    receipt = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    commit, rollback = Mock(wraps=sqlite_session.commit), Mock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "commit", commit)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    monkeypatch.setattr(repository, "_session_factory", Mock(side_effect=AssertionError("caller must own Session")))
    statements = []
    event.listen(sqlite_session, "do_orm_execute", lambda state: statements.append(state.statement))
    result = service.persist_activation(receipt, session=sqlite_session)
    assert result.membership_created
    sqlite_session.flush()
    assert account.status == AccountStatus.ACTIVE
    assert account.initialized_at is not None
    assert account.interface_theme == "light"
    assert account.name == _setup().name
    assert old_join.current is False
    account_sql = [str(s.compile(dialect=postgresql.dialect())) for s in statements if "FROM accounts" in str(s)]
    assert len(account_sql) == 1
    assert "accounts.id =" in account_sql[0]
    assert "accounts.email =" in account_sql[0]
    assert "FOR UPDATE" in account_sql[0]
    commit.assert_not_called()
    rollback.assert_not_called()
    _assert_no_effects(tokens, cache, sync)
    with sqlite_session_factory() as observer:
        assert observer.get(Account, account.id).status == AccountStatus.PENDING
        assert _count(observer, TenantAccountJoin) == 1
    sqlite_session.rollback()
    assert account.name == "Pending"
    assert account.status == AccountStatus.PENDING
    assert account.initialized_at is None
    assert old_join.current is True
    assert _count(sqlite_session, TenantAccountJoin) == 1
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(receipt, session=sqlite_session)
    _assert_no_effects(tokens, cache, sync)


@pytest.mark.parametrize("existing_role", [TenantAccountRole.EDITOR, TenantAccountRole.OWNER])
def test_existing_membership_role_and_metadata_survive_helper_reentry(
    existing_role, sqlite_session, sqlite_session_factory
):
    tenant, account = _seed(sqlite_session, status=AccountStatus.ACTIVE, existing_role=existing_role)
    join = sqlite_session.scalar(select(TenantAccountJoin))
    join.invited_by = "original-inviter"
    sqlite_session.commit()
    service, tokens, _, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account, requires_setup=False
    )
    first = service.prepare_activation(_command(tenant), authenticated_account_id=account.id)
    assert not service.persist_activation(first, session=sqlite_session).membership_created
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(first, session=sqlite_session)
    second = service.prepare_activation(_command(tenant), authenticated_account_id=account.id)
    assert not service.persist_activation(second, session=sqlite_session).membership_created
    sqlite_session.flush()
    assert _count(sqlite_session, TenantAccountJoin) == 1
    assert join.role == existing_role
    assert join.invited_by == "original-inviter"
    assert join.current
    assert account.initialized_at is None  # Model default ACTIVE is not initialization evidence.
    _assert_no_effects(tokens, cache, sync)


@pytest.mark.parametrize("stale", ["tenant", "account", "email"])
def test_noncommit_helper_fresh_recheck_rejects_stale_rows_without_consumption(
    stale, sqlite_session, sqlite_session_factory
):
    tenant, account = _seed(sqlite_session)
    service, tokens, _, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )
    receipt = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    if stale == "tenant":
        tenant.status = TenantStatus.ARCHIVE
    elif stale == "account":
        sqlite_session.delete(account)
    else:
        account.email = "changed@example.test"
    sqlite_session.commit()
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(receipt, session=sqlite_session)
    assert _count(sqlite_session, TenantAccountJoin) == 0
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(receipt, session=sqlite_session)
    _assert_no_effects(tokens, cache, sync)


def test_receipt_rejects_raw_command_and_another_service_owner(sqlite_session, sqlite_session_factory):
    tenant, account = _seed(sqlite_session)
    repository = Mock(wraps=SQLAlchemyAccountActivationRepository(sqlite_session_factory))
    service, tokens, _, _, cache, sync = _service(repository, tenant, account)
    other, *_ = _service(repository, tenant, account)
    receipt = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    with pytest.raises(InvalidInvitationError):
        other.persist_activation(receipt, session=sqlite_session)
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(_command(tenant), session=sqlite_session)
    repository.persist_activation.assert_not_called()
    assert service.persist_activation(receipt, session=sqlite_session).membership_created
    _assert_no_effects(tokens, cache, sync)


def test_helper_failure_is_caller_rollback_and_receipt_requires_fresh_prepare(
    sqlite_session, sqlite_session_factory, monkeypatch
):
    tenant, account = _seed(sqlite_session)
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    service, tokens, _, _, cache, sync = _service(repository, tenant, account)
    receipt = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    rollback = Mock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    execute = sqlite_session.execute
    monkeypatch.setattr(sqlite_session, "execute", Mock(side_effect=RuntimeError("database write failure")))
    with pytest.raises(RuntimeError, match="database write failure"):
        service.persist_activation(receipt, session=sqlite_session)
    rollback.assert_not_called()
    sqlite_session.rollback()
    monkeypatch.setattr(sqlite_session, "execute", execute)
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(receipt, session=sqlite_session)
    fresh = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    assert service.persist_activation(fresh, session=sqlite_session).membership_created
    _assert_no_effects(tokens, cache, sync)


@pytest.mark.parametrize("status", [AccountStatus.BANNED, AccountStatus.CLOSED, AccountStatus.UNINITIALIZED])
def test_preparation_does_not_claim_provider_status_admission_or_change_legacy_policy(
    status, sqlite_session, sqlite_session_factory
):
    tenant, account = _seed(sqlite_session, status=status)
    service, tokens, policy, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account, requires_setup=None
    )
    receipt = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    assert receipt.invitation.account_status == status.value
    assert (
        receipt.setup is None
    )  # Legacy inference only considers PENDING; Casdoor must separately reject banned/closed.
    policy.ensure_allowed.assert_not_called()
    with sqlite_session_factory() as observer:
        assert observer.get(Account, account.id).status == status
        assert observer.get(Account, account.id).initialized_at is None
    _assert_no_effects(tokens, cache, sync)


@pytest.mark.parametrize("existing_role", [None, TenantAccountRole.EDITOR])
def test_public_activation_still_revokes_then_commits_then_cache_if_new_and_access_sync(
    existing_role, sqlite_session, sqlite_session_factory, monkeypatch
):
    tenant, account = _seed(sqlite_session, existing_role=existing_role)
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    service, tokens, policy, eligibility, cache, sync = _service(repository, tenant, account)
    sequence = []
    token = tokens.find.return_value
    tokens.find.side_effect = lambda lookup: (sequence.append("find"), token)[1]
    resolve = repository.resolve
    monkeypatch.setattr(repository, "resolve", lambda value: (sequence.append("resolve"), resolve(value))[1])
    eligibility.get_freeze_type.side_effect = lambda email: (sequence.append("freeze"), None)[1]
    resolve_setup = service._resolve_setup
    monkeypatch.setattr(service, "_resolve_setup", lambda *args: (sequence.append("setup"), resolve_setup(*args))[1])
    tokens.revoke.side_effect = lambda lookup: sequence.append("revoke")

    def committed(session):
        with sqlite_session_factory() as observer:
            assert observer.get(Account, account.id).initialized_at is not None
            assert observer.scalar(select(TenantAccountJoin)).current
        sequence.append("commit")

    def effect(name):
        def run(*args):
            assert sequence[-1] == ("cache" if name == "sync" and existing_role is None else "commit")
            with sqlite_session_factory() as observer:
                assert _count(observer, TenantAccountJoin) == 1
            sequence.append(name)

        return run

    event.listen(sqlite_session_factory.class_, "after_commit", committed)
    cache.invalidate.side_effect = effect("cache")
    sync.sync.side_effect = effect("sync")
    try:
        assert service.activate(_command(tenant), authenticated_account_id=None) is None
    finally:
        event.remove(sqlite_session_factory.class_, "after_commit", committed)
    assert sequence == ["find", "resolve", "freeze", "setup", "revoke", "commit"] + (
        ["cache", "sync"] if existing_role is None else ["sync"]
    )
    tokens.revoke.assert_called_once_with(replace(_command(tenant).invitation, email=account.email))
    sync.sync.assert_called_once_with(tenant.id, account.id)
    policy.ensure_allowed.assert_not_called()


def test_public_none_result_preserves_original_revoke_before_rejection(sqlite_session, sqlite_session_factory):
    tenant, account = _seed(sqlite_session)
    repository = Mock(wraps=SQLAlchemyAccountActivationRepository(sqlite_session_factory))
    service, tokens, _, _, cache, sync = _service(repository, tenant, account)
    sequence = []
    tokens.revoke.side_effect = lambda lookup: sequence.append("revoke")
    repository.activate.side_effect = lambda *args, **kwargs: (sequence.append("persist"), None)[1]
    with pytest.raises(InvalidInvitationError):
        service.activate(_command(tenant), authenticated_account_id=None)
    assert sequence == ["revoke", "persist"]
    cache.invalidate.assert_not_called()
    sync.sync.assert_not_called()


def test_public_commit_failure_keeps_revoked_token_but_has_no_cache_or_task(sqlite_session, sqlite_session_factory):
    tenant, account = _seed(sqlite_session)
    service, tokens, _, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )

    def fail_commit(session):
        raise RuntimeError("original commit failure")

    event.listen(sqlite_session_factory.class_, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="original commit failure"):
            service.activate(_command(tenant), authenticated_account_id=None)
    finally:
        event.remove(sqlite_session_factory.class_, "before_commit", fail_commit)
    tokens.revoke.assert_called_once()
    cache.invalidate.assert_not_called()
    sync.sync.assert_not_called()
    with sqlite_session_factory() as observer:
        assert observer.get(Account, account.id).status == AccountStatus.PENDING
        assert _count(observer, TenantAccountJoin) == 0


@pytest.mark.parametrize("effect", ["cache", "sync"])
def test_public_postcommit_failure_does_not_roll_back_completed_activation(
    effect, sqlite_session, sqlite_session_factory
):
    tenant, account = _seed(sqlite_session)
    service, tokens, _, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )
    getattr(
        cache if effect == "cache" else sync, "invalidate" if effect == "cache" else "sync"
    ).side_effect = RuntimeError("postcommit effect failure")
    with pytest.raises(RuntimeError, match="postcommit effect failure"):
        service.activate(_command(tenant), authenticated_account_id=None)
    tokens.revoke.assert_called_once()
    if effect == "cache":
        sync.sync.assert_not_called()
    with sqlite_session_factory() as observer:
        assert observer.get(Account, account.id).initialized_at is not None
        assert _count(observer, TenantAccountJoin) == 1


def test_minimal_setup_reuses_original_fields_without_authorizing_or_committing_new_account(
    sqlite_session, sqlite_session_factory, monkeypatch, config_overrides
):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, ACCOUNT_TOTAL_QUOTA=Decimal(15))
    features = MagicMock()
    features.get_license.return_value.seats.is_available.return_value = True
    monkeypatch.setattr(account_service, "SystemFeatureService", features)
    prepared = AccountService.prepare_account_creation("new@example.test", "New", "en-US")
    account = AccountService.persist_account_creation(prepared, session=sqlite_session)
    quota = sqlite_session.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account.id))
    quota.used_quota = Decimal(3)
    assert account.status == AccountStatus.ACTIVE
    assert account.initialized_at is None
    commit, rollback = Mock(wraps=sqlite_session.commit), Mock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "commit", commit)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    SQLAlchemyAccountActivationRepository.persist_account_setup(account, _setup())
    assert account.status == AccountStatus.ACTIVE
    assert account.initialized_at is not None
    assert account.name == _setup().name
    assert account.interface_theme == "light"
    assert account.interface_language == _setup().interface_language
    assert account.timezone == _setup().timezone
    assert account.password is None
    assert account.password_salt is None
    assert quota.used_quota == Decimal(3)
    assert quota.total_quota == Decimal(15)
    sqlite_session.flush()
    commit.assert_not_called()
    rollback.assert_not_called()
    with sqlite_session_factory() as observer:
        assert _count(observer, Account) == 0
        assert _count(observer, AccountMoneyExtend) == 0
    sqlite_session.rollback()
    with sqlite_session_factory() as observer:
        assert _count(observer, Account) == 0
        assert _count(observer, AccountMoneyExtend) == 0


def test_invitation_setup_join_identity_intent_and_existing_quota_rollback_together(
    sqlite_session, sqlite_session_factory
):
    tenant, account = _seed(sqlite_session)
    quota = AccountMoneyExtend(account_id=account.id, total_quota=Decimal(8), used_quota=Decimal(2))
    sqlite_session.add(quota)
    namespace, revision = _casdoor_context(sqlite_session, tenant)
    service, tokens, _, _, cache, sync = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )
    prepared = service.prepare_activation(_command(tenant), authenticated_account_id=account.id)

    def provision_then_fail():
        with sqlite_session.begin():
            assert service.persist_activation(prepared, session=sqlite_session).membership_created
            identity = CasdoorIdentityExtend(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization="Org",
                subject="synthetic-subject",
                last_applied_json="{}",
                profile_sync_json="{}",
            )
            sqlite_session.add(identity)
            sqlite_session.flush()
            intent = CasdoorSyncIntentExtend(
                namespace_id=namespace.id,
                identity_id=identity.id,
                account_id=account.id,
                workspace_id=tenant.id,
                revision_id=revision.id,
                generation=1,
                ownership_epoch=0,
                fence_epoch=0,
                kind=CasdoorIntentKind.INVITATION_FINALIZE,
                scope_digest="c" * 64,
                idempotency_key="d" * 64,
                desired_json="{}",
            )
            sqlite_session.add(intent)
            sqlite_session.flush()
            assert _count(sqlite_session, TenantAccountJoin) == 1
            assert _count(sqlite_session, CasdoorIdentityExtend) == 1
            assert _count(sqlite_session, CasdoorSyncIntentExtend) == 1
            # Local activation has not completed durable invitation finalization.
            assert intent.operation_state.value == "pending"
            assert quota.total_quota == Decimal(8)
            assert quota.used_quota == Decimal(2)
            _assert_no_effects(tokens, cache, sync)
            raise RuntimeError("later intent failure")

    with pytest.raises(RuntimeError, match="later intent failure"):
        provision_then_fail()
    with sqlite_session_factory() as observer:
        persisted = observer.get(Account, account.id)
        assert persisted.status == AccountStatus.PENDING
        assert persisted.initialized_at is None
        assert persisted.name == "Pending"
        assert _count(observer, TenantAccountJoin) == 0
        assert _count(observer, CasdoorIdentityExtend) == 0
        assert _count(observer, CasdoorSyncIntentExtend) == 0
        existing_quota = observer.scalar(select(AccountMoneyExtend))
        assert existing_quota.total_quota == Decimal(8)
        assert existing_quota.used_quota == Decimal(2)
    _assert_no_effects(tokens, cache, sync)


def _casdoor_context(sqlite_session, tenant):
    integration = CasdoorIntegrationExtend()
    sqlite_session.add(integration)
    sqlite_session.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer="https://synthetic.example",
        organization="Org",
        application="App",
        client_id="Client",
        core_fingerprint="a" * 64,
    )
    sqlite_session.add(namespace)
    sqlite_session.flush()
    revision = CasdoorConfigRevisionExtend(
        integration_id=integration.id,
        namespace_id=namespace.id,
        revision_number=1,
        config_digest="b" * 64,
        browser_frontend_url="https://synthetic.example",
        backend_api_url="https://synthetic.example",
        expected_issuer=namespace.expected_issuer,
        organization="Org",
        application="App",
        client_id="Client",
        button_text="SSO",
        default_workspace_id=tenant.id,
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    sqlite_session.add(revision)
    sqlite_session.commit()
    return namespace, revision


def test_new_account_setup_quota_join_identity_and_intent_share_caller_rollback(
    sqlite_session, sqlite_session_factory, monkeypatch, config_overrides
):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, ACCOUNT_TOTAL_QUOTA=Decimal(15))
    features = MagicMock()
    features.get_license.return_value.seats.is_available.return_value = True
    monkeypatch.setattr(account_service, "SystemFeatureService", features)
    tenant = Tenant(name="Existing workspace")
    sqlite_session.add(tenant)
    namespace, revision = _casdoor_context(sqlite_session, tenant)
    prepared = AccountService.prepare_account_creation("new-org@example.test", "New", "en-US")
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    commit = Mock(wraps=sqlite_session.commit)
    monkeypatch.setattr(sqlite_session, "commit", commit)
    models = (Account, AccountMoneyExtend, TenantAccountJoin, CasdoorIdentityExtend, CasdoorSyncIntentExtend)

    def provision_then_fail():
        with sqlite_session.begin():
            account = AccountService.persist_account_creation(prepared, session=sqlite_session)
            assert account.initialized_at is None
            repository.persist_account_setup(account, _setup())
            assert account.initialized_at is not None
            # Synthetic internal reference is a transaction test, not verified provider authorization.
            result = repository.persist_activation(
                _invitation(tenant, account, requires_setup=False, role="normal"),
                role="normal",
                setup=None,
                session=sqlite_session,
            )
            assert result.membership_created
            identity = CasdoorIdentityExtend(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization="Org",
                subject="synthetic-new-subject",
                last_applied_json="{}",
                profile_sync_json="{}",
            )
            sqlite_session.add(identity)
            sqlite_session.flush()
            sqlite_session.add(
                CasdoorSyncIntentExtend(
                    namespace_id=namespace.id,
                    identity_id=identity.id,
                    account_id=account.id,
                    workspace_id=tenant.id,
                    revision_id=revision.id,
                    generation=1,
                    ownership_epoch=0,
                    fence_epoch=0,
                    kind=CasdoorIntentKind.RESOURCE_GRANT,
                    scope_digest="c" * 64,
                    idempotency_key="d" * 64,
                    desired_json="{}",
                )
            )
            sqlite_session.flush()
            for model in models:
                assert _count(sqlite_session, model) == 1
            with sqlite_session_factory() as observer:
                for model in models:
                    assert _count(observer, model) == 0
            commit.assert_not_called()
            raise RuntimeError("later provisioning failure")

    with pytest.raises(RuntimeError, match="later provisioning failure"):
        provision_then_fail()
    with sqlite_session_factory() as observer:
        for model in models:
            assert _count(observer, model) == 0
        assert _count(observer, Tenant) == 1
    commit.assert_not_called()


@pytest.mark.parametrize("rbac_enabled", [False, True])
@pytest.mark.parametrize("existing_role", [None, TenantAccountRole.EDITOR])
def test_public_real_sync_adapter_dispatches_original_task_only_after_commit_and_optional_cache(
    rbac_enabled, existing_role, sqlite_session, sqlite_session_factory, monkeypatch
):
    from services.account_adapters import RBACWorkspaceMemberAccessSync
    from tasks.initialize_created_app_rbac_access_task import sync_joined_workspace_member_rbac_access_task

    tenant, account = _seed(sqlite_session, existing_role=existing_role)
    service, _, _, _, cache, _ = _service(
        SQLAlchemyAccountActivationRepository(sqlite_session_factory), tenant, account
    )
    service._member_access_sync = RBACWorkspaceMemberAccessSync(enabled=rbac_enabled)

    def dispatched(*args, **kwargs):
        with sqlite_session_factory() as observer:
            assert observer.get(Account, account.id).initialized_at is not None
            assert _count(observer, TenantAccountJoin) == 1
        assert cache.invalidate.call_count == (1 if existing_role is None else 0)

    delay = Mock(side_effect=dispatched)
    monkeypatch.setattr(sync_joined_workspace_member_rbac_access_task, "delay", delay)
    prepared = service.prepare_activation(_command(tenant), authenticated_account_id=None)
    service.persist_activation(prepared, session=sqlite_session)
    delay.assert_not_called()
    cache.invalidate.assert_not_called()
    sqlite_session.rollback()
    service.activate(_command(tenant), authenticated_account_id=None)
    if rbac_enabled:
        delay.assert_called_once_with(tenant.id, account.id, operator_account_id=None)
    else:
        delay.assert_not_called()
