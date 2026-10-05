"""Private admission produced after the actual RP observation reader closes.

No HTTP parameter, database validation summary or public flag can supply this
object. It supplements only the optional RP gate; all four mandatory gates remain
the existing SQL owner's responsibility.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from core.casdoor.deployment_evidence import DeploymentBinding


@dataclass(frozen=True, repr=False)
class RPLogoutActivationAdmission:
    binding: DeploymentBinding
    proof_fingerprint: str
    checked_at: datetime
    expires_at: datetime

    def matches(self, *, revision, configuration, fingerprint, rbac_mode, now):
        if type(self.checked_at) is not datetime or type(self.expires_at) is not datetime:
            return False
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return (
            type(self.binding) is DeploymentBinding
            and self.proof_fingerprint == fingerprint
            and self.binding.namespace_id == revision.namespace_id
            and self.binding.revision_id == revision.id
            and self.binding.config_digest == revision.config_digest
            and self.binding.configuration_digest == configuration.config_digest()
            and self.binding.rbac_mode == rbac_mode == "off"
            and self.binding.deployment_edition == "COMMUNITY"
            and (
                self.binding.browser_frontend_url,
                self.binding.backend_api_url,
                self.binding.expected_issuer,
                self.binding.organization,
                self.binding.application,
                self.binding.client_id,
            )
            == (
                configuration.browser_frontend_url,
                configuration.backend_api_url,
                configuration.expected_issuer,
                configuration.organization,
                configuration.application,
                configuration.client_id,
            )
            and self.checked_at.tzinfo is not None
            and self.expires_at.tzinfo is not None
            and self.checked_at <= now < self.expires_at
            and 0 < (self.expires_at - self.checked_at).total_seconds() <= 300
        )
