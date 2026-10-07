"""Casdoor management requires independently verified global system access.

Deployment account lists, cached roles and IdP claims do not grant access. The
shared database authorization service supplies the explicit admission result.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class LocalAccount(Protocol):
    id: str
    status: str
    current_role: str | None


def _exact_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return parsed if parsed == value else None


class CasdoorManagementForbiddenError(PermissionError):
    """The controller translates this to a safe 403 response."""

    def __init__(self) -> None:
        super().__init__("casdoor_management_forbidden")


@dataclass(frozen=True)
class CasdoorManagementPolicy:
    def can_manage_casdoor(self, account: LocalAccount | None, *, system_management_allowed: bool = False) -> bool:
        return (
            account is not None
            and account.status == "active"
            and _exact_uuid(account.id) is not None
            and system_management_allowed is True
        )

    def require_management(self, account: LocalAccount | None, *, system_management_allowed: bool = False) -> None:
        if not self.can_manage_casdoor(account, system_management_allowed=system_management_allowed):
            raise CasdoorManagementForbiddenError()
