"""Deployment-owned authority limited to Casdoor configuration management.

The route owner passes dify_config.CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS and the
authenticated local Account. Workspace roles and IdP claims are not inputs.
Malformed deployment lists fail closed as a whole; no environment is read here.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class LocalAccount(Protocol):
    id: str
    status: str


def _exact_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return parsed if parsed == value else None


class CasdoorManagementForbiddenError(PermissionError):
    """The controller translates this to 403 without account-list disclosure."""

    def __init__(self) -> None:
        super().__init__("casdoor_management_forbidden")


@dataclass(frozen=True)
class CasdoorManagementPolicy:
    account_ids: frozenset[str]

    @classmethod
    def from_deployment(cls, raw_account_ids: str) -> "CasdoorManagementPolicy":
        if not isinstance(raw_account_ids, str) or not raw_account_ids.strip():
            return cls(frozenset())
        items = [item.strip() for item in raw_account_ids.split(",")]
        if any(_exact_uuid(item) is None for item in items):
            return cls(frozenset())
        return cls(frozenset(items))

    def can_manage_casdoor(self, account: LocalAccount | None) -> bool:
        return (
            account is not None
            and account.status == "active"
            and _exact_uuid(account.id) is not None
            and account.id in self.account_ids
        )

    def require_management(self, account: LocalAccount | None) -> None:
        if not self.can_manage_casdoor(account):
            raise CasdoorManagementForbiddenError()
