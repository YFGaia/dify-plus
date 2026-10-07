"""Server-only, freshly read deployment authority. No DB/network/Secret access."""

import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.deployment_evidence import (
    MAX_EVIDENCE_BYTES,
    AcceptedDeploymentPolicy,
    DeploymentEvidenceError,
    accept_deployment_evidence,
)


class CasdoorDeploymentPolicyService:
    """Deployment settings supply paths; HTTP/admin payloads never supply proof.

    The deployment operator protects both local files and owns atomic rotation.
    Every resolution reopens both files, bounded while reading (not just stat),
    so removing a pin/key or replacing trust invalidates prior accepted policy.
    Construction performs no I/O and cannot break unrelated application startup.
    """

    def __init__(
        self,
        *,
        authority_path: str = "",
        evidence_path: str = "",
        deployment_edition: str = "COMMUNITY",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._authority_path = authority_path
        self._evidence_path = evidence_path
        self._deployment_edition = deployment_edition
        self._now = now

    @staticmethod
    def _read(path: str) -> bytes:
        # Nonblocking open prevents a malicious/misconfigured FIFO from hanging;
        # only regular files are evidence. Symlinks allow operator-owned rotation.
        if type(path) is not str or not os.path.isabs(path) or len(path) > 4096:
            raise DeploymentEvidenceError("deployment_proof_invalid")
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise DeploymentEvidenceError("deployment_proof_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                document = stream.read(MAX_EVIDENCE_BYTES + 1)
        except OSError:
            raise DeploymentEvidenceError("deployment_proof_invalid") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not document or len(document) > MAX_EVIDENCE_BYTES:
            raise DeploymentEvidenceError("deployment_proof_invalid")
        return document

    def resolve(
        self,
        configuration: CasdoorConfiguration,
        namespace_id: UUID,
        revision_id: UUID,
        config_digest: str,
        rbac_mode: str,
    ) -> AcceptedDeploymentPolicy:
        if not self._authority_path or not self._evidence_path:
            raise DeploymentEvidenceError("deployment_proof_missing")
        try:
            return accept_deployment_evidence(
                authority_document=self._read(self._authority_path),
                envelope_document=self._read(self._evidence_path),
                configuration=configuration,
                namespace_id=namespace_id,
                revision_id=revision_id,
                config_digest=config_digest,
                rbac_mode=rbac_mode,
                deployment_edition=self._deployment_edition,
                now=self._now(),
            )
        except Exception:
            raise DeploymentEvidenceError("deployment_proof_invalid") from None
