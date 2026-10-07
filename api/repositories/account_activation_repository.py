"""SQLAlchemy repository for account invitation activation."""

from typing import override

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from libs.datetime_utils import naive_utc_now
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository
from services.account_activation_service import AccountActivationRepository
from services.entities.account_activation_entities import (
    AccountInvitation,
    AccountSetup,
    ActivationPersistenceResult,
    InvitationToken,
    InvitedAccountObservation,
)


class SQLAlchemyAccountActivationRepository(AccountActivationRepository):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @override
    def observe_invited_account(self, invitation: AccountInvitation) -> InvitedAccountObservation | None:
        """Close a short exact-account/normal-workspace read before eligibility I/O.

        The service validates identifiers before this read. A caller must still
        lock and refresh all required rows and recheck token/policy at its final
        barrier; this observation owns neither locks nor invitation consumption.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(
                    Account.initialized_at,
                    Account.name,
                    Account.interface_language,
                    Account.timezone,
                    Account.interface_theme,
                )
                .join(Tenant, Tenant.id == invitation.workspace_id)
                .where(
                    Account.id == invitation.account_id,
                    Account.email == invitation.account_email,
                    Account.status == invitation.account_status,
                    Tenant.status == TenantStatus.NORMAL,
                )
            ).one_or_none()
            return InvitedAccountObservation(invitation, *row) if row is not None else None

    @override
    def resolve(self, invitation: InvitationToken) -> AccountInvitation | None:
        with self._session_factory() as session:
            tenant = session.scalar(
                select(Tenant).where(
                    Tenant.id == invitation.workspace_id,
                    Tenant.status == TenantStatus.NORMAL,
                )
            )
            if tenant is None:
                return None

            account = session.scalar(
                select(Account).where(
                    Account.id == invitation.account_id,
                    Account.email == invitation.email,
                )
            )
            if account is None:
                return None

            return AccountInvitation(
                account_id=account.id,
                account_email=account.email,
                account_status=account.status.value,
                workspace_id=tenant.id,
                workspace_name=tenant.name,
                role=invitation.role,
                requires_setup=invitation.requires_setup,
            )

    @override
    def activate(
        self,
        invitation: AccountInvitation,
        *,
        role: str,
        setup: AccountSetup | None,
    ) -> ActivationPersistenceResult | None:
        with self._session_factory.begin() as session:
            return self.persist_activation(invitation, role=role, setup=setup, session=session)

    @override
    def persist_activation(
        self,
        invitation: AccountInvitation,
        *,
        role: str,
        setup: AccountSetup | None,
        session: Session,
    ) -> ActivationPersistenceResult | None:
        """Recheck and persist an invitation in the caller-owned Session.

        The caller owns authorization, bounded transaction and finalization. This
        helper does not commit/rollback or consume invitations, and a returned
        result does not prove that required access/resource sync has completed.
        New membership lifecycle writes share this transaction and require
        migration 023. The account lock remains before the lifecycle lock.
        """
        tenant_id = session.scalar(
            select(Tenant.id).where(
                Tenant.id == invitation.workspace_id,
                Tenant.status == TenantStatus.NORMAL,
            )
        )
        account = session.scalar(
            select(Account)
            .where(
                Account.id == invitation.account_id,
                Account.email == invitation.account_email,
            )
            .with_for_update()
        )
        if tenant_id is None or account is None:
            return None

        membership = session.scalar(
            select(TenantAccountJoin).where(
                TenantAccountJoin.tenant_id == tenant_id,
                TenantAccountJoin.account_id == account.id,
            )
        )
        membership_created = membership is None
        if membership is None:
            membership = TenantAccountJoin(
                tenant_id=tenant_id,
                account_id=account.id,
                role=TenantAccountRole(role),
            )
            session.add(membership)

        if setup is not None:
            self.persist_account_setup(account, setup)

        session.execute(
            update(TenantAccountJoin)
            .where(
                TenantAccountJoin.account_id == account.id,
                TenantAccountJoin.tenant_id != tenant_id,
            )
            .values(current=False)
        )
        membership.current = True
        membership.last_opened_at = naive_utc_now()

        if membership_created:
            session.flush([membership])
            InvitationAuthorityRepository().record_membership_creation(
                session, account_id=account.id, workspace_id=tenant_id
            )

        return ActivationPersistenceResult(membership_created=membership_created)

    @staticmethod
    def persist_account_setup(account: Account, setup: AccountSetup) -> None:
        """Apply the existing setup fields to the caller's ORM account.

        Field assignment is not admission or initialization authorization. Callers
        must own trusted identity/status/locale checks and Session persistence;
        Account's default ACTIVE value alone does not establish initialization.
        """
        account.name = setup.name
        account.interface_language = setup.interface_language
        account.timezone = setup.timezone
        account.interface_theme = "light"
        SQLAlchemyAccountActivationRepository.persist_account_active_status(account)
        account.initialized_at = naive_utc_now()

    @staticmethod
    def persist_account_active_status(account: Account) -> None:
        """Assign the existing active status; callers own admission and persistence."""
        account.status = AccountStatus.ACTIVE
