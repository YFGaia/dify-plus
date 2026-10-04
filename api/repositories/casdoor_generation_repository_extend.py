"""Current identity generation CAS within a short caller-owned transaction.

The caller first acquires complete sorted leases, then loads fresh I10 roles and
server workspace/builtin availability outside DB write transactions. It checks
ensure_owned after I/O and before commit. This repository performs no Redis/HTTP,
commit, rollback, account/identity creation or durable snapshot/intent storage.
An existing flushed identity and clean Session are mandatory. I18 may bind/flush
earlier in the same UoW only after acquiring integration->namespace->account
locks in that order; this repository does not prove those earlier prelocks.
"""

import hashlib
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.mapping import DesiredWorkspacePlan, MappingError, validate_mapping_context
from models.account import Account
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
)
from sqlalchemy.orm import Session

MAX_GENERATION = 2**63 - 1


class CasdoorGenerationConflict(ValueError):
    code = CasdoorErrorCode.CONFIG_CONFLICT

    def __init__(self) -> None:
        super().__init__(self.code.value)


@dataclass(frozen=True, repr=False)
class GenerationPlanVersion:
    """Local version only, not durable full-snapshot or remote fencing proof."""

    plan: DesiredWorkspacePlan
    fence_epoch: int
    generation: int

    @property
    def identity_id(self) -> UUID:
        return self.plan.context.identity_id


class CasdoorGenerationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def allocate(
        self,
        plan: DesiredWorkspacePlan,
        *,
        expected_fence_epoch: int,
        expected_generation: int,
    ) -> GenerationPlanVersion:
        """Guard current integration/revision/namespace/account/identity then CAS.

        The Session must contain no new/dirty/deleted ORM objects. Pending work
        is rejected before any SQL rather than flushed ahead of the parent locks
        or silently ignored, including pending fences and account deletion.
        Our locks follow integration -> namespace -> account -> identity.
        Draft pointer/etag changes are irrelevant to an unchanged active revision.
        On failure caller rolls back its UoW; no retry or outer rollback here.
        """
        if not self._session.in_transaction() or not self._session.is_active:
            raise RuntimeError("Casdoor generation repository requires an active caller-owned transaction")
        if self._session.new or self._session.dirty or self._session.deleted:
            raise CasdoorGenerationConflict()
        if (
            not isinstance(plan, DesiredWorkspacePlan)
            or not isinstance(plan.targets, tuple)
            or not plan.targets
            or type(expected_fence_epoch) is not int
            or not 0 <= expected_fence_epoch <= MAX_GENERATION
            or type(expected_generation) is not int
            or not 0 <= expected_generation < MAX_GENERATION
        ):
            raise CasdoorGenerationConflict()
        try:
            validate_mapping_context(plan.context)
        except MappingError:
            raise CasdoorGenerationConflict() from None
        context = plan.context
        with self._session.no_autoflush:
            integration = self._session.execute(
                sa.select(CasdoorIntegrationExtend.enabled, CasdoorIntegrationExtend.active_revision_id)
                .where(CasdoorIntegrationExtend.id == str(context.integration_id), CasdoorIntegrationExtend.slot == 1)
                .with_for_update()
            ).one_or_none()
            if (
                integration is None
                or integration.enabled is not True
                or integration.active_revision_id != str(context.revision_id)
            ):
                raise CasdoorGenerationConflict()
            namespace = self._session.execute(
                sa.select(
                    CasdoorNamespaceExtend.integration_id,
                    CasdoorNamespaceExtend.expected_issuer,
                    CasdoorNamespaceExtend.organization,
                    CasdoorNamespaceExtend.application,
                    CasdoorNamespaceExtend.client_id,
                    CasdoorNamespaceExtend.lifecycle,
                    CasdoorNamespaceExtend.fence_epoch,
                )
                .where(CasdoorNamespaceExtend.id == str(context.namespace_id))
                .with_for_update()
            ).one_or_none()
            if (
                namespace is None
                or namespace.integration_id != str(context.integration_id)
                or namespace.expected_issuer != context.issuer
                or namespace.organization != context.organization
                or namespace.application != context.application
                or namespace.client_id != context.client_id
                or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE
                or namespace.fence_epoch != expected_fence_epoch
            ):
                raise CasdoorGenerationConflict()
            # Revisions are immutable. The integration lock guards their pointer.
            revision = self._session.execute(
                sa.select(
                    CasdoorConfigRevisionExtend.integration_id,
                    CasdoorConfigRevisionExtend.namespace_id,
                    CasdoorConfigRevisionExtend.expected_issuer,
                    CasdoorConfigRevisionExtend.organization,
                    CasdoorConfigRevisionExtend.application,
                    CasdoorConfigRevisionExtend.client_id,
                    CasdoorConfigRevisionExtend.config_digest,
                ).where(CasdoorConfigRevisionExtend.id == str(context.revision_id))
            ).one_or_none()
            if (
                revision is None
                or revision.integration_id != str(context.integration_id)
                or revision.namespace_id != str(context.namespace_id)
                or revision.expected_issuer != context.issuer
                or revision.organization != context.organization
                or revision.application != context.application
                or revision.client_id != context.client_id
                or revision.config_digest != context.config_digest
            ):
                raise CasdoorGenerationConflict()
            if self._session.scalar(
                sa.select(Account.id).where(Account.id == str(context.account_id)).with_for_update()
            ) != str(context.account_id):
                raise CasdoorGenerationConflict()
            digest = hashlib.sha256(context.subject.encode("utf-8")).hexdigest()
            identity = self._session.execute(
                sa.select(
                    CasdoorIdentityExtend.id,
                    CasdoorIdentityExtend.namespace_id,
                    CasdoorIdentityExtend.account_id,
                    CasdoorIdentityExtend.issuer,
                    CasdoorIdentityExtend.organization,
                    CasdoorIdentityExtend.subject,
                    CasdoorIdentityExtend.subject_digest,
                    CasdoorIdentityExtend.sync_generation,
                )
                .where(
                    CasdoorIdentityExtend.id == str(context.identity_id),
                    CasdoorIdentityExtend.namespace_id == str(context.namespace_id),
                    CasdoorIdentityExtend.subject_digest == digest,
                )
                .with_for_update()
            ).one_or_none()
            if (
                identity is None
                or identity.id != str(context.identity_id)
                or identity.namespace_id != str(context.namespace_id)
                or identity.account_id != str(context.account_id)
                or identity.issuer != context.issuer
                or identity.organization != context.organization
                or identity.subject != context.subject
                or identity.subject_digest != digest
                or type(identity.sync_generation) is not int
                or identity.sync_generation != expected_generation
            ):
                raise CasdoorGenerationConflict()
            result = self._session.execute(
                sa.update(CasdoorIdentityExtend)
                .where(
                    CasdoorIdentityExtend.id == str(context.identity_id),
                    CasdoorIdentityExtend.namespace_id == str(context.namespace_id),
                    CasdoorIdentityExtend.account_id == str(context.account_id),
                    CasdoorIdentityExtend.issuer == context.issuer,
                    CasdoorIdentityExtend.organization == context.organization,
                    CasdoorIdentityExtend.subject == context.subject,
                    CasdoorIdentityExtend.subject_digest == digest,
                    CasdoorIdentityExtend.sync_generation == expected_generation,
                    CasdoorIdentityExtend.sync_generation < MAX_GENERATION,
                )
                .values(sync_generation=CasdoorIdentityExtend.sync_generation + 1)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise CasdoorGenerationConflict()
        return GenerationPlanVersion(plan, expected_fence_epoch, expected_generation + 1)
