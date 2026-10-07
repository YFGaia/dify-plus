"""Database-owned authorization for instance-wide system configuration.

All reads use the caller's short transaction. No authorization query elects or
repairs the initialization workspace. Fresh database state, rather than cached
Account.role or RBAC's permissive compatibility properties, grants access.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.system_management_scope_extend import SystemManagementScopeExtend


class SystemManagementForbiddenError(PermissionError):
    def __init__(self) -> None:
        super().__init__("system_management_forbidden")


@dataclass(frozen=True)
class SystemManagementMembership:
    account_id: str
    tenant_id: str
    role: TenantAccountRole


class SystemManagementAccessService:
    @staticmethod
    def membership(account: object | None, *, session: Session) -> SystemManagementMembership | None:
        # Only authenticated local Account principals reach management. IdP
        # projections and arbitrary objects cannot impersonate one by its id.
        if not isinstance(account, Account):
            return None
        account_id = account.id
        if not isinstance(account_id, str):
            return None
        try:
            if str(UUID(account_id)) != account_id:
                return None
        except ValueError:
            return None
        # Selecting scalar columns bypasses the identity map's cached attributes.
        # Authorization must not flush unrelated pending writes on a read path.
        with session.no_autoflush:
            rows = session.execute(
                select(TenantAccountJoin.tenant_id, TenantAccountJoin.role, SystemManagementScopeExtend.tenant_id)
                .join(Account, Account.id == TenantAccountJoin.account_id)
                .join(Tenant, Tenant.id == TenantAccountJoin.tenant_id)
                .join(SystemManagementScopeExtend, SystemManagementScopeExtend.id == "initialization")
                .where(
                    Account.id == account_id,
                    Account.status == AccountStatus.ACTIVE,
                    Account.initialized_at.is_not(None),
                    TenantAccountJoin.current.is_(True),
                    Tenant.status == TenantStatus.NORMAL,
                )
            ).all()
        # Corrupt multiple current joins must never pick a convenient workspace.
        if len(rows) != 1:
            return None
        tenant_id, role, anchor_id = rows[0]
        context_id = account.current_tenant_id
        if tenant_id != anchor_id or (context_id is not None and context_id != tenant_id):
            return None
        if role not in {TenantAccountRole.OWNER, TenantAccountRole.ADMIN}:
            return None
        return SystemManagementMembership(account_id, tenant_id, TenantAccountRole(role))

    @classmethod
    def can_manage(cls, account: object | None, *, session: Session) -> bool:
        return cls.membership(account, session=session) is not None

    @classmethod
    def require_management(cls, account: object | None, *, session: Session) -> None:
        if not cls.can_manage(account, session=session):
            raise SystemManagementForbiddenError()

    @staticmethod
    def record_initialization(account: Account, *, session: Session) -> None:
        """Record the actual setup-created workspace in the final setup commit."""
        session.flush()
        if session.get(SystemManagementScopeExtend, "initialization") is not None:
            raise ValueError("Initialization workspace is already recorded")
        workspace_id = account.current_tenant_id
        owner = session.scalar(
            select(TenantAccountJoin.id)
            .join(Tenant, Tenant.id == TenantAccountJoin.tenant_id)
            .where(
                TenantAccountJoin.account_id == account.id,
                TenantAccountJoin.tenant_id == workspace_id,
                TenantAccountJoin.role == TenantAccountRole.OWNER,
                Tenant.status == TenantStatus.NORMAL,
            )
        )
        if owner is None or account.initialized_at is None:
            raise ValueError("Setup did not create an initialized workspace owner")
        session.add(SystemManagementScopeExtend(tenant_id=workspace_id))
