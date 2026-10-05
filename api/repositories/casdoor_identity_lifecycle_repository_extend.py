"""Caller-owned identity unlink: zero history or actual LOCAL release provenance.

No queue status proves remote quiescence. Every intent, including avatar/terminal,
blocks this domain. Historical members require the actual LOCAL release CAS receipt.
The caller holds the original subject/account leases and configuration parent locks.
"""

import base64
import re
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.auth_transactions import AuthTransactionError
from libs.helper import email as validate_email
from models.account import Account, AccountStatus
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from services.system_feature_service import SystemFeatureService
from sqlalchemy.orm import Session


class CasdoorIdentityLifecycleRepository:
    def __init__(self, session: Session):
        self.session = session

    def account(self, account_id: UUID):
        account = self.session.scalar(
            sa.select(Account)
            .where(Account.id == str(account_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if account is None or account.status != AccountStatus.ACTIVE:
            raise AuthTransactionError("source_session_invalid")
        return account

    def identity(self, namespace_id: UUID, account_id: UUID):
        rows = self.session.scalars(
            sa.select(Identity)
            .where(Identity.namespace_id == str(namespace_id), Identity.account_id == str(account_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        if len(rows) != 1:
            raise AuthTransactionError("identity_unavailable")
        return rows[0]

    def member_lease_scopes(self, account_id):
        """Complete bounded history navigation before acquisition; no permission."""
        from core.casdoor.leases import WorkspaceMemberScope

        rows = self.session.scalars(
            sa.select(History.workspace_id)
            .where(History.account_id == str(account_id))
            .distinct()
            .order_by(History.workspace_id)
            .limit(101)
        ).all()
        if len(rows) > 100:
            raise AuthTransactionError("managed_history_requires_release")
        return tuple(WorkspaceMemberScope(UUID(value), account_id) for value in rows)

    @staticmethod
    def password_available(account):
        """Actual enabled credential in the original PBKDF2 owner format.

        Flags alone or a malformed/absent credential cannot authorize unlink.
        Other providers/mail delivery are not assumed usable without their own proof.
        """
        try:
            validate_email(account.email)
            return (
                SystemFeatureService.is_email_password_login_enabled() is True
                and type(account.password) is str
                and type(account.password_salt) is str
                and re.fullmatch(rb"[0-9a-f]{64}", base64.b64decode(account.password, validate=True)) is not None
                and len(base64.b64decode(account.password_salt, validate=True)) == 16
            )
        except (ValueError, TypeError):
            return False

    def require_unlink_safe(self, identity, account):
        if not self.password_available(account):
            raise AuthTransactionError("other_login_unavailable")
        # Check all scopes/states, not just current pages or active generations.
        history = self.session.scalar(
            sa.select(History.id)
            .where(sa.or_(History.account_id == account.id, History.identity_id == identity.id))
            .order_by(History.id)
            .limit(1)
        )
        intent = self.session.scalar(
            sa.select(Intent.id)
            .where(sa.or_(Intent.account_id == account.id, Intent.identity_id == identity.id))
            .order_by(Intent.id)
            .limit(1)
        )
        if intent is not None:
            raise AuthTransactionError("managed_history_requires_release")
        if history is not None:
            from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

            try:
                CasdoorLocalLifecycleRepository(self.session).require_released_for_unlink(
                    UUID(account.id), UUID(identity.id)
                )
            except AuthTransactionError:
                raise AuthTransactionError("managed_history_requires_release") from None

    def unlink(self, identity, account):
        self.require_unlink_safe(identity, account)
        # Exact locked row deletion preserves accounts, joins, balances and old providers.
        self.session.delete(identity)
        self.session.flush()
