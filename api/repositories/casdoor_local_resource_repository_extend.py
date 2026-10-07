"""Bounded LOCAL ID observation, never authorization or remote/snapshot proof.

The caller owns a clean root transaction and must separately establish workspace,
account, integration, generation, leases and ownership authority. It also owns
isolation/producer exclusion: keyset scans can miss concurrent lower-ID inserts.
End this transaction before remote I/O. Deadlines are pre/post logical guards;
they cannot cancel a running SQL statement.
"""

import time
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from services.enterprise.rbac_service import RBACResourceType
from sqlalchemy.orm import Session
from tasks.initialize_created_app_rbac_access_task import (
    _WHITELIST_RESOURCE_KINDS,
    APP_RBAC_RESOURCE_CONFIG_BATCH_SIZE,
)

MAX_RESOURCE_PAIRS = 4096
MAX_QUERY_ATTEMPTS = 16
MAX_SCAN_SECONDS = 2.0


class LocalResourceScanFailure(StrEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_SESSION = "invalid_session"
    RESOURCE_LIMIT = "resource_limit"
    QUERY_LIMIT = "query_limit"
    DEADLINE = "deadline"
    INVALID_ROW = "invalid_row"
    DATABASE = "database"


class CasdoorLocalResourceScanError(ValueError):
    def __init__(self, reason: LocalResourceScanFailure) -> None:
        self.reason = reason
        super().__init__(f"local_resource_scan_{reason.value}")


@dataclass(frozen=True)
class LocalResourceInventory:
    """Only exhaustion of local iterators; no completeness or authority claim."""

    workspace_id: UUID
    resources: tuple[tuple[RBACResourceType, UUID], ...]
    attempted_queries: int
    status: str = "LOCAL_SCAN_EXHAUSTED"


class CasdoorLocalResourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _require_session(self, transaction=None) -> None:
        session = self._session
        if not isinstance(session, Session):
            raise CasdoorLocalResourceScanError(LocalResourceScanFailure.INVALID_SESSION)
        root = session.get_transaction()
        if (
            root is None
            or not root.is_active
            or not session.is_active
            or session.get_nested_transaction() is not None
            or (transaction is not None and root is not transaction)
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise CasdoorLocalResourceScanError(LocalResourceScanFailure.INVALID_SESSION)

    @staticmethod
    def _require_time(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise CasdoorLocalResourceScanError(LocalResourceScanFailure.DEADLINE)

    def enumerate_ids(self, workspace_id: UUID, *, deadline: float) -> LocalResourceInventory:
        now = time.monotonic()
        if (
            not isinstance(workspace_id, UUID)
            or type(deadline) not in (int, float)
            or not now < deadline <= now + MAX_SCAN_SECONDS
        ):
            raise CasdoorLocalResourceScanError(LocalResourceScanFailure.INVALID_INPUT)
        self._require_session()
        transaction = self._session.get_transaction()
        resources: list[tuple[RBACResourceType, UUID]] = []
        attempts = 0
        with self._session.no_autoflush:
            for kind in _WHITELIST_RESOURCE_KINDS:
                iterator = kind.iter_id_batches(
                    str(workspace_id), APP_RBAC_RESOURCE_CONFIG_BATCH_SIZE, session=self._session
                )
                previous: str | None = None
                while True:
                    self._require_session(transaction)
                    self._require_time(deadline)
                    if attempts >= MAX_QUERY_ATTEMPTS:
                        raise CasdoorLocalResourceScanError(LocalResourceScanFailure.QUERY_LIMIT)
                    attempts += 1
                    try:
                        ids = next(iterator, None)
                    except Exception:
                        raise CasdoorLocalResourceScanError(LocalResourceScanFailure.DATABASE) from None
                    self._require_time(deadline)
                    self._require_session(transaction)
                    if ids is None:
                        break
                    page: list[tuple[RBACResourceType, UUID]] = []
                    for value in ids:
                        try:
                            parsed = UUID(value)
                            if str(parsed) != value or (previous is not None and value <= previous):
                                raise ValueError()
                        except (ValueError, TypeError, AttributeError):
                            raise CasdoorLocalResourceScanError(LocalResourceScanFailure.INVALID_ROW) from None
                        previous = value
                        page.append((kind.resource_type, parsed))
                    if len(resources) + len(page) > MAX_RESOURCE_PAIRS:
                        raise CasdoorLocalResourceScanError(LocalResourceScanFailure.RESOURCE_LIMIT)
                    resources.extend(page)
        return LocalResourceInventory(workspace_id, tuple(resources), attempts)
