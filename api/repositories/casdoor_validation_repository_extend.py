"""Trusted validation summaries in the caller's short, explicit unit of work.

No request DTO can supply capabilities or a success status. The orchestrators
write only after their real owners finish; this repository rechecks the exact
draft, namespace fence and digest before any row is flushed.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.deployment_evidence import AcceptedDeploymentPolicy
from core.casdoor.errors import CasdoorErrorCode
from models.casdoor_extend import (
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorValidationExtend,
    CasdoorValidationKind,
    CasdoorValidationStatus,
)

from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
    CasdoorConfigurationRepository,
    required_capabilities,
    signing_key_fingerprint,
)


@dataclass(frozen=True)
class ValidationBinding:
    integration_id: UUID
    namespace_id: UUID
    revision_id: UUID
    etag: int
    fence_epoch: int
    config_digest: str


class CasdoorValidationRepository:
    def __init__(self, owner: CasdoorConfigurationRepository):
        self.owner = owner
        self.session = owner.session

    def require_current(self, binding: ValidationBinding) -> None:
        if not self.session.in_transaction():
            raise RuntimeError("validation requires an explicit transaction")
        integration = self.session.scalar(
            sa.select(CasdoorIntegrationExtend)
            .where(CasdoorIntegrationExtend.id == str(binding.integration_id), CasdoorIntegrationExtend.slot == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if integration is None or (integration.etag, integration.draft_revision_id) != (
            binding.etag,
            str(binding.revision_id),
        ):
            raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "draft_pointer_mismatch")
        revision = self.owner._revision(integration.id, str(binding.revision_id))
        namespace = self.session.scalar(
            sa.select(CasdoorNamespaceExtend)
            .where(CasdoorNamespaceExtend.id == str(binding.namespace_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            revision.namespace_id != str(binding.namespace_id)
            or revision.config_digest != binding.config_digest
            or namespace is None
            or namespace.lifecycle is not CasdoorNamespaceLifecycle.ACTIVE
            or namespace.fence_epoch != binding.fence_epoch
        ):
            raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "namespace_fenced")

    def record(
        self,
        binding: ValidationBinding,
        *,
        kind: CasdoorValidationKind,
        status: CasdoorValidationStatus,
        actor_account_id: UUID,
        correlation_id: UUID,
        policy: AcceptedDeploymentPolicy | None = None,
        now: datetime,
        preview: dict | None = None,
        signing_keys: dict | None = None,
    ) -> None:
        self.require_current(binding)
        checked_at = now.astimezone(UTC).replace(tzinfo=None)
        expires_at = checked_at + timedelta(minutes=15)
        if policy is not None:
            accepted = policy.binding
            if (
                accepted.namespace_id != str(binding.namespace_id)
                or accepted.revision_id != str(binding.revision_id)
                or accepted.config_digest != binding.config_digest
                or accepted.rbac_mode != self.owner.rbac_mode
                or not policy.issued_at <= now < policy.expires_at
            ):
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "deployment_proof_invalid")
            expires_at = min(expires_at, policy.expires_at.replace(tzinfo=None))
        revision = self.owner._revision(str(binding.integration_id), str(binding.revision_id))
        capabilities = required_capabilities(kind, revision.schema_version)
        if capabilities is None:
            raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "validation_required")
        if kind is CasdoorValidationKind.DEPLOYMENT:
            if status is CasdoorValidationStatus.PASSED and (
                policy is None or not capabilities <= policy.deployment_capabilities
            ):
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "deployment_proof_missing")
        summary = {
            "schema_version": 1,
            "namespace_id": str(binding.namespace_id),
            "evidence_source": "real",
            "capabilities": dict.fromkeys(sorted(capabilities), status.value),
        }
        if revision.schema_version == 2:
            summary["configuration_schema_version"] = 2
            if kind is CasdoorValidationKind.PROTOCOL and status is CasdoorValidationStatus.PASSED:
                if (
                    not isinstance(signing_keys, dict)
                    or set(signing_keys) != {"source", "profile", "fingerprints"}
                    or not isinstance(signing_keys["source"], str)
                    or not signing_keys["source"]
                    or signing_keys["profile"] not in ("application", "global")
                    or not isinstance(signing_keys["fingerprints"], list)
                    or not 1 <= len(signing_keys["fingerprints"]) <= 16
                    or any(not signing_key_fingerprint(item) for item in signing_keys["fingerprints"])
                ):
                    raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "signing_key_diagnostic_required")
                summary["signing_keys"] = signing_keys
        if preview is not None and kind is CasdoorValidationKind.DIAGNOSTIC:
            summary["preview"] = preview
        self.session.add(
            CasdoorValidationExtend(
                id=str(uuid4()),
                revision_id=str(binding.revision_id),
                config_digest=binding.config_digest,
                kind=kind,
                status=status,
                rbac_mode=self.owner.rbac_mode,
                proof_fingerprint=policy.proof_fingerprint if policy else None,
                summary_json=json.dumps(summary, sort_keys=True, separators=(",", ":")),
                actor_account_id=str(actor_account_id),
                correlation_id=str(correlation_id),
                checked_at=checked_at,
                expires_at=expires_at,
            )
        )
        self.session.flush()
