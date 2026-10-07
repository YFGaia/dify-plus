"""Independent transaction and compatibility checks for invitation preparation."""

from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.sql.elements import ClauseElement

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
from services.account_activation_service import AccountActivationService, InvalidInvitationError
from services.account_service import AccountService
from services.entities.account_activation_entities import (
    AccountInvitation,
    AccountSetup,
    ActivationCommand,
    InvitationLookup,
    InvitationToken,
)


def _fixture(session, factory, *, status=AccountStatus.PENDING, role="editor", setup=True):
    tenant = Tenant(name="Existing destination")
    account = Account(name="Before", email="target@example.test", status=status)
    session.add_all([tenant, account])
    session.flush()
    session.commit()  # resolve() deliberately uses its own read Session.
    token = InvitationToken(
        account_id=account.id,
        email=account.email,
        workspace_id=tenant.id,
        role=role,
        requires_setup=setup,
    )
    tokens, eligibility, cache, sync, policy = Mock(), Mock(), Mock(), Mock(), Mock()
    tokens.find.return_value = token
    eligibility.get_freeze_type.return_value = None
    service = AccountActivationService(
        tokens=tokens,
        accounts=SQLAlchemyAccountActivationRepository(factory),
        workspace_policy=policy,
        eligibility=eligibility,
        membership_cache=cache,
        member_access_sync=sync,
    )
    command = ActivationCommand(
        invitation=InvitationLookup(tenant.id, "Target@Example.test", "opaque-test-token"),
        name="Initialized Name",
        interface_language="zh-Hans",
        timezone="Asia/Shanghai",
    )
    return tenant, account, service, command, tokens, eligibility, cache, sync, policy


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_prepare_is_a_read_only_receipt_and_does_not_turn_a_command_into_authority(
    sqlite_session, sqlite_session_factory
):
    tenant, account, service, command, tokens, eligibility, cache, sync, policy = _fixture(
        sqlite_session, sqlite_session_factory, role="owner"
    )
    prepared = service.prepare_activation(command, authenticated_account_id=account.id)

    assert prepared.invitation.account_id == account.id
    assert prepared.invitation.workspace_id == tenant.id
    assert prepared.role == "normal"  # Owner is not granted by the legacy invite role field.
    assert prepared.setup == AccountSetup("Initialized Name", "zh-Hans", "Asia/Shanghai")
    tokens.find.assert_called_once_with(command.invitation)
    eligibility.get_freeze_type.assert_called_once_with(account.email)
    policy.ensure_allowed.assert_not_called()  # That check remains separately owned by check().
    tokens.revoke.assert_not_called()
    cache.invalidate.assert_not_called()
    sync.sync.assert_not_called()
    with sqlite_session_factory() as reader:
        assert reader.get(Account, account.id).initialized_at is None
        assert _count(reader, TenantAccountJoin) == 0


@pytest.mark.parametrize("status", [AccountStatus.BANNED, AccountStatus.CLOSED, AccountStatus.UNINITIALIZED])
def test_prepare_does_not_add_provider_status_admission_or_change_account(
    status, sqlite_session, sqlite_session_factory
):
    _, account, service, command, tokens, _, cache, sync, _ = _fixture(
        sqlite_session, sqlite_session_factory, status=status, setup=None
    )
    prepared = service.prepare_activation(command, authenticated_account_id=None)
    assert prepared.invitation.account_status == status.value
    assert prepared.setup is None  # Default ACTIVE or other status is not an initialization proof.
    with sqlite_session_factory() as reader:
        fresh = reader.get(Account, account.id)
        assert fresh.status == status
        assert fresh.initialized_at is None
    tokens.revoke.assert_not_called()
    cache.invalidate.assert_not_called()
    sync.sync.assert_not_called()


def test_prepare_preserves_case_fallback_and_only_legacy_setup_inference(sqlite_session, sqlite_session_factory):
    tenant, account, service, command, tokens, _, _, _, _ = _fixture(sqlite_session, sqlite_session_factory, setup=None)
    # A token with no explicit setup bit follows the old pending-only inference.
    tokens.find.side_effect = [
        InvitationToken(account.id, account.email.title(), tenant.id, "admin", None),
        InvitationToken(account.id, account.email, tenant.id, "admin", None),
    ]
    prepared = service.prepare_activation(command, authenticated_account_id=None)
    assert [item.args[0].email for item in tokens.find.call_args_list] == [
        "Target@Example.test",
        "target@example.test",
    ]
    assert prepared.setup == AccountSetup("Initialized Name", "zh-Hans", "Asia/Shanghai")


def test_single_use_receipt_and_repository_write_remain_inside_caller_transaction(
    sqlite_session, sqlite_session_factory, monkeypatch
):
    tenant, account, service, command, tokens, _, cache, sync, _ = _fixture(sqlite_session, sqlite_session_factory)
    other = Tenant(name="Prior current workspace")
    sqlite_session.add(other)
    sqlite_session.flush()
    old_join = TenantAccountJoin(tenant_id=other.id, account_id=account.id, role=TenantAccountRole.EDITOR, current=True)
    old_join.invited_by = "local-inviter"
    sqlite_session.add(old_join)
    sqlite_session.commit()

    prepared = service.prepare_activation(command, authenticated_account_id=None)
    repository = service._accounts
    monkeypatch.setattr(repository, "_session_factory", Mock(side_effect=AssertionError("unexpected new session")))
    commit, rollback = Mock(wraps=sqlite_session.commit), Mock(wraps=sqlite_session.rollback)
    monkeypatch.setattr(sqlite_session, "commit", commit)
    monkeypatch.setattr(sqlite_session, "rollback", rollback)
    statements: list[ClauseElement] = []

    def record(state: ORMExecuteState) -> None:
        statements.append(state.statement)

    event.listen(sqlite_session, "do_orm_execute", record)
    try:
        result = service.persist_activation(prepared, session=sqlite_session)
        assert result.membership_created
        sqlite_session.flush()
    finally:
        event.remove(sqlite_session, "do_orm_execute", record)

    account_reads = [str(s.compile(dialect=postgresql.dialect())) for s in statements if "FROM accounts" in str(s)]
    assert len(account_reads) == 1
    assert "accounts.id =" in account_reads[0]
    assert "accounts.email =" in account_reads[0]
    assert "FOR UPDATE" in account_reads[0]
    assert commit.call_count == rollback.call_count == 0
    assert old_join.current is False
    assert account.status == AccountStatus.ACTIVE
    assert account.initialized_at is not None
    assert account.interface_theme == "light"
    prior_workspace_id = other.id
    with sqlite_session_factory() as reader:
        assert reader.get(Account, account.id).status == AccountStatus.PENDING
        assert _count(reader, TenantAccountJoin) == 1
    sqlite_session.rollback()
    with sqlite_session_factory() as reader:
        restored = reader.get(Account, account.id)
        assert restored.name == "Before"
        assert restored.status == AccountStatus.PENDING
        assert restored.initialized_at is None
        restored_join = reader.scalar(
            select(TenantAccountJoin).where(
                TenantAccountJoin.tenant_id == prior_workspace_id,
                TenantAccountJoin.account_id == account.id,
            )
        )
        assert restored_join is not None
        assert restored_join.current is True
        assert _count(reader, TenantAccountJoin) == 1
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(prepared, session=sqlite_session)
    monkeypatch.setattr(repository, "_session_factory", sqlite_session_factory)
    fresh = service.prepare_activation(command, authenticated_account_id=None)
    assert service.persist_activation(fresh, session=sqlite_session).membership_created
    tokens.revoke.assert_not_called()
    cache.invalidate.assert_not_called()
    sync.sync.assert_not_called()


@pytest.mark.parametrize("stale", ["tenant", "account", "email"])
def test_write_rechecks_current_normal_tenant_and_exact_account_email(stale, sqlite_session, sqlite_session_factory):
    tenant, account, service, command, _, _, _, _, _ = _fixture(sqlite_session, sqlite_session_factory)
    prepared = service.prepare_activation(command, authenticated_account_id=None)
    if stale == "tenant":
        tenant.status = TenantStatus.ARCHIVE
    elif stale == "account":
        sqlite_session.delete(account)
    else:
        account.email = "new-address@example.test"
    sqlite_session.commit()
    with pytest.raises(InvalidInvitationError):
        service.persist_activation(prepared, session=sqlite_session)
    assert _count(sqlite_session, TenantAccountJoin) == 0


@pytest.mark.parametrize("role", [TenantAccountRole.EDITOR, TenantAccountRole.OWNER])
def test_existing_membership_role_and_inviter_are_not_replaced(role, sqlite_session, sqlite_session_factory):
    tenant, account, service, command, _, _, _, _, _ = _fixture(
        sqlite_session, sqlite_session_factory, status=AccountStatus.ACTIVE, role="admin", setup=False
    )
    existing = TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=role)
    existing.invited_by = "prior-actor"
    sqlite_session.add(existing)
    sqlite_session.commit()
    prepared = service.prepare_activation(command, authenticated_account_id=account.id)
    assert service.persist_activation(prepared, session=sqlite_session).membership_created is False
    sqlite_session.flush()
    assert _count(sqlite_session, TenantAccountJoin) == 1
    assert existing.role == role
    assert existing.invited_by == "prior-actor"
    assert existing.current is True
    assert account.initialized_at is None


def test_setup_assigns_only_six_legacy_fields_and_never_creates_an_owner_workspace(
    sqlite_session, sqlite_session_factory
):
    tenant, account, *_ = _fixture(sqlite_session, sqlite_session_factory)
    account.password = None
    account.password_salt = None
    before_workspaces = _count(sqlite_session, Tenant)
    quota = AccountMoneyExtend(account_id=account.id, total_quota=Decimal(11), used_quota=Decimal(4))
    sqlite_session.add(quota)
    sqlite_session.flush()
    SQLAlchemyAccountActivationRepository.persist_account_setup(account, AccountSetup("Ready", "en-US", "UTC"))
    assert (account.name, account.interface_language, account.timezone, account.interface_theme) == (
        "Ready",
        "en-US",
        "UTC",
        "light",
    )
    assert account.status == AccountStatus.ACTIVE
    assert account.initialized_at is not None
    assert account.password is None
    assert account.password_salt is None
    assert (quota.total_quota, quota.used_quota) == (Decimal(11), Decimal(4))
    assert _count(sqlite_session, Tenant) == before_workspaces == 1
    assert _count(sqlite_session, Tenant) == before_workspaces  # Setup has no workspace creation capability.


def _casdoor_intent_context(session, workspace):
    integration = CasdoorIntegrationExtend()
    session.add(integration)
    session.flush()
    namespace = CasdoorNamespaceExtend(
        integration_id=integration.id,
        expected_issuer="https://fixture.invalid",
        organization="fixture-org",
        application="fixture-app",
        client_id="fixture-client",
        core_fingerprint="a" * 64,
    )
    session.add(namespace)
    session.flush()
    revision = CasdoorConfigRevisionExtend(
        integration_id=integration.id,
        namespace_id=namespace.id,
        revision_number=1,
        config_digest="b" * 64,
        browser_frontend_url="https://fixture.invalid",
        backend_api_url="https://fixture.invalid",
        expected_issuer=namespace.expected_issuer,
        organization=namespace.organization,
        application=namespace.application,
        client_id=namespace.client_id,
        button_text="SSO",
        default_workspace_id=workspace.id,
        certificates_json="[]",
        policy_json="{}",
        mappings_json="[]",
    )
    session.add(revision)
    session.commit()
    return namespace, revision


def test_invitation_join_identity_and_pending_finalization_intent_rollback_together(
    sqlite_session, sqlite_session_factory
):
    tenant, account, service, command, tokens, _, cache, sync, _ = _fixture(sqlite_session, sqlite_session_factory)
    quota = AccountMoneyExtend(account_id=account.id, total_quota=Decimal(8), used_quota=Decimal(3))
    sqlite_session.add(quota)
    namespace, revision = _casdoor_intent_context(sqlite_session, tenant)
    prepared = service.prepare_activation(command, authenticated_account_id=account.id)

    def provision_then_fail():
        with sqlite_session.begin():
            service.persist_activation(prepared, session=sqlite_session)
            identity = CasdoorIdentityExtend(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization=namespace.organization,
                subject="synthetic-subject-only",
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
            assert intent.operation_state.value == "pending"  # Persisted intent is not finalization.
            with sqlite_session_factory() as reader:
                assert _count(reader, TenantAccountJoin) == 0
                assert _count(reader, CasdoorIdentityExtend) == 0
                assert _count(reader, CasdoorSyncIntentExtend) == 0
            raise RuntimeError("abort invitation UoW")

    with pytest.raises(RuntimeError, match="abort invitation UoW"):
        provision_then_fail()

    with sqlite_session_factory() as reader:
        restored = reader.get(Account, account.id)
        assert restored.name == "Before"
        assert restored.status == AccountStatus.PENDING
        assert restored.initialized_at is None
        assert _count(reader, TenantAccountJoin) == 0
        assert _count(reader, CasdoorIdentityExtend) == 0
        assert _count(reader, CasdoorSyncIntentExtend) == 0
        persisted_quota = reader.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account.id))
        assert persisted_quota.total_quota == Decimal(8)
        assert persisted_quota.used_quota == Decimal(3)
    tokens.revoke.assert_not_called()
    cache.invalidate.assert_not_called()
    sync.sync.assert_not_called()


def test_legacy_public_activation_keeps_revoke_commit_cache_sync_order_and_none_defect(
    sqlite_session, sqlite_session_factory
):
    tenant, account, service, command, tokens, _, cache, sync, _ = _fixture(sqlite_session, sqlite_session_factory)
    order = []
    tokens.revoke.side_effect = lambda _: order.append("revoke")
    cache.invalidate.side_effect = lambda _: order.append("cache")
    sync.sync.side_effect = lambda *_: order.append("sync")

    def visible_after_commit(session):
        with sqlite_session_factory() as reader:
            assert _count(reader, TenantAccountJoin) == 1
            assert reader.get(Account, account.id).initialized_at is not None
        order.append("commit")

    event.listen(sqlite_session_factory.class_, "after_commit", visible_after_commit)
    try:
        assert service.activate(command, authenticated_account_id=None) is None
    finally:
        event.remove(sqlite_session_factory.class_, "after_commit", visible_after_commit)
    assert order == ["revoke", "commit", "cache", "sync"]
    tokens.revoke.assert_called_once_with(InvitationLookup(tenant.id, "target@example.test", "opaque-test-token"))

    tenant2, account2, failing, command2, tokens2, _, cache2, sync2, _ = _fixture(
        sqlite_session, sqlite_session_factory
    )
    failing._accounts.activate = Mock(return_value=None)
    with pytest.raises(InvalidInvitationError):
        failing.activate(command2, authenticated_account_id=None)
    tokens2.revoke.assert_called_once()  # Historical token consumption still precedes a None result.
    cache2.invalidate.assert_not_called()
    sync2.sync.assert_not_called()
    assert tenant2.id != tenant.id
    assert account2.id != account.id


def test_new_account_quota_setup_join_identity_and_intent_are_one_rollback_unit(
    sqlite_session, sqlite_session_factory, monkeypatch, config_overrides
):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, ACCOUNT_TOTAL_QUOTA=Decimal(15))
    license_service = Mock()
    license_service.get_license.return_value.seats.is_available.return_value = True
    monkeypatch.setattr(account_service, "SystemFeatureService", license_service)
    workspace = Tenant(name="Preexisting normal workspace")
    sqlite_session.add(workspace)
    namespace, revision = _casdoor_intent_context(sqlite_session, workspace)
    sqlite_session.expire_all()
    prepared = AccountService.prepare_account_creation("new-org@example.test", "Org User", "en-US")
    activation_repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)
    unit_models = (Account, AccountMoneyExtend, TenantAccountJoin, CasdoorIdentityExtend, CasdoorSyncIntentExtend)
    commit = Mock(wraps=sqlite_session.commit)
    monkeypatch.setattr(sqlite_session, "commit", commit)

    def provision_new_account_then_fail():
        with sqlite_session.begin():
            account = AccountService.persist_account_creation(prepared, session=sqlite_session)
            assert account.initialized_at is None  # Account default ACTIVE does not equal initialized.
            quota = sqlite_session.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account.id))
            assert quota is not None
            assert quota.total_quota == Decimal(15)
            assert quota.used_quota == Decimal(0)
            activation_repository.persist_account_setup(account, AccountSetup("Org User", "en-US", "UTC"))
            assert account.status == AccountStatus.ACTIVE
            assert account.initialized_at is not None
            assert account.password is None
            invitation = AccountInvitation(
                account_id=account.id,
                account_email=account.email,
                account_status=account.status.value,
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                role="normal",
                requires_setup=False,
            )
            assert activation_repository.persist_activation(
                invitation, role="normal", setup=None, session=sqlite_session
            ).membership_created
            identity = CasdoorIdentityExtend(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization=namespace.organization,
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
                    workspace_id=workspace.id,
                    revision_id=revision.id,
                    generation=1,
                    ownership_epoch=0,
                    fence_epoch=0,
                    kind=CasdoorIntentKind.RESOURCE_GRANT,
                    scope_digest="e" * 64,
                    idempotency_key="f" * 64,
                    desired_json="{}",
                )
            )
            sqlite_session.flush()
            assert all(_count(sqlite_session, model) == 1 for model in unit_models)
            with sqlite_session_factory() as reader:
                assert all(_count(reader, model) == 0 for model in unit_models)
            commit.assert_not_called()
            raise RuntimeError("abort new account UoW")

    with pytest.raises(RuntimeError, match="abort new account UoW"):
        provision_new_account_then_fail()

    with sqlite_session_factory() as reader:
        assert all(_count(reader, model) == 0 for model in unit_models)
        assert _count(reader, Tenant) == 1  # The already-existing destination remains.
    commit.assert_not_called()
