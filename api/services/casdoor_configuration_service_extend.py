"""Authenticated Casdoor management orchestration with short local transactions.

The repository owns immutable revisions, complete configuration validation, CAS,
Secret AAD and audit. Static checks are recorded against the exact draft; a real
browser diagnostic separately records protocol and directory results. Live
diagnostic work belongs to its separate service, outside all SQL transactions.
"""

import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.deployment_evidence import DeploymentEvidenceError
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.permissions import CasdoorManagementPolicy, LocalAccount
from core.casdoor.rp_logout_activation import RPLogoutActivationAdmission
from models.casdoor_extend import CasdoorNamespaceExtend, CasdoorValidationKind, CasdoorValidationStatus
from pydantic import SecretStr
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
    CasdoorConfigurationRepository,
    ConfigurationSnapshot,
    DisableResult,
    StaticValidationSnapshot,
    WorkspaceSelectionPage,
    _DisplayMetadata,
)
from repositories.casdoor_public_display_repository_extend import CasdoorPublicDisplayRepository
from repositories.casdoor_validation_repository_extend import CasdoorValidationRepository, ValidationBinding
from sqlalchemy.orm import Session, sessionmaker


class CasdoorConfigurationService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        management_policy: CasdoorManagementPolicy,
        secret_key: str,
        rbac_enabled: bool,
        deployment_policy_service=None,
        rp_logout_service=None,
    ) -> None:
        self._session_factory = session_factory
        self._management_policy = management_policy
        # Do not construct crypto until a privileged action needs it. Empty
        # deployment keys must not break unrelated application startup/permissions.
        self._secret_key = secret_key
        self._rbac_enabled = rbac_enabled
        self._deployment_policy_service = deployment_policy_service
        self._rp_logout_service = rp_logout_service

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
            return CasdoorPublicDisplayRepository(session).display()

    def get(self, account: LocalAccount | None) -> ConfigurationSnapshot:
        self.require_management(account)
        with self._session_factory() as session:
            return self._repository(session).get()

    def diagnostic_preview(self, revision_id: UUID):
        """Only the current, fresh latest diagnostic for this revision is shown."""
        with self._session_factory() as session:
            owner = self._repository(session)
            current = owner.get()
            snapshot = next(
                (
                    item
                    for item in (current.active, current.draft)
                    if item is not None and item.revision_id == revision_id
                ),
                None,
            )
            if snapshot is None or not any(
                item.kind is CasdoorValidationKind.DIAGNOSTIC and item.status == "passed"
                for item in snapshot.validation
            ):
                return None
            rows, ambiguous = owner._latest_validations(str(revision_id))
            row = rows.get(CasdoorValidationKind.DIAGNOSTIC)
            if row is None or CasdoorValidationKind.DIAGNOSTIC in ambiguous:
                return None
            return json.loads(row.summary_json).get("preview")

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
        error = None
        with self._session_factory() as session, session.begin():
            owner = self._repository(session)
            integration = owner._integration()
            binding = None
            if integration and integration.etag == etag and integration.draft_revision_id == str(revision_id):
                revision = owner._revision(integration.id, str(revision_id))
                namespace = session.get(CasdoorNamespaceExtend, revision.namespace_id)
                if namespace is None:
                    raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "namespace_fenced")
                binding = ValidationBinding(
                    UUID(integration.id),
                    UUID(revision.namespace_id),
                    revision_id,
                    etag,
                    namespace.fence_epoch,
                    revision.config_digest,
                )
            try:
                result = owner.validate_static(etag=etag, revision_id=revision_id, now=now)
            except Exception as caught:
                error = caught
            if binding is not None:
                assert account is not None
                CasdoorValidationRepository(owner).record(
                    binding,
                    kind=CasdoorValidationKind.STATIC,
                    status=CasdoorValidationStatus.FAILED if error else CasdoorValidationStatus.PASSED,
                    actor_account_id=UUID(account.id),
                    correlation_id=uuid4(),
                    policy=None,
                    now=now.replace(tzinfo=UTC) if now and now.tzinfo is None else now or datetime.now(UTC),
                )
        if error is not None:
            raise error
        return result

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
        """Activate only after current static checks and matching real diagnostics."""
        self.require_management(account)
        assert account is not None
        admission = self._rp_activation_admission(etag=etag, revision_id=revision_id)
        with self._session_factory() as session, session.begin():
            owner = self._repository(session)
            integration = owner._integration()
            if integration is None or integration.etag != etag or integration.draft_revision_id != str(revision_id):
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "draft_pointer_mismatch")
            revision = owner._revision(integration.id, str(revision_id))
            configuration = owner._configuration(revision)
            policy = None
            if configuration.rp_logout and self._deployment_policy_service is not None:
                try:
                    policy = self._deployment_policy_service.resolve(
                        configuration=configuration,
                        namespace_id=UUID(revision.namespace_id),
                        revision_id=UUID(revision.id),
                        config_digest=revision.config_digest,
                        rbac_mode=owner.rbac_mode,
                    )
                except DeploymentEvidenceError as caught:
                    raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, caught.reason) from None
                owner.deployment_proof_fingerprint = policy.proof_fingerprint
            fingerprint = policy.proof_fingerprint if policy is not None else None
            result = owner.activate(
                etag=etag,
                revision_id=revision_id,
                actor_account_id=UUID(account.id),
                now=now,
                rp_logout_admission=admission,
            )
            if policy is not None:
                assert result.active_revision_id is not None
                integration = owner._integration()
                revision = owner._revision(integration.id, str(result.active_revision_id))
                try:
                    current_policy = self._deployment_policy_service.resolve(
                        configuration=owner._configuration(revision),
                        namespace_id=UUID(revision.namespace_id),
                        revision_id=UUID(revision.id),
                        config_digest=revision.config_digest,
                        rbac_mode=owner.rbac_mode,
                    )
                except DeploymentEvidenceError as caught:
                    raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, caught.reason) from None
                if current_policy.proof_fingerprint != fingerprint:
                    raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "deployment_proof_invalid")
                if owner._configuration(revision).rp_logout:
                    if (
                        type(admission) is not RPLogoutActivationAdmission
                        or current_policy.rp_logout is None
                        or admission.binding != current_policy.binding
                        or admission.expires_at > current_policy.expires_at
                        or not admission.matches(
                            revision=revision,
                            configuration=owner._configuration(revision),
                            fingerprint=fingerprint,
                            rbac_mode=owner.rbac_mode,
                            now=datetime.now(UTC),
                        )
                    ):
                        raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "optional_capability_unknown")
            return result

    def _rp_activation_admission(self, *, etag, revision_id):
        """Read optional Redis outside write locks; its reader confirms cleanup."""
        with self._session_factory() as session:
            owner = self._repository(session)
            integration = owner._integration()
            if integration is None:
                return None  # Existing mandatory owner supplies its original error.
            revision = owner._revision(integration.id, str(revision_id))
            configuration = owner._configuration(revision)
            if not configuration.rp_logout:
                return None
            if integration.etag != etag or integration.draft_revision_id != str(revision_id):
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "draft_pointer_mismatch")
            namespace_id, digest = UUID(revision.namespace_id), revision.config_digest
        try:
            from services.casdoor_rp_logout_service_extend import RPProtocolObservation

            if self._rp_logout_service is None or self._deployment_policy_service is None:
                raise ValueError()
            policy = self._deployment_policy_service.resolve(
                configuration=configuration,
                namespace_id=namespace_id,
                revision_id=revision_id,
                config_digest=digest,
                rbac_mode="off",
            )
            observation = self._rp_logout_service.observation(policy.binding)
            if (
                type(observation) is not RPProtocolObservation
                or policy.rp_logout is None
                or observation.binding != policy.binding
                or observation.proof_fingerprint != policy.proof_fingerprint
                or observation.expires_at > policy.expires_at
            ):
                raise ValueError()
            return RPLogoutActivationAdmission(
                observation.binding,
                observation.proof_fingerprint,
                observation.checked_at,
                observation.expires_at,
            )
        except Exception:
            raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "optional_capability_unknown") from None

    def test_login(
        self, account: LocalAccount | None, *, etag: int, revision_id: UUID
    ) -> Literal["deployment_proof_missing", "live_test_not_wired"]:
        """Legacy compatibility probe; mounted browser diagnostics use prepare()."""
        self.require_management(account)
        with self._session_factory() as session:
            repository = self._repository(session)
            current = repository.get()
            if current.etag != etag or current.draft_revision_id != revision_id:
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "draft_pointer_mismatch")
            # Static configuration never reports this legacy probe as a login
            # success. Use the browser-bound diagnostic route for real auth.
            return "live_test_not_wired"

    def workspaces(self, account: LocalAccount | None, *, page: int, limit: int) -> WorkspaceSelectionPage:
        self.require_management(account)
        with self._session_factory() as session:
            return self._repository(session).list_workspaces(page=page, limit=limit)
