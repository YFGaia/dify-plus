"""Authenticated Casdoor management orchestration with short local transactions.

No network, activation or capability-proof producer is wired here. The repository
owns immutable revisions, complete configuration validation, CAS, Secret AAD and
audit. Each operation owns a separate session, so a Console login read transaction
cannot accidentally become the configuration write unit of work.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.permissions import CasdoorManagementPolicy, LocalAccount
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
    CasdoorConfigurationRepository,
    ConfigurationSnapshot,
    DisableResult,
    StaticValidationSnapshot,
    WorkspaceSelectionPage,
    _DisplayMetadata,
)


class CasdoorConfigurationService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        management_policy: CasdoorManagementPolicy,
        secret_key: str,
        rbac_enabled: bool,
    ) -> None:
        self._session_factory = session_factory
        self._management_policy = management_policy
        # Do not construct crypto until a privileged action needs it. Empty
        # deployment keys must not break unrelated application startup/permissions.
        self._secret_key = secret_key
        self._rbac_enabled = rbac_enabled

    def can_manage(self, account: LocalAccount | None) -> bool:
        return self._management_policy.can_manage_casdoor(account)

    def require_management(self, account: LocalAccount | None) -> None:
        self._management_policy.require_management(account)

    def _repository(self, session: Session) -> CasdoorConfigurationRepository:
        return CasdoorConfigurationRepository(
            session,
            crypto=CasdoorCrypto(secret_key=self._secret_key, key_version="v1"),
            rbac_enabled=self._rbac_enabled,
            deployment_proof_fingerprint=None,
        )

    def display(self) -> _DisplayMetadata:
        """Read public display metadata in an independent, read-only session."""
        with self._session_factory() as session:
            return self._repository(session).display()

    def get(self, account: LocalAccount | None) -> ConfigurationSnapshot:
        self.require_management(account)
        with self._session_factory() as session:
            return self._repository(session).get()

    def save(
        self,
        account: LocalAccount | None,
        *,
        configuration: CasdoorConfiguration,
        etag: int,
        secret: SecretStr | None,
    ) -> ConfigurationSnapshot:
        self.require_management(account)
        assert account is not None
        with self._session_factory() as session, session.begin():
            return self._repository(session).save_draft(
                configuration, etag=etag, actor_account_id=UUID(account.id), secret=secret
            )

    def clear_secret(self, account: LocalAccount | None, *, etag: int, revision_id: UUID) -> ConfigurationSnapshot:
        self.require_management(account)
        assert account is not None
        with self._session_factory() as session, session.begin():
            return self._repository(session).clear_draft_secret(
                etag=etag, revision_id=revision_id, actor_account_id=UUID(account.id)
            )

    def validate_static(
        self, account: LocalAccount | None, *, etag: int, revision_id: UUID, now: datetime | None = None
    ) -> StaticValidationSnapshot:
        self.require_management(account)
        with self._session_factory() as session, session.begin():
            return self._repository(session).validate_static(etag=etag, revision_id=revision_id, now=now)

    def disable(self, account: LocalAccount | None, *, etag: int) -> DisableResult:
        """Fence future Casdoor use and report unresolved remote work.

        Disabling never claims that already-sent role/resource operations have
        terminated. The repository marks the namespace fenced and returns an
        explicit reconciliation requirement for that case.
        """
        self.require_management(account)
        assert account is not None
        with self._session_factory() as session, session.begin():
            return self._repository(session).disable(etag=etag, actor_account_id=UUID(account.id))

    def activate(
        self,
        account: LocalAccount | None,
        *,
        etag: int,
        revision_id: UUID,
        now: datetime | None = None,
    ) -> ConfigurationSnapshot:
        """Activate only through the repository's real deployment proof gate."""
        self.require_management(account)
        assert account is not None
        with self._session_factory() as session, session.begin():
            return self._repository(session).activate(
                etag=etag,
                revision_id=revision_id,
                actor_account_id=UUID(account.id),
                now=now,
            )

    def test_login(
        self, account: LocalAccount | None, *, etag: int, revision_id: UUID
    ) -> Literal["deployment_proof_missing", "live_test_not_wired"]:
        """Return a blocked outcome until proof and the trusted executor are wired."""
        self.require_management(account)
        with self._session_factory() as session:
            repository = self._repository(session)
            current = repository.get()
            if current.etag != etag or current.draft_revision_id != revision_id:
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "draft_pointer_mismatch")
            if repository.deployment_proof_fingerprint is None:
                return "deployment_proof_missing"
            # A configured proof fingerprint does not itself prove that a live
            # login test executor and exact server release have been accepted.
            return "live_test_not_wired"

    def workspaces(self, account: LocalAccount | None, *, page: int, limit: int) -> WorkspaceSelectionPage:
        self.require_management(account)
        with self._session_factory() as session:
            return self._repository(session).list_workspaces(page=page, limit=limit)
