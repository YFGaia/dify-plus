"""Independent persistence checks for the account creation helper.

These tests exercise committed database state through separate sessions so the
caller-owned transaction boundary is observable without mocking its implementation.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from enums import DeploymentEdition
from models.account import Account, AccountStatus
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorIdentityExtend, CasdoorIntegrationExtend, CasdoorNamespaceExtend
from services import account_service
from services.account_service import AccountService


def _prepare(email: str):
    return AccountService.prepare_account_creation(email, "Casdoor User", "en-US")


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_prepared_account_and_quota_are_invisible_until_caller_commits(sqlite_session_factory):
    prepared = _prepare("deferred@example.test")
    account_id = prepared.account.id

    with sqlite_session_factory() as writer:
        account = AccountService.persist_account_creation(prepared, session=writer)
        assert account.id == account_id
        assert _count(writer, Account) == 1
        assert _count(writer, AccountMoneyExtend) == 1

        # A distinct connection cannot observe the account or quota before the
        # caller commits the transaction.
        with sqlite_session_factory() as observer:
            assert observer.get(Account, account_id) is None
            assert _count(observer, AccountMoneyExtend) == 0

        writer.commit()

    with sqlite_session_factory() as observer:
        account = observer.get(Account, account_id)
        assert account is not None
        assert account.status == AccountStatus.ACTIVE  # Existing model constructor default.
        assert account.initialized_at is None
        assert _count(observer, AccountMoneyExtend) == 1


def test_later_identity_failure_rolls_back_account_quota_and_identity(sqlite_session_factory):
    prepared = _prepare("rollback@example.test")
    account_id = prepared.account.id

    def provision_then_fail():
        with sqlite_session_factory.begin() as session:
            integration = CasdoorIntegrationExtend()
            session.add(integration)
            session.flush()
            namespace = CasdoorNamespaceExtend(
                integration_id=integration.id,
                expected_issuer="https://idp.example.test",
                organization="example",
                application="console",
                client_id="client",
                core_fingerprint="a" * 64,
            )
            session.add(namespace)
            session.flush()

            account = AccountService.persist_account_creation(prepared, session=session)
            identity = CasdoorIdentityExtend(
                namespace_id=namespace.id,
                account_id=account.id,
                issuer=namespace.expected_issuer,
                organization=namespace.organization,
                subject="subject-1",
                last_applied_json="{}",
                profile_sync_json="{}",
            )
            session.add(identity)
            session.flush()
            assert _count(session, Account) == 1
            assert _count(session, AccountMoneyExtend) == 1
            assert _count(session, CasdoorIdentityExtend) == 1
            raise RuntimeError("simulated failure after identity write")

    with pytest.raises(RuntimeError, match="simulated failure after identity write"):
        provision_then_fail()

    # The transaction context manager has rolled back the full write unit.
    with sqlite_session_factory() as observer:
        assert observer.get(Account, account_id) is None
        assert _count(observer, AccountMoneyExtend) == 0
        assert _count(observer, CasdoorIdentityExtend) == 0
        assert _count(observer, CasdoorNamespaceExtend) == 0


def test_legacy_public_creation_still_commits_account_and_quota(sqlite_session_factory, monkeypatch, config_overrides):
    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    features = MagicMock()
    features.is_registration_allowed.return_value = True
    features.get_license.return_value.seats.is_available.return_value = True
    billing = MagicMock()
    billing.is_email_in_freeze.return_value = False
    monkeypatch.setattr(account_service, "SystemFeatureService", features)
    monkeypatch.setattr(account_service, "BillingService", billing)
    with sqlite_session_factory() as writer:
        account = AccountService.create_account("legacy@example.test", "Legacy User", "en-US", session=writer)
        account_id = account.id

    with sqlite_session_factory() as observer:
        assert observer.get(Account, account_id) is not None
        assert _count(observer, AccountMoneyExtend) == 1
